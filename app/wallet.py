"""Wallet with hold / commit / release so a user is charged only when a report is actually
produced, and two simultaneous uploads can never overspend the balance.

Flow for one report:
    hold(price)    -> balance -= price, held += price     (before processing starts)
    commit(price)  -> held -= price                        (report produced: money is spent)
    release(price) -> held -= price, balance += price      (processing failed: money returned)
"""
from __future__ import annotations
import hashlib
import hmac
import re

from . import config
from .db import db, tx


class InsufficientBalance(Exception):
    def __init__(self, balance_paise: int, needed_paise: int):
        self.balance_paise, self.needed_paise = balance_paise, needed_paise
        super().__init__("insufficient balance")


class TopupError(ValueError):
    pass


def get_pack(pack_id: str):
    for p in config.PACKS:
        if p["id"] == pack_id:
            return p
    return None


def starter_available(conn, user_id: int) -> bool:
    """The first-report offer is for a customer who has not generated a report yet, and can be bought once."""
    if conn.execute("SELECT 1 FROM reports WHERE user_id=? AND status='done' LIMIT 1", (user_id,)).fetchone():
        return False
    return not conn.execute("SELECT 1 FROM payments WHERE user_id=? AND pack='starter' AND status IN ('paid','review') LIMIT 1", (user_id,)).fetchone()


def packs_for(conn, user_id: int, price_paise: int) -> list[dict]:
    """The options to show this customer, each with the number of reports it buys at their rate and whether it is available."""
    out = []
    for p in config.PACKS:
        ok = starter_available(conn, user_id) if p["first_only"] else True
        out.append({**p, "reports": p["paise"] // price_paise, "available": ok})
    return out


def validate_topup(amount_paise: int) -> None:
    if amount_paise < config.MIN_TOPUP_PAISE:
        raise TopupError(f"Minimum top-up is {config.rupees(config.MIN_TOPUP_PAISE)}.")
    if amount_paise > config.MAX_TOPUP_PAISE:
        raise TopupError(f"Maximum top-up is {config.rupees(config.MAX_TOPUP_PAISE)} per payment.")


def _row_hash(prev: str, user_id, kind, amount, balance_after, ref, note) -> str:
    msg = "|".join(str(x) for x in (prev, user_id, kind, amount, balance_after, ref or "", note or ""))
    return hmac.new(config.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _log(conn, user_id, kind, amount, balance_after, ref=None, note=None):
    """Append one history row. Rows are chained per user with an HMAC, and the table rejects UPDATE and DELETE,
    so history cannot be edited quietly."""
    if kind not in ("topup", "hold", "charge", "release", "adjust") or int(amount) <= 0:
        raise ValueError("invalid wallet entry")
    last = conn.execute("SELECT row_hash FROM wallet_ledger WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    prev = (last["row_hash"] if last and last["row_hash"] else "")
    conn.execute(
        "INSERT INTO wallet_ledger(user_id, kind, amount_paise, balance_after_paise, ref, note, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, kind, amount, balance_after, ref, note, prev, _row_hash(prev, user_id, kind, amount, balance_after, ref, note)),
    )


_RUPEES = re.compile(r"^[0-9]{1,7}(\.[0-9]{1,2})?$")


def parse_rupees(raw: str) -> int:
    """Strict rupee amount -> paise. Digits with up to two decimals only: no signs, exponents, NaN, inf or spaces inside."""
    txt = (raw or "").replace(",", "").strip()
    if not _RUPEES.match(txt):
        raise TopupError("Enter the amount in rupees, for example 500.")
    whole, _, frac = txt.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def verify_ledger(user_id: int | None = None) -> list[str]:
    """Return a list of problems (empty = clean): broken hash chain, wrong running balance, or a stored balance that
    does not match the history (balance + held must equal everything credited minus everything charged)."""
    problems: list[str] = []
    with db() as conn:
        if user_id:
            users = conn.execute("SELECT id, email, balance_paise, held_paise FROM users WHERE id=?", (user_id,)).fetchall()
        else:
            users = conn.execute("SELECT id, email, balance_paise, held_paise FROM users").fetchall()
        for u in users:
            rows = conn.execute("SELECT * FROM wallet_ledger WHERE user_id=? ORDER BY id", (u["id"],)).fetchall()
            prev, bal, held, started = "", 0, 0, False
            credits = charges = 0
            for r in rows:
                if r["row_hash"]:
                    started = True
                    if r["prev_hash"] != prev or r["row_hash"] != _row_hash(prev, r["user_id"], r["kind"], r["amount_paise"], r["balance_after_paise"], r["ref"], r["note"]):
                        problems.append(f"{u['email']}: history entry #{r['id']} fails its integrity check")
                    prev = r["row_hash"]
                elif started:
                    problems.append(f"{u['email']}: history entry #{r['id']} is unsigned")
                k, a = r["kind"], r["amount_paise"]
                if k in ("topup", "adjust"):
                    bal, credits = bal + a, credits + a
                elif k == "hold":
                    bal, held = bal - a, held + a
                elif k == "release":
                    bal, held = bal + a, held - a
                elif k == "charge":
                    held, charges = held - a, charges + a
                if r["balance_after_paise"] != bal:
                    problems.append(f"{u['email']}: running balance wrong at entry #{r['id']}")
            if rows and (bal != u["balance_paise"] or held != u["held_paise"]):
                problems.append(f"{u['email']}: stored balance {u['balance_paise']}/{u['held_paise']} does not match history {bal}/{held}")
    return problems


def get_balance(conn, user_id: int) -> int:
    row = conn.execute("SELECT balance_paise FROM users WHERE id=?", (user_id,)).fetchone()
    return int(row["balance_paise"]) if row else 0


def credit(user_id: int, amount_paise: int, ref: str, note: str = "Wallet top-up", kind: str = "topup") -> int:
    with tx() as conn:
        conn.execute("UPDATE users SET balance_paise = balance_paise + ? WHERE id=?", (amount_paise, user_id))
        bal = get_balance(conn, user_id)
        _log(conn, user_id, kind, amount_paise, bal, ref, note)
        return bal


def hold(user_id: int, amount_paise: int, ref: str) -> int:
    with tx() as conn:
        bal = get_balance(conn, user_id)
        if bal < amount_paise:
            raise InsufficientBalance(bal, amount_paise)
        conn.execute(
            "UPDATE users SET balance_paise = balance_paise - ?, held_paise = held_paise + ? WHERE id=?",
            (amount_paise, amount_paise, user_id),
        )
        _log(conn, user_id, "hold", amount_paise, bal - amount_paise, ref, "Reserved for report")
        return bal - amount_paise


def commit(user_id: int, amount_paise: int, ref: str, note: str = "Report generated") -> None:
    with tx() as conn:
        conn.execute("UPDATE users SET held_paise = held_paise - ? WHERE id=?", (amount_paise, user_id))
        _log(conn, user_id, "charge", amount_paise, get_balance(conn, user_id), ref, note)


def release(user_id: int, amount_paise: int, ref: str, note: str = "Report failed, amount returned") -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE users SET held_paise = held_paise - ?, balance_paise = balance_paise + ? WHERE id=?",
            (amount_paise, amount_paise, user_id),
        )
        _log(conn, user_id, "release", amount_paise, get_balance(conn, user_id), ref, note)


def history(conn, user_id: int, limit: int = 100):
    """Statement shown to the user: top-ups and charges only (holds and releases cancel out)."""
    return conn.execute(
        "SELECT * FROM wallet_ledger WHERE user_id=? AND kind IN ('topup','charge','adjust') "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
