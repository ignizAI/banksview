# BanksView: architecture status against the business plan (section 6)

Checked 24 Oct 2026. "In place" means it is in the code and covered by a test. Nothing here is a certification.

| Layer | Plan | Status | Detail |
|---|---|---|---|
| Data minimisation | PDF deleted after parsing; password in memory only; reports expire | **In place** | PDF read from memory (`io.BytesIO`), never written to disk. Password never stored or logged. At expiry (`REPORT_RETENTION_DAYS`, 90) the workbook, the stored summary and the file name are deleted; only the billing record stays. Test: `test_expiry_deletes_file_summary_and_filename`. |
| Consent | Click-wrap with logged timestamp; user warrants borrower consent | **In place** | Three mandatory tickboxes at sign-up, enforced on the server. Each is stored with version, time, IP and browser (`consents`). A new `TERMS_VERSION` forces re-acceptance. Every upload also needs a borrower-consent confirmation, stored on the report (`borrower_consent_at`). |
| Terms with liability cap | Cap = fees of previous 3 months; user indemnifies | **Drafted** | `/terms`. Needs review by an Indian technology lawyer before launch. A cap cannot waive DPDP duties or liability for fraud or wilful misconduct. |
| Privacy policy and grievance contact | DPDP Rules expectation | **Drafted** | `/privacy`, `/grievance`. Set `GRIEVANCE_EMAIL`, `GRIEVANCE_OFFICER`, `COMPANY_NAME`, `COMPANY_ADDRESS`. Lawyer review needed. |
| Identity | Argon2id; mandatory MFA | **In place** | Argon2id for new passwords; old PBKDF2 hashes upgrade at next sign-in. TOTP two-factor (`MFA_REQUIRED=1`, default on when `APP_ENV=production`). Not yet: passkeys, SSO, recovery codes (a user who loses the authenticator needs a manual reset). |
| Tenant isolation | Tenant ID in every query; automated cross-tenant tests | **Partly** | Every report query filters on the signed-in user; test `test_other_users_cannot_open_report`. No database row-level security (SQLite). No team or tenant model yet. |
| Audit | Immutable logs, every view and download | **Partly** | `audit_log` records sign-in, failed sign-in, MFA, upload, view, download, consent. It is a normal table, not immutable (no S3 Object Lock). No alerting. |
| Edge | WAF, bot and DDoS protection, security headers | **Partly** | Security headers (CSP, frame denial, nosniff, HSTS in production, `no-store`), CSRF tokens, sign-in and sign-up throttling. No WAF or DDoS layer: needs the hosting platform. |
| Encryption | TLS 1.2+, AES-256 at rest, per-tenant KMS keys | **Not yet** | TLS must be terminated by the hosting layer. Data at rest is plain SQLite and files. The TOTP key is stored unencrypted. |
| Hosting | AWS Mumbai, private subnets, no public database | **Not yet** | Runs on a laptop. Staging on AWS Mumbai is item 4 of the 90-day plan. |
| Parsing isolation | Sandboxed workers, no internet, memory and time limits, malware scan | **Partly** | Size and page limits, parser-only code path, no model calls. No process sandbox, no malware scan. |
| API | OAuth2 or HMAC, IP allowlist, rate limits, idempotency | **Not yet** | No public API. Planned for month 6. |
| Supply chain | Pinned dependencies, scanning, signed builds | **Partly** | Dependencies are version-bounded, not pinned or scanned. No CI. |
| Testing | Independent penetration test before launch | **Not yet** | 110 automated tests only. Commission the penetration test before the public launch. |
| Resilience | Daily encrypted backups, restore drill | **Not yet** | None. |
| Insurance | Technology E&O and cyber cover | **Not yet** | Ask a broker before launch. It is the real risk transfer when a disclaimer fails. |
| Incident response | Playbook with the 6-hour CERT-In step | **Drafted** | `docs/INCIDENT_PLAYBOOK.md`. Confirm the reporting duties with counsel. |
| Bank coverage | 15 banks, 3+ layouts each | **In place (synthetic)** | `app/banks.py`: 15 banks, 3 to 4 layouts each, 48 parse-and-reconcile tests. Built from published column formats with fictional data. Accuracy on real statements is not yet benchmarked (plan: 500+ real, consented statements). |

## Before the first public user
1. Lawyer review of Terms, Privacy and the consent wording.
2. Real values for `COMPANY_NAME`, `COMPANY_ADDRESS`, `GRIEVANCE_EMAIL`, `JURISDICTION_CITY`, `SECRET_KEY`, `APP_ENV=production`, live Razorpay keys.
3. TLS, hosting in India, encrypted backups.
4. Independent penetration test; insurance.
5. Accuracy benchmark on real consented statements for each of the 15 banks.
