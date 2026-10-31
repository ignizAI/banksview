"""FastAPI application: accounts, wallet, upload -> report, downloads, payments."""
from __future__ import annotations

import hashlib
import json
import time
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import banks, config, coupons, excel_report, ingest, metrics, otp, payments, security, wallet
from .db import db, init_db, tx

APP_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app):
    config.check_production_safety()
    init_db()
    recover_stuck()
    cleanup_expired()
    if config.BACKUP_DAILY:
        from . import backup
        backup.start_daily()
    yield


app = FastAPI(title=config.BRAND, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
from starlette.middleware.trustedhost import TrustedHostMiddleware
app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)


class _HealthFirst:
    """The host's health checker calls /health with an internal Host header; answer it before the host check so the machine is not marked down."""
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/health":
            body = b'{"status":"ok"}'
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.inner(scope, receive, send)


app.add_middleware(_HealthFirst)    # a forged Host header (used for phishing links in emails) is refused
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["ASSET_V"] = int((APP_DIR / "static" / "style.css").stat().st_mtime)   # new CSS is fetched after every deploy, never served stale from the browser cache


# ----------------------------------------------------------------------------- template helpers
def _group(n: int) -> str:
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + s


def fmt_inr(v, dec: int = 0) -> str:
    if v is None:
        return "-"
    v = float(v)
    txt = f"{abs(v):.{dec}f}"
    whole, _, frac = txt.partition(".")
    out = _group(int(whole)) + (("." + frac) if frac else "")
    return ("-" if v < 0 and float(txt) != 0 else "") + "\u20b9" + out


def fmt_paise(p) -> str:
    p = int(p or 0)
    return fmt_inr(p / 100, 2 if p % 100 else 0)


templates.env.filters["inr"] = fmt_inr
templates.env.filters["paise"] = fmt_paise
_parts = re.findall(r'[A-Z][a-z0-9]*|[a-z0-9]+', config.BRAND)
_head, _tail = ''.join(_parts[:-1]) or config.BRAND, (_parts[-1] if len(_parts) > 1 else '')
templates.env.globals.update(BRAND=config.BRAND, BRAND_HEAD=_head, BRAND_TAIL=_tail, PRICE=fmt_paise(config.PRICE_PAISE), MIN_TOPUP=fmt_paise(config.MIN_TOPUP_PAISE),
                             MIN_TOPUP_RUPEES=config.MIN_TOPUP_PAISE // 100, PRIV_PRICE=config.PRIVILEGED_PRICE_PAISE, MAX_MB=config.MAX_UPLOAD_MB, METRIC_COUNT=len(metrics.REGISTRY), DUP_DAYS=config.DUPLICATE_WINDOW_DAYS, RETAIN_DAYS=config.REPORT_RETENTION_DAYS, PAYMENT_MODE=config.PAYMENT_MODE,
                             BANKS=banks.SUPPORTED_BANKS, BANK_COUNT=banks.BANK_COUNT, COMPANY_NAME=config.COMPANY_NAME, COMPANY_ADDRESS=config.COMPANY_ADDRESS,
                             GRIEVANCE_EMAIL=config.GRIEVANCE_EMAIL, GRIEVANCE_OFFICER=config.GRIEVANCE_OFFICER, TERMS_VERSION=config.TERMS_VERSION,
                             CAP_MONTHS=config.LIABILITY_CAP_MONTHS, JURISDICTION=config.JURISDICTION_CITY, MFA_REQUIRED=config.MFA_REQUIRED, COUPON_PERCENT=config.COUPON_PERCENT, COUPONS_ENABLED=config.COUPONS_ENABLED)

FLASH = {
    "upi_review": ("ok", "Thanks. We received your reference number and will credit your wallet once the payment is confirmed in our bank account."),
    "topup_ok": ("ok", "Payment received. Your wallet has been credited."),
    "need_balance": ("warn", f"Your balance is too low for a report. Choose a payment option on the wallet page to add reports."),
    "signed_out": ("ok", "You have been signed out."),
    "login_first": ("warn", "Please sign in to continue."),
    "verify_fail": ("warn", "Payment could not be verified. If money was deducted it will be credited automatically within a few minutes."),
    "dup": ("ok", "You already generated this report recently, so we opened it again. No charge was made."),
}


def current_user(request: Request):
    sess = security.read_session(request.cookies.get("sid"))
    if not sess:
        return None
    now = time.time()
    if now - sess["ls"] > config.SESSION_IDLE_MINUTES * 60:      # unattended too long: the cookie is dead
        return None
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (sess["uid"],)).fetchone()
    if row is None or int(row["session_epoch"] or 0) != sess["e"]:    # signed out elsewhere, or the account was reset
        return None
    if now - sess["ls"] > 60:
        request.state.refresh_sid = security.make_session(row["id"], row["session_epoch"] or 0)   # sliding idle window
    u = dict(row)
    u["master"] = bool(u.get("is_master")) and config.MASTER_ENABLED   # master accounts are never charged
    u["price"] = u["price_paise"] if u.get("privileged") and u.get("price_paise") else config.PRICE_PAISE   # privileged customers pay their own rate
    with db() as conn:
        agreed = {r["kind"] for r in conn.execute("SELECT kind FROM consents WHERE user_id=? AND version=?", (u["id"], config.TERMS_VERSION))}
    u["consent_ok"] = CONSENT_KINDS <= agreed       # false until the user accepts the current Terms version
    return u


CONSENT_KINDS = {"terms", "privacy", "borrower_consent"}
GUARDED = ("/dashboard", "/reports", "/wallet", "/pay")


def audit(request, user_id, event: str, ref: str | None = None) -> None:
    """Append-only record of sign-ins, uploads, views and downloads (who, what, from where, when)."""
    try:
        with tx() as conn:
            conn.execute("INSERT INTO audit_log(user_id, event, ref, ip) VALUES (?,?,?,?)", (user_id, event, ref, ip_of(request) if request else None))
    except Exception:   # never let logging break a request
        pass


def record_consents(conn, user_id: int, request) -> None:
    ua = (request.headers.get("user-agent") or "")[:200]
    for kind in sorted(CONSENT_KINDS):
        conn.execute("INSERT INTO consents(user_id, kind, version, ip, user_agent) VALUES (?,?,?,?,?)",
                     (user_id, kind, config.TERMS_VERSION, ip_of(request), ua))


@app.middleware("http")
async def guard_and_headers(request: Request, call_next):
    path = request.url.path
    if path.startswith(GUARDED):
        user = current_user(request)
        if user and not user["consent_ok"]:
            return redirect("/terms/accept")
        if user and config.MFA_REQUIRED and user["has_password"] and not user["mfa_enabled"]:
            return redirect("/mfa/setup")
    resp = await call_next(request)
    h = resp.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    h["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                    "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://checkout.razorpay.com; "
                                    "frame-src https://api.razorpay.com; connect-src 'self' https://*.razorpay.com; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
    if config.APP_ENV == "production":
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not path.startswith("/static"):
        h["Cache-Control"] = "no-store"        # pages can contain borrower data
    token = getattr(request.state, "refresh_sid", None)
    if token:
        resp.set_cookie("sid", token, max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=config.APP_ENV == "production")
    return resp


def render(request: Request, name: str, user=None, status: int = 200, **ctx):
    scope = str(user["id"]) if user else "anon"
    flash = FLASH.get(request.query_params.get("m", ""))
    ctx.setdefault("flash", flash)
    if user and user.get("price") and user["price"] != config.PRICE_PAISE and name in ("dashboard.html", "wallet.html"):
        ctx["PRICE"] = fmt_paise(user["price"])     # a privileged customer sees their own rate
    return templates.TemplateResponse(request, name, {"user": user, "csrf": security.make_csrf(scope), **ctx}, status_code=status)


def redirect(url: str, status: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status)


def set_session(resp, user_id: int):
    with db() as conn:
        ep = conn.execute("SELECT session_epoch FROM users WHERE id=?", (user_id,)).fetchone()
    resp.set_cookie("sid", security.make_session(user_id, ep["session_epoch"] if ep else 0), max_age=config.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=config.APP_ENV == "production")
    return resp


def ip_of(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return (xff.split(",")[0].strip() if xff else (request.client.host if request.client else "?"))


EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
ROLES = {"lender": "Lender / NBFC", "agent": "Loan agent", "dsa": "DSA"}


# ----------------------------------------------------------------------------- public pages
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return render(request, "landing.html", current_user(request))      # public page: also shown to signed-in users (the nav has Reports)


def _otp_cookie(resp, otp_id: int):
    resp.set_cookie("otp_pending", security.make_otp_token(otp_id), max_age=1800, httponly=True, samesite="lax", secure=config.APP_ENV == "production")
    return resp


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if current_user(request):
        return redirect("/dashboard")
    return render(request, "signup.html", roles=ROLES, form={}, error=None)


@app.post("/signup", response_class=HTMLResponse)
def signup(request: Request, name: str = Form(""), email: str = Form(""), org: str = Form(""),
           role: str = Form(""), mobile: str = Form(""), csrf: str = Form(""), agree_terms: str = Form(""), agree_privacy: str = Form(""),
           agree_borrower: str = Form("")):
    """Step 1: check the details and email a code to the address. The account is created only after the code is verified."""
    form = {"name": name, "email": email, "org": org, "role": role, "mobile": mobile}

    def fail(msg, code=422):
        return render(request, "signup.html", status=code, roles=ROLES, form=form, error=msg)

    if not config.ALLOW_SIGNUP:
        return fail("Sign-ups are closed right now.", 403)
    if not security.check_csrf(csrf, "anon"):
        return fail("Your session expired. Please try again.", 400)
    if not security.signup_throttle.allow(ip_of(request)):
        return fail("Too many sign-ups from this network. Try again later.", 429)
    email = email.strip().lower()
    name, org = name.strip(), org.strip()
    num = otp.clean_mobile(mobile)
    if not name:
        return fail("Please enter your name.")
    if not EMAIL_RX.match(email):
        return fail("Please enter a valid email address.")
    if not num:
        return fail("Enter a valid 10-digit Indian mobile number.")
    if role not in ROLES:
        return fail("Please choose whether you are a lender, agent or DSA.")
    if not (agree_terms and agree_privacy and agree_borrower):
        return fail("To create an account you must accept the Terms of Use, the Privacy Policy and the borrower-consent undertaking.")
    with db() as conn:
        taken = conn.execute("SELECT 1 FROM users WHERE email=? OR mobile=?", (email, num)).fetchone()
    if taken:
        return fail("An account with this email or mobile number already exists. Sign in instead.", 409)
    try:
        oid = otp.issue("signup", email, email, {"name": name, "org": org, "role": role, "mobile": num, "agree_ip": ip_of(request)})
    except otp.OtpError as e:
        return fail(e.message, e.status)
    return _otp_cookie(redirect("/verify"), oid)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return redirect("/dashboard")
    return render(request, "login.html", error=None, form={})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(""), csrf: str = Form("")):
    """Step 1 of sign-in: a code is emailed to the registered address."""
    form = {"email": email}
    email = email.strip().lower()

    def fail(msg, code=401):
        return render(request, "login.html", status=code, error=msg, form=form)

    if not security.check_csrf(csrf, "anon"):
        return fail("Your session expired. Please try again.", 400)
    if not EMAIL_RX.match(email):
        return fail("Enter your registered email address.", 422)
    if not security.login_throttle.allow(f"{ip_of(request)}|{email}"):
        return fail("Too many attempts. Please wait a few minutes.", 429)
    with db() as conn:
        u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not u:
        audit(request, None, "login_failed", email[:60])
        return fail("No account was found for that email. Create an account first.")
    try:
        oid = otp.issue("login", email, email)
    except otp.OtpError as e:
        return fail(e.message, e.status)
    return _otp_cookie(redirect("/verify"), oid)


def _verify_ctx(oid: int, error=None):
    o = otp.get(oid)
    ctx = {"masked": otp.mask_email(o["mobile"]), "purpose": o["purpose"], "error": error, "resend_seconds": config.OTP_RESEND_SECONDS}
    if config.OTP_PROVIDER == "dev":
        ctx["dev_code"] = otp.LAST.get(o["mobile"])
    return ctx


@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request):
    oid = security.read_otp_token(request.cookies.get("otp_pending"))
    if not oid or not otp.get(oid):
        return redirect("/login")
    return render(request, "verify.html", **_verify_ctx(oid))


@app.post("/verify", response_class=HTMLResponse)
def verify_submit(request: Request, code: str = Form(""), csrf: str = Form("")):
    oid = security.read_otp_token(request.cookies.get("otp_pending"))
    if not oid or not otp.get(oid):
        return redirect("/login")
    if not security.check_csrf(csrf, "anon"):
        return render(request, "verify.html", status=400, **_verify_ctx(oid, "Your session expired. Please reload and try again."))
    try:
        res = otp.verify(oid, code)
    except otp.OtpError as e:
        audit(request, None, "otp_failed", str(oid))
        return render(request, "verify.html", status=e.status, **_verify_ctx(oid, e.message))
    if res["purpose"] == "signup":
        p = res["payload"]
        try:
            with tx() as conn:
                cur = conn.execute("INSERT INTO users(email, password_hash, name, org, role, mobile, has_password, mobile_verified) VALUES (?,?,?,?,?,?,0,0)",
                                   (res["email"], security.hash_password(secrets.token_urlsafe(32)), p["name"], p["org"] or None, p["role"], p.get("mobile")))
                uid = cur.lastrowid
                record_consents(conn, uid, request)
        except Exception as e:
            if "UNIQUE" in str(e):
                return render(request, "verify.html", status=409, **_verify_ctx(oid, "An account with these details already exists. Please sign in."))
            raise
        audit(request, uid, "signup", config.TERMS_VERSION)
    else:
        with db() as conn:
            u = conn.execute("SELECT id FROM users WHERE email=?", (res["email"],)).fetchone()
        if not u:
            return redirect("/login")
        uid = u["id"]
        audit(request, uid, "login", "otp")
    resp = set_session(redirect("/dashboard"), uid)
    resp.delete_cookie("otp_pending")
    return resp


@app.post("/verify/resend", response_class=HTMLResponse)
def verify_resend(request: Request, csrf: str = Form("")):
    oid = security.read_otp_token(request.cookies.get("otp_pending"))
    o = otp.get(oid) if oid else None
    if not o:
        return redirect("/login")
    if not security.check_csrf(csrf, "anon"):
        return render(request, "verify.html", status=400, **_verify_ctx(oid, "Your session expired. Please reload and try again."))
    payload = json.loads(o["payload"]) if o["payload"] else None
    try:
        new_id = otp.issue(o["purpose"], o["mobile"], o["email"], payload)
    except otp.OtpError as e:
        return render(request, "verify.html", status=e.status, **_verify_ctx(oid, e.message))
    return _otp_cookie(redirect("/verify"), new_id)


@app.get("/staff/login", response_class=HTMLResponse)
def staff_login_page(request: Request):
    if current_user(request):
        return redirect("/dashboard")
    return render(request, "staff_login.html", error=None, email="")


@app.post("/staff/login", response_class=HTMLResponse)
def staff_login(request: Request, email: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    """Password sign-in for staff and test (master) accounts. Customers use the emailed code."""
    email = email.strip().lower()

    def fail(msg, code=401):
        return render(request, "staff_login.html", status=code, error=msg, email=email)

    if not security.check_csrf(csrf, "anon"):
        return fail("Your session expired. Please try again.", 400)
    key = f"{ip_of(request)}|{email}"
    if not security.login_throttle.allow(key):
        return fail("Too many attempts. Please wait a few minutes.", 429)
    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=? AND has_password=1", (email,)).fetchone()
    if not u or not security.verify_password(password, u["password_hash"]):
        audit(request, u["id"] if u else None, "login_failed", email[:60])
        return fail("Email or password is incorrect.")
    security.login_throttle.reset(key)
    if security.needs_rehash(u["password_hash"]):          # upgrade PBKDF2 hashes to Argon2id
        with tx() as conn:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(password), u["id"]))
    if u["mfa_enabled"]:
        resp = redirect("/mfa")
        resp.set_cookie("mfa_pending", security.make_mfa_token(u["id"]), max_age=600, httponly=True, samesite="lax", secure=config.APP_ENV == "production")
        return resp
    audit(request, u["id"], "login")
    return set_session(redirect("/dashboard"), u["id"])


# ----------------------------------------------------------------------------- two-factor sign-in
@app.get("/mfa", response_class=HTMLResponse)
def mfa_page(request: Request):
    if not security.read_mfa_token(request.cookies.get("mfa_pending")):
        return redirect("/staff/login")
    return render(request, "mfa.html", error=None)


@app.post("/mfa", response_class=HTMLResponse)
def mfa_verify(request: Request, code: str = Form(""), csrf: str = Form("")):
    uid = security.read_mfa_token(request.cookies.get("mfa_pending"))
    if not uid:
        return redirect("/staff/login")
    if not security.check_csrf(csrf, "anon"):
        return render(request, "mfa.html", status=400, error="Your session expired. Please reload and try again.")
    if not security.login_throttle.allow(f"mfa|{uid}"):
        return render(request, "mfa.html", status=429, error="Too many attempts. Please wait a few minutes.")
    with db() as conn:
        u = conn.execute("SELECT mfa_secret, mfa_enabled FROM users WHERE id=?", (uid,)).fetchone()
    if not u or not u["mfa_enabled"] or not security.verify_totp(u["mfa_secret"], code):
        audit(request, uid, "mfa_failed")
        return render(request, "mfa.html", status=401, error="That code is not correct. Enter the current 6-digit code from your authenticator app.")
    security.login_throttle.reset(f"mfa|{uid}")
    audit(request, uid, "login", "mfa")
    resp = set_session(redirect("/dashboard"), uid)
    resp.delete_cookie("mfa_pending")
    return resp


@app.get("/mfa/setup", response_class=HTMLResponse)
def mfa_setup_page(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    if user["mfa_enabled"]:
        return redirect("/dashboard")
    secret = user["mfa_secret"]
    if not secret:
        secret = security.new_totp_secret()
        with tx() as conn:
            conn.execute("UPDATE users SET mfa_secret=? WHERE id=?", (secret, user["id"]))
    return render(request, "mfa_setup.html", user, secret=secret, grouped=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                  uri=security.totp_uri(secret, user["email"], config.BRAND), error=None)


@app.post("/mfa/setup", response_class=HTMLResponse)
def mfa_setup_confirm(request: Request, code: str = Form(""), csrf: str = Form("")):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    secret = user["mfa_secret"] or ""
    if not security.check_csrf(csrf, str(user["id"])) or not security.verify_totp(secret, code):
        return render(request, "mfa_setup.html", user, status=422, secret=secret, grouped=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                      uri=security.totp_uri(secret, user["email"], config.BRAND), error="That code is not correct. Check the code in your app and try again.")
    with tx() as conn:
        conn.execute("UPDATE users SET mfa_enabled=1 WHERE id=?", (user["id"],))
    audit(request, user["id"], "mfa_enabled")
    return redirect("/dashboard")


# ----------------------------------------------------------------------------- legal and information pages
def _page(name):
    def view(request: Request):
        return render(request, name, current_user(request))
    return view


for _path, _tpl in (("/terms", "terms.html"), ("/privacy", "privacy.html"), ("/banks", "banks.html"), ("/security", "security.html"),
                    ("/grievance", "grievance.html")):
    app.add_api_route(_path, _page(_tpl), methods=["GET"], response_class=HTMLResponse)


@app.get("/terms/accept", response_class=HTMLResponse)
def terms_accept_page(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    if user["consent_ok"]:
        return redirect("/dashboard")
    return render(request, "accept_terms.html", user, error=None)


@app.post("/terms/accept", response_class=HTMLResponse)
def terms_accept(request: Request, csrf: str = Form(""), agree_terms: str = Form(""), agree_privacy: str = Form(""), agree_borrower: str = Form("")):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    if not security.check_csrf(csrf, str(user["id"])):
        return render(request, "accept_terms.html", user, status=400, error="Your session expired. Please try again.")
    if not (agree_terms and agree_privacy and agree_borrower):
        return render(request, "accept_terms.html", user, status=422, error="You must accept all three to continue.")
    with tx() as conn:
        record_consents(conn, user["id"], request)
    audit(request, user["id"], "consent_accepted", config.TERMS_VERSION)
    return redirect("/dashboard")


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    user = current_user(request)
    if user and not security.check_csrf(csrf, str(user["id"])):
        return redirect("/dashboard")
    resp = redirect("/login?m=signed_out")
    resp.delete_cookie("sid")
    if user:      # revoke every cookie issued so far, so a copied cookie stops working
        with tx() as conn:
            conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id=?", (user["id"],))
        audit(request, user["id"], "logout")
    return resp


# ----------------------------------------------------------------------------- dashboard & upload
def _recent_reports(uid: int, limit: int = 50):
    with db() as conn:
        return conn.execute("SELECT * FROM reports WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (uid, limit)).fetchall()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    return render(request, "dashboard.html", user, reports=_recent_reports(user["id"]), error=None, form={},
                  low=(not user["master"]) and user["balance_paise"] < user["price"])


def _dash_error(request, user, status, code, message, form):
    return render(request, "dashboard.html", user, status=status, reports=_recent_reports(user["id"]),
                  error={"code": code, "message": message}, form=form, low=(not user["master"]) and user["balance_paise"] < user["price"])


@app.post("/reports", response_class=HTMLResponse)
async def create_report(request: Request, file: UploadFile, password: str = Form(""), applicant_ref: str = Form(""), csrf: str = Form(""),
                        borrower_consent: str = Form("")):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    form = {"applicant_ref": applicant_ref}
    audit(request, user["id"], "upload", None)
    if not security.check_csrf(csrf, str(user["id"])):
        return _dash_error(request, user, 400, "CSRF", "Your session expired. Please reload the page and try again.", form)
    applicant_ref = applicant_ref.strip()[:60]
    if not borrower_consent:        # the user must confirm, every time, that the account holder has agreed to this upload
        return _dash_error(request, user, 422, "CONSENT_REQUIRED",
                           "Please confirm that the account holder has consented to this statement being uploaded and analysed.", form)

    # 1. balance check before doing any work
    free = user["master"]
    if not free and user["balance_paise"] < user["price"]:
        return redirect("/wallet?m=need_balance")

    # 2. read the file with a hard size cap
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(limit + 1)
    filename = (file.filename or "statement.pdf")[:120]
    rid = uuid.uuid4().hex[:12]
    sha = hashlib.sha256(data).hexdigest()

    def record_failure(code, message):
        with tx() as conn:
            conn.execute("INSERT INTO reports(id, user_id, applicant_ref, filename, file_sha256, status, error_code, error_message) VALUES (?,?,?,?,?,'failed',?,?)",
                         (rid, user["id"], applicant_ref, filename, sha, code, message))

    if len(data) > limit:
        record_failure("FILE_TOO_LARGE", ingest.ERRORS["FILE_TOO_LARGE"])
        return _dash_error(request, user, 413, "FILE_TOO_LARGE", ingest.ERRORS["FILE_TOO_LARGE"], form)
    if not data:
        record_failure("EMPTY_FILE", ingest.ERRORS["EMPTY_FILE"])
        return _dash_error(request, user, 422, "EMPTY_FILE", ingest.ERRORS["EMPTY_FILE"], form)

    # 3. the same file again within the window is free: open the existing report
    since = (datetime.now(timezone.utc) - timedelta(days=config.DUPLICATE_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        dup = conn.execute("SELECT id FROM reports WHERE user_id=? AND file_sha256=? AND status='done' AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                           (user["id"], sha, since)).fetchone()
    if dup and not free:   # master accounts always regenerate, so every change can be tested
        return redirect(f"/reports/{dup['id']}?m=dup")

    # 4. reserve the price, process, then charge only if a report came out
    try:
        if not free:
            wallet.hold(user["id"], user["price"], rid)
    except wallet.InsufficientBalance:
        return redirect("/wallet?m=need_balance")
    with tx() as conn:
        conn.execute("INSERT INTO reports(id, user_id, applicant_ref, filename, file_sha256, status, borrower_consent_at) VALUES (?,?,?,?,?,'processing',datetime('now'))",
                     (rid, user["id"], applicant_ref, filename, sha))
    try:
        result = await run_in_threadpool(_process, data, password or None, applicant_ref, rid)
    except ingest.StatementError as e:
        if not free:
            wallet.release(user["id"], user["price"], rid, f"Not charged: {e.code}")
        with tx() as conn:
            conn.execute("UPDATE reports SET status='failed', error_code=?, error_message=? WHERE id=?", (e.code, e.message, rid))
        user = current_user(request)
        return _dash_error(request, user, 422, e.code, e.message, form)
    except Exception:
        if not free:
            wallet.release(user["id"], user["price"], rid, "Not charged: processing error")
        msg = "Something went wrong while reading this statement. You have not been charged. Please try again or contact support."
        with tx() as conn:
            conn.execute("UPDATE reports SET status='failed', error_code='INTERNAL', error_message=? WHERE id=?", (msg, rid))
        user = current_user(request)
        return _dash_error(request, user, 500, "INTERNAL", msg, form)

    if not free:
        wallet.commit(user["id"], user["price"], rid)
    with tx() as conn:
        conn.execute("UPDATE reports SET status='done', bank=?, account_masked=?, period_from=?, period_to=?, txn_count=?, risk_score=?, risk_band=?, "
                     "charged_paise=?, summary_json=?, xlsx_path=? WHERE id=?",
                     (result["bank"], result["account_masked"], result["period_from"], result["period_to"], result["txn_count"],
                      result["risk_score"], result["risk_band"], 0 if free else user["price"], json.dumps(result["summary"]), result["xlsx_path"], rid))
    return redirect(f"/reports/{rid}")


def _process(data: bytes, password: str | None, applicant_ref: str, rid: str) -> dict:
    parsed = ingest.ingest(data, password)
    a = metrics.analyze(parsed)
    path = config.REPORT_DIR / f"{rid}.xlsx"
    excel_report.build(a, applicant_ref, rid, path)
    t, meta = a["total"], a["meta"]
    summary = {
        "head": metrics.headline(a), "meta": {k: (str(v) if v is not None else None) for k, v in meta.items() if k not in ("account_number", "summary_check")},
        "months": [{"key": m, "label": datetime.strptime(m, "%Y-%m").strftime("%b %Y"), "partial": a["partial"][m],
                    "credit": a["monthly"][m]["credit_adj"], "debit": a["monthly"][m]["debit_adj"], "bal_avg": a["monthly"][m]["bal_avg"],
                    "net": a["monthly"][m]["net_flow"]} for m in a["months"]],
        "flags": a["flags"], "top_payers": a["parties_credit"][:5], "top_payees": a["parties_debit"][:5],
        "channels": {k: t[f"credit_ch_{k.lower()}"] for k in metrics.CH},
        "doc_flags": a["doc_flags"],
        "summary_check": ({"verified": meta["summary_check"]["verified"], "cr": meta["summary_check"]["printed"]["cr_count"],
                           "dr": meta["summary_check"]["printed"]["dr_count"]} if meta.get("summary_check") else None),
    }
    summary["top_payers"] = [{k: (str(v) if k in ("first", "last") else v) for k, v in p.items()} for p in summary["top_payers"]]
    summary["top_payees"] = [{k: (str(v) if k in ("first", "last") else v) for k, v in p.items()} for p in summary["top_payees"]]
    return {"bank": meta.get("bank"), "account_masked": meta.get("account_masked"), "period_from": str(a["rows"][0]["date"]),
            "period_to": str(a["rows"][-1]["date"]), "txn_count": t["txn_count"], "risk_score": t["risk_score"],
            "risk_band": a["risk"]["band"], "summary": summary, "xlsx_path": str(path)}


def _own_report(uid: int, rid: str):
    with db() as conn:
        return conn.execute("SELECT * FROM reports WHERE id=? AND user_id=?", (rid, uid)).fetchone()


@app.get("/reports/{rid}", response_class=HTMLResponse)
def report_view(request: Request, rid: str):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    r = _own_report(user["id"], rid)
    if not r or r["status"] != "done":
        return render(request, "error.html", user, status=404, title="Report not found", message="This report does not exist or is still being prepared.")
    if not r["summary_json"]:
        return render(request, "error.html", user, status=410, title="Report expired",
                      message=f"Reports are kept for {config.REPORT_RETENTION_DAYS} days and then deleted. Upload the statement again to generate a new one.")
    audit(request, user["id"], "view_report", rid)
    s = json.loads(r["summary_json"])
    expired = not (r["xlsx_path"] and Path(r["xlsx_path"]).exists())
    return render(request, "report.html", user, r=r, s=s, expired=expired)


@app.get("/reports/{rid}/download")
def report_download(request: Request, rid: str):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    r = _own_report(user["id"], rid)
    if not r or r["status"] != "done" or not r["xlsx_path"] or not Path(r["xlsx_path"]).exists():
        return render(request, "error.html", user, status=404, title="File not available",
                      message=f"Excel files are kept for {config.REPORT_RETENTION_DAYS} days. Upload the statement again to regenerate it.")
    audit(request, user["id"], "download_report", rid)
    try:
        with tx() as conn:
            conn.execute("UPDATE reports SET download_count=download_count+1, last_downloaded_at=datetime('now') WHERE id=? AND user_id=?", (rid, user["id"]))
    except Exception:       # a counter problem must never block the customer's download
        pass
    ref = re.sub(r"[^A-Za-z0-9_-]+", "_", r["applicant_ref"] or "") or "report"
    return FileResponse(r["xlsx_path"], filename=f"{config.BRAND}_{ref}_{r['id']}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ----------------------------------------------------------------------------- wallet & payments
def _wallet_page(request: Request, user, error=None, status: int = 200):
    with db() as conn:
        hist = wallet.history(conn, user["id"])
        packs = wallet.packs_for(conn, user["id"], user["price"])
    return render(request, "wallet.html", user, status=status, history=hist, error=error, packs=packs,
                  reports_possible=user["balance_paise"] // user["price"])


@app.get("/wallet", response_class=HTMLResponse)
def wallet_page(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    return _wallet_page(request, user)


@app.get("/wallet/coupon")
def coupon_preview(request: Request, code: str = "", amount: str = ""):
    """Live preview of what a coupon takes off a top-up."""
    if not config.COUPONS_ENABLED:
        return JSONResponse({"ok": False, "message": "Coupons are not available."}, status_code=404)
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "message": "Please sign in."}, status_code=401)
    if not security.login_throttle.allow(f"coupon|{user['id']}"):
        return JSONResponse({"ok": False, "message": "Too many tries. Please wait a few minutes."}, status_code=429)
    try:
        paise = int(round(float(amount.replace(",", "").strip()) * 100))
        wallet.validate_topup(paise)
        with db() as conn:
            q = coupons.quote(conn, code, user["id"], paise)
    except (ValueError, wallet.TopupError, coupons.CouponError) as e:
        msg = str(e) if isinstance(e, (wallet.TopupError, coupons.CouponError)) else "Enter the amount in rupees first."
        return JSONResponse({"ok": False, "message": msg})
    return {"ok": True, "percent": q["percent"], "discount": fmt_paise(q["discount_paise"]), "pay": fmt_paise(q["pay_paise"]),
            "message": f"{q['percent']}% off applied. You pay {fmt_paise(q['pay_paise'])} and {fmt_paise(paise)} is added to your wallet."}


@app.post("/wallet/topup", response_class=HTMLResponse)
def topup(request: Request, pack: str = Form(""), coupon: str = Form(""), csrf: str = Form("")):
    """Payment options are fixed. The amount comes from the server's own table, never from the browser."""
    user = current_user(request)
    if not config.COUPONS_ENABLED:
        coupon = ""
    if not user:
        return redirect("/login?m=login_first")

    def fail(msg, code=422):
        return _wallet_page(request, user, error=msg, status=code)

    if not security.check_csrf(csrf, str(user["id"])):
        return fail("Your session expired. Please reload and try again.", 400)
    if not security.topup_throttle.allow(f"topup|{user['id']}"):
        return fail("Too many payment attempts. Please wait a while and try again.", 429)
    pk = wallet.get_pack(pack)
    if not pk:
        return fail("Please choose one of the payment options.")
    with db() as conn:
        if pk["first_only"] and not wallet.starter_available(conn, user["id"]):
            return fail("The first-report offer is only for your first report and can be used once.")
    try:
        order = payments.create_order(user["id"], pk["paise"], coupon.strip() or None, pack=pk["id"])
    except payments.PaymentError as e:
        return fail(str(e), 422 if (coupon.strip() or e.code == "pack") else 502)
    audit(request, user["id"], "topup_started", f"{order['order_id']} {pk['id']}")
    return redirect(f"/pay/{order['order_id']}")


# ----------------------------------------------------------------------------- coupons (master account only)
def _master_only(request: Request, coupons_page: bool = False):
    if coupons_page and not config.COUPONS_ENABLED:
        return None
    user = current_user(request)
    if not user or not user["master"]:
        return None      # shown as a plain 404 so the page is not discoverable
    return user


@app.get("/admin/coupons", response_class=HTMLResponse)
def coupons_page(request: Request):
    user = _master_only(request, coupons_page=True)
    if not user:
        return render(request, "error.html", current_user(request), status=404, title="Page not found", message="We could not find that page.")
    return render(request, "coupons.html", user, rows=coupons.listing(), error=None, default_percent=config.COUPON_PERCENT)


@app.post("/admin/coupons", response_class=HTMLResponse)
def coupons_create(request: Request, code: str = Form(""), percent: str = Form(""), max_redemptions: str = Form(""), days_valid: str = Form(""),
                   note: str = Form(""), csrf: str = Form("")):
    user = _master_only(request, coupons_page=True)
    if not user:
        return render(request, "error.html", current_user(request), status=404, title="Page not found", message="We could not find that page.")
    if not security.check_csrf(csrf, str(user["id"])):
        return render(request, "coupons.html", user, status=400, rows=coupons.listing(), error="Your session expired. Please reload and try again.", default_percent=config.COUPON_PERCENT)

    def num(v):
        v = v.strip()
        return int(v) if v else None
    try:
        new = coupons.create(user["id"], code or None, num(percent), num(max_redemptions), num(days_valid), note)
    except (coupons.CouponError, ValueError) as e:
        msg = str(e) if isinstance(e, coupons.CouponError) else "Use whole numbers for the percentage, limit and days."
        return render(request, "coupons.html", user, status=422, rows=coupons.listing(), error=msg, default_percent=config.COUPON_PERCENT)
    audit(request, user["id"], "coupon_created", new)
    return redirect(f"/admin/coupons?new={new}")


def _users_ctx(error=None, created=None):
    with db() as conn:
        rows = conn.execute("SELECT id, name, email, mobile, role, org, balance_paise, is_master, privileged, price_paise, created_at FROM users ORDER BY id DESC LIMIT 100").fetchall()
    return {"rows": rows, "error": error, "created": created, "roles": ROLES}


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    user = _master_only(request)
    if not user:
        return render(request, "error.html", current_user(request), status=404, title="Page not found", message="We could not find that page.")
    return render(request, "admin_users.html", user, **_users_ctx())


@app.post("/admin/users", response_class=HTMLResponse)
def admin_users_create(request: Request, name: str = Form(""), email: str = Form(""), mobile: str = Form(""), role: str = Form(""),
                       org: str = Form(""), credit: str = Form(""), privileged: str = Form(""), rate: str = Form(""), csrf: str = Form("")):
    """A master account can open an account for a customer. The customer signs in with the mobile code and accepts the Terms on first sign-in."""
    user = _master_only(request)
    if not user:
        return render(request, "error.html", current_user(request), status=404, title="Page not found", message="We could not find that page.")

    def fail(msg, code=422):
        return render(request, "admin_users.html", user, status=code, **_users_ctx(msg))

    if not security.check_csrf(csrf, str(user["id"])):
        return fail("Your session expired. Please reload and try again.", 400)
    email, name, org = email.strip().lower(), name.strip(), org.strip()
    num = otp.clean_mobile(mobile)
    if not name:
        return fail("Please enter the customer's name.")
    if not EMAIL_RX.match(email):
        return fail("Please enter a valid email address.")
    if not num:
        return fail("Enter a valid 10-digit Indian mobile number.")
    if role not in ROLES:
        return fail("Please choose a role.")
    paise = 0
    if credit.strip():
        try:
            paise = wallet.parse_rupees(credit)
        except wallet.TopupError:
            return fail("Enter the starting credit in rupees, or leave it blank.")
        if paise < 0 or paise > config.MAX_TOPUP_PAISE:
            return fail("Starting credit is out of range.")
    priv, rate_paise = 0, None
    if privileged:
        priv = 1
        try:
            rate_paise = wallet.parse_rupees(rate) if rate.strip() else config.PRIVILEGED_PRICE_PAISE
        except wallet.TopupError:
            return fail("Enter the privileged rate in rupees, or leave it blank for the default.")
        if not 100 <= rate_paise <= config.PRICE_PAISE:
            return fail(f"The privileged rate must be between Rs 1 and {fmt_paise(config.PRICE_PAISE)}.")
    try:
        with tx() as conn:
            cur = conn.execute("INSERT INTO users(email, password_hash, name, org, role, mobile, has_password, mobile_verified, privileged, price_paise) VALUES (?,?,?,?,?,?,0,0,?,?)",
                               (email, security.hash_password(secrets.token_urlsafe(32)), name, org or None, role, num, priv, rate_paise))
            uid = cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            return fail("An account with this email or mobile number already exists.", 409)
        raise
    if paise:
        wallet.credit(uid, paise, f"master:{user['id']}", "Credit added by master account", kind="adjust")
    audit(request, user["id"], "user_created_by_master", str(uid))
    return render(request, "admin_users.html", user, status=201, **_users_ctx(created=f"{name} ({email})"))


@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request):
    """Master-only integrity check: every wallet balance against its append-only history and hash chain."""
    user = _master_only(request)
    if not user:
        return render(request, "error.html", current_user(request), status=404, title="Page not found", message="We could not find that page.")
    with db() as conn:
        n_users = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        n_rows = conn.execute("SELECT COUNT(*) n FROM wallet_ledger").fetchone()["n"]
        held = conn.execute("SELECT COALESCE(SUM(balance_paise),0) b, COALESCE(SUM(held_paise),0) h FROM users").fetchone()
        stuck = conn.execute("SELECT COUNT(*) n FROM payments WHERE status='review'").fetchone()["n"]
    return render(request, "admin_audit.html", user, problems=wallet.verify_ledger(), n_users=n_users, n_rows=n_rows, total_balance=held["b"], total_held=held["h"], review=stuck)


@app.get("/admin/users/{uid}/wallet", response_class=HTMLResponse)
def admin_user_wallet(request: Request, uid: int):
    user = _master_only(request)
    if not user:
        return render(request, "error.html", current_user(request), status=404, title="Page not found", message="We could not find that page.")
    with db() as conn:
        u = conn.execute("SELECT id, name, email, balance_paise, held_paise, privileged, price_paise FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return render(request, "error.html", user, status=404, title="Not found", message="No such user.")
        rows = conn.execute("SELECT * FROM wallet_ledger WHERE user_id=? ORDER BY id DESC LIMIT 500", (uid,)).fetchall()
    return render(request, "admin_user_wallet.html", user, u=u, rows=rows, problems=wallet.verify_ledger(uid))


@app.post("/admin/users/{uid}/privilege")
def admin_users_privilege(request: Request, uid: int, on: str = Form("0"), rate: str = Form(""), csrf: str = Form("")):
    user = _master_only(request)
    if not user or not security.check_csrf(csrf, str(user["id"])):
        return redirect("/admin/users")
    paise = config.PRIVILEGED_PRICE_PAISE
    if rate.strip():
        try:
            paise = wallet.parse_rupees(rate)
        except wallet.TopupError:
            return redirect("/admin/users")
    if on == "1" and not 100 <= paise <= config.PRICE_PAISE:
        return redirect("/admin/users")
    with tx() as conn:
        conn.execute("UPDATE users SET privileged=?, price_paise=? WHERE id=?", (1 if on == "1" else 0, paise if on == "1" else None, uid))
    audit(request, user["id"], "privilege_on" if on == "1" else "privilege_off", str(uid))
    return redirect("/admin/users")


@app.post("/admin/coupons/{code}/toggle")
def coupons_toggle(request: Request, code: str, active: str = Form("0"), csrf: str = Form("")):
    user = _master_only(request, coupons_page=True)
    if not user or not security.check_csrf(csrf, str(user["id"])):
        return redirect("/dashboard")
    coupons.set_active(code, active == "1")
    audit(request, user["id"], "coupon_on" if active == "1" else "coupon_off", code)
    return redirect("/admin/coupons")


UPI_ERRORS = {"utr": "Enter the 12-digit UPI reference number (UTR) from your payment app.",
              "dup": "That reference number has already been submitted.",
              "throttle": "Too many attempts. Please wait a while and try again.",
              "error": "We could not record that reference number. Please check it and try again."}


@app.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_page(request: Request, order_id: str):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    p = payments.get_payment(order_id)
    if not p or p["user_id"] != user["id"]:
        return render(request, "error.html", user, status=404, title="Order not found", message="We could not find this payment.")
    if p["status"] == "paid":
        return redirect("/wallet?m=topup_ok")
    qr = payments.upi_qr_svg(order_id, p["amount_paise"]) if p["provider"] == "upi" else None
    return render(request, "pay.html", user, p=p, key_id=config.RAZORPAY_KEY_ID, provider=p["provider"], qr=qr, upi_id=config.UPI_ID, error=UPI_ERRORS.get(request.query_params.get("e", "")))      # only fixed messages are ever shown, never text taken from the URL


@app.post("/pay/{order_id}/upi")
def pay_upi(request: Request, order_id: str, utr: str = Form(""), csrf: str = Form("")):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    if not security.check_csrf(csrf, str(user["id"])):
        return redirect("/wallet")
    if not security.utr_throttle.allow(f"utr|{user['id']}"):
        return redirect(f"/pay/{order_id}?e=throttle")
    try:
        payments.submit_utr(order_id, user["id"], utr)
    except payments.PaymentError as e:
        return redirect(f"/pay/{order_id}?e={e.code if e.code in UPI_ERRORS else 'error'}")
    audit(request, user["id"], "upi_utr_submitted", order_id)
    return redirect("/wallet?m=upi_review")


@app.get("/admin/payments", response_class=HTMLResponse)
def admin_payments(request: Request):
    user = _master_only(request)
    if not user:
        return render(request, "error.html", current_user(request), status=404, title="Page not found", message="We could not find that page.")
    return render(request, "admin_payments.html", user, rows=payments.pending_review())


@app.post("/admin/payments/{order_id}/{action}")
def admin_payment_action(request: Request, order_id: str, action: str, csrf: str = Form("")):
    user = _master_only(request)
    if not user or action not in ("approve", "reject") or not security.check_csrf(csrf, str(user["id"])):
        return redirect("/admin/payments")
    p = payments.get_payment(order_id)
    if p and p["user_id"] == user["id"]:      # maker-checker: nobody approves a payment into their own wallet
        audit(request, user["id"], "upi_self_approval_blocked", order_id)
        return redirect("/admin/payments")
    try:
        if p and p["provider"] == "upi" and p["status"] == "review":
            if action == "approve":
                payments.settle(order_id, p["payment_id"], p["amount_paise"])
            else:
                payments.reject(order_id)
            audit(request, user["id"], f"upi_{action}", order_id)
    except payments.PaymentError:
        pass
    return redirect("/admin/payments")


@app.post("/pay/{order_id}/mock")
def pay_mock(request: Request, order_id: str, csrf: str = Form("")):
    user = current_user(request)
    if not user or config.PAYMENT_MODE != "mock":
        return redirect("/login?m=login_first")
    p = payments.get_payment(order_id)
    if not p or p["user_id"] != user["id"] or p["provider"] != "mock" or not security.check_csrf(csrf, str(user["id"])):
        return redirect("/wallet")
    payments.settle(order_id, "mock_pay_" + order_id[-6:])
    return redirect("/wallet?m=topup_ok")


@app.post("/pay/{order_id}/razorpay")
def pay_razorpay_verify(request: Request, order_id: str, razorpay_payment_id: str = Form(""), razorpay_signature: str = Form("")):
    user = current_user(request)
    if not user:
        return redirect("/login?m=login_first")
    p = payments.get_payment(order_id)
    if not p or p["user_id"] != user["id"] or p["provider"] != "razorpay":
        return redirect("/wallet")
    if not payments.verify_checkout_signature(order_id, razorpay_payment_id, razorpay_signature):
        audit(request, user["id"], "payment_signature_invalid", order_id)
        return redirect("/wallet?m=verify_fail")
    try:
        payments.confirm_with_gateway(razorpay_payment_id, order_id, p["amount_paise"])
        payments.settle(order_id, razorpay_payment_id, p["amount_paise"])
    except payments.PaymentError:
        audit(request, user["id"], "payment_not_confirmed", order_id)
        return redirect("/wallet?m=verify_fail")
    return redirect("/wallet?m=topup_ok")


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    body = await request.body()
    if not payments.verify_webhook(body, request.headers.get("x-razorpay-signature", "")):
        return JSONResponse({"ok": False}, status_code=400)
    try:
        payments.handle_webhook(body)
    except payments.PaymentError:
        return JSONResponse({"ok": False}, status_code=422)
    return {"ok": True}


# ----------------------------------------------------------------------------- housekeeping
def cleanup_expired() -> int:
    """Delete expired reports: the Excel file, the stored summary (counterparty names etc.) and the uploaded file name.
    Only the billing record (date, reference, bank, period, counts, risk band, amount) stays."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.REPORT_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with tx() as conn:
        for r in conn.execute("SELECT id, xlsx_path FROM reports WHERE (xlsx_path IS NOT NULL OR summary_json IS NOT NULL) AND created_at<?", (cutoff,)).fetchall():
            try:
                if r["xlsx_path"]:
                    Path(r["xlsx_path"]).unlink(missing_ok=True)
            except OSError:
                pass
            conn.execute("UPDATE reports SET xlsx_path=NULL, summary_json=NULL, filename=NULL WHERE id=?", (r["id"],))
            n += 1
    return n


def recover_stuck(max_age_minutes: int = 10) -> int:
    """If the server died while a report was processing, give the reserved money back."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        stuck = conn.execute("SELECT id, user_id FROM reports WHERE status='processing' AND created_at<?", (cutoff,)).fetchall()
    for r in stuck:
        with db() as conn:
            pu = conn.execute("SELECT privileged, price_paise FROM users WHERE id=?", (r["user_id"],)).fetchone()
        price = pu["price_paise"] if pu and pu["privileged"] and pu["price_paise"] else config.PRICE_PAISE
        wallet.release(r["user_id"], price, r["id"], "Not charged: processing interrupted")
        with tx() as conn:
            conn.execute("UPDATE reports SET status='failed', error_code='INTERRUPTED', error_message='Processing was interrupted. You were not charged.' WHERE id=?", (r["id"],))
    return len(stuck)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return render(request, "error.html", current_user(request), status=404, title="Page not found", message="That page does not exist.")


# ----------------------------------------------------------------------------- admin console (own sign-in at /admin/login)
from . import admin_console  # noqa: E402  (registers filters; imported last because it uses helpers defined above)
app.include_router(admin_console.router)
