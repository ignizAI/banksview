"""Password hashing, signed session cookies, CSRF tokens, simple login throttling."""
from __future__ import annotations
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from collections import defaultdict, deque

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

_ITER = 240_000
_ser = URLSafeTimedSerializer(config.SECRET_KEY, salt="session-v1")
_csrf_ser = URLSafeTimedSerializer(config.SECRET_KEY, salt="csrf-v1")
_mfa_ser = URLSafeTimedSerializer(config.SECRET_KEY, salt="mfa-v1")
_otp_ser = URLSafeTimedSerializer(config.SECRET_KEY, salt="otp-v1")
_admin_setup_ser = URLSafeTimedSerializer(config.SECRET_KEY, salt="admin-mfa-setup-v1")     # proves the password was just checked; only good for enrolling
_admin_ser = URLSafeTimedSerializer(config.SECRET_KEY, salt="admin-session-v1")      # a different salt: a customer cookie can never be read as an admin one


try:  # Argon2id is the preferred password hash; PBKDF2 stays only as a fallback and for old hashes
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    _argon = PasswordHasher()   # argon2id, library defaults
except ImportError:  # pragma: no cover
    _argon = None


def _pbkdf2(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2${_ITER}${salt.hex()}${dk.hex()}"


def hash_password(password: str) -> str:
    return _argon.hash(password) if _argon else _pbkdf2(password)


def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith("$argon2"):
            return bool(_argon) and _argon.verify(stored, password)
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    """True for PBKDF2 hashes (or outdated Argon2 parameters) so they can be upgraded at the next sign-in."""
    if not _argon:
        return False
    return not stored.startswith("$argon2") or _argon.check_needs_rehash(stored)


# ---- two-factor sign-in: TOTP (RFC 6238, 30 s, 6 digits), compatible with any authenticator app
def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def verify_totp(secret: str, code: str, window: int = 1, now: float | None = None) -> bool:
    code = re.sub(r"\D", "", code or "")
    if len(code) != 6 or not secret:
        return False
    c = int((now if now is not None else time.time()) // 30)
    return any(hmac.compare_digest(_totp_at(secret, c + d), code) for d in range(-window, window + 1))


def totp_uri(secret: str, account: str, issuer: str) -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


def make_session(user_id: int, epoch: int = 0, now: float | None = None) -> str:
    """Signed cookie carrying the user, the account's session epoch (bumped on sign-out, which kills every older cookie)
    and the last-activity time (checked against the idle limit)."""
    return _ser.dumps({"uid": user_id, "e": int(epoch), "ls": int(now if now is not None else time.time()), "n": secrets.token_hex(4)})


def read_session(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        data = _ser.loads(token, max_age=config.SESSION_DAYS * 86400)
        return {"uid": int(data["uid"]), "e": int(data.get("e", 0)), "ls": int(data.get("ls", 0))}
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def make_admin_session(user_id: int, epoch: int = 0, now: float | None = None) -> str:
    return _admin_ser.dumps({"uid": user_id, "e": int(epoch), "ls": int(now if now is not None else time.time()), "n": secrets.token_hex(4)})


def read_admin_session(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        data = _admin_ser.loads(token, max_age=12 * 3600)       # at most 12 hours, whatever the activity
        return {"uid": int(data["uid"]), "e": int(data.get("e", 0)), "ls": int(data.get("ls", 0))}
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def make_admin_setup_token(user_id: int) -> str:
    return _admin_setup_ser.dumps({"uid": int(user_id)})


def read_admin_setup_token(token: str | None) -> int | None:
    if not token:
        return None
    try:
        return int(_admin_setup_ser.loads(token, max_age=600)["uid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def make_mfa_token(user_id: int) -> str:
    """Short-lived proof that the password step passed; exchanged for a session after the one-time code."""
    return _mfa_ser.dumps({"uid": user_id})


def read_mfa_token(token: str | None) -> int | None:
    if not token:
        return None
    try:
        return int(_mfa_ser.loads(token, max_age=600)["uid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError):
        return None


def make_otp_token(otp_id: int) -> str:
    return _otp_ser.dumps({"o": otp_id})


def read_otp_token(token: str | None) -> int | None:
    if not token:
        return None
    try:
        return int(_otp_ser.loads(token, max_age=1800)["o"])
    except (BadSignature, SignatureExpired, KeyError, ValueError):
        return None


def make_csrf(scope: str) -> str:
    """scope = 'anon' for logged-out forms, or the user id for logged-in forms."""
    return _csrf_ser.dumps({"s": str(scope)})


def check_csrf(token: str | None, scope: str) -> bool:
    if not token:
        return False
    try:
        data = _csrf_ser.loads(token, max_age=2 * 3600)
        return data.get("s") == str(scope)
    except (BadSignature, SignatureExpired):
        return False


class Throttle:
    """In-memory sliding window; good enough for one process. Use Redis if you scale out."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        q = self.hits[key]
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def reset(self, key: str) -> None:
        self.hits.pop(key, None)


login_throttle = Throttle(limit=10, window_s=600)
topup_throttle = Throttle(limit=15, window_s=3600)       # new payment orders per user per hour
utr_throttle = Throttle(limit=10, window_s=3600)         # reference-number submissions per user per hour
signup_throttle = Throttle(limit=10, window_s=3600)
admin_throttle = Throttle(limit=5, window_s=900)           # admin sign-in attempts per address and email in 15 minutes
