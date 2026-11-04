# Deploy BanksView on AWS Lightsail

Region: **Mumbai (ap-south-1)** so customer data stays in India. Plan: **2 GB RAM / 2 vCPU (about USD 12 a month)**. 1 GB works for normal statements but a 200-page file needs 2 GB.

## 1. Create the server
1. Lightsail > Create instance > Linux/Unix > **OS only > Ubuntu 24.04** > Mumbai > 2 GB plan.
2. Networking > attach a **static IP**. Firewall: open **22 (restrict to your own IP)**, **80**, **443**.
3. DNS: add an **A record** for `banksview.com` and `www` pointing to the static IP. HTTPS needs this before step 3.

## 2. First deploy (on the server, via SSH)
```
git clone https://github.com/ignizAI/banksview.git
cd banksview/deploy/lightsail
sudo bash setup.sh        # installs Docker, adds swap, creates .env with a fresh SECRET_KEY
nano .env                 # DOMAIN, ALLOWED_HOSTS, company details
sudo bash setup.sh        # builds and starts app + HTTPS
```
Check: `curl https://banksview.com/health`

## 3. Create the first master (admin)
```
cd ~/banksview/deploy/lightsail
docker compose exec app python -m app.cli master you@example.com "a-strong-password"
```

## 4. Update later
```
cd ~/banksview/deploy/lightsail && sudo bash update.sh
```

## 5. Backups (do this on day one)
- Data lives in `deploy/lightsail/data` (SQLite, reports, daily in-app backups, 14 kept).
- Lightsail > Snapshots > **enable automatic snapshots** (daily).
- Monthly: copy a backup off the server and test a restore.

## Notes
- Payments stay in `contact_admin` until Razorpay or UPI is configured and tested.
- Never open port 8000 publicly; only Caddy (80/443) is exposed.
