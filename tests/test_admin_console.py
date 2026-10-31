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
    for path in PAGES + ["/admin/customers/1", "/admin/data/users", "/admin/export/users.csv"]:
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
    assert r.status_code == 401 and "Incorrect username, password or code" in r.text


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


def test_authenticator_code_is_required_when_enrolled(client):
    from app import security
    from app.db import tx
    make_admin()
    secret = security.new_totp_secret() if hasattr(security, "new_totp_secret") else "JBSWY3DPEHPK3PXP"
    with tx() as c:
        c.execute("UPDATE users SET mfa_secret=?, mfa_enabled=1 WHERE email=?", (secret, ADMIN_EMAIL))
    assert admin_login(client).status_code == 401
    assert admin_login(client, code="000000").status_code == 401
    now = time.time()
    # compute the current code the same way the app verifies it
    import base64, hashlib, hmac, struct
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", int(now // 30)), hashlib.sha1).digest()
    o = h[-1] & 15
    code = f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7fffffff) % 1000000:06d}"
    assert admin_login(client, code=code).status_code == 303


def test_production_requires_authenticator(client, monkeypatch):
    from app import config
    make_admin()
    monkeypatch.setattr(config, "MFA_REQUIRED", True)
    r = admin_login(client)
    assert r.status_code == 403 and "authenticator" in r.text.lower()


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
    assert admin.get("/admin/data/otps").status_code == 404           # sign-in codes are not browsable
    assert admin.get("/admin/export/otps.csv").status_code == 404
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
