# Production server setup — Ubuntu 22.04

End-to-end runbook for standing up `mcc.bradfordbroadband.com` on a fresh Ubuntu 22.04 LTS droplet. Follow the sections top-to-bottom. Every command assumes you're logged in as root or prefixing with `sudo` where appropriate.

If any step fails, stop and fix it before moving on — later steps depend on earlier ones (especially Postgres → Alembic, WireGuard → overlay IPs, nginx → cert renewal, systemd → everything).

---

## 0. Prerequisites

Before touching the server:

- Droplet: Ubuntu 22.04 LTS, 2 GB RAM is plenty for Phase 1 at 700 customers.
- DNS: `A` record for `mcc.bradfordbroadband.com` pointing at the droplet's public IPv4. Let's Encrypt will not issue a cert until DNS resolves correctly.
- Inbound firewall at the DO level: allow TCP 22, TCP 443, UDP 51820.
- Your SSH public key is in `/root/.ssh/authorized_keys`.

On your laptop, confirm DNS:

```bash
dig +short mcc.bradfordbroadband.com A
```

---

## 1. Base OS hardening

```bash
apt update && apt -y full-upgrade
apt install -y ca-certificates curl gnupg lsb-release ufw git build-essential
```

### Firewall

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp     # SSH
ufw allow 80/tcp     # HTTP — Let's Encrypt HTTP-01 challenge + nginx redirect
ufw allow 443/tcp    # HTTPS
ufw allow 51820/udp  # WireGuard
ufw --force enable
ufw status
```

Port 80 is required by certbot's HTTP-01 challenge (both initial issuance and auto-renewal every 60 days). No real app traffic flows over HTTP — nginx's port 80 server block only serves Let's Encrypt challenge tokens and a 301 redirect to HTTPS.

### SSH hardening (edit `/etc/ssh/sshd_config`)

```
PasswordAuthentication no
PermitRootLogin prohibit-password
```

```bash
systemctl restart ssh
```

### Service account

The FastAPI app will never run as root.

```bash
useradd --system --create-home --home-dir /opt/cpe-cloud --shell /usr/sbin/nologin cpecloud
```

---

## 2. Python 3.11 (deadsnakes PPA)

Ubuntu 22.04 ships Python 3.10. The design targets 3.11, so add the deadsnakes PPA:

```bash
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.11 python3.11-venv python3.11-dev
python3.11 --version   # → Python 3.11.x
```

---

## 3. Postgres 15 (PGDG repo)

Jammy's default Postgres is 14; we want 15 to match the design.

```bash
install -d /etc/apt/keyrings
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg

echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list

apt update
apt install -y postgresql-15 postgresql-contrib-15
systemctl enable --now postgresql
```

Create the database + role:

```bash
# Generate a strong password and keep it handy — you'll paste it into .env in §7
DB_PASSWORD=$(openssl rand -base64 24)
echo "Postgres cpecloud password: $DB_PASSWORD"

sudo -u postgres psql <<SQL
CREATE USER cpecloud WITH PASSWORD '$DB_PASSWORD';
CREATE DATABASE cpecloud OWNER cpecloud;
\c cpecloud
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
```

(The Alembic migration in §7 enables `pg_trgm` too — the `IF NOT EXISTS` makes both calls safe. Enabling it here lets you run quick ad-hoc `ILIKE` / trigram queries during debugging.)

---

## 4. InfluxDB 2.x

```bash
install -d /etc/apt/keyrings
curl -fsSL https://repos.influxdata.com/influxdata-archive.key \
  | gpg --dearmor -o /etc/apt/keyrings/influxdata.gpg

echo "deb [signed-by=/etc/apt/keyrings/influxdata.gpg] https://repos.influxdata.com/debian stable main" \
  > /etc/apt/sources.list.d/influxdata.list

apt update
apt install -y influxdb2 influxdb2-cli
systemctl enable --now influxdb
```

One-time setup — creates the org, bucket, admin user, and admin token:

```bash
INFLUX_PASSWORD=$(openssl rand -base64 24)
echo "Influx admin password: $INFLUX_PASSWORD"

influx setup \
  --username admin \
  --password "$INFLUX_PASSWORD" \
  --org bradford \
  --bucket cpe-cloud \
  --retention 90d \
  --force

# Grab the admin token — goes into .env as INFLUX_TOKEN
influx auth list --user admin --json | jq -r '.[0].token'
```

Retention on the raw bucket is 90 days; downsampling + longer retention is a Phase 2 concern.

---

## 5. WireGuard

```bash
apt install -y wireguard wireguard-tools
install -d -m 700 /etc/wireguard
```

Generate the server keypair:

```bash
cd /etc/wireguard
umask 077
wg genkey | tee server.key | wg pubkey > server.pub
chmod 600 server.key
```

Write the initial `wg0.conf` with just the `[Interface]` stanza — peers get added dynamically by the WireGuard sync service (deliverable #3) as routers enroll:

```bash
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = 10.100.0.1/22
ListenPort = 51820
PrivateKey = $(cat /etc/wireguard/server.key)
SaveConfig = false
EOF
chmod 600 /etc/wireguard/wg0.conf

systemctl enable --now wg-quick@wg0
wg show        # should print "interface: wg0" with listening port 51820
```

Save the public key — goes into `.env` as `WG_SERVER_PUBLIC_KEY`, and into every router's peer config:

```bash
cat /etc/wireguard/server.pub
```

---

## 6. nginx + Let's Encrypt TLS

```bash
apt install -y nginx
systemctl enable --now nginx
```

Certbot via snap (the officially recommended install method — the apt package is frozen):

```bash
apt install -y snapd
snap install core && snap refresh core
snap install --classic certbot
ln -sf /snap/bin/certbot /usr/bin/certbot

certbot --nginx \
  -d mcc.bradfordbroadband.com \
  --non-interactive --agree-tos \
  -m aaron@bradfordbroadband.com \
  --redirect
```

Certbot auto-renew is enabled via a systemd timer on snap install — verify:

```bash
systemctl list-timers | grep certbot
certbot renew --dry-run
```

Now replace the certbot-generated site with the CPE-Cloud-specific reverse proxy config:

```bash
cat > /etc/nginx/sites-available/cpe-cloud <<'NGINX'
server {
    listen 443 ssl http2;
    server_name mcc.bradfordbroadband.com;

    ssl_certificate     /etc/letsencrypt/live/mcc.bradfordbroadband.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcc.bradfordbroadband.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # Telemetry payloads are tiny. Anything bigger is suspicious.
    client_max_body_size 256k;

    add_header Strict-Transport-Security "max-age=63072000" always;
    add_header X-Content-Type-Options "nosniff" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
}

server {
    listen 80;
    server_name mcc.bradfordbroadband.com;
    return 301 https://$host$request_uri;
}
NGINX

ln -sf /etc/nginx/sites-available/cpe-cloud /etc/nginx/sites-enabled/cpe-cloud
rm -f /etc/nginx/sites-enabled/default

nginx -t && systemctl reload nginx
```

---

## 7. Install the CPE Cloud app

```bash
# Put the repo at /opt/cpe-cloud. Adjust the remote to wherever Bradford hosts it.
git clone https://github.com/kb3gzo/mikrotik-cpe-cloud.git /opt/cpe-cloud
chown -R cpecloud:cpecloud /opt/cpe-cloud

# Sanity check: dotfiles (especially .env.example) made it over.
# If you copied via SCP/SFTP/GUI file manager instead of git, dotfiles are
# often silently skipped. Verify before continuing:
ls -la /opt/cpe-cloud | grep -E '\.env\.example|\.gitignore'
# If .env.example is missing, re-copy with a dotfile-preserving tool:
#   rsync -av --progress "/path/to/Mikrotik CPE Cloud/" root@host:/opt/cpe-cloud/

sudo -u cpecloud bash <<'BASH'
cd /opt/cpe-cloud
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
BASH
```

Now edit `/opt/cpe-cloud/.env` and fill in the real values you collected above. **Edit as the `cpecloud` user** so ownership is preserved — editors atomically rewrite the file, and a root-as-editor rewrite ends up root-owned, which blocks alembic and uvicorn from reading it:

```bash
sudo -u cpecloud nano /opt/cpe-cloud/.env
```

If you *did* edit as root by accident and alembic errors with `PermissionError: [Errno 13] Permission denied: '.env'`, fix the ownership + mode:

```bash
chown cpecloud:cpecloud /opt/cpe-cloud/.env
chmod 640 /opt/cpe-cloud/.env
```

At minimum you need to replace:

- `DATABASE_URL` — use the password from §3 (`postgresql+psycopg://cpecloud:YOUR_PW@127.0.0.1:5432/cpecloud`)
- `INFLUX_TOKEN` — the token from §4
- `WG_SERVER_PUBLIC_KEY` — `cat /etc/wireguard/server.pub`
- `PROVISIONING_SECRET_CURRENT` — generate with `openssl rand -base64 32`

Then run the Alembic migration (creates all 9 tables + pg_trgm + the seed rule):

```bash
sudo -u cpecloud bash -c 'cd /opt/cpe-cloud && source .venv/bin/activate && alembic upgrade head'

# Sanity check
sudo -u postgres psql cpecloud -Atc \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"
# Expected: 10  (9 app tables + alembic_version)
```

---

## 8. Systemd unit for the FastAPI service

```bash
cat > /etc/systemd/system/cpe-cloud.service <<'UNIT'
[Unit]
Description=Mikrotik CPE Cloud API
After=network-online.target postgresql.service influxdb.service
Wants=network-online.target

[Service]
Type=simple
User=cpecloud
Group=cpecloud
WorkingDirectory=/opt/cpe-cloud
EnvironmentFile=/opt/cpe-cloud/.env
ExecStart=/opt/cpe-cloud/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

# Sandboxing
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/cpe-cloud

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now cpe-cloud
systemctl status cpe-cloud --no-pager
```

Tail the logs in a second terminal:

```bash
journalctl -u cpe-cloud -f
```

---

## 9. Sudo-wrapped WireGuard sync helper

The app user can't reload WireGuard directly (root-only op). Create a minimal helper + a narrow sudoers rule. Deliverable #3 (the `app/services/wireguard.py` implementation) will shell out to this:

```bash
cat > /usr/local/sbin/cpe-cloud-wg-sync <<'SH'
#!/usr/bin/env bash
# Reload wg0 without tearing down live tunnels.
# Intended to be invoked via `sudo` by the cpecloud user.
set -euo pipefail
exec /usr/bin/wg syncconf wg0 <(/usr/bin/wg-quick strip wg0)
SH
chmod 755 /usr/local/sbin/cpe-cloud-wg-sync

cat > /etc/sudoers.d/cpe-cloud-wg <<'SUDO'
cpecloud ALL=(root) NOPASSWD: /usr/local/sbin/cpe-cloud-wg-sync
SUDO
chmod 440 /etc/sudoers.d/cpe-cloud-wg
visudo -cf /etc/sudoers.d/cpe-cloud-wg   # syntax check
```

Also grant the app write access to `wg0.conf` so it can append peer stanzas (tightly scoped):

```bash
chown root:cpecloud /etc/wireguard/wg0.conf
chmod 640 /etc/wireguard/wg0.conf
```

---

## 10. Verification

From your laptop:

```bash
# Liveness — should return {"status":"ok"}
curl -s https://mcc.bradfordbroadband.com/healthz

# OpenAPI — browser this in a web browser for the endpoint explorer
open https://mcc.bradfordbroadband.com/docs

# Stubbed endpoints return 501 until deliverables #4/#5 land
curl -sX POST https://mcc.bradfordbroadband.com/api/v1/auto-enroll
```

On the server:

```bash
# WireGuard
wg show wg0

# Postgres table count
sudo -u postgres psql cpecloud -c "\dt"

# Seeded auto-approve rule
sudo -u postgres psql cpecloud -c \
  "SELECT priority, kind, pattern, effect, enabled FROM provisioning_rules"
# Should show the '^hAP .+ - .+, .+$' identity regex

# Influx
influx bucket list --org bradford

# App
systemctl status cpe-cloud
journalctl -u cpe-cloud -n 50 --no-pager
```

---

## 11. Ongoing operations

- **Backups** — `pg_dump` on a nightly cron is enough for Phase 1. Store off-box (DO Spaces, S3, etc.).
- **OS updates** — `apt update && apt upgrade` weekly; reboot during a low-traffic window.
- **Cert renewal** — automatic via the certbot snap timer. Watch `systemctl list-timers | grep certbot`.
- **Secret rotation** — `python -m app.cli provisioning-secret rotate --grace-days 60` (deliverable #7).
- **Fetch tokens** — mint per-session with `python -m app.cli fetch-tokens mint --label "Aaron prep YYYY-MM-DD" --ttl-hours 24`.

---

## 12. Troubleshooting cheat sheet

| Symptom                                          | Check                                                          |
|---|---|
| `curl /healthz` times out                        | `systemctl status cpe-cloud nginx`, `ufw status`              |
| `alembic upgrade head` fails on `pg_trgm`        | `sudo -u postgres psql cpecloud -c "CREATE EXTENSION pg_trgm"` |
| nginx 502                                        | `journalctl -u cpe-cloud -n 50` — uvicorn probably crashed     |
| Certbot refuses to issue                         | `dig mcc.bradfordbroadband.com` — DNS not yet propagated       |
| `wg show` shows no listening port                | `systemctl status wg-quick@wg0`, check UFW UDP 51820           |
| Router can't reach server                        | `curl -vk https://mcc.bradfordbroadband.com/healthz` from router shell (`/tool fetch mode=https`) |
