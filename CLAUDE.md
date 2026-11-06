# BanksView: guide for Claude (read this first)

BanksView (by IgnizAI) turns a PDF bank statement into a validated Excel report (208 metrics, flags, score) for DSAs, loan agents and small NBFCs in India. Prepaid wallet, Rs 50 per report. Live at banksview.com.

## Who you are working with
The owner is a beginner with servers and code. Explain simply. Give exact copy-paste commands and say WHERE to run each one (Mac Terminal, or the Lightsail server). Never ask for passwords, keys or secrets in chat. Be honest about risks and gaps. Legal, tax and security-compliance questions need a professional's review; say so.

## Stack and layout
FastAPI + Jinja2 + SQLite (one worker). Docker. Caddy reverse proxy on AWS Lightsail (Mumbai).
- `app/main.py` routes, sign-up/login, wallet, upload; `app/admin_console.py` admin pages (/admin/*)
- `app/security.py` hashing, sessions, CSRF, throttles, bot token; `app/validate.py` sign-up input rules
- `app/ingest.py`, `banks.py`, `metrics.py`, `categorize.py`, `excel_report.py` the report engine
- `app/wallet.py`, `payments.py` wallet ledger (hash-chained) and payment modes
- `app/db.py` schema and migrations (add columns via the migration list); `app/config.py` all settings
- `app/templates`, `app/static` pages and assets; `tests/` pytest suite; `docs/` notes (DEPLOY.md is for Fly.io and LIGHTSAIL.md is the unused compose kit; neither is how we deploy)

## Run and test
    pip install -r requirements-dev.txt
    uvicorn app.main:app --reload
    pytest tests -q          # about 3 minutes; all must pass before pushing
Demo server env: `DATA_DIR=/tmp/bvdata SECRET_KEY=test APP_ENV=development PAYMENT_MODE=mock`.

## Deploy (manual Docker command only)
The owner deploys ONLY with this manual command, run on the Lightsail server. The repo also contains a compose-based kit (`docs/LIGHTSAIL.md`, `deploy/lightsail/`) added by a teammate; do NOT use it or suggest it, because it keeps data in a different folder and would start the app on an empty database.

    cd ~/banksview && git pull && docker build -t banksview . && docker rm -f banksview && docker run -d --name banksview --restart unless-stopped -p 8000:8000 --env-file ~/banksview.env -v ~/banksview-data:/data banksview && docker image prune -f
After any change, finish by giving the owner this command, then `docker logs --tail 20 banksview` to check it started. Settings live in `~/banksview.env` on the server (it overrides code defaults; check it when a default change seems to have no effect).

## Rules that must not break
- Customer signs up and signs in with email + password (8 to 16 chars). There are no emailed codes. Sign-up has strict validation and bot traps (hidden field, form-timing token).
- PAYMENT_MODE default is `contact_admin`: customers pick a plan, the admin gets a dashboard request, collects payment outside the site and adds wallet credit. Online payment code (razorpay, upi_qr) exists but is off. `mock` must never run in production.
- Uploaded PDFs are processed in memory and never stored. Excel reports are deleted after 30 days (REPORT_RETENTION_DAYS).
- Failed uploads are never charged. Wallet: reserve, then charge only on success, else release. Ledger and payment rows are append-only (DB triggers); do not add code that updates or deletes them.
- All SQL uses bound parameters. `test_no_sql_is_built_by_string_formatting` enforces it; never weaken it.
- Admin console is a separate session (cookie `asid`, path /admin), light theme, 20-minute idle timeout. Admin actions are audit-logged. Never show borrower names, file names or statement contents there.
- Delete customer = hard delete if no money history, otherwise anonymise in place.
- Template globals are fixed at startup; pass anything that can change at runtime (for example the payment mode) through the render context.
- Keep the repo private. Never commit secrets.

## Working agreements
- Make the change, add or update tests, run the full suite, then commit with a clear message and push to `main`.
- Check UI changes in a real browser (Playwright) on desktop and phone widths.
- Other people push to `main` too (check `git log` and `git pull --rebase` before pushing; do not overwrite their work).
- Known gaps and roadmap are in the PRD (project knowledge): email verification, password reset, online payments, legal review, OCR, Postgres/Redis, CI.
