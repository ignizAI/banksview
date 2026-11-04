"""Admin console: separate sign-in, access control, download counting, safe CSV, upgrade of an existing database."""
import re
import time

import pytest

from conftest import csrf_from, signup_otp
from test_app import GOOD, add_money, upload

ADMIN_EMAIL, ADMIN_PW = "boss@example.com", "a-very-long-passphrase-1"
PAGES = ["/admin", "/admin?days=7", "/admin?days=90&test=1", "/admin/customers", "/admin/customers?q=asha&sort=spend", "/admin/transactions",
         "/admin/transactions?status=paid", "/admin/reports", "/admin/reports?sort=downloads", "/admin/data", "/admin/export/customers.csv"]


def make_admin(email=ADMIN_EMAIL, pw=ADMIN_PW, admin=True):
    from app import security
    from app.db import tx
    with tx() as c:
        c.execute("INSERT INTO users(email,password_hash,name,org,role,is_admin) VALUES (?,?,?,?,?,?)",
                  (email, security.hash_password(pw), "Boss", "Test", "lender", 1 if admin else 0))


def admin_login(client, email=ADMIN_EMAIL, pw=ADMIN_PW, code=""):
    page = client.get("/admin/login")
    return client.post("/admin/login", data={"email": email, "password": pw, "code": code, "csrf": csrf_from(page.text)}, follow_redirects=False)


@pytest.fixture()
def admin(client):
    make_admin()
    assert admin_login(client).status_code == 303
    return client


# ----------------------------------------------------------------------------- access control
def test_every_admin_page_needs_sign_in(client):
    for path in PAGES + ["/admin/customers/1", "/admin/customers/new", "/admin/data/users", "/admin/export/users.csv"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", path


def test_customer_session_is_not_an_admin_session(client):
    assert signup_otp(client).status_code == 303          # a normal customer, signed in
    for path in ("/admin", "/admin/customers", "/admin/export/users.csv"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_customer_cookie_cannot_be_replayed_as_admin_cookie(client):
    make_admin()
    signup_otp(client)
    sid = client.cookies.get("sid")
    client.cookies.clear()
    client.cookies.set("asid", sid, path="/admin")
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_master_test_account_is_not_an_admin(client):
    make_admin("tester@example.com", ADMIN_PW, admin=False)
    r = admin_login(client, "tester@example.com")
    assert r.status_code == 401 and "Incorrect username or password" in r.text


def test_admin_cannot_use_customer_pages_through_admin_cookie(admin):
    assert admin.get("/dashboard", follow_redirects=False).status_code in (303, 307)      # no customer session


# ----------------------------------------------------------------------------- sign-in
def test_admin_sign_in_and_pages_render(admin):
    for path in PAGES:
        r = admin.get(path)
        assert r.status_code == 200, path
    assert "Analytics" in admin.get("/admin").text


def test_cookie_is_locked_down(client):
    make_admin()
    r = admin_login(client)
    sc = r.headers["set-cookie"].lower()
    assert "asid=" in sc and "httponly" in sc and "samesite=strict" in sc and "path=/admin" in sc


def test_wrong_password_unknown_user_and_missing_csrf_all_look_the_same(client):
    make_admin()
    a = admin_login(client, pw="wrong-wrong-wrong")
    b = admin_login(client, email="nobody@example.com")
    assert a.status_code == b.status_code == 401
    err = lambda t: re.search(r'role="alert">([^<]+)<', t).group(1)       # noqa: E731
    assert err(a.text) == err(b.text)
    r = client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PW, "csrf": "bad"}, follow_redirects=False)
    assert r.status_code == 400


def test_sign_in_is_throttled(client):
    make_admin()
    codes = [admin_login(client, pw="wrong-wrong-wrong").status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and 429 in codes[5:]
    assert admin_login(client).status_code == 429          # even the right password waits


def test_sign_in_is_username_and_password_only(client, monkeypatch):
    from app import config
    make_admin()
    monkeypatch.setattr(config, "MFA_REQUIRED", True)          # the customer-side rule must not leak into the admin console
    page = client.get("/admin/login")
    assert "authenticator" not in page.text.lower() and 'name="code"' not in page.text
    r = admin_login(client)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/mfa-setup", follow_redirects=False).status_code == 404


def test_logout_kills_the_cookie_everywhere(admin):
    old = admin.cookies.get("asid")
    page = admin.get("/admin")
    r = admin.post("/admin/logout", data={"csrf": csrf_from(page.text)}, follow_redirects=False)
    assert r.status_code == 303
    admin.cookies.clear()
    admin.cookies.set("asid", old, path="/admin")          # a copied cookie
    assert admin.get("/admin", follow_redirects=False).status_code == 303


def test_idle_admin_session_expires(client):
    from app import security
    make_admin()
    assert admin_login(client).status_code == 303
    client.cookies.clear()
    from app.db import db
    with db() as c:
        row = c.execute("SELECT id, session_epoch FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    client.cookies.set("asid", security.make_admin_session(row["id"], row["session_epoch"], now=time.time() - 3600), path="/admin")
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_revoking_admin_ends_access_immediately(admin):
    from app.db import tx
    assert admin.get("/admin").status_code == 200
    with tx() as c:
        c.execute("UPDATE users SET is_admin=0 WHERE email=?", (ADMIN_EMAIL,))
    assert admin.get("/admin", follow_redirects=False).status_code == 303


# ----------------------------------------------------------------------------- download counting
def test_download_counter_and_admin_views(client):
    from app.db import db
    assert signup_otp(client).status_code == 303
    add_money(client, 1000)
    rid = upload(client, GOOD).headers["location"].split("/reports/")[1]
    for _ in range(3):
        assert client.get(f"/reports/{rid}/download").status_code == 200
    with db() as c:
        row = c.execute("SELECT download_count, last_downloaded_at FROM reports WHERE id=?", (rid,)).fetchone()
    assert row["download_count"] == 3 and row["last_downloaded_at"]
    client.cookies.clear()
    make_admin()
    assert admin_login(client).status_code == 303
    html = client.get("/admin/reports").text
    assert rid[:6] in html or "Asha" in html
    k = client.get("/admin").text
    assert "Excel downloads" in k and re.search(r'Excel downloads</div><div class="v">3<', k)
    assert "Asha Rao" in client.get("/admin/customers").text
    detail = client.get("/admin/customers/1").text
    assert "Statement" in detail or "Reports and downloads" in detail
    assert "LAN" not in detail          # borrower reference never shown


def test_other_users_download_does_not_count(client):
    from app.db import db
    signup_otp(client)
    add_money(client, 1000)
    rid = upload(client, GOOD).headers["location"].split("/reports/")[1]
    client.cookies.clear()
    signup_otp(client, mobile="9811111111", email="bob@example.com", name="Bob", role="lender")
    assert client.get(f"/reports/{rid}/download").status_code == 404
    with db() as c:
        assert c.execute("SELECT download_count FROM reports WHERE id=?", (rid,)).fetchone()[0] == 0


# ----------------------------------------------------------------------------- data safety
def test_exports_and_pages_never_contain_secrets(client):
    from app.db import tx
    signup_otp(client)
    make_admin()
    with tx() as c:
        c.execute("UPDATE users SET mfa_secret='SECRETSEEDSECRET' WHERE email='asha@example.com'")
        c.execute("INSERT INTO reports(id,user_id,applicant_ref,filename,status,summary_json,xlsx_path) VALUES ('rX',1,'BORROWER-NAME-9','borrower_statement.pdf','done','{\"x\":1}','/srv/secret/path.xlsx')")
    client.cookies.clear()
    assert admin_login(client).status_code == 303
    blob = ""
    for t in ("users", "wallet_ledger", "payments", "reports", "consents", "audit_log"):
        r = client.get(f"/admin/export/{t}.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        blob += r.text + client.get(f"/admin/data/{t}").text
    blob += client.get("/admin/export/customers.csv").text + client.get("/admin/reports?test=1").text + client.get("/admin/customers/1").text
    for needle in ("password_hash", "pbkdf2$", "$argon2", "SECRETSEEDSECRET", "mfa_secret", "BORROWER-NAME-9", "borrower_statement", "/srv/secret", "summary_json", "code_hash"):
        assert needle not in blob, needle


def test_csv_export_neutralises_spreadsheet_formulas(client):
    from app.db import tx
    make_admin()
    with tx() as c:
        c.execute("INSERT INTO users(email,password_hash,name,org,role) VALUES ('evil@example.com','x','=HYPERLINK(\"http://x\")','+cmd|calc','dsa')")
    assert admin_login(client).status_code == 303
    text = client.get("/admin/export/customers.csv").text
    assert "'=HYPERLINK" in text and "'+cmd|calc" in text and ",=HYPERLINK" not in text


def test_search_text_is_escaped_and_odd_input_is_safe(admin):
    for q in ("<script>alert(1)</script>", "%", "_", "' OR 1=1 --", "a" * 500):
        r = admin.get("/admin/customers", params={"q": q, "sort": "x; DROP TABLE users", "page_no": "999"})
        assert r.status_code == 200 and "<script>alert(1)" not in r.text
    assert admin.get("/admin/data/users;drop").status_code == 404
    assert admin.get("/admin/customers/99999").status_code == 404
    assert admin.get("/admin/transactions?status=%27", follow_redirects=False).status_code == 200


def test_admin_actions_are_audited(admin):
    from app.db import db
    admin.get("/admin/customers/1")
    admin.get("/admin/export/users.csv")
    with db() as c:
        events = {e[0] for e in c.execute("SELECT event FROM audit_log")}
    assert {"admin_login", "admin_export"} <= events


def test_responses_are_not_cached_or_indexed(admin):
    r = admin.get("/admin")
    assert "no-store" in r.headers["cache-control"] and "noindex" in r.headers["x-robots-tag"]


# ----------------------------------------------------------------------------- upgrading an existing database
def test_old_database_gets_download_counts_from_its_history(client):
    from app import db as dbm
    from app.db import tx, db
    signup_otp(client)
    with tx() as c:
        c.execute("INSERT INTO reports(id,user_id,status) VALUES ('old1',1,'done')")
        for _ in range(4):
            c.execute("INSERT INTO audit_log(user_id,event,ref) VALUES (1,'download_report','old1')")
        c.execute("ALTER TABLE reports DROP COLUMN download_count")
        c.execute("ALTER TABLE reports DROP COLUMN last_downloaded_at")
    dbm.init_db()                                   # what happens when the new version starts
    with db() as c:
        r = c.execute("SELECT download_count, last_downloaded_at FROM reports WHERE id='old1'").fetchone()
    assert r["download_count"] == 4 and r["last_downloaded_at"]
    dbm.init_db()                                   # a second start changes nothing
    with db() as c:
        assert c.execute("SELECT download_count FROM reports WHERE id='old1'").fetchone()[0] == 4


def test_cli_admin_command(client, capsys):
    from app import cli
    from app.db import db
    assert cli.main(["admin", "new@example.com", "short"]) == 1                      # too short
    assert cli.main(["admin", "new@example.com"]) == 1                               # new account needs a password
    assert cli.main(["admin", "new@example.com", "a-long-enough-pass-1"]) in (0, None)
    with db() as c:
        assert c.execute("SELECT is_admin FROM users WHERE email='new@example.com'").fetchone()[0] == 1
    assert admin_login(client, "new@example.com", "a-long-enough-pass-1").status_code == 303
    assert cli.main(["admin", "new@example.com", "x", "revoke"]) in (0, None)
    client.cookies.clear()
    assert admin_login(client, "new@example.com", "a-long-enough-pass-1").status_code == 401



# ----------------------------------------------------------------------------- customer actions: add, credit, delete
def _new(client, **kw):
    page = client.get("/admin/customers/new")
    data = {"name": "Meera Shah", "email": "meera@example.com", "mobile": "9876543210", "role": "lender", "org": "Shah Finance", "credit": "", "password": "Welcome-pass-24", "csrf": csrf_from(page.text)}
    data.update(kw)
    return client.post("/admin/customers/new", data=data, follow_redirects=False)


def _cid(email):
    from app.db import db
    with db() as c:
        r = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    return r[0] if r else None


def _csrf(client, uid):
    return csrf_from(client.get(f"/admin/customers/{uid}").text)


def test_actions_need_an_admin_session(client):
    make_admin("other@example.com", ADMIN_PW, admin=False)
    signup_otp(client)                                         # a signed-in customer
    for path, data in (("/admin/customers/new", {"name": "x"}), ("/admin/customers/1/credit", {"amount": "5"}), ("/admin/customers/1/delete", {"confirm": "x"})):
        r = client.post(path, data={**data, "csrf": "x"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", path


def test_add_customer_with_starting_credit(admin):
    from app import wallet
    from app.db import db
    r = _new(admin, credit="250")
    assert r.status_code == 303
    uid = _cid("meera@example.com")
    assert r.headers["location"] == f"/admin/customers/{uid}?done=created"
    with db() as c:
        u = c.execute("SELECT balance_paise, has_password, mobile, role FROM users WHERE id=?", (uid,)).fetchone()
        led = c.execute("SELECT kind, amount_paise, note FROM wallet_ledger WHERE user_id=?", (uid,)).fetchall()
        ev = c.execute("SELECT event FROM audit_log WHERE event='admin_create_customer'").fetchall()
    assert (u["balance_paise"], u["has_password"], u["mobile"], u["role"]) == (25000, 1, "9876543210", "lender")
    assert [(x["kind"], x["amount_paise"]) for x in led] == [("adjust", 25000)] and ev
    assert wallet.verify_ledger() == []
    assert "Customer added" in admin.get(r.headers["location"]).text


def test_add_customer_validation(admin):
    assert _new(admin, name="").status_code == 422
    assert _new(admin, email="not-an-email").status_code == 422
    assert _new(admin, mobile="12345").status_code == 422
    assert _new(admin, role="wizard").status_code == 422
    assert _new(admin, credit="-5").status_code == 422
    assert _new(admin, credit="99999999").status_code == 422
    assert _new(admin, csrf="bad").status_code == 400
    assert _new(admin).status_code == 303
    assert _new(admin).status_code == 409                       # same email and mobile again
    assert _new(admin, email="meera2@example.com").status_code == 409     # same mobile


def test_credit_wallet_records_reason_and_keeps_ledger_valid(admin):
    from app import wallet
    from app.db import db
    _new(admin)
    uid = _cid("meera@example.com")
    tok = _csrf(admin, uid)
    r = admin.post(f"/admin/customers/{uid}/credit", data={"amount": "1,500.50", "reason": "offline payment received", "csrf": tok}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("done=credited")
    with db() as c:
        bal = c.execute("SELECT balance_paise FROM users WHERE id=?", (uid,)).fetchone()[0]
        row = c.execute("SELECT kind, amount_paise, note, ref FROM wallet_ledger WHERE user_id=?", (uid,)).fetchone()
        ev = c.execute("SELECT ref FROM audit_log WHERE event='admin_credit_wallet'").fetchone()
    assert bal == 150050 and row["kind"] == "adjust" and "offline payment received" in row["note"] and row["ref"].startswith("admin:")
    assert ev["ref"] == f"{uid}:150050" and wallet.verify_ledger() == []
    assert "Rs 1,500.50" in admin.get(f"/admin/customers/{uid}").text or "1,500.50" in admin.get(f"/admin/customers/{uid}").text


@pytest.mark.parametrize("amount,reason,code", [("", "x reason", 422), ("0", "valid reason", 422), ("abc", "valid reason", 422), ("-10", "valid reason", 422),
                                                ("1e3", "valid reason", 422), ("100001", "valid reason", 422), ("50", "", 422), ("50", "ab", 422)])
def test_credit_rejects_bad_input(admin, amount, reason, code):
    from app.db import db
    _new(admin)
    uid = _cid("meera@example.com")
    r = admin.post(f"/admin/customers/{uid}/credit", data={"amount": amount, "reason": reason, "csrf": _csrf(admin, uid)}, follow_redirects=False)
    assert r.status_code == code
    with db() as c:
        assert c.execute("SELECT balance_paise FROM users WHERE id=?", (uid,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM wallet_ledger WHERE user_id=?", (uid,)).fetchone()[0] == 0


def test_credit_needs_valid_csrf_and_existing_customer(admin):
    _new(admin)
    uid = _cid("meera@example.com")
    assert admin.post(f"/admin/customers/{uid}/credit", data={"amount": "5", "reason": "valid reason", "csrf": "bad"}, follow_redirects=False).status_code == 400
    r = admin.post("/admin/customers/99999/credit", data={"amount": "5", "reason": "valid reason", "csrf": _csrf(admin, uid)}, follow_redirects=False)
    assert r.status_code == 404


def test_delete_customer_without_history_removes_the_row(admin):
    from app.db import db
    _new(admin)
    uid = _cid("meera@example.com")
    wrong = admin.post(f"/admin/customers/{uid}/delete", data={"confirm": "someone@else.com", "csrf": _csrf(admin, uid)}, follow_redirects=False)
    assert wrong.status_code == 422 and _cid("meera@example.com")
    ok = admin.post(f"/admin/customers/{uid}/delete", data={"confirm": " Meera@Example.com ", "csrf": _csrf(admin, uid)}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/admin/customers?done=deleted"
    assert _cid("meera@example.com") is None
    with db() as c:
        ev = c.execute("SELECT ref FROM audit_log WHERE event='admin_delete_customer'").fetchone()[0]
    assert ev.startswith(f"{uid}:removed") and "meera" not in ev
    assert _new(admin).status_code == 303                       # the email and mobile can be used again


def test_delete_customer_with_history_erases_personal_data_but_keeps_financial_records(admin, tmp_path):
    from app import config, wallet
    from app.db import db, tx
    _new(admin, credit="300")
    uid = _cid("meera@example.com")
    f = config.REPORT_DIR / "meera-report.xlsx"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    with tx() as c:
        c.execute("INSERT INTO payments(user_id, provider, order_id, amount_paise, status) VALUES (?,?,?,?,?)", (uid, "mock", "order_keep_1", 10000, "paid"))
        c.execute("INSERT INTO reports(id, user_id, applicant_ref, filename, status, xlsx_path, bank) VALUES (?,?,?,?,?,?,?)",
                  ("rep1", uid, "BORROWER-NAME-9", "borrower.pdf", "done", str(f), "HDFC"))
    r = admin.post(f"/admin/customers/{uid}/delete", data={"confirm": "meera@example.com", "csrf": _csrf(admin, uid)}, follow_redirects=False)
    assert r.status_code == 303
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        assert u["deleted_at"] and u["email"] == f"deleted-{uid}@deleted.invalid"
        assert u["name"] == "Deleted customer" and u["mobile"] is None and u["org"] is None and u["has_password"] == 0
        assert c.execute("SELECT COUNT(*) FROM reports WHERE user_id=?", (uid,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM payments WHERE user_id=?", (uid,)).fetchone()[0] == 1       # financial records stay
        assert c.execute("SELECT COUNT(*) FROM wallet_ledger WHERE user_id=?", (uid,)).fetchone()[0] == 1
    assert not f.exists() and wallet.verify_ledger() == []
    page = admin.get(f"/admin/customers/{uid}")
    assert page.status_code == 200 and "was deleted" in page.text and "Add credit to wallet" not in page.text
    assert "meera@example.com" not in admin.get("/admin/customers").text and "Deleted customer" not in admin.get("/admin/customers").text
    again = admin.post(f"/admin/customers/{uid}/credit", data={"amount": "5", "reason": "valid reason", "csrf": csrf_from(page.text)}, follow_redirects=False)
    assert again.status_code == 409
    assert _new(admin).status_code == 303                       # the email can sign up again as a new customer


def test_admin_master_and_own_accounts_cannot_be_deleted(admin):
    from app.db import tx
    make_admin("second@example.com", ADMIN_PW, admin=True)
    make_admin("tester@example.com", ADMIN_PW, admin=False)
    with tx() as c:
        c.execute("UPDATE users SET is_master=1 WHERE email='tester@example.com'")
    for email in ("second@example.com", "tester@example.com", ADMIN_EMAIL):
        uid = _cid(email)
        r = admin.post(f"/admin/customers/{uid}/delete", data={"confirm": email, "csrf": _csrf(admin, uid)}, follow_redirects=False)
        assert r.status_code == 403 and _cid(email) == uid, email


def test_console_is_always_light():
    css = open("app/static/admin.css").read()
    assert "prefers-color-scheme" not in css and 'data-theme="dark"' not in css and "color-scheme:light" in css



def test_dashboard_shows_server_storage(admin):
    r = admin.get("/admin")
    assert r.status_code == 200 and "Server storage" in r.text and "Report files (Excel)" in r.text and "Database backups" in r.text
    assert "app.db" not in r.text and "/tmp" not in r.text and "backups/" not in r.text           # no server paths on the page


@pytest.mark.parametrize("pct,level", [(10, "ok"), (69.9, "ok"), (70, "warn"), (84.9, "warn"), (85, "bad"), (99, "bad")])
def test_storage_levels(monkeypatch, pct, level):
    import collections
    from app import admin_console
    du = collections.namedtuple("du", "total used free")
    monkeypatch.setattr(admin_console.shutil, "disk_usage", lambda p: du(1000, int(pct * 10), 1000 - int(pct * 10)))
    assert admin_console.storage()["level"] == level


def test_storage_warning_banner_and_size_format(admin, monkeypatch):
    import collections
    from app import admin_console
    du = collections.namedtuple("du", "total used free")
    monkeypatch.setattr(admin_console.shutil, "disk_usage", lambda p: du(40 * 1024 ** 3, 36 * 1024 ** 3, 4 * 1024 ** 3))
    page = admin.get("/admin").text
    assert "Almost full" in page and "90.0%" in page and "36.0 GB of 40.0 GB" in page and "docker image prune" in page
    assert admin_console.size(0) == "0 B" and admin_console.size(1536) == "1.5 KB"


# ----------------------------------------------------------------------------- admin accounts, session log, sign-in watermark
def _admin_form(client, **kw):
    page = client.get("/admin/admins")
    data = {"name": "Nikhil Rao", "email": "nikhil@example.com", "password": "Correct-Horse-Battery-9", "confirm": "Correct-Horse-Battery-9", "csrf": csrf_from(page.text)}
    data.update(kw)
    return client.post("/admin/admins/new", data=data, follow_redirects=False)


def test_admins_page_needs_sign_in_and_actions_need_an_admin(client):
    assert client.get("/admin/admins", follow_redirects=False).headers["location"] == "/admin/login"
    for path in ("/admin/admins/new", "/admin/admins/1/remove"):
        r = client.post(path, data={"csrf": "x", "name": "a", "email": "a@b.co", "password": "x" * 14, "confirm": "x" * 14}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", path


def test_signin_and_signout_are_logged_with_times(admin):
    from app.db import db
    with db() as c:
        row = c.execute("SELECT * FROM admin_sessions").fetchone()
    assert row["user_id"] and row["started_at"] and row["ended_at"] is None and row["sid"]
    html = admin.get("/admin/admins").text
    assert "Active now" in html and ADMIN_EMAIL in html and "Signed in now" in html
    out = admin.post("/admin/logout", data={"csrf": csrf_from(admin.get("/admin/customers").text)}, follow_redirects=False)
    assert out.status_code == 303
    with db() as c:
        row = c.execute("SELECT ended_at, ended_reason FROM admin_sessions").fetchone()
    assert row["ended_at"] and row["ended_reason"] == "signed out"
    admin_login(admin)                                          # sign back in to read the log
    html = admin.get("/admin/admins").text
    assert "signed out" in html and "Active now" in html


def test_session_state_timeouts():
    from datetime import datetime, timedelta
    from app import admin_console as ac
    t0 = datetime(2026, 11, 1, 10, 0, 0)
    f = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")      # noqa: E731
    row = {"started_at": f(t0), "last_seen_at": f(t0 + timedelta(minutes=5)), "ended_at": None, "ended_reason": None}
    assert ac.session_state(row, t0 + timedelta(minutes=10))["active"]
    idle = ac.session_state(row, t0 + timedelta(hours=1))
    assert not idle["active"] and idle["reason"] == "Timed out (idle)" and idle["secs"] == (5 + 20) * 60
    done = ac.session_state({**row, "ended_at": f(t0 + timedelta(minutes=8)), "ended_reason": "signed out"}, t0 + timedelta(hours=3))
    assert done["reason"] == "signed out" and done["secs"] == 8 * 60
    assert ac.duration(45) == "45s" and ac.duration(600) == "10m" and ac.duration(3 * 3600 + 5 * 60) == "3h 05m"
    assert ac.browser("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36") == "Chrome on Mac"


def test_create_a_new_admin_who_can_sign_in(admin):
    from app.db import db
    r = _admin_form(admin)
    assert r.status_code == 303 and r.headers["location"] == "/admin/admins?done=created"
    with db() as c:
        u = c.execute("SELECT is_admin, has_password, is_master, password_hash FROM users WHERE email='nikhil@example.com'").fetchone()
        ev = c.execute("SELECT event FROM audit_log WHERE event='admin_create_admin'").fetchone()
    assert (u["is_admin"], u["has_password"], u["is_master"]) == (1, 1, 0) and ev and "Correct-Horse" not in u["password_hash"]
    assert "Nikhil Rao" in admin.get("/admin/admins").text
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as other:
        assert admin_login(other, "nikhil@example.com", "Correct-Horse-Battery-9").status_code == 303
        assert other.get("/admin").status_code == 200


@pytest.mark.parametrize("kw,code", [({"name": ""}, 422), ({"email": "nope"}, 422), ({"confirm": "different-password-1"}, 422), ({"password": "short", "confirm": "short"}, 422),
                                     ({"password": "nikhil-password-1", "confirm": "nikhil-password-1"}, 422), ({"password": "aaaaaaaaaaaaaa", "confirm": "aaaaaaaaaaaaaa"}, 422),
                                     ({"csrf": "bad"}, 400)])
def test_create_admin_validation(admin, kw, code):
    from app.db import db
    assert _admin_form(admin, **kw).status_code == code
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM users WHERE email='nikhil@example.com'").fetchone()[0] == 0


def test_create_admin_refuses_an_email_that_is_already_registered(admin):
    from app.db import tx
    with tx() as c:
        c.execute("INSERT INTO users(email,password_hash,name,role) VALUES ('cust@example.com','x','Customer','lender')")
    r = _admin_form(admin, email="cust@example.com")
    assert r.status_code == 409
    from app.db import db
    with db() as c:
        assert c.execute("SELECT is_admin FROM users WHERE email='cust@example.com'").fetchone()[0] == 0     # a customer is never promoted by accident


def test_remove_admin_access_ends_their_session_and_not_your_own(admin):
    from app.db import db
    from fastapi.testclient import TestClient
    from app.main import app
    _admin_form(admin)
    with TestClient(app) as other:
        assert admin_login(other, "nikhil@example.com", "Correct-Horse-Battery-9").status_code == 303
        assert other.get("/admin").status_code == 200
        with db() as c:
            nid = c.execute("SELECT id FROM users WHERE email='nikhil@example.com'").fetchone()[0]
            me = c.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()[0]
        tok = csrf_from(admin.get("/admin/admins").text)
        assert admin.post(f"/admin/admins/{me}/remove", data={"csrf": tok}, follow_redirects=False).status_code == 403
        r = admin.post(f"/admin/admins/{nid}/remove", data={"csrf": tok}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("done=removed")
        assert other.get("/admin", follow_redirects=False).status_code == 303          # their open session no longer works
    with db() as c:
        assert c.execute("SELECT is_admin FROM users WHERE id=?", (nid,)).fetchone()[0] == 0
        assert c.execute("SELECT ended_reason FROM admin_sessions WHERE user_id=?", (nid,)).fetchone()[0] == "access removed"
    assert admin_login(TestClient(app), "nikhil@example.com", "Correct-Horse-Battery-9").status_code == 401


def test_signin_screen_has_the_watermark_without_inline_script(client):
    html = client.get("/admin/login").text
    assert 'class="wm"' in html and "wm-bank" in html and "/static/admin-wm.js" in html
    assert re.search(r"<script(?![^>]*src=)[^>]*>", html) is None           # nothing inline: the page's script policy stays strict
    js = client.get("/static/admin-wm.js")
    assert js.status_code == 200 and "signbox" in js.text


def test_admin_add_customer_with_a_country_code(admin):
    from app.db import db
    r = _new(admin, mobile="50 123 4567", country="+971")
    assert r.status_code == 303
    with db() as c:
        u = c.execute("SELECT mobile, country_code FROM users WHERE email='meera@example.com'").fetchone()
    assert (u["mobile"], u["country_code"]) == ("501234567", "+971")
    page = admin.get(f"/admin/customers/{_cid('meera@example.com')}").text
    assert "+971 501234567" in page and "+971 501234567" in admin.get("/admin/customers").text
    assert "+971,501234567" in admin.get("/admin/export/customers.csv").text or "+971" in admin.get("/admin/export/customers.csv").text
    assert _new(admin, email="m2@example.com", mobile="123", country="+971").status_code == 422
    assert 'name="country"' in admin.get("/admin/customers/new").text and "data-digits" in admin.get("/admin/customers/new").text


def test_admin_sets_a_customer_password(admin, client):
    from conftest import PASSWORD
    from app.db import db
    _new(admin)
    uid = _cid("meera@example.com")
    assert admin.post(f"/admin/customers/{uid}/password", data={"password": "abc", "csrf": _csrf(admin, uid)}).status_code == 422
    r = admin.post(f"/admin/customers/{uid}/password", data={"password": "Fresh-pass-2024", "csrf": _csrf(admin, uid)}, follow_redirects=False)
    assert r.status_code == 303
    with db() as c:
        assert c.execute("SELECT has_password FROM users WHERE id=?", (uid,)).fetchone()[0] == 1
        assert "Fresh-pass-2024" not in str(c.execute("SELECT ref FROM audit_log WHERE event='admin_set_customer_password'").fetchall()[0][0])
    from conftest import otp_login
    client.cookies.clear()
    assert otp_login(client, email="meera@example.com", password="Fresh-pass-2024").status_code == 303


def test_admin_cannot_set_an_admin_password_here(admin):
    from app.db import db
    with db() as c:
        aid = c.execute("SELECT id FROM users WHERE is_admin=1").fetchone()[0]
    assert admin.post(f"/admin/customers/{aid}/password", data={"password": "Fresh-pass-2024", "csrf": _csrf(admin, aid)}).status_code == 403


def test_plan_click_notifies_admin_dashboard_once_per_click(client, monkeypatch):
    from app import config
    from conftest import signup_otp, csrf_from
    from app.db import db
    monkeypatch.setattr(config, "PAYMENT_MODE", "contact_admin")
    assert signup_otp(client, email="plan@example.com", name="Plan Person").status_code == 303
    page = client.get("/wallet")
    client.post("/wallet/topup", data={"pack": "p250", "csrf": csrf_from(page.text)})
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM admin_notifications").fetchone()[0] == 1
    make_admin()
    client.cookies.clear()
    assert admin_login(client).status_code == 303
    d = client.get("/admin")
    assert "Plan requests" in d.text and "plan@example.com" in d.text and "Starter pack" in d.text
    nid = 1
    r = client.post(f"/admin/notifications/{nid}/handled", data={"csrf": csrf_from(d.text)}, follow_redirects=False)
    assert r.status_code == 303
    assert "Plan requests" not in client.get("/admin").text
