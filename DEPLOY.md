# Deploying the hosted app

The hosted app lives in `saas/` and is a different deployment target from the
single-user `web/` app. Both drive the same agents; only persistence, auth and the
execution model differ.

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

Any managed Postgres works. Neon and Supabase both have a free tier and both
provide a **pooled** connection string — use that one. Serverless functions open
and discard connections constantly, and an unpooled endpoint will exhaust a small
instance's connection limit.

Copy the connection URI. It should look like:

```
postgresql://user:password@host/dbname?sslmode=require
```

Tables are created automatically on the first request; there is no migration step
to run for the initial deploy.

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
`APP_SECRET` | yes | Encrypts stored API keys. 32+ characters |
`CRON_SECRET` | yes | Stops anyone from calling `/api/worker` and burning function time |
`ADMIN_EMAILS` | yes | Comma-separated emails that get the owner dashboard. Unset means nobody is an admin |
`PUBLIC_BASE_URL` | yes | e.g. `https://codeskate.vercel.app`. Used to build password-reset links |
`RESEND_API_KEY` | strongly advised | Sends password-reset email. Without it, resets only reach the server log |
`MAIL_FROM` | no | Defaults to Resend's shared sender. Use your own verified domain |
`SECURE_COOKIES` | no | Defaults to on. Set `0` only for local HTTP testing |
`WORKER_BUDGET` | no | Seconds the cron worker runs. Keep below `maxDuration` |
`INLINE_WORKER_BUDGET` | no | Seconds spent draining the queue inside a user request |

### About ADMIN_EMAILS

Sign up with that address like any other user; the Admin tab then appears. The
panel shows counts, the activation funnel, queue failures and per-user progress.

It deliberately cannot show users' documents, skill graphs, generated resumes, or
decrypted API keys — only a provider name and the last four characters of a key.
That boundary is enforced in `saas/admin.py` and covered by tests. Keep it: it
limits what a compromised owner account can leak, and it makes the claim in the
privacy policy true rather than aspirational.

### About email

Password reset is the difference between an account being recoverable and a user
being locked out forever. With no `RESEND_API_KEY`, the flow still works end to
end but the link is printed to the function log, which means you have to fetch it
manually for every user. [Resend](https://resend.com) has a free tier and takes a
few minutes to set up — do it before inviting anyone other than yourself.

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
