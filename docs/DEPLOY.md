# Deploy BanksView on Fly.io (Mumbai)

BanksView needs a server with a persistent disk (SQLite). It cannot run on Vercel's serverless functions.
Cost: about Rs 600 to 700 a month for a 1 GB machine plus the disk.

## One-time setup (on your Mac)
```
brew install flyctl
fly auth signup            # or: fly auth login
cd ~/Documents/Github/banksview
```
1. Edit `app = "banksview"` in `fly.toml` to a unique name.
2. Create the app and the disk:
```
fly apps create <your-app-name>
fly volumes create banksview_data --region bom --size 3
```
3. Set the secrets (see `.env.example` for every name). The server refuses to start in production without the first group:
```
fly secrets set SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  ALLOWED_HOSTS="banksview.com,www.banksview.com,<your-app-name>.fly.dev" \
  OTP_PROVIDER=email SMTP_HOST=... SMTP_USER=... SMTP_PASSWORD=... SMTP_FROM="BanksView <no-reply@banksview.com>" \
  PAYMENT_MODE=razorpay RAZORPAY_KEY_ID=... RAZORPAY_KEY_SECRET=... RAZORPAY_WEBHOOK_SECRET=... \
  COMPANY_NAME=... COMPANY_ADDRESS=... GRIEVANCE_OFFICER=... GRIEVANCE_EMAIL=...
```
   Use `PAYMENT_MODE=upi_qr` with `UPI_ID` and `UPI_PAYEE_NAME` instead of the Razorpay keys if you take UPI payments directly.
4. Deploy:
```
fly deploy
fly status
```
5. Create your master account. Sign up on the site, then promote it:
```
fly ssh console -C "python -m app.cli master you@example.com"
```
   (Master accounts are disabled in production unless you also set `ALLOW_MASTER=1`.)

## Your domain
```
fly certs add banksview.com
fly certs add www.banksview.com
```
Then add the DNS records Fly prints (an A and an AAAA record for the root, a CNAME for www).

## Razorpay webhook
Dashboard > Webhooks > add `https://banksview.com/webhooks/razorpay`, events `payment.captured` and `order.paid`, same secret as `RAZORPAY_WEBHOOK_SECRET`.
Test one real Rs 50 payment end to end before you announce anything. The Razorpay path has not been run against live keys.

## Backups
- `BACKUP_DAILY=1` writes a consistent copy of the database to `/data/backups` every 24 hours and keeps the last 14.
- Fly also snapshots the disk daily. Both live next to the data, so also copy a backup off Fly weekly:
```
fly ssh sftp get /data/backups/<newest-file>.db
```
- Check the wallets against their history any time: `fly ssh console -C "python -m app.cli audit"`.

## Check list before real customers
- [ ] A test sign-up receives its email code from the real SMTP provider.
- [ ] A real payment credits exactly once (and a repeated callback does not credit twice).
- [ ] `python -m app.cli audit` says all wallets match their history.
- [ ] The repository is private; no `.env` or database file is committed.
- [ ] Terms, Privacy and Grievance details are reviewed by a lawyer and the contact fields above are filled in.
- [ ] Restore test: download a backup and open it locally.
