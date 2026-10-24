"""Discount coupons. Only a master account can create them. A coupon takes a percentage off a wallet top-up:
the customer pays less and the wallet is credited with the full amount. One redemption per customer per code."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from . import config
from .db import db, tx

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # no 0/O/1/I
RESERVATION_MINUTES = 30                            # an unpaid order holds its coupon this long


class CouponError(ValueError):
    pass


def normalise(code: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", (code or "").upper())[:24]


def create(created_by: int, code: str | None = None, percent: int | None = None, max_redemptions: int | None = None,
           days_valid: int | None = None, note: str = "") -> str:
    percent = config.COUPON_PERCENT if percent is None else int(percent)
    if not 1 <= percent <= 100:
        raise CouponError("Discount must be between 1 and 100 percent.")
    code = normalise(code) if code else "BV" + "".join(secrets.choice(_ALPHABET) for _ in range(6))
    if len(code) < 4:
        raise CouponError("A code needs at least 4 letters or digits.")
    expires = (datetime.now(timezone.utc) + timedelta(days=int(days_valid))).strftime("%Y-%m-%d %H:%M:%S") if days_valid else None
    try:
        with tx() as conn:
            conn.execute("INSERT INTO coupons(code, percent, max_redemptions, expires_at, note, created_by) VALUES (?,?,?,?,?,?)",
                         (code, percent, max_redemptions or None, expires, note.strip()[:80], created_by))
    except Exception as e:
        if "UNIQUE" in str(e) or "PRIMARY" in str(e):
            raise CouponError("That code already exists.") from e
        raise
    return code


def release_stale(conn) -> None:
    """Give back coupons held by orders that were never paid."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=RESERVATION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM coupon_redemptions WHERE status='reserved' AND created_at<?", (cutoff,))


def quote(conn, code: str, user_id: int, credit_paise: int) -> dict:
    """Return {'code', 'percent', 'discount_paise', 'pay_paise'} or raise CouponError with a user-facing message."""
    code = normalise(code)
    c = conn.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()
    if not c or not c["active"]:
        raise CouponError("This coupon code is not valid.")
    if c["expires_at"] and c["expires_at"] < datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"):
        raise CouponError("This coupon has expired.")
    mine = conn.execute("SELECT status FROM coupon_redemptions WHERE code=? AND user_id=?", (code, user_id)).fetchall()
    if any(r["status"] == "redeemed" for r in mine):
        raise CouponError("You have already used this coupon.")
    if c["max_redemptions"] is not None:
        used = conn.execute("SELECT COUNT(*) n FROM coupon_redemptions WHERE code=? AND NOT (user_id=? AND status='reserved')", (code, user_id)).fetchone()["n"]
        if used >= c["max_redemptions"]:
            raise CouponError("This coupon has been fully used.")
    discount = int(round(credit_paise * c["percent"] / 100))
    return {"code": code, "percent": c["percent"], "discount_paise": discount, "pay_paise": credit_paise - discount}


def reserve(conn, code: str, user_id: int, order_id: str, discount_paise: int) -> None:
    conn.execute("DELETE FROM coupon_redemptions WHERE code=? AND user_id=? AND status='reserved'", (code, user_id))
    conn.execute("INSERT INTO coupon_redemptions(code, user_id, order_id, status, discount_paise) VALUES (?,?,?,'reserved',?)",
                 (code, user_id, order_id, discount_paise))


def mark_redeemed(conn, order_id: str) -> None:
    conn.execute("UPDATE coupon_redemptions SET status='redeemed' WHERE order_id=?", (order_id,))


def listing():
    with db() as conn:
        return conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM coupon_redemptions r WHERE r.code=c.code AND r.status='redeemed') AS redeemed "
            "FROM coupons c ORDER BY c.created_at DESC, c.rowid DESC").fetchall()


def set_active(code: str, active: bool) -> None:
    with tx() as conn:
        conn.execute("UPDATE coupons SET active=? WHERE code=?", (1 if active else 0, normalise(code)))
