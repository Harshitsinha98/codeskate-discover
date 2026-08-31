# Deploying updates

Two ways, both safe. Pick whichever suits the moment.

## The short version

You already deployed once by hand (`git pull`, build, up). From now on you do not
have to remember those steps. Every deploy — either way below — does four things:

1. checks whether there is actually anything new (does nothing if not),
2. keeps the current version as a rollback point,
3. builds and starts the new one,
4. **health-checks it, and if it fails, puts the old one back.**

So a bad push can leave you "still on the old version", but not "down".

---

## Option A — one command (recommended for day to day)

```bash
ssh -i ~/Downloads/ssh-key-2026-08-30.key ubuntu@129.225.85.120
cd ~/codeskate && ./deploy/deploy.sh
```

That is the whole thing. It prints what it is doing and ends with either
"deployed <commit>" or a rollback message. Run it as often as you like — if
nothing changed it says so and exits.

First time only, make it executable:

```bash
chmod +x ~/codeskate/deploy/deploy.sh
```

## Option B — fully automatic on every push

With this set up, you never SSH in at all: pushing to `main` makes the instance
deploy itself, using the exact same `deploy.sh` (same rollback safety).

One-time setup — in the GitHub repo, go to
**Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|--------|-------|
| `SSH_HOST` | `129.225.85.120` |
| `SSH_USER` | `ubuntu` |
| `SSH_KEY`  | the full contents of your `ssh-key-2026-08-30.key` file — open it in a text editor and copy everything, including the `-----BEGIN-----` / `-----END-----` lines |
| `SSH_PORT` | `22` (optional) |

That is it. The workflow in `.github/workflows/deploy.yml` does the rest. Watch a
deploy run under the repo's **Actions** tab. You can also trigger one without a
commit using the **Run workflow** button there.

The key is stored encrypted by GitHub, never printed in logs, and not visible to
anyone browsing the repo. If you would rather not use it, delete the workflow
file — Option A does not depend on it.

---

## If a deploy fails

`deploy.sh` rolls back on its own. If you ever need to force the previous image
back by hand:

```bash
cd ~/codeskate/deploy
docker tag codeskate-app:previous codeskate-app:latest
docker compose up -d
```

To see why the new version was unhealthy:

```bash
cd ~/codeskate/deploy && docker compose logs --tail 50 app
```

## Wiping the job pool (rarely needed)

Only if you want to clear all fetched jobs and start the search fresh — accounts,
resumes and payments are untouched:

```bash
docker compose exec app python -m saas.reset_jobs --yes
```
