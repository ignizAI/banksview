"""Admin commands:  python -m app.cli credit user@example.com 500   |  master user@example.com PASSWORD [revoke]  |  admin EMAIL [PASSWORD] [x revoke]  |  coupon MASTER_EMAIL [CODE] [PERCENT]  |  users  |  audit  |  backup  |  cleanup"""
from __future__ import annotations
import sys

from . import config, wallet
from .db import db, init_db


def main(argv):
    init_db()
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "credit" and len(argv) == 3:
        with db() as c:
            u = c.execute("SELECT id FROM users WHERE email=?", (argv[1].lower(),)).fetchone()
        if not u:
            print("No such user")
            return 1
        bal = wallet.credit(u["id"], int(round(float(argv[2]) * 100)), "admin", "Credit added by admin", kind="adjust")
        print("New balance:", config.rupees(bal))
    elif cmd == "master" and len(argv) in (2, 3, 4):
        from . import security
        email = argv[1].lower()
        if len(argv) == 4 and argv[3] == "revoke":
            with db() as c:
                c.execute("UPDATE users SET is_master=0 WHERE email=?", (email,))
            print("Master access removed from", email)
            return 0
        if not config.MASTER_ENABLED:
            print("Master accounts are disabled in production. Set ALLOW_MASTER=1 to allow.")
            return 1
        with db() as c:
            u = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if u:
                c.execute("UPDATE users SET is_master=1 WHERE id=?", (u["id"],))
                print("Existing user promoted to master:", email)
            else:
                if len(argv) < 3 or len(argv[2]) < 8:
                    print("New account needs a password of at least 8 characters:  master EMAIL PASSWORD")
                    return 1
                c.execute("INSERT INTO users(email,password_hash,name,org,role,is_master) VALUES (?,?,?,?,?,1)",
                          (email, security.hash_password(argv[2]), "Master Tester", config.BRAND, "lender"))
                print("Master account created:", email)
    elif cmd == "admin" and len(argv) in (2, 3, 4):     # python -m app.cli admin EMAIL [PASSWORD] | admin EMAIL x revoke
        from . import security
        email = argv[1].lower()
        if len(argv) == 4 and argv[3] == "revoke":
            with db() as c:
                c.execute("UPDATE users SET is_admin=0, session_epoch=session_epoch+1 WHERE email=?", (email,))
            print("Admin access removed from", email)
            return 0
        with db() as c:
            u = c.execute("SELECT id, has_password FROM users WHERE email=?", (email,)).fetchone()
            pw = argv[2] if len(argv) >= 3 else ""
            if pw and len(pw) < 12:
                print("An admin password needs at least 12 characters.")
                return 1
            if u:
                if pw:
                    c.execute("UPDATE users SET is_admin=1, has_password=1, password_hash=?, session_epoch=session_epoch+1 WHERE id=?", (security.hash_password(pw), u["id"]))
                    print("Existing account promoted to admin and its password set:", email)
                elif u["has_password"]:
                    c.execute("UPDATE users SET is_admin=1 WHERE id=?", (u["id"],))
                    print("Existing account promoted to admin (its password is unchanged):", email)
                else:
                    print("This account has no password. Give one:  admin EMAIL PASSWORD")
                    return 1
            else:
                if not pw:
                    print("New admin needs a password of at least 12 characters:  admin EMAIL PASSWORD")
                    return 1
                c.execute("INSERT INTO users(email,password_hash,name,org,role,is_admin) VALUES (?,?,?,?,?,1)",
                          (email, security.hash_password(pw), "Administrator", config.BRAND, "lender"))
                print("Admin account created:", email)
        print("Sign in at /admin/login. Admins see all customer records; keep this list short.")
    elif cmd == "coupon" and len(argv) >= 2:      # python -m app.cli coupon EMAIL_OF_MASTER [CODE] [PERCENT]
        from . import coupons
        with db() as c:
            u = c.execute("SELECT id FROM users WHERE email=? AND is_master=1", (argv[1].lower(),)).fetchone()
        if not u or not config.MASTER_ENABLED:
            print("Only an enabled master account can create coupons.")
            return 1
        try:
            print("Coupon created:", coupons.create(u["id"], argv[2] if len(argv) > 2 else None, int(argv[3]) if len(argv) > 3 else None))
        except coupons.CouponError as e:
            print(e)
            return 1
    elif cmd == "backup":     # python -m app.cli backup
        from . import backup
        print(backup.make_backup())
    elif cmd == "audit":      # python -m app.cli audit   (exit code 2 if any wallet fails its integrity check)
        problems = wallet.verify_ledger()
        print("\n".join(problems) if problems else "All wallets match their history.")
        return 2 if problems else 0
    elif cmd == "users":
        with db() as c:
            for u in c.execute("SELECT id,email,name,role,balance_paise,created_at,is_master FROM users ORDER BY id"):
                print(u["id"], u["email"], u["role"], config.rupees(u["balance_paise"]), u["created_at"], "MASTER" if u["is_master"] else "")
    elif cmd == "cleanup":
        from .main import cleanup_expired, recover_stuck
        print("expired files removed:", cleanup_expired(), "| stuck reports refunded:", recover_stuck())
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
