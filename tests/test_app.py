import io
import openpyxl
import pytest

from conftest import add_money, balance, csrf_from, otp_login, signup_otp, upload
from make_pdfs import *  # noqa
from app import config, metrics

ROWS, OPEN = synthetic_ledger()
GOOD = render_statement(ROWS, OPEN)


# ----------------------------------------------------------------------------- accounts
def test_signup_login_logout(client):
    from app import otp
    tok = csrf_from(client.get("/signup").text)
    base = {"name": "A", "email": "a@b.co", "mobile": "9812345678", "role": "agent", "csrf": tok, "agree_terms": "1", "agree_privacy": "1", "agree_borrower": "1"}
    assert "valid email" in client.post("/signup", data={**base, "email": "nope"}).text
    assert "10-digit" in client.post("/signup", data={**base, "mobile": "12345"}).text
    assert client.post("/signup", data={**base, "role": "boss"}).status_code == 422
    assert client.post("/signup", data={**base, "csrf": "x"}).status_code == 400
    # nothing is created until the code is verified
    assert client.post("/signup", data=base, follow_redirects=False).status_code == 303
    from app.db import db
    with db() as c:
        assert c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0
    page = client.get("/verify")
    assert "a**@b.co" in page.text
    wrong = client.post("/verify", data={"code": "000000" if otp.LAST["a@b.co"] != "000000" else "111111", "csrf": csrf_from(page.text)})
    assert wrong.status_code == 401
    ok = client.post("/verify", data={"code": otp.LAST["a@b.co"], "csrf": csrf_from(page.text)}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/dashboard" and "sid" in ok.cookies
    with db() as c:
        u = c.execute("SELECT mobile, mobile_verified, has_password FROM users").fetchone()
    assert (u["mobile"], u["mobile_verified"], u["has_password"]) == ("9812345678", 0, 0)
    assert client.get("/dashboard").status_code == 200
    out = client.post("/logout", data={"csrf": csrf_from(client.get("/dashboard").text)}, follow_redirects=False)
    assert out.status_code == 303
    client.cookies.clear()
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    # a second sign-up with the same email or mobile is refused
    assert signup_otp(client, mobile="9812345678", email="other@b.co").status_code == 409
    assert signup_otp(client, mobile="9000000001", email="a@b.co").status_code == 409
    # sign in: the email must belong to an account
    r = client.get("/login")
    nomatch = client.post("/login", data={"email": "wrong@b.co", "csrf": csrf_from(r.text)})
    assert nomatch.status_code == 401
    assert otp_login(client, email="a@b.co").status_code == 303
    assert client.get("/dashboard").status_code == 200


def test_otp_expiry_reuse_attempt_limit_and_resend(client, monkeypatch):
    from app import otp
    from app.db import db
    monkeypatch.setattr(config, "OTP_RESEND_SECONDS", 30)
    r = client.get("/signup")
    data = {"name": "A", "email": "a@b.co", "mobile": "+91 98123 45678", "role": "dsa", "csrf": csrf_from(r.text), "agree_terms": "1", "agree_privacy": "1", "agree_borrower": "1"}
    client.post("/signup", data=data)
    code = otp.LAST["a@b.co"]
    page = client.get("/verify")
    tok = csrf_from(page.text)
    bad = "000000" if code != "000000" else "111111"
    for _ in range(config.OTP_MAX_ATTEMPTS):
        assert client.post("/verify", data={"code": bad, "csrf": tok}).status_code == 401
    assert client.post("/verify", data={"code": code, "csrf": tok}).status_code == 429       # locked even with the right code
    # resend is rate limited, then a fresh code works
    assert client.post("/verify/resend", data={"csrf": tok}).status_code == 429
    with db() as c:
        c.execute("UPDATE otps SET created_at=created_at-3600")
    assert client.post("/verify/resend", data={"csrf": tok}, follow_redirects=False).status_code == 303
    page = client.get("/verify")
    code2 = otp.LAST["a@b.co"]
    # expired code is refused
    with db() as c:
        c.execute("UPDATE otps SET expires_at=0 WHERE id=(SELECT MAX(id) FROM otps)")
    assert client.post("/verify", data={"code": code2, "csrf": csrf_from(page.text)}).status_code == 410
    # a used code cannot be used twice
    with db() as c:
        c.execute("UPDATE otps SET created_at=created_at-3600")
    client.post("/verify/resend", data={"csrf": tok})
    page = client.get("/verify")
    code3 = otp.LAST["a@b.co"]
    pending = client.cookies.get("otp_pending")
    assert client.post("/verify", data={"code": code3, "csrf": csrf_from(page.text)}, follow_redirects=False).status_code == 303
    client.cookies.set("otp_pending", pending)
    assert client.post("/verify", data={"code": code3, "csrf": csrf_from(page.text)}, follow_redirects=False).status_code == 410


def test_otp_stored_hashed_and_dev_provider_refused_in_production(client, monkeypatch):
    from app import otp
    from app.db import db
    client.post("/signup", data={"name": "A", "email": "a@b.co", "mobile": "9812345678", "role": "dsa", "csrf": csrf_from(client.get("/signup").text),
                                 "agree_terms": "1", "agree_privacy": "1", "agree_borrower": "1"})
    with db() as c:
        h = c.execute("SELECT code_hash FROM otps").fetchone()["code_hash"]
    assert otp.LAST["a@b.co"] not in h and len(h) == 64
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "OTP_PROVIDER", "dev")
    monkeypatch.setattr(config, "PAYMENT_MODE", "razorpay")
    monkeypatch.setattr(config, "SECRET_KEY", "a-long-production-secret")
    with pytest.raises(RuntimeError, match="OTP_PROVIDER"):
        config.check_production_safety()


def test_login_throttle(client):
    r = client.get("/login")
    tok = csrf_from(r.text)
    codes = [client.post("/login", data={"email": "x@y.co", "csrf": tok}).status_code for _ in range(12)]
    assert codes[0] == 401 and codes[-1] == 429


# ----------------------------------------------------------------------------- wallet
def test_only_fixed_packs_can_be_bought(user):
    r = user.get("/wallet")
    for bad in ("", "p100", "p999999", "STARTER", "../x", "250", "500", "-1"):
        res = user.post("/wallet/topup", data={"pack": bad, "csrf": csrf_from(r.text)})
        assert res.status_code == 422, bad
    # the amount field means nothing: only the pack decides what is charged
    res = user.post("/wallet/topup", data={"pack": "p250", "amount": "1", "csrf": csrf_from(r.text)}, follow_redirects=False)
    assert res.status_code == 303
    from app.db import db
    with db() as c:
        assert c.execute("SELECT amount_paise, pack FROM payments").fetchone()["amount_paise"] == 25000
    add_money(user, 1000)
    assert balance(user) == 100000


def test_topup_is_idempotent(user):
    from app import payments
    order = add_money(user, 1000)
    assert payments.settle(order, "again") is False
    assert balance(user) == 100000


def test_no_balance_blocks_upload_and_points_to_wallet(user):
    r = upload(user, GOOD)
    assert r.status_code == 303 and r.headers["location"].startswith("/wallet")
    page = user.get("/wallet?m=need_balance")
    assert "payment option" in page.text


# ----------------------------------------------------------------------------- happy path
def test_report_generated_charged_and_downloadable(user):
    add_money(user, 1000)
    r = upload(user, GOOD, ref="LAN-1")
    assert r.status_code == 303 and r.headers["location"].startswith("/reports/")
    assert balance(user) == 100000 - config.PRICE_PAISE
    page = user.get(r.headers["location"])
    assert page.status_code == 200 and "LAN-1" in page.text and "Download Excel" in page.text
    dl = user.get(r.headers["location"] + "/download")
    assert dl.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(dl.content))
    assert wb.sheetnames[0] == "Summary" and {"Metrics", "Transactions", "Daily Balances", "Counterparties", "Flags & Patterns", "Data Dictionary"} <= set(wb.sheetnames)
    assert wb["Transactions"].max_row == len(ROWS) + 1
    # wallet ledger shows one top-up and one charge
    hist = user.get("/wallet").text
    assert "Report generated" in hist and "Wallet top-up" in hist


def test_other_users_cannot_open_report(user, client):
    add_money(user, 1000)
    loc = upload(user, GOOD).headers["location"]
    user.cookies.clear()
    assert signup_otp(user, mobile="9811111111", email="bob@example.com", name="Bob", role="lender").status_code == 303
    assert user.get(loc).status_code == 404
    assert user.get(loc + "/download").status_code == 404


def test_same_file_twice_is_free(user):
    add_money(user, 1000)
    first = upload(user, GOOD).headers["location"]
    bal = balance(user)
    second = upload(user, GOOD)
    assert second.headers["location"].split("?")[0] == first
    assert balance(user) == bal


# ----------------------------------------------------------------------------- validation: never charged
BAD_CASES = [
    ("empty", b"", "EMPTY_FILE"),
    ("text", b"hello world", "NOT_A_PDF"),
    ("invoice", invoice_pdf(), "NOT_A_STATEMENT"),
    ("letter", plain_text_pdf(), "NOT_A_STATEMENT"),
    ("card", card_statement_pdf(), "CREDIT_CARD_STATEMENT"),
    ("scan", scanned_pdf(), "SCANNED_PDF"),
    ("tampered", tampered_balance_statement(ROWS, OPEN), "UNRELIABLE_BALANCES"),
]


@pytest.mark.parametrize("name,data,code", BAD_CASES)
def test_invalid_documents_rejected_without_charge(user, name, data, code):
    add_money(user, 1000)
    r = upload(user, data)
    assert r.status_code in (422, 413), (name, r.status_code)
    assert code in r.text, (name, code)
    assert "not charged" in r.text.lower()
    assert balance(user) == 100000
    from app.db import db
    with db() as c:
        row = c.execute("SELECT status, error_code, charged_paise FROM reports ORDER BY rowid DESC LIMIT 1").fetchone()
        u = c.execute("SELECT held_paise FROM users").fetchone()
    assert row["status"] == "failed" and row["charged_paise"] == 0 and u["held_paise"] == 0
    if code != "EMPTY_FILE":
        assert row["error_code"] == code


def test_password_flow(user):
    add_money(user, 1000)
    enc = render_statement(ROWS, OPEN, password="Secret@123")
    r = upload(user, enc)
    assert r.status_code == 422 and "PASSWORD_REQUIRED" in r.text and "password" in r.text.lower()
    r = upload(user, enc, password="nope")
    assert r.status_code == 422 and "WRONG_PASSWORD" in r.text
    assert balance(user) == 100000
    r = upload(user, enc, password="Secret@123")
    assert r.status_code == 303 and balance(user) == 100000 - config.PRICE_PAISE


def test_oversize_file_rejected(user, monkeypatch):
    add_money(user, 1000)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)
    r = upload(user, b"%PDF-1.4" + b"0" * 2000)
    assert r.status_code == 413 and balance(user) == 100000


def test_balance_exactly_one_report(user):
    """Twenty reports at Rs 50 use a Rs 1,000 wallet exactly: the 21st is refused and sends the user to top up."""
    assert config.PRICE_PAISE == 5000
    add_money(user, 1000)
    for i in range(20):
        rows, op = synthetic_ledger(seed=100 + i)
        assert upload(user, render_statement(rows, op)).status_code == 303
    assert balance(user) == 0
    r = upload(user, render_statement(*synthetic_ledger(seed=999)[:1], synthetic_ledger()[1]))
    assert r.status_code == 303 and r.headers["location"].startswith("/wallet")
    assert balance(user) == 0


# ----------------------------------------------------------------------------- layouts and metrics
@pytest.mark.parametrize("layout", LAYOUTS)
def test_every_layout_parses_and_reconciles(layout):
    from app import ingest
    p = ingest.ingest(render_statement(ROWS, OPEN, layout=layout), None)
    assert len(p.rows) == len(ROWS) and not p.recon_breaks
    assert abs(p.closing_balance - ROWS[-1]["balance"]) < 0.01
    assert p.meta["bank"] == "Bank of Baroda" and p.meta["account_masked"] == "XXXX3324"


def test_at_least_100_data_points_per_column():
    assert len(metrics.WIN_KEYS) >= 100
    from app import ingest
    a = metrics.analyze(ingest.ingest(GOOD, None))
    for k in metrics.WIN_KEYS:
        assert k in a["total"] and all(k in m for m in a["monthly"].values()), k
    assert len(metrics.REGISTRY) >= 130


def test_totals_recomputed_not_summed():
    from app import ingest
    a = metrics.analyze(ingest.ingest(GOOD, None))
    t = a["total"]
    assert abs(sum(m["credit_adj"] for m in a["monthly"].values()) - t["credit_adj"]) < 0.01
    assert abs(sum(m["debit_adj"] for m in a["monthly"].values()) - t["debit_adj"]) < 0.01
    assert abs(t["credit_gross"] - sum(r["amount"] for r in ROWS if r["dir"] == "C")) < 0.01
    assert abs(t["debit_gross"] - sum(r["amount"] for r in ROWS if r["dir"] == "D")) < 0.01
    assert abs(t["bal_close"] - ROWS[-1]["balance"]) < 0.01


def test_synthetic_detects_emi_and_gst():
    from app import ingest
    t = metrics.analyze(ingest.ingest(GOOD, None))["total"]
    assert t["emi_count"] == 6 and t["emi_debit"] == 6 * 18500
    assert t["gst_tax_count"] == 6 and t["recurring_debit_count"] >= 12


SAMPLE = "/mnt/user-data/uploads/632BZ8683787_perf6946016_261069977297_A-43690401_accountAggregator__3__xlsx.txt"


@pytest.mark.skipif(not __import__("os").path.exists(SAMPLE), reason="sample ledger not present")
def test_against_perfios_sample_ledger():
    """The exact weaknesses found in the reference report: reversals counted as income, no bounces shown."""
    from app import ingest
    rows, op = ledger_from_perfios_txt(SAMPLE)
    a = metrics.analyze(ingest.ingest(render_statement(rows, op, holder="NASHER MILES"), None))
    t = a["total"]
    assert t["txn_count"] == len(rows) == 246
    assert t["credit_gross"] == round(sum(r["amount"] for r in rows if r["dir"] == "C"), 2)
    assert t["bounce_count"] == 6
    assert t["credit_reversal"] == 695286.0 and t["credit_adj"] == t["credit_gross"] - 695286.0
    assert t["reversal_inflation_pct"] > 15
    assert t["gst_tax_count"] == 7
    assert any(f["code"] == "BOUNCE" for f in a["flags"])


# ----------------------------------------------------------------------------- money safety
def test_concurrent_uploads_cannot_overspend(user):
    """Balance for exactly one report; two uploads at once. Exactly one may succeed."""
    import threading
    from app import wallet
    from app.db import db, tx
    add_money(user, 1000)
    with tx() as c:  # leave exactly one report's worth
        c.execute("UPDATE users SET balance_paise=?", (config.PRICE_PAISE,))
    results = []

    def go(i):
        try:
            wallet.hold(1, config.PRICE_PAISE, f"r{i}")
            results.append("ok")
        except wallet.InsufficientBalance:
            results.append("no")

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1 and results.count("no") == 7
    with db() as c:
        u = c.execute("SELECT balance_paise, held_paise FROM users").fetchone()
    assert u["balance_paise"] == 0 and u["held_paise"] == config.PRICE_PAISE


def test_stuck_processing_report_is_refunded(user):
    from app import main, wallet
    from app.db import db, tx
    add_money(user, 1000)
    wallet.hold(1, config.PRICE_PAISE, "stuck1")
    with tx() as c:
        c.execute("INSERT INTO reports(id,user_id,status,created_at) VALUES ('stuck1',1,'processing','2020-01-01 00:00:00')")
    assert main.recover_stuck() == 1
    assert balance(user) == 100000
    with db() as c:
        assert c.execute("SELECT held_paise FROM users").fetchone()[0] == 0


def test_razorpay_signatures(monkeypatch):
    import hashlib, hmac, json
    from app import payments
    monkeypatch.setattr(config, "RAZORPAY_KEY_SECRET", "ksecret")
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "wsecret")
    sig = hmac.new(b"ksecret", b"order_1|pay_1", hashlib.sha256).hexdigest()
    assert payments.verify_checkout_signature("order_1", "pay_1", sig)
    assert not payments.verify_checkout_signature("order_1", "pay_2", sig)
    body = json.dumps({"event": "payment.captured"}).encode()
    assert payments.verify_webhook(body, hmac.new(b"wsecret", body, hashlib.sha256).hexdigest())
    assert not payments.verify_webhook(body, "bad")


def test_production_refuses_unsafe_config(monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    with pytest.raises(RuntimeError):
        config.check_production_safety()
    monkeypatch.setattr(config, "SECRET_KEY", "a-real-secret")
    with pytest.raises(RuntimeError):   # still mock payments
        config.check_production_safety()


# ----------------------------------------------------------------------------- bank-printed summary (HDFC style)
def _hdfc_rows():
    rows, op = synthetic_ledger(seed=21)
    rows = [dict(r, idx=i) for i, r in enumerate(rows, 1)]
    # make the first transaction a credit so the opening balance is the only way to know its direction
    first = rows[0]
    if first["dir"] == "D":
        first["dir"] = "C"
        delta = first["amount"] * 2
        for r in rows:
            r["balance"] = round(r["balance"] + delta, 2)
        first["balance"] = round(op + first["amount"], 2)
    return rows, op


def test_printed_summary_gives_opening_balance_and_verifies():
    from app import ingest
    rows, op = _hdfc_rows()
    p = ingest.ingest(render_statement(rows, op, layout="hdfc"), None)
    assert p.rows[0]["dir"] == rows[0]["dir"] and abs(p.opening_balance - op) < 0.01
    assert not p.recon_breaks and p.meta["summary_check"]["verified"] is True
    assert len(p.rows) == len(rows)


def test_page_footers_do_not_leak_into_narrations():
    from app import ingest
    rows, op = _hdfc_rows()
    p = ingest.ingest(render_statement(rows, op, layout="hdfc"), None)
    assert not any("Contents of this statement" in r["narration"] or "Registered Office" in r["narration"] for r in p.rows)


def test_mismatching_printed_summary_is_flagged_not_hidden():
    from app import ingest
    rows, op = _hdfc_rows()
    p = ingest.ingest(render_statement(rows, op, layout="hdfc", summary_credit_delta=5000.0), None)
    assert p.meta["summary_check"]["verified"] is False
    a = metrics.analyze(p)
    assert any(f["code"] == "SUMMARY_MISMATCH" for f in a["flags"])


def test_card_settlements_and_failed_mandate():
    from app import ingest
    from datetime import date
    base = [("C", 9000.0, "98152897TERMINAL 1 CARDS SETTL. 06/04/25"), ("C", 36276.0, "SALARY-X"), ("D", 36276.0, "ACH D- HDFC BANK LTD-417026883 0000008806413095"),
            ("C", 36276.0, "ACH D- HDFC BANK LTD-417026883 0000008806413095"), ("D", 413.0, ".ACH DEBIT RETURN CHARGES 050425"),
            ("D", 8295.0, "ACH D- PIRAMAL FINANCE LIMI-HLS"), ("D", 4000.0, "UPI-CRED CLUB 0000104137386225 02/05/25 CRED-CRED.CLUB@AXISB")]
    rows, bal = [], 50000.0
    for i, (k, amt, n) in enumerate(base):
        bal = round(bal + amt if k == "C" else bal - amt, 2)
        rows.append({"date": date(2026, 4, 1 + i), "narration": n, "amount": amt, "dir": k, "balance": bal})
    a = metrics.analyze(ingest.ingest(render_statement(rows, 50000.0), None))
    t = a["total"]
    assert t["card_settlement_credit"] == 9000.0 and t["bounce_count"] == 1 and t["card_bill_debit"] == 4000.0
    assert t["emi_debit"] == 8295.0 + 0 and t["bounce_charge_debit"] == 413.0


# ----------------------------------------------------------------------------- master (test) account
def _make_master(client):
    from app import cli
    signup_otp(client, mobile="9822222222", email="master@example.com", name="Mia Tester", role="lender")
    assert cli.main(["master", "master@example.com"]) == 0


def test_master_generates_reports_free_with_zero_balance(client):
    _make_master(client)
    assert balance(client) == 0
    assert "Master" in client.get("/dashboard").text
    r = upload(client, GOOD)
    assert r.status_code == 303 and "/reports/" in r.headers["location"]
    assert balance(client) == 0
    from app.db import db
    with db() as c:
        row = c.execute("SELECT charged_paise FROM reports WHERE status='done'").fetchone()
        assert row["charged_paise"] == 0
        assert c.execute("SELECT COUNT(*) n FROM wallet_ledger").fetchone()["n"] == 0
    # same file again regenerates (a normal user would get the free duplicate)
    r2 = upload(client, GOOD)
    assert r2.headers["location"] != r.headers["location"]
    assert client.get("/wallet", follow_redirects=False).status_code == 200      # masters can open the wallet to test top-ups


def test_master_failed_upload_still_shows_error(client):
    _make_master(client)
    r = upload(client, b"not a pdf at all")
    assert r.status_code == 422


def test_master_can_be_created_from_cli_and_revoked(client):
    from app import cli
    from app.db import db
    assert cli.main(["master", "qa@example.com", "short"]) == 1          # weak password refused
    assert cli.main(["master", "qa@example.com", "long-enough-pass"]) == 0
    with db() as c:
        assert c.execute("SELECT is_master FROM users WHERE email='qa@example.com'").fetchone()["is_master"] == 1
    assert cli.main(["master", "qa@example.com", "x", "revoke"]) == 0
    with db() as c:
        assert c.execute("SELECT is_master FROM users WHERE email='qa@example.com'").fetchone()["is_master"] == 0


def test_master_disabled_in_production(client, monkeypatch):
    from app import cli
    monkeypatch.setattr(config, "MASTER_ENABLED", False)
    assert cli.main(["master", "qa@example.com", "long-enough-pass"]) == 1
    signup_otp(client, mobile="9833333333", email="rae@example.com", name="Rae")
    from app.db import db
    with db() as c:
        c.execute("UPDATE users SET is_master=1")
    r = upload(client, GOOD)                     # flag set but switch off: treated as a normal user
    assert r.status_code == 303 and "/wallet" in r.headers["location"]


# ----------------------------------------------------------------------------- bank coverage (business plan: 15 banks, 3+ layouts each)
from app.banks import SUPPORTED_BANKS  # noqa: E402

_BANK_CASES = [(b["name"], b["ifsc"], lay) for b in SUPPORTED_BANKS for lay in b["layouts"]]


def test_at_least_15_banks_and_3_layouts_each():
    assert len(SUPPORTED_BANKS) >= 15
    assert all(len(b["layouts"]) >= 3 for b in SUPPORTED_BANKS)


@pytest.mark.parametrize("bank,ifsc,layout", _BANK_CASES)
def test_bank_layout_parses_and_reconciles(bank, ifsc, layout):
    from app import ingest
    rows, opening = synthetic_ledger(months=2, seed=abs(hash((bank, layout))) % 1000)
    pdf = render_statement(rows, opening, layout=layout, bank=bank, ifsc=f"{ifsc}0001234", holder="FICTIONAL TRADERS")
    p = ingest.ingest(pdf, None)
    assert p.meta["bank"] == bank
    assert len(p.rows) == len(rows)
    assert not p.recon_breaks


# ----------------------------------------------------------------------------- consent, liability position and legal pages
def _signup(client, **over):
    return signup_otp(client, **over)


@pytest.mark.parametrize("missing", ["agree_terms", "agree_privacy", "agree_borrower"])
def test_signup_blocked_unless_every_consent_ticked(client, missing):
    r = _signup(client, **{missing: None})
    assert r.status_code == 422 and "must accept" in r.text
    from app.db import db
    with db() as c:
        assert c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0


def test_signup_records_consent_with_version_time_and_ip(client):
    assert _signup(client).status_code == 303
    from app.db import db
    with db() as c:
        rows = c.execute("SELECT kind, version, ip, accepted_at FROM consents ORDER BY kind").fetchall()
    assert [r["kind"] for r in rows] == ["borrower_consent", "privacy", "terms"]
    assert all(r["version"] == config.TERMS_VERSION and r["ip"] and r["accepted_at"] for r in rows)


def test_upload_without_borrower_consent_is_refused_and_free(user):
    before = balance(user)
    add_money(user, 1000)
    r = upload(user, GOOD, consent=False)
    assert r.status_code == 422 or "CONSENT_REQUIRED" in r.text
    assert balance(user) == 100000
    r = upload(user, GOOD)
    assert r.status_code == 303
    from app.db import db
    with db() as c:
        assert c.execute("SELECT borrower_consent_at FROM reports WHERE status='done'").fetchone()["borrower_consent_at"]


def test_new_terms_version_forces_reacceptance(user, monkeypatch):
    assert user.get("/dashboard", follow_redirects=False).status_code == 200
    monkeypatch.setattr(config, "TERMS_VERSION", "2099-01-01")
    r = user.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/terms/accept"
    page = user.get("/terms/accept")
    assert user.post("/terms/accept", data={"csrf": csrf_from(page.text), "agree_terms": "1"}).status_code == 422
    ok = user.post("/terms/accept", data={"csrf": csrf_from(page.text), "agree_terms": "1", "agree_privacy": "1", "agree_borrower": "1"}, follow_redirects=False)
    assert ok.status_code == 303
    assert user.get("/dashboard", follow_redirects=False).status_code == 200


@pytest.mark.parametrize("path", ["/terms", "/privacy", "/banks", "/security", "/grievance", "/"])
def test_public_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "BanksView" in r.text


def test_terms_state_platform_only_and_user_responsibility(client):
    t = client.get("/terms").text
    for needle in ("software platform", "not a bank", "consent", "indemnify", "fees you paid in the", "cannot be excluded"):
        assert needle in t
    assert "fraud detected" not in t.lower()


def test_no_competitor_names_or_overclaims_on_site(client):
    for path in ("/", "/terms", "/privacy", "/banks", "/security"):
        txt = client.get(path).text.lower()
        for bad in ("perfios", "digitap", "sikoia", "finbox", "100% accurate", "bank-grade", "rbi-compliant", "fraud-proof", "fraud detected", "verified genuine"):
            assert bad not in txt, (path, bad)


def test_security_headers_and_no_store(client):
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"


# ----------------------------------------------------------------------------- password hashing and two-factor sign-in
def _staff_login(client, password="correct-horse"):
    lg = client.get("/staff/login")
    return client.post("/staff/login", data={"email": "staff@example.com", "password": password, "csrf": csrf_from(lg.text)}, follow_redirects=False)


def test_argon2id_hashing_and_pbkdf2_upgrade(client):
    from app import cli, security
    from app.db import db
    assert cli.main(["master", "staff@example.com", "correct-horse"]) == 0
    with db() as c:
        assert c.execute("SELECT password_hash FROM users").fetchone()["password_hash"].startswith("$argon2id$")
        c.execute("UPDATE users SET password_hash=?", (security._pbkdf2("correct-horse"),))   # simulate an old account
    assert _staff_login(client, "wrong-pass-1").status_code == 401
    assert _staff_login(client).status_code == 303
    with db() as c:
        assert c.execute("SELECT password_hash FROM users").fetchone()["password_hash"].startswith("$argon2id$")


def test_otp_accounts_cannot_use_staff_password_login(client):
    from app.db import db
    assert signup_otp(client, email="staff@example.com").status_code == 303
    client.cookies.clear()
    assert _staff_login(client).status_code == 401


def test_totp_matches_rfc6238_vector():
    import base64
    from app import security
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert security.verify_totp(secret, "287082", now=59)
    assert not security.verify_totp(secret, "287082", now=59 + 300)


def test_mfa_required_flow_for_staff_password_accounts(client, monkeypatch):
    from app import cli, security
    from app.db import db
    monkeypatch.setattr(config, "MFA_REQUIRED", True)
    assert cli.main(["master", "staff@example.com", "correct-horse"]) == 0
    assert _staff_login(client).status_code == 303
    page = client.get("/terms/accept")
    assert client.post("/terms/accept", data={"csrf": csrf_from(page.text), "agree_terms": "1", "agree_privacy": "1", "agree_borrower": "1"}, follow_redirects=False).status_code == 303
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/mfa/setup"   # blocked until enrolled
    page = client.get("/mfa/setup")
    assert page.status_code == 200
    with db() as c:
        secret = c.execute("SELECT mfa_secret FROM users").fetchone()["mfa_secret"]
    assert client.post("/mfa/setup", data={"csrf": csrf_from(page.text), "code": "000000"}).status_code == 422
    good = security._totp_at(secret, int(__import__("time").time() // 30))
    assert client.post("/mfa/setup", data={"csrf": csrf_from(page.text), "code": good}, follow_redirects=False).status_code == 303
    assert client.get("/dashboard", follow_redirects=False).status_code == 200
    # next sign-in needs the password and a code
    client.cookies.clear()
    r = _staff_login(client)
    assert r.headers["location"] == "/mfa"
    assert client.get("/dashboard", follow_redirects=False).status_code == 303          # password alone is not enough
    mp = client.get("/mfa")
    bad = client.post("/mfa", data={"csrf": csrf_from(mp.text), "code": "111111"})
    assert bad.status_code == 401
    ok = client.post("/mfa", data={"csrf": csrf_from(mp.text), "code": security._totp_at(secret, int(__import__("time").time() // 30))}, follow_redirects=False)
    assert ok.status_code == 303 and client.get("/dashboard", follow_redirects=False).status_code == 200


def test_mfa_not_forced_on_mobile_otp_accounts(client, monkeypatch):
    monkeypatch.setattr(config, "MFA_REQUIRED", True)
    r = signup_otp(client)
    assert r.headers["location"] == "/dashboard"
    assert client.get("/dashboard", follow_redirects=False).status_code == 200


# ----------------------------------------------------------------------------- audit trail and retention
def test_audit_log_records_upload_view_download(user):
    add_money(user, 1000)
    r = upload(user, GOOD)
    rid = r.headers["location"].split("/reports/")[1]
    user.get(f"/reports/{rid}")
    user.get(f"/reports/{rid}/download")
    from app.db import db
    with db() as c:
        events = [e["event"] for e in c.execute("SELECT event FROM audit_log ORDER BY id")]
    assert {"signup", "upload", "view_report", "download_report"} <= set(events)


def test_expiry_deletes_file_summary_and_filename(user):
    add_money(user, 1000)
    rid = upload(user, GOOD).headers["location"].split("/reports/")[1]
    from app.db import db
    from app import main
    with db() as c:
        c.execute("UPDATE reports SET created_at='2000-01-01 00:00:00'")
    assert main.cleanup_expired() == 1
    with db() as c:
        row = c.execute("SELECT xlsx_path, summary_json, filename, bank FROM reports").fetchone()
    assert row["xlsx_path"] is None and row["summary_json"] is None and row["filename"] is None and row["bank"]
    assert user.get(f"/reports/{rid}").status_code == 410


# ----------------------------------------------------------------------------- pricing, coupons and no-refund wallet
def _topup(client, amount, coupon=""):
    pack = {50: "starter", 250: "p250", 1000: "p1000", 5000: "p5000"}[int(amount)]
    page = client.get("/wallet")
    return client.post("/wallet/topup", data={"pack": pack, "coupon": coupon, "csrf": csrf_from(page.text)}, follow_redirects=False)


def _pay(client, location):
    order = location.split("/pay/")[1]
    page = client.get(f"/pay/{order}")
    r = client.post(f"/pay/{order}/mock", data={"csrf": csrf_from(page.text)}, follow_redirects=False)
    assert r.status_code == 303
    return order


def _coupon(code="SAVE20", **kw):
    from app import coupons
    from app.db import db
    with db() as c:
        uid = c.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    return coupons.create(uid, code, **kw)


def test_price_is_50_and_packs_are_fixed():
    assert config.PRICE_PAISE == 5000
    assert [(p["id"], p["paise"] // config.PRICE_PAISE) for p in config.PACKS] == [("starter", 1), ("p250", 5), ("p1000", 20), ("p5000", 100)]


def test_wallet_page_shows_only_the_packs_and_no_amount_field(user):
    page = user.get("/wallet").text
    for pid in ("starter", "p250", "p1000", "p5000"):
        assert f'value="{pid}"' in page
    assert 'name="amount"' not in page and 'type="number"' not in page
    assert "5 reports" in page and "20 reports" in page and "100 reports" in page


def test_first_report_offer_is_once_and_only_before_the_first_report(user):
    from app.db import db
    assert 'value="starter"' in user.get("/wallet").text and "disabled" not in user.get("/wallet").text.split('value="starter"')[1].split("</label>")[0]
    order = add_money(user, 50)
    assert balance(user) == 5000
    # bought once: not offered again, and a forced post is refused
    page = user.get("/wallet")
    assert "disabled" in page.text.split('value="starter"')[1].split("</label>")[0]
    assert user.post("/wallet/topup", data={"pack": "starter", "csrf": csrf_from(page.text)}).status_code == 422
    # the report it bought is generated; the offer stays gone
    assert upload(user, GOOD).status_code == 303 and balance(user) == 0
    assert user.post("/wallet/topup", data={"pack": "starter", "csrf": csrf_from(user.get("/wallet").text)}).status_code == 422
    add_money(user, 250)
    assert balance(user) == 25000


def test_first_report_offer_disabled_after_first_report_even_if_never_bought(user):
    from app.db import db
    add_money(user, 250)
    assert upload(user, GOOD).status_code == 303
    assert user.post("/wallet/topup", data={"pack": "starter", "csrf": csrf_from(user.get("/wallet").text)}).status_code == 422


def test_no_refund_wording_on_wallet_terms_landing_signup(user):
    for path in ("/wallet", "/terms"):
        assert "not refundable" in user.get(path).text.lower(), path
    user.cookies.clear()
    assert "not refundable" in user.get("/signup").text.lower()
    assert "not refundable" in user.get("/").text.lower()
    assert "₹50" in user.get("/").text


def test_coupon_discounts_payment_but_credits_full_amount(user):
    _coupon("SAVE20", percent=20)
    r = _topup(user, 1000, "save20")                      # code is case-insensitive
    assert r.status_code == 303
    from app.db import db
    order = r.headers["location"].split("/pay/")[1]
    with db() as c:
        p = c.execute("SELECT amount_paise, credit_paise, coupon_code FROM payments WHERE order_id=?", (order,)).fetchone()
    assert (p["amount_paise"], p["credit_paise"], p["coupon_code"]) == (80000, 100000, "SAVE20")
    assert "800" in user.get(f"/pay/{order}").text
    _pay(user, r.headers["location"])
    assert balance(user) == 100000
    with db() as c:
        assert c.execute("SELECT status FROM coupon_redemptions").fetchone()["status"] == "redeemed"


def test_coupon_works_once_per_user_and_unpaid_order_does_not_burn_it(user):
    _coupon("ONCE20", percent=20)
    first = _topup(user, 250, "ONCE20")                  # started but not paid
    assert first.status_code == 303
    second = _topup(user, 250, "ONCE20")                 # allowed: the first was never paid
    assert second.status_code == 303
    _pay(user, second.headers["location"])
    again = _topup(user, 250, "ONCE20")
    assert again.status_code == 422 and "already used" in again.text


def test_invalid_expired_off_and_exhausted_coupons_refused(user, client):
    from app import coupons
    from app.db import db
    assert _topup(user, 250, "NOPE").status_code == 422
    _coupon("OFF20")
    coupons.set_active("OFF20", False)
    assert "not valid" in _topup(user, 250, "OFF20").text
    _coupon("OLD20")
    with db() as c:
        c.execute("UPDATE coupons SET expires_at='2000-01-01 00:00:00' WHERE code='OLD20'")
    assert "expired" in _topup(user, 250, "OLD20").text
    _coupon("ONE20", max_redemptions=1)
    other = coupons.normalise("ONE20")
    with db() as c:
        c.execute("INSERT INTO users(email,password_hash,name,role) VALUES ('z@z.co','x','Z','dsa')")
        zid = c.execute("SELECT id FROM users WHERE email='z@z.co'").fetchone()["id"]
        c.execute("INSERT INTO coupon_redemptions(code,user_id,order_id,status,discount_paise) VALUES (?,?,?,?,?)", (other, zid, "o1", "redeemed", 100))
    assert "fully used" in _topup(user, 250, "ONE20").text


def test_coupon_preview_endpoint(user):
    _coupon("PREV20")
    j = user.get("/wallet/coupon", params={"code": "prev20", "amount": "500"}).json()
    assert j["ok"] and j["percent"] == 20 and "₹400" in j["pay"] and "₹100" in j["discount"]
    assert user.get("/wallet/coupon", params={"code": "bad", "amount": "500"}).json()["ok"] is False
    assert user.get("/wallet/coupon", params={"code": "prev20", "amount": "49"}).json()["ok"] is False


def test_only_master_can_create_coupons(user):
    from app import cli
    from app.db import db
    assert user.get("/admin/coupons").status_code == 404
    page = user.get("/wallet")
    r = user.post("/admin/coupons", data={"code": "HACK", "percent": "90", "csrf": csrf_from(page.text)})
    assert r.status_code == 404
    with db() as c:
        assert c.execute("SELECT COUNT(*) n FROM coupons").fetchone()["n"] == 0
    assert cli.main(["master", "asha@example.com"]) == 0                  # promote
    page = user.get("/admin/coupons")
    assert page.status_code == 200
    r = user.post("/admin/coupons", data={"code": "welcome", "percent": "20", "max_redemptions": "5", "days_valid": "30", "note": "launch", "csrf": csrf_from(page.text)}, follow_redirects=False)
    assert r.status_code == 303
    with db() as c:
        row = c.execute("SELECT code, percent, max_redemptions, created_by FROM coupons").fetchone()
    assert (row["code"], row["percent"], row["max_redemptions"]) == ("WELCOME", 20, 5) and row["created_by"]
    assert "WELCOME" in user.get("/admin/coupons").text
    bad = user.post("/admin/coupons", data={"code": "x", "percent": "150", "csrf": csrf_from(page.text)})
    assert bad.status_code == 422


def test_master_disabled_blocks_coupon_admin(user, monkeypatch):
    from app.db import db
    with db() as c:
        c.execute("UPDATE users SET is_master=1")
    monkeypatch.setattr(config, "MASTER_ENABLED", False)
    assert user.get("/admin/coupons").status_code == 404


def test_cli_coupon_requires_master(user):
    from app import cli
    assert cli.main(["coupon", "asha@example.com"]) == 1
    assert cli.main(["master", "asha@example.com"]) == 0
    assert cli.main(["coupon", "asha@example.com", "CLI20", "20"]) == 0


def test_razorpay_order_charges_discounted_amount(user, monkeypatch):
    """The gateway order must be created for what the customer pays, not for the wallet credit."""
    from app import payments
    sent = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"id": "order_X1"}

    def fake_post(url, **kw):
        sent.update(kw["json"])
        return R()
    monkeypatch.setattr(config, "PAYMENT_MODE", "razorpay")
    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", "k")
    monkeypatch.setattr(config, "RAZORPAY_KEY_SECRET", "s")
    monkeypatch.setattr(payments.httpx, "post", fake_post)
    _coupon("RZP20")
    r = _topup(user, 1000, "RZP20")
    assert r.status_code == 303 and sent["amount"] == 80000
    from app.db import db
    with db() as c:
        p = c.execute("SELECT amount_paise, credit_paise FROM payments").fetchone()
    assert (p["amount_paise"], p["credit_paise"]) == (80000, 100000)
    # a paid amount that differs from the order is not credited
    with pytest.raises(payments.PaymentError):
        payments.settle("order_X1", "pay_1", 10000)
    assert balance(user) == 0
    assert payments.settle("order_X1", "pay_1", 80000)
    assert balance(user) == 100000
    assert not payments.settle("order_X1", "pay_1", 80000)       # idempotent
    assert balance(user) == 100000


# ----------------------------------------------------------------------------- master creates customer accounts
def test_master_creates_user_who_signs_in_by_otp(client):
    _make_master(client)
    page = client.get("/admin/users")
    assert page.status_code == 200
    data = {"name": "Ravi K", "email": "ravi@example.com", "mobile": "98111 22233", "role": "agent", "org": "RK Loans", "credit": "300", "csrf": csrf_from(page.text)}
    assert client.post("/admin/users", data={**data, "email": "bad"}).status_code == 422
    assert client.post("/admin/users", data={**data, "mobile": "123"}).status_code == 422
    ok = client.post("/admin/users", data=data)
    assert ok.status_code == 201 and "ravi@example.com" in ok.text
    assert client.post("/admin/users", data=data).status_code == 409
    from app.db import db
    with db() as c:
        u = c.execute("SELECT balance_paise, has_password, mobile FROM users WHERE email='ravi@example.com'").fetchone()
        assert (u["balance_paise"], u["has_password"], u["mobile"]) == (30000, 0, "9811122233")
        assert c.execute("SELECT COUNT(*) n FROM consents WHERE user_id=(SELECT id FROM users WHERE email='ravi@example.com')").fetchone()["n"] == 0
    client.cookies.clear()
    r = otp_login(client, email="ravi@example.com")
    assert r.status_code == 303
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/terms/accept"   # consent is the customer's own


def test_non_master_cannot_create_users(user):
    assert user.get("/admin/users").status_code == 404
    r = user.post("/admin/users", data={"name": "X", "email": "x@x.co", "mobile": "9811122233", "role": "dsa", "csrf": csrf_from(user.get("/wallet").text)})
    assert r.status_code == 404


# ----------------------------------------------------------------------------- UPI QR payments
def test_upi_qr_flow_with_master_approval(client, monkeypatch):
    from app.db import db
    monkeypatch.setattr(config, "PAYMENT_MODE", "upi_qr")
    monkeypatch.setattr(config, "UPI_ID", "owner@upi")
    monkeypatch.setattr(config, "UPI_PAYEE_NAME", "Test Payee")
    assert signup_otp(client, mobile="9844444444", email="u1@example.com", name="U One").status_code == 303
    page = client.get("/wallet")
    r = client.post("/wallet/topup", data={"pack": "p250", "csrf": csrf_from(page.text)}, follow_redirects=False)
    assert r.status_code == 303
    oid = r.headers["location"].rsplit("/", 1)[1]
    pay = client.get(f"/pay/{oid}")
    assert "<svg" in pay.text and "owner@upi" in pay.text
    tok = csrf_from(pay.text)
    assert "12-digit" in client.get(client.post(f"/pay/{oid}/upi", data={"utr": "123", "csrf": tok}, follow_redirects=False).headers["location"]).text
    assert client.post(f"/pay/{oid}/upi", data={"utr": "123456789012", "csrf": tok}, follow_redirects=False).status_code == 303
    with db() as c:
        assert c.execute("SELECT status FROM payments WHERE order_id=?", (oid,)).fetchone()["status"] == "review"
        uid = c.execute("SELECT id FROM users WHERE email='u1@example.com'").fetchone()["id"]
    # a customer cannot reach the approval page
    assert client.get("/admin/payments").status_code == 404
    # a different order cannot reuse the same UTR
    r2 = client.post("/wallet/topup", data={"pack": "p1000", "csrf": csrf_from(client.get("/wallet").text)}, follow_redirects=False)
    oid2 = r2.headers["location"].rsplit("/", 1)[1]
    back = client.post(f"/pay/{oid2}/upi", data={"utr": "123456789012", "csrf": csrf_from(client.get(f"/pay/{oid2}").text)}, follow_redirects=False)
    assert "already" in client.get(back.headers["location"]).text
    # master approves -> wallet credited once
    with db() as c:
        c.execute("UPDATE users SET is_master=1 WHERE id=?", (uid,))
    adm = client.get("/admin/payments")
    assert adm.status_code == 200 and "123456789012" in adm.text
    tk = csrf_from(adm.text)
    client.post(f"/admin/payments/{oid}/approve", data={"csrf": tk})      # maker-checker: nobody approves their own payment
    with db() as c:
        assert c.execute("SELECT status FROM payments WHERE order_id=?", (oid,)).fetchone()["status"] == "review"
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app) as c2:
        assert signup_otp(c2, mobile="9899999999", email="checker@example.com", name="Checker").status_code == 303
        with db() as c:
            c.execute("UPDATE users SET is_master=1 WHERE email='checker@example.com'")
        tk2 = csrf_from(c2.get("/admin/payments").text)
        c2.post(f"/admin/payments/{oid}/approve", data={"csrf": tk2})
        c2.post(f"/admin/payments/{oid}/approve", data={"csrf": tk2})
    with db() as c:
        assert c.execute("SELECT balance_paise FROM users WHERE id=?", (uid,)).fetchone()["balance_paise"] == 25000
        assert c.execute("SELECT status FROM payments WHERE order_id=?", (oid,)).fetchone()["status"] == "paid"


def test_upi_mode_needs_upi_id(client, monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_MODE", "upi_qr")
    monkeypatch.setattr(config, "UPI_ID", "")
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "OTP_PROVIDER", "email")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.x.co")
    monkeypatch.setattr(config, "SMTP_FROM", "a@x.co")
    monkeypatch.setattr(config, "SECRET_KEY", "a-long-production-secret")
    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["banksview.com"])
    with pytest.raises(RuntimeError, match="UPI_ID"):
        config.check_production_safety()


# ----------------------------------------------------------------------------- privileged customers
def test_privileged_customer_pays_own_rate(client):
    from app.db import db
    assert signup_otp(client, mobile="9855555555", email="adm@example.com", name="Adm").status_code == 303
    with db() as c:
        c.execute("UPDATE users SET is_master=1 WHERE email='adm@example.com'")
    page = client.get("/admin/users")
    base = {"name": "Priv One", "email": "p1@example.com", "mobile": "9866666666", "role": "agent", "credit": "100", "privileged": "1", "csrf": csrf_from(page.text)}
    assert client.post("/admin/users", data={**base, "rate": "500"}).status_code == 422       # above the standard price
    assert client.post("/admin/users", data=base).status_code == 201                          # blank rate -> default Rs 25
    with db() as c:
        u = c.execute("SELECT privileged, price_paise, balance_paise FROM users WHERE email='p1@example.com'").fetchone()
    assert (u["privileged"], u["price_paise"], u["balance_paise"]) == (1, 2500, 10000)
    client.cookies.clear()
    assert otp_login(client, email="p1@example.com").status_code == 303
    pg = client.get("/terms/accept")
    client.post("/terms/accept", data={"csrf": csrf_from(pg.text), "agree_terms": "1", "agree_privacy": "1", "agree_borrower": "1"})
    r = client.get("/wallet", follow_redirects=True)
    assert "Rs 25" in r.text or "₹25" in r.text or "25.00" in r.text, r.text[r.text.find("<h1"):][:300]
    # a finished report charges the privileged rate, not Rs 100
    assert upload(client, GOOD).status_code == 303
    with db() as c:
        row = c.execute("SELECT balance_paise, held_paise FROM users WHERE email='p1@example.com'").fetchone()
        rep = c.execute("SELECT charged_paise FROM reports ORDER BY created_at DESC LIMIT 1").fetchone()
    assert (row["balance_paise"], row["held_paise"], rep["charged_paise"]) == (7500, 0, 2500)


def test_coupons_hidden_when_switched_off(client, monkeypatch):
    from app.db import db
    monkeypatch.setattr(config, "COUPONS_ENABLED", False)
    assert signup_otp(client, mobile="9877777777", email="m@example.com", name="Mo").status_code == 303
    with db() as c:
        c.execute("UPDATE users SET is_master=1 WHERE email='m@example.com'")
    assert client.get("/admin/coupons").status_code == 404
    assert client.post("/admin/coupons", data={"code": "X", "csrf": "x"}).status_code == 404
    assert client.get("/wallet/coupon?code=X&amount=500").status_code == 404
    w = client.get("/wallet")
    assert 'name="coupon"' not in w.text and "/admin/coupons" not in w.text
    assert "coupon" not in client.get("/").text.lower()
    # a coupon code posted anyway is ignored: the top-up goes through at full price
    r = client.post("/wallet/topup", data={"pack": "p250", "coupon": "ANY", "csrf": csrf_from(w.text)}, follow_redirects=False)
    assert r.status_code == 303


# ----------------------------------------------------------------------------- wallet and payment hardening
def _signed_in(client, email="w@example.com", mobile="9801010101"):
    assert signup_otp(client, mobile=mobile, email=email, name="W").status_code == 303


def test_database_refuses_to_rewrite_wallet_history_or_overdraw(client):
    import sqlite3
    from app.db import db, tx
    _signed_in(client)
    add_money(client, 250)
    with db() as c:
        uid = c.execute("SELECT id FROM users").fetchone()["id"]
    for sql in ("UPDATE wallet_ledger SET amount_paise=1", "DELETE FROM wallet_ledger", "UPDATE users SET balance_paise=-1",
                "UPDATE users SET held_paise=-5", "UPDATE payments SET amount_paise=1", "UPDATE payments SET status='created'", "DELETE FROM payments"):
        with pytest.raises(sqlite3.IntegrityError):
            with tx() as c:
                c.execute(sql)
    with db() as c:
        assert c.execute("SELECT balance_paise FROM users WHERE id=?", (uid,)).fetchone()["balance_paise"] == 25000


def test_ledger_is_chained_and_detects_tampering(client):
    from app import wallet
    from app.db import connect
    _signed_in(client)
    add_money(client, 250)
    add_money(client, 1000)
    assert upload(client, GOOD).status_code == 303
    assert wallet.verify_ledger() == []
    conn = connect()                       # an attacker with raw file access drops the guard and edits a row
    conn.execute("DROP TRIGGER trg_ledger_no_update")
    conn.execute("UPDATE wallet_ledger SET amount_paise=amount_paise+100 WHERE id=1")
    conn.commit(); conn.close()
    assert any("integrity" in x or "running balance" in x for x in wallet.verify_ledger())


def test_stored_balance_must_match_history(client):
    from app import wallet
    from app.db import db
    _signed_in(client)
    add_money(client, 250)
    with db() as c:
        c.execute("UPDATE users SET balance_paise=balance_paise+5000")
    assert any("does not match history" in x for x in wallet.verify_ledger())


@pytest.mark.parametrize("bad", ["inf", "nan", "1e9", "-500", "0", "500; DROP TABLE users", "p250 ", "P250", "p250\x00", "０", "' OR 1=1 --", "starter,p250", "p250&pack=p5000"])
def test_topup_rejects_hostile_values_without_a_500(client, bad):
    from app.db import db
    _signed_in(client)
    r = client.post("/wallet/topup", data={"pack": bad, "csrf": csrf_from(client.get("/wallet").text)}, follow_redirects=False)
    assert r.status_code in (422, 429), (bad, r.status_code)
    with db() as c:
        assert c.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"] == 0


def test_no_report_without_the_minimum_credit(client):
    from app.db import db
    _signed_in(client)
    for bal in (0, config.PRICE_PAISE - 1):
        with db() as c:
            c.execute("UPDATE users SET balance_paise=?", (bal,))
        r = upload(client, GOOD)
        assert r.status_code == 303 and "need_balance" in r.headers["location"]
        with db() as c:
            assert c.execute("SELECT COUNT(*) n FROM reports WHERE status='done'").fetchone()["n"] == 0
            assert c.execute("SELECT balance_paise, held_paise FROM users").fetchone()["balance_paise"] == bal


def test_two_simultaneous_uploads_cannot_spend_one_report_twice(client):
    import threading
    from app.db import db
    _signed_in(client)
    with db() as c:
        c.execute("UPDATE users SET balance_paise=?", (config.PRICE_PAISE,))
    tok = csrf_from(client.get("/dashboard").text)
    out = []

    def go(ref):
        r = client.post("/reports", data={"password": "", "applicant_ref": ref, "csrf": tok, "borrower_consent": "1"},
                        files={"file": ("s.pdf", GOOD, "application/pdf")}, follow_redirects=False)
        out.append(r.headers.get("location", ""))
    ts = [threading.Thread(target=go, args=(f"R{i}",)) for i in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM reports WHERE status='done' AND charged_paise>0").fetchone()["n"]
        u = c.execute("SELECT balance_paise, held_paise FROM users").fetchone()
    assert n <= 1 and u["balance_paise"] >= 0 and u["held_paise"] == 0


def test_client_cannot_set_price_amount_or_owner(client):
    from app.db import db
    _signed_in(client)
    add_money(client, 250)
    tok = csrf_from(client.get("/dashboard").text)
    client.post("/reports", data={"password": "", "applicant_ref": "T", "csrf": tok, "borrower_consent": "1", "price": "1", "price_paise": "1", "charged_paise": "0",
                                  "amount_paise": "1", "user_id": "999", "free": "1", "master": "1", "is_master": "1"},
                files={"file": ("s.pdf", GOOD, "application/pdf")}, follow_redirects=False)
    with db() as c:
        r = c.execute("SELECT charged_paise, user_id FROM reports WHERE status='done'").fetchone()
        u = c.execute("SELECT balance_paise, is_master FROM users").fetchone()
    assert r["charged_paise"] == config.PRICE_PAISE and u["balance_paise"] == 25000 - config.PRICE_PAISE and u["is_master"] == 0
    page = client.get("/wallet")
    r = client.post("/wallet/topup", data={"pack": "p250", "credit_paise": "99999999", "amount_paise": "1", "amount": "1", "user_id": "1", "csrf": csrf_from(page.text)}, follow_redirects=False)
    oid = r.headers["location"].rsplit("/", 1)[1]
    with db() as c:
        p = c.execute("SELECT amount_paise, credit_paise FROM payments WHERE order_id=?", (oid,)).fetchone()
    assert (p["amount_paise"], p["credit_paise"]) == (25000, 25000)


def test_mock_payment_is_refused_outside_mock_mode_and_for_other_users(client, monkeypatch):
    from app.db import db
    from fastapi.testclient import TestClient
    from app import main
    _signed_in(client)
    r = client.post("/wallet/topup", data={"pack": "p250", "csrf": csrf_from(client.get("/wallet").text)}, follow_redirects=False)
    oid = r.headers["location"].rsplit("/", 1)[1]
    tok = csrf_from(client.get(f"/pay/{oid}").text)
    with TestClient(main.app) as other:                       # another customer cannot see or settle this order
        _signed_in(other, "o@example.com", "9802020202")
        assert other.get(f"/pay/{oid}").status_code == 404
        assert other.post(f"/pay/{oid}/mock", data={"csrf": csrf_from(other.get("/wallet").text)}, follow_redirects=False).status_code == 303
    with db() as c:
        assert c.execute("SELECT status FROM payments WHERE order_id=?", (oid,)).fetchone()["status"] == "created"
    monkeypatch.setattr(config, "PAYMENT_MODE", "razorpay")
    client.post(f"/pay/{oid}/mock", data={"csrf": tok}, follow_redirects=False)
    with db() as c:
        assert c.execute("SELECT status FROM payments WHERE order_id=?", (oid,)).fetchone()["status"] == "created"
        assert c.execute("SELECT balance_paise FROM users WHERE email='w@example.com'").fetchone()["balance_paise"] == 0


def _razorpay_order(client, monkeypatch):
    import hashlib, hmac
    from app.db import db
    monkeypatch.setattr(config, "PAYMENT_MODE", "razorpay")
    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", "k"); monkeypatch.setattr(config, "RAZORPAY_KEY_SECRET", "s")

    class R:
        def __init__(self, d): self.d = d
        def raise_for_status(self): pass
        def json(self): return self.d
    monkeypatch.setattr("app.payments.httpx.post", lambda *a, **k: R({"id": "order_TEST1"}))
    r = client.post("/wallet/topup", data={"pack": "p250", "csrf": csrf_from(client.get("/wallet").text)}, follow_redirects=False)
    assert r.headers["location"].endswith("/pay/order_TEST1")
    sig = hmac.new(b"s", b"order_TEST1|pay_X1", hashlib.sha256).hexdigest()
    return R, sig


def test_razorpay_callback_needs_gateway_confirmation(client, monkeypatch):
    from app.db import db
    _signed_in(client)
    R, sig = _razorpay_order(client, monkeypatch)
    def bal():
        with db() as c:
            return c.execute("SELECT balance_paise FROM users").fetchone()["balance_paise"]
    # valid-looking signature but the gateway says the amount was only Rs 5 -> refused
    monkeypatch.setattr("app.payments.httpx.get", lambda *a, **k: R({"order_id": "order_TEST1", "amount": 500, "currency": "INR", "status": "captured"}))
    r = client.post("/pay/order_TEST1/razorpay", data={"razorpay_payment_id": "pay_X1", "razorpay_signature": sig}, follow_redirects=False)
    assert "verify_fail" in r.headers["location"] and bal() == 0
    # a forged signature is refused
    r = client.post("/pay/order_TEST1/razorpay", data={"razorpay_payment_id": "pay_X1", "razorpay_signature": "0" * 64}, follow_redirects=False)
    assert "verify_fail" in r.headers["location"] and bal() == 0
    # an authorised-but-not-captured payment is refused
    monkeypatch.setattr("app.payments.httpx.get", lambda *a, **k: R({"order_id": "order_TEST1", "amount": 25000, "currency": "INR", "status": "authorized"}))
    r = client.post("/pay/order_TEST1/razorpay", data={"razorpay_payment_id": "pay_X1", "razorpay_signature": sig}, follow_redirects=False)
    assert bal() == 0
    # the real thing credits exactly once, even if the callback is replayed
    monkeypatch.setattr("app.payments.httpx.get", lambda *a, **k: R({"order_id": "order_TEST1", "amount": 25000, "currency": "INR", "status": "captured"}))
    for _ in range(3):
        client.post("/pay/order_TEST1/razorpay", data={"razorpay_payment_id": "pay_X1", "razorpay_signature": sig}, follow_redirects=False)
    assert bal() == 25000


SQLI = ["' OR '1'='1", "'; DROP TABLE users;--", "\" OR 1=1 --", "1; UPDATE users SET balance_paise=99999999", "' UNION SELECT password_hash FROM users--", "%27%20OR%201=1", "a'||(SELECT 1)||'"]


def test_sql_injection_attempts_change_nothing(client):
    from app.db import db
    _signed_in(client)
    add_money(client, 250)
    tok = csrf_from(client.get("/dashboard").text)
    for bad in SQLI:
        client.post("/login", data={"email": bad, "csrf": csrf_from(client.get("/login").text)})
        client.post("/signup", data={"name": bad, "email": bad, "mobile": bad, "role": bad, "org": bad, "csrf": csrf_from(client.get("/signup").text)})
        client.post("/reports", data={"password": bad, "applicant_ref": bad, "csrf": tok, "borrower_consent": "1"}, files={"file": (bad + ".pdf", GOOD, "application/pdf")}, follow_redirects=False)
        client.post("/wallet/topup", data={"pack": bad, "coupon": bad, "csrf": csrf_from(client.get("/wallet").text)})
        for path in (f"/reports/{bad}", f"/reports/{bad}/download", f"/pay/{bad}", f"/wallet/coupon?code={bad}&amount={bad}", f"/dashboard?q={bad}"):
            assert client.get(path).status_code < 500, path
        assert client.post(f"/pay/{bad}/upi", data={"utr": bad, "csrf": tok}, follow_redirects=False).status_code < 500
    with db() as c:
        assert c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 1
        assert c.execute("SELECT balance_paise FROM users").fetchone()["balance_paise"] <= 25000


def test_no_sql_is_built_by_string_formatting():
    import re
    from pathlib import Path
    bad = []
    for f in Path(__file__).resolve().parents[1].joinpath("app").glob("*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"\.execute(many|script)?\(\s*(f[\"']|[\"'][^\"']*[\"']\s*(%|\.format|\+))", line) and "ALTER TABLE" not in line:
                bad.append(f"{f.name}:{n}: {line.strip()[:90]}")
    assert not bad, bad


def test_session_idle_logout_and_revocation(client):
    from app import security
    _signed_in(client)
    good = client.cookies.get("sid")
    assert client.get("/dashboard", follow_redirects=False).status_code == 200
    # untouched for longer than the idle limit -> dead cookie
    import time
    stale = security.make_session(1, 0, now=time.time() - config.SESSION_IDLE_MINUTES * 60 - 5)
    client.cookies.set("sid", stale)
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    # signing out kills a copied cookie too
    client.cookies.set("sid", good)
    out = client.post("/logout", data={"csrf": csrf_from(client.get("/dashboard").text)}, follow_redirects=False)
    assert out.status_code == 303
    client.cookies.set("sid", good)
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_host_header_and_url_text_cannot_be_used_for_phishing(client):
    r = client.get("/", headers={"host": "evil.example"})
    assert r.status_code == 400
    _signed_in(client)
    r = client.post("/wallet/topup", data={"pack": "p250", "csrf": csrf_from(client.get("/wallet").text)}, follow_redirects=False)
    oid = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/pay/{oid}?e=<b>Call+9999999999+to+fix+your+payment</b>")
    assert "Call 9999999999" not in page.text and "<b>Call" not in page.text
    h = client.get("/").headers
    assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"] and "no-store" in h["cache-control"]


def test_audit_pages_are_master_only_and_clean(client):
    from app.db import db
    _signed_in(client)
    add_money(client, 250)
    assert client.get("/admin/audit").status_code == 404
    with db() as c:
        c.execute("UPDATE users SET is_master=1")
        uid = c.execute("SELECT id FROM users").fetchone()["id"]
    a = client.get("/admin/audit")
    assert a.status_code == 200 and "All clear" in a.text
    h = client.get(f"/admin/users/{uid}/wallet")
    assert h.status_code == 200 and "Wallet top-up" in h.text and "intact" in h.text
