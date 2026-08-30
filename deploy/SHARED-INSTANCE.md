# Running alongside an existing app on one instance

If the instance already serves something on ports 80 and 443, you have two options.
Read the first section before choosing — a second instance is free.

## First: check whether you even need to share

Oracle gives every tenancy **two** Always Free x86 micro VMs. If your CRM is on the
first one, the second is available at no cost, and separate instances mean no port
conflicts, no competing for 1 GB of RAM, and one app cannot take the other down.

**A second instance is the better answer unless you specifically want one box to
manage.**

Note that the Ampere A1 (ARM) allowance was reduced on 15 June 2026 to **2 OCPU and
12 GB total** across the tenancy, down from 4 and 24. Still far better than the x86
micro, and it can be split — for example 1 OCPU / 6 GB each for two instances.

## Before deciding, measure what you have

On the existing instance:

```bash
free -m
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
sudo ss -tlnp | grep -E ':(80|443)\s'
```

The last command matters most: it names the process holding 80 and 443. That is the
proxy you will be adding a virtual host to.

Rough budget on a 1 GB box: the OS takes ~250 MB, CodeSkate peaks at ~71 MB, and a
proxy is ~30 MB. So there is room for a CRM using up to roughly 400 MB. A statically
served Flutter web build is tiny; a CRM with its own database on the same box is not,
and in that case take the second instance.

## Option A — second instance (recommended)

Nothing special. Create the instance and follow the normal Oracle section in
[DEPLOY.md](../DEPLOY.md). The default `docker-compose.yml` includes Caddy and takes
80 and 443 for itself.

## Option B — share the instance

CodeSkate runs without its own proxy, listening on `127.0.0.1:8000`, and your
existing proxy routes to it.

**Use a subdomain, not a path.** Google OAuth redirect URIs are matched exactly, and
serving the app under something like `/codeskate/` would mean rewriting every
absolute path the frontend uses. A subdomain avoids all of it.

Point an A record for `app.yourdomain.com` at the instance IP. With no domain,
`<ip>.sslip.io` works but cannot be split into subdomains, which is a good reason to
use a real domain here.

```bash
cd ~/codeskate/deploy
cp .env.example .env && nano .env      # SITE_DOMAIN is unused in this mode
docker compose -f docker-compose.shared-proxy.yml up -d --build
curl -s localhost:8000/healthz          # expect {"ok":true}
```

Set `PUBLIC_BASE_URL=https://app.yourdomain.com` in `.env`, and add
`https://app.yourdomain.com/api/auth/google/callback` to your Google OAuth client.

### If the existing proxy is Caddy

Add to your Caddyfile and reload with `caddy reload` or `docker compose restart caddy`:

```caddyfile
app.yourdomain.com {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8000 {
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
}
```

If Caddy runs in a container, `127.0.0.1` refers to that container rather than the
host. Either add `extra_hosts: ["host.docker.internal:host-gateway"]` and proxy to
`host.docker.internal:8000`, or put both services on one Docker network and proxy to
`app:8000`.

### If the existing proxy is nginx

`/etc/nginx/sites-available/codeskate`, then symlink into `sites-enabled`,
`nginx -t`, `systemctl reload nginx`:

```nginx
server {
    listen 443 ssl http2;
    server_name app.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/app.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.yourdomain.com/privkey.pem;

    client_max_body_size 4M;          # resume uploads are capped at 2MB each

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        # Without this the app believes the request was plain HTTP and emits
        # http:// URLs on an https:// site.
        proxy_set_header X-Forwarded-Proto $scheme;

        # A queued job can be nudged for several seconds at a time.
        proxy_read_timeout 90s;
    }
}

server {
    listen 80;
    server_name app.yourdomain.com;
    return 301 https://$host$request_uri;
}
```

Certificate for the new subdomain:

```bash
sudo certbot --nginx -d app.yourdomain.com
```

### Memory guard rails

The shared-proxy compose file caps the app at 384 MB, well above its measured 71 MB
peak, so hitting the cap means something is wrong rather than busy. Make sure swap
exists — the setup script creates 2 GB, and on a 1 GB box running two apps it is not
optional:

```bash
swapon --show
```

## Verifying it worked

```bash
curl -s https://app.yourdomain.com/healthz          # {"ok":true}
curl -s https://app.yourdomain.com/api/config       # plans, google_configured
docker compose -f docker-compose.shared-proxy.yml logs -f app
```

Then open the site and sign in with the Google account listed in `ADMIN_EMAILS` —
the Admin tab should appear.

If sign-in fails with a redirect URI mismatch, the URI in the Google console must
match `PUBLIC_BASE_URL` exactly, including `https://` and no trailing slash.
