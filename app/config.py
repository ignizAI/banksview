"""Central configuration. Everything is read from environment variables so the same
code runs on a laptop, a VPS or a container. See .env.example."""
from __future__ import annotations
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


APP_ENV = os.environ.get("APP_ENV", "development")          # development | production
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
BRAND = os.environ.get("BRAND_NAME", "BanksView")

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "app.db"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", DATA_DIR / "reports"))
UPLOAD_TMP_DIR = Path(os.environ.get("UPLOAD_TMP_DIR", DATA_DIR / "tmp"))

# --- Pricing (all money is stored in paise, integers only) ---
PRIVILEGED_PRICE_PAISE = _int("PRIVILEGED_PRICE_PAISE", 2500)   # default Rs 25 for privileged customers (set by a master account)
PRICE_PAISE = _int("PRICE_PAISE", 5000)                    # Rs 50 per generated report
MIN_TOPUP_PAISE = _int("MIN_TOPUP_PAISE", 5000)             # smallest payment (wallet money is not refundable)
# The only payment options a customer can choose. No free-text amount exists anywhere in the product.
PACKS = (
    {"id": "starter", "name": "First report", "paise": 5000, "first_only": True},     # once, and only before the first report is generated
    {"id": "p250", "name": "Starter pack", "paise": 25000, "first_only": False},
    {"id": "p1000", "name": "Growth pack", "paise": 100000, "first_only": False},
    {"id": "p5000", "name": "Business pack", "paise": 500000, "first_only": False},
)
MAX_TOPUP_PAISE = _int("MAX_TOPUP_PAISE", 10000000)        # Rs 1,00,000 per top-up

# --- Upload limits ---
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 15)
MAX_PDF_PAGES = _int("MAX_PDF_PAGES", 150)
KEEP_UPLOADED_PDF = os.environ.get("KEEP_UPLOADED_PDF", "0") == "1"   # default: delete after parsing
DUPLICATE_WINDOW_DAYS = _int("DUPLICATE_WINDOW_DAYS", 30)
REPORT_RETENTION_DAYS = _int("REPORT_RETENTION_DAYS", 90)

# --- Sessions ---
BACKUP_DAILY = os.environ.get("BACKUP_DAILY", "0") == "1"           # in-app daily database backup
BACKUP_KEEP = _int("BACKUP_KEEP", 14)
SESSION_DAYS = _int("SESSION_DAYS", 7)
SESSION_IDLE_MINUTES = _int("SESSION_IDLE_MINUTES", 30)     # an unattended screen signs itself out
ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if h.strip()]
ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "1") == "1"

# --- Legal and policy ---
TERMS_VERSION = "2026-10-24"                 # bump when Terms, Privacy Policy or the borrower-consent undertaking change
COMPANY_NAME = os.environ.get("COMPANY_NAME", "IgnizAi")
COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "")
GRIEVANCE_OFFICER = os.environ.get("GRIEVANCE_OFFICER", "")
GRIEVANCE_EMAIL = os.environ.get("GRIEVANCE_EMAIL", "")
JURISDICTION_CITY = os.environ.get("JURISDICTION_CITY", "Mumbai")       # courts with jurisdiction under the Terms
LIABILITY_CAP_MONTHS = _int("LIABILITY_CAP_MONTHS", 3)   # liability cap = fees paid in this many months
# Two-factor sign-in (TOTP). On by default in production, optional while developing.
MFA_REQUIRED = os.environ.get("MFA_REQUIRED", "1" if os.environ.get("APP_ENV") == "production" else "0") == "1"

# --- Master (test) accounts: use every feature with no wallet or charges ---
# Created only from the command line (python -m app.cli master EMAIL PASSWORD). Disabled in production unless ALLOW_MASTER=1.
MASTER_ENABLED = APP_ENV != "production" or os.environ.get("ALLOW_MASTER", "0") == "1"

# --- Coupons: created only by the master account; they discount a wallet top-up ---
COUPONS_ENABLED = os.environ.get("COUPONS_ENABLED", "0") == "1"   # coupons are switched off for now; set COUPONS_ENABLED=1 to bring them back
COUPON_PERCENT = _int("COUPON_PERCENT", 20)

# --- Mobile OTP sign-in ---
OTP_PROVIDER = os.environ.get("OTP_PROVIDER", "dev")      # dev (code shown on screen and in the log) | email | msg91
OTP_TTL_SECONDS = _int("OTP_TTL_SECONDS", 300)
OTP_MAX_ATTEMPTS = _int("OTP_MAX_ATTEMPTS", 5)
OTP_RESEND_SECONDS = _int("OTP_RESEND_SECONDS", 30)
OTP_SENDS_PER_HOUR = _int("OTP_SENDS_PER_HOUR", 5)
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
MSG91_AUTH_KEY = os.environ.get("MSG91_AUTH_KEY", "")
MSG91_TEMPLATE_ID = os.environ.get("MSG91_TEMPLATE_ID", "")     # DLT-registered OTP template

# --- Payments ---
PAYMENT_MODE = os.environ.get("PAYMENT_MODE", "mock")      # mock | razorpay | upi_qr
UPI_ID = os.environ.get("UPI_ID", "")                       # payee UPI ID (VPA) for PAYMENT_MODE=upi_qr; blank until supplied
UPI_PAYEE_NAME = os.environ.get("UPI_PAYEE_NAME", "")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# --- Analysis thresholds (tunable) ---
ROUND_TRIP_MIN_AMOUNT = _int("ROUND_TRIP_MIN_AMOUNT", 100000)       # rupees
ROUND_TRIP_OUT_RATIO = float(os.environ.get("ROUND_TRIP_OUT_RATIO", 0.8))
ROUND_TRIP_WINDOW_HOURS = _int("ROUND_TRIP_WINDOW_HOURS", 48)
RECON_FAIL_RATIO = float(os.environ.get("RECON_FAIL_RATIO", 0.05))  # reject file if > 5% rows break the balance rule
MIN_TRANSACTIONS = _int("MIN_TRANSACTIONS", 5)


def check_production_safety() -> None:
    """Refuse to boot in production with unsafe defaults."""
    if APP_ENV == "production":
        if SECRET_KEY == "dev-only-change-me":
            raise RuntimeError("Set SECRET_KEY before running in production.")
        if PAYMENT_MODE == "mock":
            raise RuntimeError("PAYMENT_MODE=mock credits wallets without real money; use razorpay in production.")
        if OTP_PROVIDER == "dev":
            raise RuntimeError("OTP_PROVIDER=dev shows the code on screen; configure a real SMS provider (OTP_PROVIDER=msg91) in production.")
        if OTP_PROVIDER == "email" and not (SMTP_HOST and SMTP_FROM):
            raise RuntimeError("SMTP_HOST and SMTP_FROM are required for email OTP.")
        if OTP_PROVIDER == "msg91" and not (MSG91_AUTH_KEY and MSG91_TEMPLATE_ID):
            raise RuntimeError("MSG91_AUTH_KEY and MSG91_TEMPLATE_ID are required for SMS OTP.")
        if not [h for h in ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1", "testserver")]:
            raise RuntimeError("Set ALLOWED_HOSTS to your real domain(s), for example banksview.com,www.banksview.com.")
        if PAYMENT_MODE == "upi_qr" and not UPI_ID:
            raise RuntimeError("UPI_ID is required for PAYMENT_MODE=upi_qr.")
        if PAYMENT_MODE == "razorpay" and not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
            raise RuntimeError("Razorpay keys are missing.")


def ensure_dirs() -> None:
    for p in (DATA_DIR, REPORT_DIR, UPLOAD_TMP_DIR):
        p.mkdir(parents=True, exist_ok=True)


def rupees(paise: int) -> str:
    """Format paise as Indian-grouped rupees, e.g. 12345600 -> 'Rs 1,23,456'."""
    neg = paise < 0
    paise = abs(int(paise))
    r, p = divmod(paise, 100)
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    out = f"Rs {s}" + (f".{p:02d}" if p else "")
    return "-" + out if neg else out
