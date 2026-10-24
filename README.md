# BanksView - bank statement analysis for DSAs and small NBFCs

Upload a PDF bank statement (with its password) -> validated -> ~138 metrics, flags and a downloadable Excel dashboard. Rs 75 per report from a prepaid wallet (minimum top-up Rs 1,000).

## Run locally (Python 3.9 or newer; tested on 3.9 and 3.13)
    pip install -r requirements-dev.txt
    uvicorn app.main:app --reload          # http://localhost:8000, mock payments, data in ./data
    pytest tests                           # 30 tests, about 1 minute

## What it does
- Sign-up for lenders, agents and DSAs; PBKDF2 passwords, signed cookies, CSRF, login throttling.
- Upload checks (no charge on any failure): empty, too large, not a PDF, corrupt, password missing or wrong, scanned (no text), not a bank statement, credit-card statement, no transactions, no running balance, balances that do not reconcile (>5% of rows), too many pages.
- Wallet: the price is reserved before processing, charged only when the report is produced, and returned otherwise. Concurrent uploads cannot overspend. The same file within 30 days re-opens the old report free. Reports interrupted by a crash are refunded at next start.
- Report: 117 metrics per month plus total, 21 statement-level metrics, risk flags and score, counterparties, recurring debits, round-trip detection. Excel sheets: Summary (KPIs and 4 charts), Metrics, Transactions, Daily Balances, Counterparties, Flags & Patterns, Data Dictionary.
- Reversals (for example rejected cheques credited back) are removed from income; there is one definition of average balance; totals are recomputed from transactions.

## Go live (single VPS or container)
    docker build -t banksview .
    docker run -d -p 8000:8000 -v banksview-data:/data --env-file .env banksview
Put it behind HTTPS (Caddy or nginx). Keep one worker (SQLite and in-memory throttles); move to Postgres and Redis before scaling out.
1. Create a Razorpay account; set RAZORPAY_KEY_ID and KEY_SECRET, PAYMENT_MODE=razorpay.
2. Add a webhook to https://your-domain/webhooks/razorpay for payment.captured and set RAZORPAY_WEBHOOK_SECRET.
3. Test a real Rs 1,000 payment end to end. The Razorpay path follows their documented API but was NOT tested against live keys.
4. Back up /data/app.db daily.

Admin: `python -m app.cli credit user@x.com 500` | `users` | `cleanup`

## Known limits
- Digital (text) PDFs only; scans are rejected, there is no OCR. The parser is tested on four layout families plus one real-world sample, not on every bank. Add failing bank PDFs to tests as you meet them.
- Authenticity is checked only through balance reconciliation and PDF metadata, not with the bank.
- Processing is synchronous (seconds per statement); add a job queue for high volume.
- Not built: email verification, password reset, GST invoices, team accounts.
- Decide whether Rs 75 includes 18% GST; the code charges exactly PRICE_PAISE.
- Uploaded PDFs are deleted after parsing; Excel files after 90 days. Have counsel review consent and DPDP wording.

## Master (test) account

Use every feature with no wallet and no charges. Reports are always regenerated, even for a file you uploaded before.

```
python -m app.cli master tester@example.com 'a-strong-password'   # create, or promote an existing user
python -m app.cli master tester@example.com x revoke              # remove master access
```

Disabled when `APP_ENV=production` unless `ALLOW_MASTER=1`. It can only be created from the command line, never from the website.

## Legal, consent and sign-in settings

| Variable | Purpose |
|---|---|
| `COMPANY_NAME`, `COMPANY_ADDRESS` | Operator named in the Terms and Privacy Policy |
| `GRIEVANCE_OFFICER`, `GRIEVANCE_EMAIL` | Shown on `/grievance` and in the policies |
| `JURISDICTION_CITY` | Courts named in the Terms (default Mumbai) |
| `LIABILITY_CAP_MONTHS` | Liability cap = fees paid in this many months (default 3) |
| `MFA_REQUIRED` | `1` makes authenticator-app sign-in mandatory (default on in production) |

Sign-up needs three ticks (Terms, Privacy, borrower-consent undertaking) and each upload needs a borrower-consent tick. Both are logged. Change `TERMS_VERSION` in `app/config.py` to make every user accept updated terms. See `docs/ARCHITECTURE_STATUS.md` for what is and is not in place, and `docs/INCIDENT_PLAYBOOK.md`.

After pulling this version run `pip install -r requirements.txt` (adds `argon2-cffi`).
