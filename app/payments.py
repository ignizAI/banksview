"""Wallet top-ups. Two modes:
  mock      development only: a fake checkout page that credits the wallet (refused in production)
  razorpay  creates an order, verifies the checkout signature, and also accepts the webhook.
Crediting is idempotent: an order can be settled once however many times it is confirmed."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

import httpx

from . import config, coupons
from .db import db, tx
from .wallet import _log, get_balance


class PaymentError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


def create_order(user_id: int, credit_paise: int, coupon: str | None = None, pack: str | None = None) -> dict:
    """credit_paise is what the wallet receives. With a coupon the customer pays less than that."""
    amount_paise, quote = credit_paise, None
    if coupon:
        with db() as conn:
            coupons.release_stale(conn)
            try:
                quote = coupons.quote(conn, coupon, user_id, credit_paise)
            except coupons.CouponError as e:
                raise PaymentError(str(e)) from e
        amount_paise = quote["pay_paise"]
    if config.PAYMENT_MODE == "razorpay":
        try:
            resp = httpx.post(
                "https://api.razorpay.com/v1/orders",
                auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET),
                json={"amount": amount_paise, "currency": "INR", "receipt": f"u{user_id}-{secrets.token_hex(4)}",
                      "notes": {"user_id": str(user_id)}},
                timeout=20,
            )
            resp.raise_for_status()
            order_id = resp.json()["id"]
        except Exception as e:  # network, auth or API error
            raise PaymentError("The payment gateway is not reachable right now. Please try again in a minute.") from e
        provider = "razorpay"
    elif config.PAYMENT_MODE == "upi_qr":
        if not config.UPI_ID:
            raise PaymentError("Payments are not set up yet. Please contact support.")
        order_id, provider = "upi_" + secrets.token_hex(6), "upi"
    else:
        order_id, provider = "mock_" + secrets.token_hex(8), "mock"
    with tx() as conn:
        if quote:   # re-check inside the write lock so two people cannot take the last redemption together
            try:
                quote = coupons.quote(conn, coupon, user_id, credit_paise)
            except coupons.CouponError as e:
                raise PaymentError(str(e)) from e
            if quote["pay_paise"] != amount_paise:
                raise PaymentError("The coupon changed while you were paying. Please try again.")
            coupons.reserve(conn, quote["code"], user_id, order_id, quote["discount_paise"])
        if pack:
            from . import wallet
            pk = wallet.get_pack(pack)
            if not pk or pk["paise"] != credit_paise:
                raise PaymentError("That payment option does not exist.", "pack")
            if pk["first_only"] and not wallet.starter_available(conn, user_id):
                raise PaymentError("The first-report offer has already been used.", "pack")
        conn.execute("INSERT INTO payments(user_id, provider, order_id, amount_paise, status, credit_paise, coupon_code, pack) VALUES (?,?,?,?, 'created', ?, ?, ?)",
                     (user_id, provider, order_id, amount_paise, credit_paise, quote["code"] if quote else None, pack))
    return {"order_id": order_id, "provider": provider, "amount_paise": amount_paise, "credit_paise": credit_paise}


def get_payment(order_id: str):
    from .db import db
    with db() as conn:
        return conn.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()


def settle(order_id: str, payment_id: str | None, paid_amount_paise: int | None = None) -> bool:
    """Credit the wallet once for a paid order. Returns True if this call did the credit."""
    with tx() as conn:
        p = conn.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
        if not p:
            raise PaymentError("Unknown order.")
        if p["status"] == "paid":
            return False
        if paid_amount_paise is not None and paid_amount_paise != p["amount_paise"]:
            raise PaymentError("Paid amount does not match the order.")
        if payment_id and conn.execute("SELECT 1 FROM payments WHERE payment_id=? AND order_id<>? AND status='paid'", (payment_id, order_id)).fetchone():
            raise PaymentError("This payment has already been used for another order.")
        conn.execute("UPDATE payments SET status='paid', payment_id=?, paid_at=datetime('now') WHERE order_id=?", (payment_id, order_id))
        credit = p["credit_paise"] or p["amount_paise"]
        conn.execute("UPDATE users SET balance_paise = balance_paise + ? WHERE id=?", (credit, p["user_id"]))
        _log(conn, p["user_id"], "topup", credit, get_balance(conn, p["user_id"]), order_id,
             f"Wallet top-up (coupon {p['coupon_code']})" if p["coupon_code"] else "Wallet top-up")
        if p["coupon_code"]:
            coupons.mark_redeemed(conn, order_id)
        return True


def upi_uri(order_id: str, amount_paise: int) -> str:
    from urllib.parse import quote as q
    return (f"upi://pay?pa={q(config.UPI_ID)}&pn={q(config.UPI_PAYEE_NAME or config.BRAND)}"
            f"&am={amount_paise / 100:.2f}&cu=INR&tn={q('BanksView ' + order_id)}&tr={q(order_id)}")


def upi_qr_svg(order_id: str, amount_paise: int) -> str:
    import io
    import segno
    buf = io.BytesIO()
    segno.make(upi_uri(order_id, amount_paise), error="m").save(buf, kind="svg", scale=5, border=2, xmldecl=False, svgns=True, nl=False)
    return buf.getvalue().decode()


def submit_utr(order_id: str, user_id: int, utr: str) -> None:
    """Customer reports the UPI reference (UTR, 12 digits). The order waits for a master to confirm it in the bank app."""
    utr = (utr or "").strip()
    if not (utr.isdigit() and len(utr) == 12):
        raise PaymentError("Enter the 12-digit UPI reference number (UTR) from your payment app.", "utr")
    with tx() as conn:
        p = conn.execute("SELECT * FROM payments WHERE order_id=? AND user_id=? AND provider='upi'", (order_id, user_id)).fetchone()
        if not p:
            raise PaymentError("Unknown order.")
        if p["status"] == "paid":
            return
        if conn.execute("SELECT 1 FROM payments WHERE payment_id=? AND order_id<>?", (utr, order_id)).fetchone():
            raise PaymentError("That reference number has already been submitted.", "dup")
        conn.execute("UPDATE payments SET status='review', payment_id=? WHERE order_id=?", (utr, order_id))


def pending_review() -> list:
    with db() as conn:
        return conn.execute("SELECT p.*, u.email, u.name FROM payments p JOIN users u ON u.id=p.user_id WHERE p.provider='upi' AND p.status='review' ORDER BY p.id").fetchall()


def reject(order_id: str) -> None:
    with tx() as conn:
        p = conn.execute("SELECT status FROM payments WHERE order_id=? AND provider='upi'", (order_id,)).fetchone()
        if not p or p["status"] != "review":
            raise PaymentError("Nothing to reject.")
        conn.execute("UPDATE payments SET status='failed' WHERE order_id=?", (order_id,))
        conn.execute("DELETE FROM coupon_redemptions WHERE order_id=? AND status='reserved'", (order_id,))


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    expected = hmac.new(config.RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def confirm_with_gateway(payment_id: str, order_id: str, amount_paise: int) -> None:
    """Ask Razorpay directly (server to server) whether this payment really was captured for this order and amount.
    The browser's say-so is never enough: a checkout callback is only accepted after this call agrees."""
    try:
        r = httpx.get(f"https://api.razorpay.com/v1/payments/{payment_id}", auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET), timeout=20)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        raise PaymentError("Could not confirm the payment with the gateway.", "gateway") from e
    if d.get("order_id") != order_id or int(d.get("amount", -1)) != amount_paise or d.get("currency") != "INR" or d.get("status") != "captured":
        raise PaymentError("The gateway does not show a matching captured payment.", "mismatch")


def verify_webhook(body: bytes, signature: str) -> bool:
    if not config.RAZORPAY_WEBHOOK_SECRET:
        return False
    expected = hmac.new(config.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def handle_webhook(body: bytes) -> None:
    ev = json.loads(body)
    if ev.get("event") not in ("payment.captured", "order.paid"):
        return
    ent = ev["payload"]["payment"]["entity"]
    settle(ent["order_id"], ent["id"], int(ent["amount"]))
