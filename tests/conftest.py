import os, sys, tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="stmtiq_test_")
os.environ.update({"DATA_DIR": _tmp, "SECRET_KEY": "test-secret", "APP_ENV": "development", "PAYMENT_MODE": "mock"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app import db, main, security
    # fresh database per test
    for f in Path(_tmp).glob("app.db*"):
        f.unlink()
    security.login_throttle.hits.clear()
    security.signup_throttle.hits.clear()
    security.topup_throttle.hits.clear()
    security.utr_throttle.hits.clear()
    security.admin_throttle.hits.clear()
    from app import otp
    otp.LAST.clear()
    from app import config
    config.OTP_RESEND_SECONDS = 0        # tests that check the cooldown set it back
    config.COUPONS_ENABLED = True       # the coupon tests exercise the feature; the default (off) is tested separately
    with TestClient(main.app) as c:
        yield c


def csrf_from(html: str) -> str:
    import re
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def signup_otp(client, mobile="9876543210", email="asha@example.com", name="Asha Rao", role="dsa", org="Rao Finserve", **over):
    """Full OTP sign-up: submit the form, read the dev code, verify it. Returns the final response."""
    from app import otp
    r = client.get("/signup")
    data = {"name": name, "email": email, "role": role, "org": org, "mobile": mobile, "csrf": csrf_from(r.text),
            "agree_terms": "1", "agree_privacy": "1", "agree_borrower": "1"}
    data.update({k: v for k, v in over.items() if v is not None})
    for k, v in over.items():
        if v is None:
            data.pop(k, None)
    r = client.post("/signup", data=data, follow_redirects=False)
    if r.status_code != 303:
        return r
    page = client.get("/verify")
    return client.post("/verify", data={"code": otp.LAST[email], "csrf": csrf_from(page.text)}, follow_redirects=False)


def otp_login(client, mobile="9876543210", email="asha@example.com"):
    from app import otp
    r = client.get("/login")
    r = client.post("/login", data={"email": email, "csrf": csrf_from(r.text)}, follow_redirects=False)
    if r.status_code != 303:
        return r
    page = client.get("/verify")
    return client.post("/verify", data={"code": otp.LAST[email], "csrf": csrf_from(page.text)}, follow_redirects=False)


@pytest.fixture()
def user(client):
    """A signed-in user with an empty wallet."""
    r = signup_otp(client)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard", r.text
    return client


def add_money(client, rupees):
    r = client.get("/wallet")
    pack = {50: "starter", 250: "p250", 1000: "p1000", 5000: "p5000"}[int(rupees)]
    r = client.post("/wallet/topup", data={"pack": pack, "csrf": csrf_from(r.text)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    order = r.headers["location"].split("/pay/")[1]
    r = client.get(f"/pay/{order}")
    r = client.post(f"/pay/{order}/mock", data={"csrf": csrf_from(r.text)}, follow_redirects=False)
    assert r.status_code == 303
    return order


def balance(client) -> int:
    from app.db import db
    with db() as c:
        return c.execute("SELECT balance_paise, held_paise FROM users").fetchone()["balance_paise"]


def upload(client, pdf: bytes, password: str = "", ref: str = "T1", name="statement.pdf", consent: bool = True):
    r = client.get("/dashboard")
    data = {"password": password, "applicant_ref": ref, "csrf": csrf_from(r.text)}
    if consent:
        data["borrower_consent"] = "1"
    return client.post("/reports", data=data,
                       files={"file": (name, pdf, "application/pdf")}, follow_redirects=False)
