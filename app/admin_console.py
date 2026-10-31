"""Admin console: its own sign-in, an analytics dashboard, and read-only views of customers, payments,
reports and downloads, plus CSV export.

Design rules
- A separate cookie (asid, path /admin, SameSite=Strict, shorter idle limit) from the customer session, so a customer
  cookie is never an admin cookie. Only accounts with is_admin=1 get in; create one with  python -m app.cli admin EMAIL PASSWORD.
- Read-only. Nothing here changes a wallet, a payment or a report.
- Never shown: password hashes, authenticator secrets, sign-in codes, borrower names, statement contents, file names, file paths.
- Every sign-in, customer view and export is written to the audit log.
"""
from __future__ import annotations

import csv
import io
import math
import time
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from markupsafe import Markup

from . import config, security
from .db import db
from .main import APP_DIR, _group, audit, fmt_paise, ip_of, templates

router = APIRouter()
COOKIE = "asid"
IST = timedelta(hours=5, minutes=30)
PER_PAGE = 50

# ----------------------------------------------------------------------------- template helpers


def ist(v, fmt: str = "%d %b %Y, %H:%M") -> str:
    """Timestamps are stored in UTC; the console shows Indian time."""
    if not v:
        return "-"
    try:
        return (datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S") + IST).strftime(fmt)
    except ValueError:
        return str(v)


def num(v) -> str:
    return _group(int(v or 0))


def pct(a, b) -> str:
    return f"{(100.0 * a / b):.0f}%" if b else "-"


def urlq(path: str, **kw) -> str:
    q = urlencode({k: v for k, v in kw.items() if v not in (None, "", False)})
    return path + ("?" + q if q else "")


templates.env.filters.update({"ist": ist, "num": num})
templates.env.globals.update(urlq=urlq, pct=pct, ADMIN_IDLE=config.ADMIN_IDLE_MINUTES)

# ----------------------------------------------------------------------------- sign-in state


def admin_user(request: Request):
    """The signed-in admin, or None. Re-checked against the database on every request."""
    sess = security.read_admin_session(request.cookies.get(COOKIE))
    if not sess:
        return None
    now = time.time()
    if now - sess["ls"] > config.ADMIN_IDLE_MINUTES * 60:
        return None
    with db() as conn:
        row = conn.execute("SELECT id, email, name, is_admin, session_epoch FROM users WHERE id=?", (sess["uid"],)).fetchone()
    if row is None or not row["is_admin"] or int(row["session_epoch"] or 0) != sess["e"]:
        return None
    u = dict(row)
    u["refresh"] = now - sess["ls"] > 60
    return u


def _cookie(resp, uid: int, epoch: int):
    resp.set_cookie(COOKIE, security.make_admin_session(uid, epoch), max_age=12 * 3600, httponly=True, samesite="strict",
                    secure=config.APP_ENV == "production", path="/admin")
    return resp


def finish(resp, u=None):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    if u and u.get("refresh"):
        _cookie(resp, u["id"], u["session_epoch"] or 0)       # sliding idle window
    return resp


def page(request: Request, name: str, u, code: int = 200, **ctx):
    scope = f"adm{u['id']}" if u else "adm-anon"
    ctx.update(admin=u, csrf=security.make_csrf(scope), ASSET_A=int((APP_DIR / "static" / "admin.css").stat().st_mtime))
    return finish(templates.TemplateResponse(request, name, ctx, status_code=code), u)


def to_login():
    return finish(RedirectResponse("/admin/login", status_code=303))


_DUMMY = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = security.hash_password("not-a-real-password-0000")
    return _DUMMY


@router.get("/admin/login")
def login_page(request: Request):
    if admin_user(request):
        return finish(RedirectResponse("/admin", status_code=303))
    return page(request, "console_login.html", None, error=None, email="")


@router.post("/admin/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), code: str = Form(""), csrf: str = Form("")):
    email = email.strip().lower()[:200]

    def fail(msg="Incorrect username, password or code.", status=401):
        return page(request, "console_login.html", None, code=status, error=msg, email=email)

    if not security.check_csrf(csrf, "adm-anon"):
        return fail("Your session expired. Please try again.", 400)
    ip = ip_of(request)
    if not (security.admin_throttle.allow(f"{ip}|{email}") and security.admin_throttle.allow(f"ip|{ip}")):
        audit(request, None, "admin_login_throttled", email[:60])
        return fail("Too many attempts. Please wait 15 minutes.", 429)
    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=? AND is_admin=1 AND has_password=1", (email,)).fetchone()
    ok = security.verify_password(password, u["password_hash"] if u else _dummy_hash()) and u is not None   # same work whether or not the account exists
    if not ok:
        audit(request, u["id"] if u else None, "admin_login_failed", email[:60])
        return fail()
    if u["mfa_enabled"]:
        if not security.verify_totp(u["mfa_secret"], code.strip()):
            audit(request, u["id"], "admin_login_failed", "mfa")
            return fail()
    elif config.MFA_REQUIRED:
        return fail("Set up the authenticator app first: sign in once at /staff/login and follow the prompts, then return here.", 403)
    security.admin_throttle.reset(f"{ip}|{email}")
    audit(request, u["id"], "admin_login")
    return finish(_cookie(RedirectResponse("/admin", status_code=303), u["id"], u["session_epoch"] or 0))


@router.post("/admin/logout")
def logout(request: Request, csrf: str = Form("")):
    u = admin_user(request)
    resp = finish(RedirectResponse("/admin/login", status_code=303))
    resp.delete_cookie(COOKIE, path="/admin")
    if u and security.check_csrf(csrf, f"adm{u['id']}"):
        from .db import tx
        with tx() as conn:
            conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id=?", (u["id"],))     # kills every older cookie
        audit(request, u["id"], "admin_logout")
    return resp


# ----------------------------------------------------------------------------- charts (plain SVG/HTML, no scripts)


def _nice_max(v: float, money: bool = False) -> float:
    """A round top for the axis. Counts get an even top, so the middle tick is a whole number."""
    if money:
        v = max(v, 1)
        m = 10 ** math.floor(math.log10(v))
        for k in (1, 2, 2.5, 5, 10):
            if v <= k * m:
                return k * m
        return 10 * m
    if v <= 10:
        return max(4, math.ceil(v / 2) * 2)
    m = 10 ** math.floor(math.log10(v))
    for k in (2, 4, 6, 8, 10):
        if v <= k * m:
            return k * m
    return 10 * m


def _tick(v: float, money: bool) -> str:
    if money:
        return "₹" + (f"{v / 1000:g}k" if v >= 1000 else f"{v:g}")
    return f"{v:g}"


def _round_top(x: float, y: float, w: float, h: float, r: float) -> str:
    r = max(0.0, min(r, w / 2, h))
    return (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}"
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z")


def bars_svg(labels: list[str], series: list[tuple[str, list[float], str]], money: bool = False, title: str = "") -> Markup:
    """Daily columns, stacked when there are several series. Marks are thin, rounded at the data end, with a 2px gap
    between stacked fills, a recessive hairline grid, and a hover tooltip (<title>) on every column."""
    W, H, L, R, T, B = 720, 230, 44, 8, 10, 30
    n = max(len(labels), 1)
    totals = [sum(s[1][i] for s in series) for i in range(n)]
    top = _nice_max(max(totals) if totals else 0, money)
    iw, ih = W - L - R, H - T - B
    slot = iw / n
    bw = max(2.0, min(26.0, slot * 0.64))
    scale = ih / top
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}" preserveAspectRatio="xMidYMid meet">']
    for frac in (0, 0.5, 1):
        y = T + ih * (1 - frac)
        out.append(f'<line class="grid{" base" if frac == 0 else ""}" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="tick" x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{_tick(top * frac, money)}</text>')
    step = max(1, math.ceil(n / 8))
    for i in range(n):
        cx = L + slot * i + slot / 2
        x = cx - bw / 2
        tip = labels[i] + ": " + ", ".join(f"{(fmt_paise(round(s[1][i] * 100)) if money else num(s[1][i]))} {s[0].lower()}" for s in series if s[1][i]) \
            if totals[i] else labels[i] + ": none"
        out.append(f"<g><title>{escape(tip)}</title><rect class=\"hit\" x=\"{L + slot * i:.1f}\" y=\"{T}\" width=\"{slot:.1f}\" height=\"{ih}\"/>")
        acc = 0.0
        live = [k for k, s in enumerate(series) if s[1][i]]
        for k, s in enumerate(series):
            v = s[1][i]
            if not v:
                continue
            h = v * scale
            y = T + ih - (acc + v) * scale
            gap = 2 if acc > 0 and h > 3 else 0
            if k == live[-1]:
                out.append(f'<path class="{s[2]}" d="{_round_top(x, y, bw, max(h - gap, 1), 4)}"/>')
            else:
                out.append(f'<rect class="{s[2]}" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(h - gap, 1):.1f}"/>')
            acc += v
        out.append("</g>")
        if i % step == 0 or i == n - 1 and (n - 1) % step >= step // 2 + 1:
            out.append(f'<text class="tick" x="{cx:.1f}" y="{H - 10}" text-anchor="middle">{escape(labels[i])}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def hbars(rows: list[tuple[str, float, str]], cls: str = "s1") -> Markup:
    """Ranked horizontal bars: (label, value, text). One hue; the value is written at the end, so colour never carries it alone."""
    if not rows:
        return Markup('<p class="muted">No data in this period.</p>')
    mx = max(r[1] for r in rows) or 1
    items = "".join(
        f'<li><span class="hl">{escape(str(l))}</span><span class="hb"><i class="{cls}" style="width:{max(2.0, 100.0 * v / mx):.1f}%"></i></span>'
        f'<span class="hv">{escape(str(t))}</span></li>' for l, v, t in rows)
    return Markup(f'<ul class="hbars">{items}</ul>')


# ----------------------------------------------------------------------------- queries
# Every query is fixed text with bound parameters. Fragments below are joined once, at import, from constants: no request
# value is ever formatted into SQL (a test enforces this for the whole app).
T = "(:test=1 OR (u.is_master=0 AND u.is_admin=0))"          # customers only, unless test accounts are asked for
PU = "FROM payments p JOIN users u ON u.id=p.user_id"
RU = "FROM reports r JOIN users u ON u.id=r.user_id"

Q = {
    "users_total": f"SELECT COUNT(*) FROM users u WHERE {T}",
    "users_since": f"SELECT COUNT(*) FROM users u WHERE {T} AND u.created_at>=:since",
    "users_since7": f"SELECT COUNT(*) FROM users u WHERE {T} AND u.created_at>=:since7",
    "paying": f"SELECT COUNT(DISTINCT p.user_id) {PU} WHERE p.status='paid' AND {T}",
    "revenue": f"SELECT COALESCE(SUM(p.amount_paise),0) {PU} WHERE p.status='paid' AND {T}",
    "revenue_since": f"SELECT COALESCE(SUM(p.amount_paise),0) {PU} WHERE p.status='paid' AND {T} AND p.created_at>=:since",
    "pay_status": f"SELECT p.status s, COUNT(*) n {PU} WHERE {T} GROUP BY p.status",
    "wallet": f"SELECT COALESCE(SUM(u.balance_paise),0) b, COALESCE(SUM(u.held_paise),0) h FROM users u WHERE {T}",
    "reports": f"SELECT COUNT(*) up, COALESCE(SUM(r.status='done'),0) done, COALESCE(SUM(r.status='failed'),0) failed, COALESCE(SUM(r.charged_paise),0) spent, "
               f"COALESCE(SUM(r.download_count),0) dl, COALESCE(SUM(r.download_count>0),0) dlr {RU} WHERE {T}",
    "reports_since": f"SELECT COUNT(*) up, COALESCE(SUM(r.status='done'),0) done, COALESCE(SUM(r.status='failed'),0) failed, COALESCE(SUM(r.charged_paise),0) spent, "
                     f"COALESCE(SUM(r.download_count),0) dl {RU} WHERE {T} AND r.created_at>=:since",
    "f_uploaded": f"SELECT COUNT(DISTINCT r.user_id) {RU} WHERE {T}",
    "f_done": f"SELECT COUNT(DISTINCT r.user_id) {RU} WHERE r.status='done' AND {T}",
    "f_downloaded": f"SELECT COUNT(DISTINCT r.user_id) {RU} WHERE r.download_count>0 AND {T}",
    "d_signups": f"SELECT u.created_at t, 1 v FROM users u WHERE {T} AND u.created_at>=:since",
    "d_revenue": f"SELECT p.created_at t, p.amount_paise v {PU} WHERE p.status='paid' AND {T} AND p.created_at>=:since",
    "d_done": f"SELECT r.created_at t, 1 v {RU} WHERE r.status='done' AND {T} AND r.created_at>=:since",
    "d_failed": f"SELECT r.created_at t, 1 v {RU} WHERE r.status<>'done' AND {T} AND r.created_at>=:since",
    "d_downloads": f"SELECT a.created_at t, 1 v FROM audit_log a JOIN users u ON u.id=a.user_id WHERE a.event='download_report' AND {T} AND a.created_at>=:since",
    "banks": f"SELECT COALESCE(r.bank,'Unknown') b, COUNT(*) n {RU} WHERE r.status='done' AND {T} AND r.created_at>=:since GROUP BY b ORDER BY n DESC LIMIT 8",
    "fails": f"SELECT COALESCE(r.error_code,'UNKNOWN') e, COUNT(*) n {RU} WHERE r.status='failed' AND {T} AND r.created_at>=:since GROUP BY e ORDER BY n DESC LIMIT 8",
    "packs": f"SELECT COALESCE(p.pack,'-') p, COUNT(*) n, SUM(p.amount_paise) s {PU} WHERE p.status='paid' AND {T} GROUP BY p ORDER BY n DESC LIMIT 8",
    "roles": f"SELECT u.role role, COUNT(*) n FROM users u WHERE {T} GROUP BY u.role ORDER BY n DESC",
    "top": f"SELECT u.id, u.name, u.email, u.org, COALESCE(SUM(r.charged_paise),0) spent, COUNT(r.id) uploaded, COALESCE(SUM(r.status='done'),0) done, "
           f"COALESCE(SUM(r.download_count),0) dl FROM users u LEFT JOIN reports r ON r.user_id=u.id WHERE {T} GROUP BY u.id HAVING uploaded>0 "
           f"ORDER BY spent DESC, uploaded DESC LIMIT 8",
    "recent_users": f"SELECT u.id, u.name, u.email, u.role, u.created_at FROM users u WHERE {T} ORDER BY u.id DESC LIMIT 8",
    "recent_pay": f"SELECT p.order_id, p.amount_paise, p.status, p.created_at, u.id uid, u.name {PU} WHERE {T} ORDER BY p.id DESC LIMIT 8",
}

CUSTOMER_SQL = f"""
SELECT u.id, u.name, u.email, u.mobile, u.org, u.role, u.created_at, u.balance_paise, u.held_paise, u.is_master, u.is_admin,
       COALESCE(p.topped,0) topped, COALESCE(p.n,0) payments, COALESCE(r.spent,0) spent, COALESCE(r.up,0) uploaded,
       COALESCE(r.done,0) generated, COALESCE(r.failed,0) failed, COALESCE(r.dl,0) downloads,
       (SELECT MAX(a.created_at) FROM audit_log a WHERE a.user_id=u.id) last_seen
FROM users u
LEFT JOIN (SELECT user_id, SUM(amount_paise) topped, COUNT(*) n FROM payments WHERE status='paid' GROUP BY user_id) p ON p.user_id=u.id
LEFT JOIN (SELECT user_id, COUNT(*) up, SUM(status='done') done, SUM(status='failed') failed, SUM(charged_paise) spent, SUM(download_count) dl
           FROM reports GROUP BY user_id) r ON r.user_id=u.id
WHERE {T}
  AND (:q='' OR u.email LIKE :like ESCAPE '\\' OR u.name LIKE :like ESCAPE '\\' OR u.mobile LIKE :like ESCAPE '\\' OR u.org LIKE :like ESCAPE '\\')
"""
CUSTOMER_COUNT = f"SELECT COUNT(*) FROM ({CUSTOMER_SQL})"
_CUSTOMER_ORDER = {"new": "u.id DESC", "spend": "spent DESC, u.id DESC", "reports": "uploaded DESC, u.id DESC", "downloads": "downloads DESC, u.id DESC",
                   "balance": "u.balance_paise DESC, u.id DESC", "seen": "last_seen DESC, u.id DESC"}
CUSTOMER_PAGE = {k: f"{CUSTOMER_SQL} ORDER BY {v} LIMIT :n OFFSET :o" for k, v in _CUSTOMER_ORDER.items()}
CUSTOMER_EXPORT = f"{CUSTOMER_SQL} ORDER BY u.id"

TX_WHERE = (f"{T} AND (:status='' OR p.status=:status) AND (:q='' OR u.email LIKE :like ESCAPE '\\' OR u.name LIKE :like ESCAPE '\\' "
            f"OR p.order_id LIKE :like ESCAPE '\\' OR p.payment_id LIKE :like ESCAPE '\\')")
TX_COUNT = f"SELECT COUNT(*) {PU} WHERE {TX_WHERE}"
TX_ROWS = f"SELECT p.*, u.name, u.email {PU} WHERE {TX_WHERE} ORDER BY p.id DESC LIMIT :n OFFSET :o"
TX_SUMS = f"SELECT p.status, COUNT(*) n, COALESCE(SUM(p.amount_paise),0) s {PU} WHERE {T} GROUP BY p.status"

RP_WHERE = (f"{T} AND (:status='' OR r.status=:status) AND (:q='' OR u.email LIKE :like ESCAPE '\\' OR u.name LIKE :like ESCAPE '\\' "
            f"OR r.bank LIKE :like ESCAPE '\\' OR r.id LIKE :like ESCAPE '\\')")
RP_COUNT = f"SELECT COUNT(*) {RU} WHERE {RP_WHERE}"
_RP_SELECT = (f"SELECT r.id, r.status, r.error_code, r.bank, r.account_masked, r.period_from, r.period_to, r.txn_count, r.risk_band, r.charged_paise, "
              f"r.download_count, r.last_downloaded_at, r.created_at, u.id uid, u.name, u.email {RU} WHERE {RP_WHERE}")
RP_ROWS = {"new": f"{_RP_SELECT} ORDER BY r.created_at DESC LIMIT :n OFFSET :o",
           "downloads": f"{_RP_SELECT} ORDER BY r.download_count DESC, r.created_at DESC LIMIT :n OFFSET :o"}
RP_SUMMARY = (f"SELECT COUNT(*) up, COALESCE(SUM(r.status='done'),0) done, COALESCE(SUM(r.status='failed'),0) failed, COALESCE(SUM(r.download_count),0) dl, "
              f"COALESCE(SUM(r.download_count>0),0) dlr {RU} WHERE {T}")

# Raw tables. Only these columns are ever shown or exported: no password hashes, authenticator secrets, sign-in codes, file names,
# borrower references, statement summaries or server file paths.
_TABLE_DEFS = {
    "users": (["id", "email", "name", "org", "role", "mobile", "balance_paise", "held_paise", "is_master", "is_admin", "mfa_enabled", "has_password",
               "mobile_verified", "privileged", "price_paise", "created_at"], "id DESC"),
    "wallet_ledger": (["id", "user_id", "kind", "amount_paise", "balance_after_paise", "ref", "note", "created_at"], "id DESC"),
    "payments": (["id", "user_id", "provider", "order_id", "payment_id", "amount_paise", "credit_paise", "status", "pack", "coupon_code", "created_at", "paid_at"], "id DESC"),
    "reports": (["id", "user_id", "status", "error_code", "bank", "account_masked", "period_from", "period_to", "txn_count", "risk_score", "risk_band",
                 "charged_paise", "download_count", "last_downloaded_at", "borrower_consent_at", "created_at"], "created_at DESC"),
    "consents": (["id", "user_id", "kind", "version", "ip", "user_agent", "accepted_at"], "id DESC"),
    "audit_log": (["id", "user_id", "event", "ref", "ip", "created_at"], "id DESC"),
}
TABLES = {t: (cols, f"SELECT {', '.join(cols)} FROM {t} ORDER BY {order} LIMIT :n OFFSET :o", f"SELECT {', '.join(cols)} FROM {t} ORDER BY {order}",
              f"SELECT COUNT(*) FROM {t}") for t, (cols, order) in _TABLE_DEFS.items()}       # name -> (columns, page SQL, export SQL, count SQL)
TABLE_NOTES = {
    "users": "Registered accounts. Wallet balance is the current value; the history is in wallet_ledger.",
    "wallet_ledger": "Append-only wallet history: top-ups, holds, charges, releases, adjustments.",
    "payments": "Every payment attempt and its status.",
    "reports": "One row per upload. Borrower names, file names and statement contents are not shown.",
    "consents": "Which Terms, Privacy and borrower-consent versions each user accepted, and from where.",
    "audit_log": "Who did what: sign-ins, uploads, views, downloads, admin actions.",
}
CUSTOMER_ONE = ("SELECT id, name, email, mobile, org, role, created_at, balance_paise, held_paise, privileged, price_paise, mfa_enabled, mobile_verified, "
                "is_master, is_admin FROM users WHERE id=?")


# ----------------------------------------------------------------------------- analytics
def _today_ist():
    return (datetime.now(timezone.utc).replace(tzinfo=None) + IST).date()


def _day_labels(days: int):
    today = _today_ist()
    ds = [(today - timedelta(days=days - 1 - i)) for i in range(days)]
    return [d.isoformat() for d in ds], [d.strftime("%d %b").lstrip("0") for d in ds]


def _utc_start_of(day_iso: str) -> str:
    """The stored (UTC) timestamp at which an Indian calendar day begins."""
    return (datetime.fromisoformat(day_iso) - IST).strftime("%Y-%m-%d %H:%M:%S")


def _bucket(rows, keys: list[str], scale: float = 1.0) -> list[float]:
    """Add rows (stored UTC time, value) into Indian calendar days."""
    out = dict.fromkeys(keys, 0.0)
    for r in rows:
        try:
            day = (datetime.strptime(str(r["t"])[:19], "%Y-%m-%d %H:%M:%S") + IST).date().isoformat()
        except ValueError:
            continue
        if day in out:
            out[day] += (r["v"] or 0) * scale
    return [out[k] for k in keys]


def analytics(days: int, include_test: bool) -> dict:
    keys, labels = _day_labels(days)
    prm = {"test": 1 if include_test else 0, "since": _utc_start_of(keys[0]), "since7": _utc_start_of(keys[-7] if days >= 7 else keys[0])}
    with db() as c:
        one = lambda q: c.execute(Q[q], prm).fetchone()[0] or 0     # noqa: E731
        rows = lambda q: c.execute(Q[q], prm).fetchall()            # noqa: E731
        k = {"users_total": one("users_total"), "users_window": one("users_since"), "users_7d": one("users_since7"), "paying": one("paying"),
             "revenue_total": one("revenue"), "revenue_window": one("revenue_since")}
        st = {r["s"]: r["n"] for r in rows("pay_status")}
        k.update(pay_paid=st.get("paid", 0), pay_failed=st.get("failed", 0), pay_review=st.get("review", 0))
        w = c.execute(Q["wallet"], prm).fetchone()
        k["wallet_balance"], k["wallet_held"] = w["b"], w["h"]
        r = c.execute(Q["reports"], prm).fetchone()
        k.update(rep_total=r["up"], rep_done=r["done"], rep_failed=r["failed"], spent_total=r["spent"], downloads_total=r["dl"], dl_reports=r["dlr"])
        r = c.execute(Q["reports_since"], prm).fetchone()
        k.update(w_up=r["up"], w_done=r["done"], w_failed=r["failed"], w_spent=r["spent"], w_dl=r["dl"])
        funnel = [("Registered", k["users_total"]), ("Added money", k["paying"]), ("Uploaded a statement", one("f_uploaded")),
                  ("Got a report", one("f_done")), ("Downloaded the Excel", one("f_downloaded"))]
        out = dict(
            k=k, funnel=funnel, labels=labels,
            sign=_bucket(rows("d_signups"), keys), rev=_bucket(rows("d_revenue"), keys, 0.01), done=_bucket(rows("d_done"), keys),
            failed=_bucket(rows("d_failed"), keys), dls=_bucket(rows("d_downloads"), keys),
            banks=[(x["b"], x["n"], num(x["n"])) for x in rows("banks")], fails=[(x["e"], x["n"], num(x["n"])) for x in rows("fails")],
            packs=[(x["p"], x["n"], f"{num(x['n'])} · {fmt_paise(x['s'])}") for x in rows("packs")],
            roles=[(x["role"], x["n"], num(x["n"])) for x in rows("roles")],
            top=rows("top"), recent_users=rows("recent_users"), recent_pay=rows("recent_pay"))
    return out


@router.get("/admin")
def dashboard(request: Request, days: int = 30, test: int = 0):
    u = admin_user(request)
    if not u:
        return to_login()
    days = days if days in (7, 30, 90) else 30
    a = analytics(days, bool(test))
    k = a["k"]
    charts = {
        "sign": bars_svg(a["labels"], [("Sign-ups", a["sign"], "s1")], title="Sign-ups per day"),
        "rev": bars_svg(a["labels"], [("Revenue", a["rev"], "s1")], money=True, title="Revenue per day"),
        "rep": bars_svg(a["labels"], [("Generated", a["done"], "s1"), ("Failed", a["failed"], "s2")], title="Reports per day"),
        "dl": bars_svg(a["labels"], [("Downloads", a["dls"], "s1")], title="Downloads per day"),
        "banks": hbars(a["banks"]), "fails": hbars(a["fails"], "s2"), "packs": hbars(a["packs"]), "roles": hbars(a["roles"]),
        "funnel": hbars([(n, v, f"{num(v)} · {pct(v, a['funnel'][0][1])}") for n, v in a["funnel"]]),
    }
    return page(request, "console_dashboard.html", u, a=a, k=k, ch=charts, days=days, test=test, nav="dashboard",
                success=pct(k["w_done"], k["w_done"] + k["w_failed"]), now=datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"))


# ----------------------------------------------------------------------------- list pages
def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _paging(total: int, pg: int) -> dict:
    pages = max(1, math.ceil(total / PER_PAGE))
    pg = min(max(1, pg), pages)
    return {"page": pg, "pages": pages, "total": total, "offset": (pg - 1) * PER_PAGE}


@router.get("/admin/customers")
def customers(request: Request, q: str = "", sort: str = "new", page_no: int = 1, test: int = 0):
    u = admin_user(request)
    if not u:
        return to_login()
    q = q.strip()[:80]
    sort = sort if sort in CUSTOMER_PAGE else "new"
    args = {"test": 1 if test else 0, "q": q, "like": _like(q)}
    with db() as c:
        pg = _paging(c.execute(CUSTOMER_COUNT, args).fetchone()[0], page_no)
        rows = c.execute(CUSTOMER_PAGE[sort], {**args, "n": PER_PAGE, "o": pg["offset"]}).fetchall()
    return page(request, "console_customers.html", u, rows=rows, pg=pg, q=q, sort=sort, test=test, nav="customers")


@router.get("/admin/customers/{uid}")
def customer(request: Request, uid: int):
    u = admin_user(request)
    if not u:
        return to_login()
    with db() as c:
        cu = c.execute(CUSTOMER_ONE, (uid,)).fetchone()
        if not cu:
            return page(request, "console_message.html", u, code=404, title="No such customer", message="That customer does not exist.", nav="customers")
        stats = c.execute("SELECT COUNT(*) up, COALESCE(SUM(status='done'),0) done, COALESCE(SUM(status='failed'),0) failed, COALESCE(SUM(charged_paise),0) spent, "
                          "COALESCE(SUM(download_count),0) dl FROM reports WHERE user_id=?", (uid,)).fetchone()
        topped = c.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE user_id=? AND status='paid'", (uid,)).fetchone()[0]
        pays = c.execute("SELECT order_id, provider, pack, amount_paise, credit_paise, status, payment_id, coupon_code, created_at, paid_at FROM payments "
                         "WHERE user_id=? ORDER BY id DESC LIMIT 100", (uid,)).fetchall()
        reps = c.execute("SELECT id, status, error_code, bank, account_masked, period_from, period_to, txn_count, risk_band, charged_paise, download_count, "
                         "last_downloaded_at, created_at FROM reports WHERE user_id=? ORDER BY created_at DESC LIMIT 200", (uid,)).fetchall()
        ledger = c.execute("SELECT kind, amount_paise, balance_after_paise, ref, note, created_at FROM wallet_ledger WHERE user_id=? ORDER BY id DESC LIMIT 60", (uid,)).fetchall()
        consents = c.execute("SELECT kind, version, ip, accepted_at FROM consents WHERE user_id=? ORDER BY id DESC LIMIT 12", (uid,)).fetchall()
        acts = c.execute("SELECT event, ref, ip, created_at FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT 40", (uid,)).fetchall()
    audit(request, u["id"], "admin_view_customer", str(uid))
    return page(request, "console_customer.html", u, c=cu, stats=stats, topped=topped, pays=pays, reps=reps, ledger=ledger, consents=consents, acts=acts, nav="customers")


@router.get("/admin/transactions")
def transactions(request: Request, status: str = "", q: str = "", page_no: int = 1, test: int = 0):
    u = admin_user(request)
    if not u:
        return to_login()
    q = q.strip()[:80]
    status = status if status in ("paid", "created", "failed", "review") else ""
    args = {"test": 1 if test else 0, "status": status, "q": q, "like": _like(q)}
    with db() as c:
        pg = _paging(c.execute(TX_COUNT, args).fetchone()[0], page_no)
        rows = c.execute(TX_ROWS, {**args, "n": PER_PAGE, "o": pg["offset"]}).fetchall()
        sums = c.execute(TX_SUMS, args).fetchall()
    return page(request, "console_transactions.html", u, rows=rows, pg=pg, q=q, status=status, sums={r["status"]: r for r in sums}, test=test, nav="transactions")


@router.get("/admin/reports")
def reports(request: Request, status: str = "", q: str = "", sort: str = "new", page_no: int = 1, test: int = 0):
    u = admin_user(request)
    if not u:
        return to_login()
    q = q.strip()[:80]
    status = status if status in ("done", "failed", "processing") else ""
    sort = sort if sort in RP_ROWS else "new"
    args = {"test": 1 if test else 0, "status": status, "q": q, "like": _like(q)}
    with db() as c:
        pg = _paging(c.execute(RP_COUNT, args).fetchone()[0], page_no)
        rows = c.execute(RP_ROWS[sort], {**args, "n": PER_PAGE, "o": pg["offset"]}).fetchall()
        s = c.execute(RP_SUMMARY, args).fetchone()
    return page(request, "console_reports.html", u, rows=rows, pg=pg, q=q, status=status, sort=sort, s=s, test=test, nav="reports")


# ----------------------------------------------------------------------------- raw tables and CSV
@router.get("/admin/data")
def data_index(request: Request):
    u = admin_user(request)
    if not u:
        return to_login()
    with db() as c:
        counts = {t: c.execute(TABLES[t][3]).fetchone()[0] for t in TABLES}
    return page(request, "console_data.html", u, tables=TABLES, counts=counts, notes=TABLE_NOTES, table=None, nav="data")


@router.get("/admin/data/{table}")
def data_table(request: Request, table: str, page_no: int = 1):
    u = admin_user(request)
    if not u:
        return to_login()
    if table not in TABLES:
        return page(request, "console_message.html", u, code=404, title="No such table", message="That table is not available.", nav="data")
    cols, page_sql, _, count_sql = TABLES[table]
    with db() as c:
        pg = _paging(c.execute(count_sql).fetchone()[0], page_no)
        rows = c.execute(page_sql, {"n": PER_PAGE, "o": pg["offset"]}).fetchall()
    return page(request, "console_data.html", u, tables=TABLES, counts=None, notes=TABLE_NOTES, table=table, cols=cols, rows=rows, pg=pg, nav="data")


def _safe_cell(v):
    """A spreadsheet runs text that starts with = + - @ as a formula; prefix it so exported data stays data."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


CUSTOMER_CSV = (["id", "name", "email", "mobile", "org", "role", "joined_utc", "balance_paise", "held_paise", "is_master", "is_admin", "topped_up_paise",
                 "payments", "spent_paise", "uploaded", "generated", "failed", "downloads", "last_seen_utc"],
                ["id", "name", "email", "mobile", "org", "role", "created_at", "balance_paise", "held_paise", "is_master", "is_admin", "topped", "payments",
                 "spent", "uploaded", "generated", "failed", "downloads", "last_seen"])


@router.get("/admin/export/{name}.csv")
def export(request: Request, name: str, test: int = 0):
    u = admin_user(request)
    if not u:
        return to_login()
    with db() as c:
        if name == "customers":
            cur = c.execute(CUSTOMER_EXPORT, {"test": 1 if test else 0, "q": "", "like": "%"})
            head, fields = CUSTOMER_CSV
        elif name in TABLES:
            cur = c.execute(TABLES[name][2])
            head = fields = TABLES[name][0]
        else:
            return page(request, "console_message.html", u, code=404, title="No such export", message="That export does not exist.", nav="data")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(head)
        for r in cur:
            w.writerow([_safe_cell(r[f]) for f in fields])
    audit(request, u["id"], "admin_export", name)
    resp = Response(buf.getvalue(), media_type="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{config.BRAND.lower()}_{name}_{_today_ist().isoformat()}.csv"'
    return finish(resp, u)
