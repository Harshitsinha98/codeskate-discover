# Go live — step by step

Written for the specific case of an Oracle Cloud Always Free instance that already
runs another service (SNS-ADS-ERP's `whatsapp-backend` on port 3001) behind a
reverse proxy holding 80 and 443.

Nothing is deployed to Vercel. Everything runs on the Oracle instance, at no cost.
Vercel's Hobby plan forbids commercial use, so charging for the product there would
require Pro at $20/month, and splitting the deployment across two hosts buys nothing
at this scale.

Total time: about 45 minutes, most of it waiting for a build.

---

## Phase 1 — Database (browser, 5 min)

Postgres must not run on a 1 GB instance alongside two apps. Neon's free tier
removes the problem.

1. Sign up at <https://neon.com> and create a project. Region: **Singapore** or
   **Mumbai** if offered — closest to Indian users.
2. On the dashboard, click **Connect** and pick the **Pooled connection** string.
   Serverless-style reconnects will exhaust a small instance otherwise.
3. Copy it. It looks like:

```
postgresql://user:pass@ep-xxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
```

Keep it somewhere for Phase 5. Tables are created automatically on first boot —
there is no migration to run.

---

## Phase 2 — Google sign-in (browser, 8 min)

Decide the hostname first, because the redirect URI must match it exactly.

If you own a domain, use a subdomain such as `app.yourdomain.com` and point an **A
record** at `129.225.113.130`. Without a domain, `129.225.113.130.sslip.io` resolves
to that IP and works with Let's Encrypt — but it cannot be split into subdomains, and
the existing service already occupies the root, so a real domain is worth the ₹800/yr
here.

Then, at <https://console.cloud.google.com>:

1. Create a project (or reuse one).
2. **APIs & Services → OAuth consent screen**: External, fill in the app name and
   your email, save. It can stay in *Testing* mode while only you use it — add your
   own Gmail under **Test users**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   - Application type: **Web application**
   - Authorised redirect URI, exactly this and nothing else:

```
https://app.yourdomain.com/api/auth/google/callback
```

4. Copy the **Client ID** and **Client secret**.

A trailing slash, `http` instead of `https`, or a different host will fail with
`redirect_uri_mismatch`. It is the single most common problem at this step.

---

## Phase 3 — Open the ports in the Oracle console (browser, 3 min)

Oracle blocks inbound traffic in **two** places. This is the cloud half; the script
in Phase 4 handles the instance half. Missing either means Let's Encrypt cannot
reach the box, no certificate is issued, and it looks exactly like a broken app.

**Networking → Virtual Cloud Networks →** your VCN **→** the subnet **→ Security
List → Add Ingress Rules:**

| Source CIDR | IP Protocol | Destination Port |
|---|---|---|
`0.0.0.0/0` | TCP | `80` |
`0.0.0.0/0` | TCP | `443` |

If your existing service is already reachable over HTTPS, these rules exist and you
can skip this phase.

---

## Phase 4 — Prepare the instance (SSH, 10 min)

```bash
ssh ubuntu@129.225.113.130
```

Check what is already running, so nothing is a surprise:

```bash
free -m
sudo ss -tlnp | grep -E ':(80|443|3001)\s'
docker ps --format 'table {{.Names}}\t{{.Ports}}'
```

Note which process holds 80/443 — nginx or caddy. Phase 6 depends on the answer.

Then bootstrap:

```bash
curl -fsSL https://raw.githubusercontent.com/Harshitsinha98/codeskate-discover/main/deploy/setup-oracle.sh | bash
```

This installs Docker, creates 2 GB of swap, opens 80/443 in the instance firewall
and persists the rule, and clones the repo to `~/codeskate`. Re-running it is safe.

If it adds you to the `docker` group, apply that without logging out:

```bash
newgrp docker
```

Confirm swap exists. On a 1 GB box running two apps this is load-bearing, not
precautionary — the image build is the peak memory moment:

```bash
swapon --show          # expect a 2G entry
```

### Preflight — run this before building

```bash
bash ~/codeskate/deploy/preflight.sh
```

Read-only apart from writing a baseline and copying your proxy config. It checks
there is enough memory to build without endangering the existing service, records
what is running and how it responds so you can compare afterwards, and backs up
`/etc/nginx` and `/etc/caddy`.

It exits non-zero and tells you to stop if headroom is insufficient. Take that
seriously: the Linux OOM killer does not kill the newest process, it kills the one
with the worst badness score — usually the largest. On a 1 GB box that is more
likely to be the existing app than the new one.

---

## Phase 5 — Configuration (SSH, 7 min)

```bash
cd ~/codeskate/deploy
cp -n .env.example .env
chmod 600 .env
nano .env
```

Fill in these. Everything else can keep its default:

```ini
PUBLIC_BASE_URL=https://app.yourdomain.com
DATABASE_URL=postgresql://...pooler...neon.tech/neondb?sslmode=require
GOOGLE_CLIENT_ID=<from Phase 2>
GOOGLE_CLIENT_SECRET=<from Phase 2>
OPENAI_API_KEY=<your own key — users never supply one>
ADMIN_EMAILS=you@gmail.com
```

`ADMIN_EMAILS` is what gives you the Admin tab. Use the same Gmail you sign in with.

`SITE_DOMAIN` is unused in shared-proxy mode; leave it.

Rotate the OpenAI key if it has ever been pasted into a chat window, a commit, or a
screenshot.

---

## Phase 6 — Start it (SSH, 8 min)

Because the existing proxy owns 80 and 443, use the shared-proxy compose file. It
starts no Caddy of its own and binds the app to `127.0.0.1:8000`, so it is not
reachable from the internet except through the proxy.

Build with a memory cap first, so the build itself cannot starve the existing
service, then start:

```bash
cd ~/codeskate/deploy
docker compose -f docker-compose.shared-proxy.yml build --memory 400m
docker compose -f docker-compose.shared-proxy.yml up -d
```

Do it at a quiet hour. The build is the only moment in this process that puts real
pressure on memory. First build takes 5-10 minutes on a micro instance. Then:

```bash
docker compose -f docker-compose.shared-proxy.yml logs -f app
# look for: "in-process queue worker started"
# Ctrl-C to stop following

curl -s localhost:8000/healthz          # {"ok":true}
curl -s localhost:8000/api/config       # plans, google_configured: true
```

If `google_configured` is `false`, the client ID or secret did not load — check
`.env` and restart with `docker compose -f docker-compose.shared-proxy.yml restart app`.

Confirm the other service is unaffected:

```bash
curl -s localhost:3001/health || curl -si localhost:3001 | head -1
free -m                                 # expect ~450-500 MB used
```

---

## Phase 7 — Route the subdomain (SSH, 5 min)

### If nginx holds 80/443

```bash
sudo tee /etc/nginx/sites-available/codeskate >/dev/null <<'EOF'
server {
    listen 80;
    server_name app.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name app.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/app.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.yourdomain.com/privkey.pem;

    client_max_body_size 4M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 90s;
    }
}
EOF

sudo sed -i 's/app\.yourdomain\.com/YOUR_ACTUAL_HOST/g' /etc/nginx/sites-available/codeskate
sudo ln -sf /etc/nginx/sites-available/codeskate /etc/nginx/sites-enabled/
```

Get the certificate first — nginx will not start with a config pointing at a
certificate that does not exist yet:

```bash
sudo certbot --nginx -d app.yourdomain.com
sudo nginx -t && sudo systemctl reload nginx
```

`X-Forwarded-Proto` is not optional. Without it the app treats every request as
plain HTTP and emits `http://` URLs on an `https://` site, which breaks the OAuth
round trip.

### If Caddy holds 80/443

```bash
sudo tee -a /etc/caddy/Caddyfile >/dev/null <<'EOF'

app.yourdomain.com {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8000 {
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
}
EOF

sudo systemctl reload caddy
```

Certificates are automatic. If Caddy itself runs in a container, `127.0.0.1` is that
container rather than the host — proxy to `host.docker.internal:8000` and add
`extra_hosts: ["host.docker.internal:host-gateway"]` to its compose service.

---

## Phase 8 — Verify (3 min)

```bash
curl -s https://app.yourdomain.com/healthz     # {"ok":true}
curl -s https://app.yourdomain.com/api/config  # google_configured: true
```

Then in a browser:

1. Open `https://app.yourdomain.com`
2. **Continue with Google**, using the address in `ADMIN_EMAILS`
3. The **Admin** tab should appear in the header
4. Upload a resume and a brag document, then work through steps 1-4

Free plan gives 40 agent runs a month and locks tailoring, outreach, interview prep,
company intel and salary bands. To unlock everything for yourself without paying,
promote your own account once:

```bash
cd ~/codeskate/deploy
docker compose -f docker-compose.shared-proxy.yml exec app python -c "
from saas import store
u = store.user_by_email('you@gmail.com')
print(store.extend_plan(u['id'], 'pro', 12))
"
```

---

## Phase 9 — Payments, later

Skip this until you have used the product yourself. It runs fine without payments;
only the upgrade button is inert.

1. Create a Razorpay account and complete KYC.
2. **Settings → API Keys** → generate, copy key ID and secret.
3. Add a webhook at `https://app.yourdomain.com/api/billing/webhook`, event
   `payment.captured`, and copy the webhook secret.
4. Add `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` to
   `.env`, then `docker compose -f docker-compose.shared-proxy.yml up -d`.

---

## Rollback

Nothing here shares state with the existing app: separate directory, separate port,
separate database. So undoing it is genuinely clean.

```bash
cd ~/codeskate/deploy
docker compose -f docker-compose.shared-proxy.yml down          # stop and remove
docker image rm codeskate-app 2>/dev/null                        # reclaim the disk
```

If the proxy was edited and something is wrong, restore the backup preflight made:

```bash
ls -d ~/codeskate-proxy-backup-*                                 # pick the timestamp
sudo cp -a ~/codeskate-proxy-backup-<stamp>/nginx /etc/
sudo nginx -t && sudo systemctl reload nginx                     # test BEFORE reloading
```

`nginx -t` before every reload is the habit that matters. A reload with a broken
config takes down the site that was already working.

For Caddy, `caddy validate --config /etc/caddy/Caddyfile` plays the same role.

To remove it entirely, delete `~/codeskate` and the swap file if you added it —
though the swap is worth keeping regardless, since it protects the existing app too.

## Operating it

```bash
cd ~/codeskate/deploy
C="docker compose -f docker-compose.shared-proxy.yml"

$C logs -f app                    # follow logs
$C restart app                    # restart
$C down && $C up -d --build       # pull code changes and rebuild
git -C ~/codeskate pull           # get the latest first

docker stats --no-stream          # memory in use
free -m
```

### Health checks worth doing weekly

- **Admin tab → activation funnel.** Where users stop is the only number that says
  what to fix next.
- **Admin tab → recent failures.** Queue errors surface here with their messages.
- **OpenAI usage dashboard.** Model usage bills you now. `GLOBAL_DAILY_RUN_CAP`
  (default 5000 runs/day) is the circuit breaker, and per-user quotas sit in front
  of it, but check the real bill for the first few weeks.

### One security item

The instance's public IP and the `ubuntu` username are in a public repository's
workflow file. Key-based authentication means that is not a vulnerability, but it
does make the host a scanning target:

```bash
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl reload ssh
sudo apt-get install -y fail2ban && sudo systemctl enable --now fail2ban
```

---

## If something breaks

| Symptom | Cause |
|---|---|
`redirect_uri_mismatch` from Google | The URI in the console must match `PUBLIC_BASE_URL` character for character — `https`, no trailing slash, same host |
Certificate never issues | Port 80 blocked. Both the Oracle security list *and* the instance firewall must allow it |
Signed in, then immediately signed out | `SECURE_COOKIES=1` over plain HTTP, or the proxy is not sending `X-Forwarded-Proto` |
`prepared statement` errors | A pooler without `prepare_threshold=None`. Already handled in `saas/engine.py` — do not remove it |
Container killed during build | Swap missing. `swapon --show` |
Jobs stay queued | The worker did not start. Look for "in-process queue worker started" in the logs |
`google_configured: false` | Client ID or secret not loaded from `.env` |

Postgres has not been exercised against a live instance from the development
sandbox, so the first boot is the first real test of that path. Send the exact error
text if anything fails there.
