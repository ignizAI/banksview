"""SQLite storage. One small file DB is plenty for the MVP; the schema is plain SQL so it
moves to PostgreSQL later with minimal change."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    org TEXT,
    role TEXT NOT NULL,                       -- lender | agent | dsa
    mobile TEXT,
    balance_paise INTEGER NOT NULL DEFAULT 0, -- available balance
    is_master INTEGER NOT NULL DEFAULT 0,     -- test account: reports are free
    mfa_secret TEXT,                          -- TOTP secret, set when the user enrols
    mfa_enabled INTEGER NOT NULL DEFAULT 0,
    has_password INTEGER NOT NULL DEFAULT 1,  -- 0 for accounts that sign in with a mobile OTP only
    mobile_verified INTEGER NOT NULL DEFAULT 0,
    held_paise INTEGER NOT NULL DEFAULT 0,    -- reserved for reports in progress
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS wallet_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,                       -- topup | hold | charge | release | adjust
    amount_paise INTEGER NOT NULL,            -- always positive
    balance_after_paise INTEGER NOT NULL,
    ref TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    prev_hash TEXT,                           -- tamper-evident chain: each row signs its own fields and the previous row of the same user
    row_hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_user ON wallet_ledger(user_id, id);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL,
    order_id TEXT NOT NULL UNIQUE,
    payment_id TEXT,
    amount_paise INTEGER NOT NULL,
    status TEXT NOT NULL,                     -- created | paid | failed
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    paid_at TEXT,
    credit_paise INTEGER,                     -- wallet credit for this order (amount_paise is what the customer pays)
    coupon_code TEXT,
    pack TEXT                                 -- which fixed payment option was bought
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    applicant_ref TEXT,
    filename TEXT,
    file_sha256 TEXT,
    status TEXT NOT NULL,                     -- processing | done | failed
    error_code TEXT,
    error_message TEXT,
    bank TEXT,
    account_masked TEXT,
    period_from TEXT,
    period_to TEXT,
    txn_count INTEGER,
    risk_score INTEGER,
    risk_band TEXT,
    charged_paise INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    xlsx_path TEXT,
    borrower_consent_at TEXT,                 -- when the user confirmed the borrower's consent for this upload
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_reports_user ON reports(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_reports_hash ON reports(user_id, file_sha256);
CREATE TABLE IF NOT EXISTS consents (        -- what each user agreed to, when, and from where
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,                       -- terms | privacy | borrower_consent
    version TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    accepted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_consents_user ON consents(user_id, kind);
CREATE TABLE IF NOT EXISTS audit_log (       -- append-only record of who did what
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    event TEXT NOT NULL,                      -- signup | login | login_failed | upload | view_report | download_report | ...
    ref TEXT,
    ip TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_audit_user ON audit_log(user_id, id);
CREATE TABLE IF NOT EXISTS otps (            -- one-time codes sent to a mobile number (stored hashed)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,                    -- signup | login
    mobile TEXT NOT NULL,                     -- recipient key: the email address the code was sent to
    email TEXT,
    payload TEXT,                             -- signup details held until the number is verified
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL,              -- unix seconds
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_otps_mobile ON otps(mobile, created_at);
CREATE TABLE IF NOT EXISTS coupons (         -- created only by a master account
    code TEXT PRIMARY KEY,
    percent INTEGER NOT NULL,
    max_redemptions INTEGER,                  -- NULL = unlimited
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS coupon_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL REFERENCES coupons(code),
    user_id INTEGER NOT NULL REFERENCES users(id),
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,                     -- reserved (order created) | redeemed (payment received)
    discount_paise INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_redemptions_code ON coupon_redemptions(code, status);

-- database-level guards: even a bug or a hand-written query cannot overdraw a wallet, rewrite its history or alter a settled payment
CREATE TRIGGER IF NOT EXISTS trg_wallet_nonneg BEFORE UPDATE OF balance_paise, held_paise ON users
  WHEN NEW.balance_paise < 0 OR NEW.held_paise < 0 BEGIN SELECT RAISE(ABORT, 'wallet cannot go negative'); END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update BEFORE UPDATE ON wallet_ledger BEGIN SELECT RAISE(ABORT, 'wallet history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete BEFORE DELETE ON wallet_ledger BEGIN SELECT RAISE(ABORT, 'wallet history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_pay_immutable BEFORE UPDATE ON payments
  WHEN NEW.amount_paise <> OLD.amount_paise OR NEW.user_id <> OLD.user_id OR NEW.order_id <> OLD.order_id OR NEW.credit_paise IS NOT OLD.credit_paise
  BEGIN SELECT RAISE(ABORT, 'payment amount and owner cannot change'); END;
CREATE TRIGGER IF NOT EXISTS trg_pay_final BEFORE UPDATE ON payments
  WHEN OLD.status IN ('paid','failed') AND NEW.status <> OLD.status BEGIN SELECT RAISE(ABORT, 'a settled payment cannot change status'); END;
CREATE TRIGGER IF NOT EXISTS trg_pay_no_delete BEFORE DELETE ON payments BEGIN SELECT RAISE(ABORT, 'payments cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS trg_report_charge_final BEFORE UPDATE OF charged_paise ON reports
  WHEN OLD.status = 'done' AND NEW.charged_paise IS NOT OLD.charged_paise BEGIN SELECT RAISE(ABORT, 'charged amount is final'); END;
"""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)  # autocommit; we use explicit BEGIN
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        for col, ddl in (("is_master", "INTEGER NOT NULL DEFAULT 0"), ("mfa_secret", "TEXT"), ("mfa_enabled", "INTEGER NOT NULL DEFAULT 0"),
                         ("has_password", "INTEGER NOT NULL DEFAULT 1"), ("mobile_verified", "INTEGER NOT NULL DEFAULT 0"),
                         ("privileged", "INTEGER NOT NULL DEFAULT 0"), ("price_paise", "INTEGER"), ("session_epoch", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:  # databases created before this column existed
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        lcols = {r["name"] for r in conn.execute("PRAGMA table_info(wallet_ledger)")}
        for col in ("prev_hash", "row_hash"):
            if col not in lcols:
                conn.execute(f"ALTER TABLE wallet_ledger ADD COLUMN {col} TEXT")
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(payments)")}
        for col, ddl in (("credit_paise", "INTEGER"), ("coupon_code", "TEXT"), ("pack", "TEXT")):
            if col not in pcols:
                conn.execute(f"ALTER TABLE payments ADD COLUMN {col} {ddl}")
        try:   # one account per mobile number (ignored if old test data already shares a number)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_mobile ON users(mobile) WHERE mobile IS NOT NULL")
        except Exception:
            pass
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(reports)")}
        if "borrower_consent_at" not in rcols:
            conn.execute("ALTER TABLE reports ADD COLUMN borrower_consent_at TEXT")
    finally:
        conn.close()


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx():
    """Write transaction that takes the write lock up front (BEGIN IMMEDIATE) so two requests
    cannot both read the same balance and both spend it."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
