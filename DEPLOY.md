# Deploying the hosted app

The hosted app lives in `saas/` and is a different deployment target from the
single-user `web/` app. Both drive the same agents; only persistence, auth and the
execution model differ.

## Where to deploy — read this first

**Recommendation: Railway (or Render), not Vercel.** Both are set up in this repo,
but the numbers are one-sided at launch.

| | Railway / Render | Vercel |
|---|---|---|
Cost to charge money | **~$5/month** | **$20/month** — Hobby forbids commercial use |
Queue worker | In-process thread, drains continuously | External cron |
Cron frequency | n/a | **Hobby: once per day.** Pro: per minute |
Long jobs | No timeout | 60s per unit on Hobby |
Setup | Point at the Dockerfile | Import repo, set env vars |

Two facts decide it:

1. **Vercel's Hobby plan is non-commercial.** Charging ₹999 on it breaks their
   terms, so a paid product needs Pro at $20/month before the first customer.
   Railway's Hobby plan is $5/month, and a Python service running continuously
   consumes well under that in credits.
2. **Vercel Hobby caps cron at once per day.** An hourly schedule fails at deploy
   time with "Hobby accounts are limited to daily cron jobs", so `vercel.json`
   here uses a daily schedule. That means a user who starts a long job and closes
   the tab could wait up to 24 hours for it to finish. On a persistent host the
   in-process worker drains the queue every few seconds and the problem disappears.

Vercel becomes the better answer later — put the frontend on its CDN and keep the
API on a persistent host — but not for a first paid launch.

### Railway (recommended)

1. Create a Postgres database (see below) and collect the secrets.
2. On [railway.com](https://railway.com): **New Project → Deploy from GitHub repo**.
   Railway detects the `Dockerfile` on its own.
3. Add the environment variables from the table below. `WORKER_IN_PROCESS=1` is
   already baked into the image, so no scheduler is needed.
4. Settings → Networking → **Generate Domain**, then set `PUBLIC_BASE_URL` to it
   and add `<domain>/api/auth/google/callback` to your Google OAuth client.

### Render

Same image, plus `render.yaml` as a blueprint: **New → Blueprint**, point it at the
repo, then fill in the variables marked `sync: false`. Use the Starter plan rather
than Free — free instances sleep, which stalls the queue worker.

### Vercel, if you insist

`vercel.json`, `api/index.py` and `requirements.txt` are all present and correct.
Import the repo, add the same variables plus `CRON_SECRET`, and accept the daily
cron. Do not charge for it on Hobby.

---

## Why the single-user app could not just be deployed

Three things had to change before Vercel was viable, and two of them were not
optional:

1. **SQLite had to go.** Vercel's filesystem is read-only apart from an ephemeral
   `/tmp`, and concurrent function instances do not share storage, so every user's
   data would vanish silently. Postgres is now the store.
2. **The in-memory task registry had to go.** Progress lived in a Python dict on
   one instance. Serverless requests land on whichever instance is available, so
   polling would return "unknown job" at random. Job state is now a table.
3. **Long work had to be chunked.** Discovery took 2-3 minutes as one operation,
   against a 60s ceiling on Hobby. It is now one unit per company board, with
   progress committed after each unit — a killed function loses one unit, and the
   next tick resumes.

## 1. Create a Postgres database

Any managed Postgres works — the code is plain SQLAlchemy Core and has no opinion
about the host. **Use the pooled connection string**, whichever provider you pick:
serverless functions are created and discarded constantly, and each one opening a
direct connection will exhaust a small instance's connection limit.

Tables are created automatically on the first request; there is no migration step
for the initial deploy.

### Neon or Supabase

Both are fine. The differences that actually matter here:

| | Neon | Supabase |
|---|---|---|
Idle behaviour | Scales to zero after ~5 min, **wakes automatically** in well under a second | Free projects **pause after 7 days idle** and stay down until restored by hand from the dashboard |
Also includes | Postgres (auth/storage newer, in beta) | Auth, Storage, Realtime, Edge Functions |
Free tier | 0.5 GB | 500 MB, 2 active projects |

The pause behaviour sounds like a problem for Supabase and mostly is not, because
`vercel.json` already runs the queue worker hourly — that traffic keeps the project
from ever reaching seven idle days. Worth knowing rather than worrying about, but
if the cron is ever removed, Supabase will pause and need a manual restore.

**Supabase:** Connect → *Transaction pooler*, port **6543**. The username looks
like `postgres.<project-ref>`.

**Neon:** copy the pooled connection string from the dashboard.

### One gotcha, already handled in the code

Transaction-mode poolers — Supabase's Supavisor on 6543, PgBouncer in transaction
mode — do not support server-side prepared statements, and psycopg3 starts using
them automatically after the fifth execution of a query. This app runs the same
session lookup on every request, so that threshold is crossed within seconds and
the failures would appear only once traffic arrived.

`saas/engine.py` therefore passes `prepare_threshold=None` unconditionally. Nothing
to configure — but do not remove it, and if you swap the data layer for an ORM of
your own, check how that ORM handles prepared statements first.

### Supabase Auth — not used, deliberately

Supabase ships an auth product that also does Google sign-in. This project does not
use it. The Google OAuth flow here is about a hundred lines, is tested, and keeps
sign-in portable across database providers. Swapping in Supabase Auth would replace
working, covered code with a vendor dependency for no functional gain.

That calculation changes if magic links, phone sign-in or a social provider matrix
are ever wanted — at that point Supabase Auth earns its lock-in.

## 2. Generate the secrets

```bash
python -c "import secrets; print('APP_SECRET =', secrets.token_urlsafe(48))"
python -c "import secrets; print('CRON_SECRET =', secrets.token_urlsafe(32))"
```

`APP_SECRET` encrypts users' stored API keys. It is the only thing between a
database dump and usable credentials, so it belongs in the platform's environment
variables and nowhere else. Changing it invalidates every stored key — which is
the correct behaviour if it ever leaks, but means users must re-enter theirs.

## 3. Deploy

```bash
npm i -g vercel
vercel link
vercel env add DATABASE_URL production
vercel env add APP_SECRET production
vercel env add CRON_SECRET production
vercel --prod
```

Or import the repository in the Vercel dashboard and add the same variables under
Settings → Environment Variables.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
`DATABASE_URL` | yes | Pooled Postgres URI |
`APP_SECRET` | yes | Signing secret. 32+ characters |
`CRON_SECRET` | yes | Stops anyone calling `/api/worker` and burning function time |
`PUBLIC_BASE_URL` | yes | e.g. `https://codeskate.vercel.app`. Used for the OAuth callback |
`GOOGLE_CLIENT_ID` | yes | Google OAuth client |
`GOOGLE_CLIENT_SECRET` | yes | Google OAuth client secret |
`OPENAI_API_KEY` | yes | **The platform's own key.** Users do not supply one |
`ADMIN_EMAILS` | yes | Comma-separated emails that get the owner dashboard |
`RAZORPAY_KEY_ID` | for payments | From the Razorpay dashboard |
`RAZORPAY_KEY_SECRET` | for payments | From the Razorpay dashboard |
`RAZORPAY_WEBHOOK_SECRET` | for payments | Set when creating the webhook |
`LLM_PROVIDER` | no | `openai` (default) or `anthropic` |
`LLM_MODEL_CHEAP` / `LLM_MODEL_SMART` | no | Override the routed models |
`GLOBAL_DAILY_RUN_CAP` | no | Circuit breaker across all accounts. Default 5000 |
`PER_USER_HARD_USD_CEILING` | no | Backstop dollar guard per account. Default 25 |
`SECURE_COOKIES` | no | Defaults to on. Set `0` only for local HTTP testing |
`WORKER_BUDGET` | no | Seconds the cron worker runs. Keep below `maxDuration` |

### Google sign-in setup

In Google Cloud Console: create a project, then **APIs & Services → Credentials →
Create OAuth client ID → Web application**. Add an authorised redirect URI of
exactly `https://<your-domain>/api/auth/google/callback`. Copy the client ID and
secret into the variables above.

Google is the only sign-in method. That removed passwords, hashing, reset tokens
and reset email from the system — a large amount of security-sensitive code that no
longer has to be correct.

### The platform API key, and why quotas matter

Users do **not** bring their own key. Asking a job seeker to create an OpenAI
account and paste a key is a wall most will not climb, and if they are paying the
model provider directly there is little left to charge a subscription for.

The consequence is that usage spends **your** money, so quotas are the cost
control, not a nicety:

- Free: 40 agent runs a month, and the expensive agents (tailoring, outreach,
  interview prep, company intel, salary bands) are locked.
- Pro (₹999/month): 800 runs, everything unlocked.
- A run averages roughly $0.004, so Pro caps one account near $3.20/month.
- `GLOBAL_DAILY_RUN_CAP` sits behind the per-user limits as a circuit breaker for
  a bad deploy or many accounts misbehaving at once.

Quotas are checked before a job is queued *and* again before each unit runs, so a
long job cannot sail past the ceiling once started.

### Razorpay setup

Razorpay rather than Stripe because the users are in India: UPI and netbanking are
what people actually pay with, and onboarding an Indian entity is far less
friction.

1. Create an account and complete KYC.
2. Copy the key ID and secret from **Settings → API Keys**.
3. Add a webhook pointing at `https://<your-domain>/api/billing/webhook`,
   subscribed to `payment.captured`, and copy the webhook secret.

This is **buy-a-month**, not auto-debit. Order verification is a single
well-defined HMAC check that is hard to get wrong; Razorpay Subscriptions need
mandate handling that is easy to get subtly wrong and cannot be verified without a
live merchant account. Auto-renewal is the next step once real payments flow.

Both the browser redirect and the webhook mark a payment good, and both are
idempotent through a unique constraint on the payment id — a user closing the tab
mid-redirect must not lose a month they paid for.

## 4. Know what the plan limits mean

`vercel.json` sets `maxDuration` to 60s, the Hobby ceiling. On Pro this can go
considerably higher, which makes each worker tick drain more units per invocation.

**Cron frequency is the real constraint.** The schedule here is hourly, because
Hobby restricts how often crons may run. That would make discovery painfully slow
on its own, so the browser also nudges the queue: while a job is open, the UI calls
`/api/jobs/{id}/resume`, which drains a few more units per poll. Cron is the
backstop for jobs whose owner closed the tab, not the primary driver.

If you move to Pro, tighten the schedule to `* * * * *` and long jobs finish
without the tab open.

One more thing worth knowing before this is a business: Vercel's Hobby plan is for
non-commercial use. Charging for this means Pro.

## 5. After the first deploy

1. Open the URL and create an account.
2. Settings → paste your OpenAI or Anthropic API key. It is encrypted before
   storage and only its last four characters are ever shown back.
3. Settings → set a spend limit. Start low, around $5.
4. Upload your resume and brag document, then work through the four steps.

## Cost

Users bring their own key, so their model usage bills their account, not yours.
That is the responsible default at launch: a single enthusiastic user cannot
generate a surprise invoice, and the per-user spend guard caps each account's
exposure regardless.

Your own costs are the Postgres instance and Vercel — both free to start.

## Running the hosted app locally

```bash
export DATABASE_URL="sqlite:///./data/saas.db"   # Postgres in production, not this
export APP_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export SECURE_COOKIES=0                          # cookies over plain HTTP
uvicorn saas.app:app --reload --port 8000
```

SQLite is supported locally only so the suite runs without a server. It must not
be used for the deployed app, for the reasons in the first section.
