"""One-time codes for sign-up and sign-in. The code goes to the user's email address (the otps.mobile column holds the recipient key, which is the email).

Providers:
  dev    development only: the code is shown on the verify page and written to the server log. Refused in production.
  email  sends the code by SMTP (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM). Works with Zoho, Brevo, Amazon SES, Gmail app passwords.
  msg91  sends the code by SMS through MSG91's OTP API. India requires a DLT-registered template.
A code is 6 digits, valid for OTP_TTL_SECONDS, usable once, and locked after OTP_MAX_ATTEMPTS wrong tries."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time

import smtplib
from email.message import EmailMessage

import httpx

from . import config
from .db import db, tx

log = logging.getLogger("banksview.otp")
LAST: dict[str, str] = {}      # dev provider only: latest code per mobile, so tests and the verify page can read it

MOBILE_RX = re.compile(r"^[6-9]\d{9}$")


class OtpError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message, self.status = message, status


def clean_mobile(raw: str) -> str | None:
    """Return a 10-digit Indian mobile number, or None. Accepts +91, 91 and 0 prefixes."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if MOBILE_RX.match(d) else None


def mask(mobile: str) -> str:
    return f"+91 {mobile[:2]}XXXXX{mobile[-3:]}"


def _hash(otp_id: int, code: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"{otp_id}:{code}".encode(), hashlib.sha256).hexdigest()


def mask_email(addr: str) -> str:
    local, _, domain = (addr or "").partition("@")
    return f"{local[:1]}{'*' * max(2, min(5, len(local) - 1))}@{domain}"


def _send_email(addr: str, code: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your {config.BRAND} sign-in code"
    msg["From"] = config.SMTP_FROM
    msg["To"] = addr
    mins = max(1, config.OTP_TTL_SECONDS // 60)
    msg.set_content(f"Your {config.BRAND} code is {code}.\n\nIt is valid for {mins} minutes and works once. If you did not ask for it, ignore this email; nobody can sign in without the code.\n")
    msg.add_alternative(f"""<div style="font-family:Arial,sans-serif;max-width:420px;margin:auto;padding:24px;color:#0B1220">
<p style="font-size:15px">Your {config.BRAND} sign-in code</p>
<p style="font-size:34px;font-weight:600;letter-spacing:8px;margin:12px 0;color:#1C78FF">{code}</p>
<p style="font-size:13px;color:#5B6677">Valid for {mins} minutes and works once. If you did not ask for it, ignore this email.</p></div>""", subtype="html")
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as srv:
            if config.SMTP_PORT != 25:
                srv.starttls()
            if config.SMTP_USER:
                srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
            srv.send_message(msg)
    except Exception as e:
        log.warning("OTP email failed: %s", e)
        raise OtpError("We could not send the email right now. Please try again in a minute.", 502) from e


def _send_sms(mobile: str, code: str) -> None:
    if config.OTP_PROVIDER == "email":
        _send_email(mobile, code)
    elif config.OTP_PROVIDER == "msg91":
        try:
            r = httpx.post("https://control.msg91.com/api/v5/otp",
                           params={"template_id": config.MSG91_TEMPLATE_ID, "mobile": f"91{mobile}", "authkey": config.MSG91_AUTH_KEY, "otp": code},
                           timeout=15)
            r.raise_for_status()
        except Exception as e:
            raise OtpError("We could not send the SMS right now. Please try again in a minute.", 502) from e
    else:
        LAST[mobile] = code
        log.warning("DEV OTP for %s is %s", mobile, code)


def issue(purpose: str, mobile: str, email: str | None = None, payload: dict | None = None) -> int:
    now = int(time.time())
    with db() as conn:
        recent = conn.execute("SELECT created_at FROM otps WHERE mobile=? AND created_at>? ORDER BY created_at DESC", (mobile, now - 3600)).fetchall()
    if recent and now - recent[0]["created_at"] < config.OTP_RESEND_SECONDS:
        raise OtpError(f"Please wait {config.OTP_RESEND_SECONDS - (now - recent[0]['created_at'])} seconds before asking for another code.", 429)
    if len(recent) >= config.OTP_SENDS_PER_HOUR:
        raise OtpError("Too many codes were requested for this number. Please try again in an hour.", 429)
    code = f"{secrets.randbelow(10**6):06d}"
    with tx() as conn:
        cur = conn.execute("INSERT INTO otps(purpose, mobile, email, payload, code_hash, expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
                           (purpose, mobile, email, json.dumps(payload) if payload else None, "", now + config.OTP_TTL_SECONDS, now))
        otp_id = cur.lastrowid
        conn.execute("UPDATE otps SET code_hash=? WHERE id=?", (_hash(otp_id, code), otp_id))
    _send_sms(mobile, code)
    return otp_id


def get(otp_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM otps WHERE id=?", (otp_id,)).fetchone()


def verify(otp_id: int, code: str) -> dict:
    """Check a code. Returns {'purpose', 'mobile', 'email', 'payload'} and burns the code, or raises OtpError."""
    code = re.sub(r"\D", "", code or "")
    with tx() as conn:
        o = conn.execute("SELECT * FROM otps WHERE id=?", (otp_id,)).fetchone()
        if not o or o["consumed"]:
            raise OtpError("This code has already been used. Please start again.", 410)
        if o["expires_at"] < int(time.time()):
            raise OtpError("This code has expired. Ask for a new one.", 410)
        if o["attempts"] >= config.OTP_MAX_ATTEMPTS:
            raise OtpError("Too many wrong attempts. Please start again.", 429)
        ok = len(code) == 6 and hmac.compare_digest(_hash(otp_id, code), o["code_hash"])
        if not ok:
            conn.execute("UPDATE otps SET attempts=attempts+1 WHERE id=?", (otp_id,))
        else:
            conn.execute("UPDATE otps SET consumed=1 WHERE id=?", (otp_id,))
    if not ok:
        raise OtpError("That code is not correct.", 401)
    return {"purpose": o["purpose"], "mobile": o["mobile"], "email": o["email"], "payload": json.loads(o["payload"]) if o["payload"] else None}
