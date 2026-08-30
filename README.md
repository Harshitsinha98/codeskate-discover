# CodeSkate

A team of AI agents that finds and wins you a better job.

Most job tools are single-shot: generate one resume, run one search. But a job
hunt is a **long-running pipeline** — profile, match, apply, referral, interview,
offer. The advantage isn't a clever prompt; it's orchestration, memory, and a
feedback loop that learns which of *your* applications actually got callbacks.

**Status: Phase 0.** Four of the eighteen planned agents. Single-user, CLI only,
runs on a laptop. No auth, no server, no payments — those are deliberately absent
until the matching quality is proven.

---

## Quickstart

```bash
uv venv --python 3.11
uv pip install -e .

cp .env.example .env          # add your API key
codeskate doctor              # verify config + model IDs before spending

# drop your resume.pdf and brag.md into data/inbox/ first
codeskate profile             # Agents 1+2  -> evidence-backed skill graph
codeskate discover            # Agent 4     -> live jobs, $0
codeskate score --limit 40    # Agent 5     -> prefilter free, then LLM
codeskate report              # top matches with reasoning
codeskate spend               # what you actually spent, per agent
```

### What to put in `data/inbox/`

| File | Why it matters |
|---|---|
`resume.pdf` | Your master resume. Long is fine — trimming happens later. |
`brag.md` | Every achievement **with numbers**. "Optimised an API" is worthless; "cut p99 from 800ms to 120ms across 3 services" is the raw material the whole system runs on. |

`data/inbox/` and `data/*.db` are gitignored. Your personal data never leaves
your machine except as API calls.

---

## The four agents in this phase

| # | Agent | Cost | What it does |
|---|---|---|---|
1 | **Intake** (`agents/intake.py`) | $0 | Reads PDFs and markdown into one text bundle. No LLM. |
2 | **Skill Graph** (`agents/skill_graph.py`) | ~$0.06/run | Builds a structured skill inventory: level 0-5, years, and **evidence** for every skill. |
4 | **Discovery** (`agents/discovery.py`) | $0 | Pulls live postings from public ATS boards. |
5 | **Fit Scoring** (`agents/fit_scoring.py`) | ~$0.006/job | Free rule-based prefilter, then LLM scores survivors with reasoning. |

*(3, and 6-18 are in the roadmap below.)*

### Evidence is the core constraint

A skill only exists if there's proof of it. `Skill.is_claimable` requires a
non-empty `evidence` list, and the extraction prompt is explicitly forbidden from
inferring or upgrading skills.

This isn't a nicety. The moment a tailoring agent invents experience, one failed
background check destroys the product's credibility. The constraint has to live
in the data model, not in a prompt you might later rewrite.

### Job data without scraping

Every modern ATS publishes a keyless public JSON endpoint:

```
Greenhouse  boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
Lever       api.lever.co/v0/postings/{slug}?mode=json
Ashby       api.ashbyhq.com/posting-api/job-board/{slug}
```

Verified live on 2026-08-30, the 27 boards in `config/companies.yaml` return
**~5,800 postings for $0**. No scraping, no ToS risk, no rate-limit games. This
matters beyond convenience: aggressive scraping of the big job boards gets you
banned, and a compliance retrofit is far more expensive than designing around it.

Growing `companies.yaml` is the highest-leverage thing you can do. 300 slugs of
companies you'd actually join beats 5,000 jobs you don't want.

---

## Cost control is a first-class feature

Three mechanisms, all active from day one:

1. **Spend guard.** Every call checks cumulative spend against
   `CODESKATE_SPEND_LIMIT_USD` *before* firing, including between schema-retry
   attempts. A buggy loop stops instead of draining your credits.
2. **Model routing.** `tier="cheap"` for volume (scoring, extraction),
   `tier="smart"` for quality-critical work (skill graph, tailoring).
3. **Prompt caching.** Scoring N jobs re-sends the same profile every time, so
   that block is marked cacheable — roughly 90% off repeated input.

`codeskate spend` shows real per-agent cost. On the sample run the prefilter
dropped **98% of 5,797 jobs for free**, leaving 117 for the LLM.

Measured Phase 0 budget for testing on yourself: **~$8**, with $20 as a
comfortable ceiling. Set a hard limit in your provider console too — belt and
braces.

Projected steady-state cost once this is a product: **~$2.20/user/month**
(scoring + tailoring + outreach + prep, with caching and batching). At ₹999/mo
that's ~80% gross margin, which is why model routing is a habit worth forming
now rather than at 1,000 users.

---

## Roadmap — 18 agents, 5 pods

**Pod 1 — Intake & Profiling**
1. Intake ✅ · 2. Skill Graph ✅ · 3. Gap Analysis

**Pod 2 — Market Intelligence**
4. Discovery ✅ · 5. Fit Scoring ✅ · 6. Company Intel · 7. Compensation

**Pod 3 — Application Execution**
8. Resume Tailoring · 9. Cover Letter & Outreach · 10. Referral · 11. Submission

**Pod 4 — Interview & Close**
12. Interview Prep · 13. Mock Interview · 14. Negotiation · 15. Upskilling

**Pod 5 — Orchestration**
16. Career Manager (the only agent the user talks to) · 17. Pipeline & Follow-up
· 18. Learning Loop

Ship order: Phase 1 adds 8, 17, 16. Phase 2 adds 10, 9, 11 — referrals move
interview rate the most. Phase 3 adds 12, 13, 6. Phase 4 adds 14, 15, 18.

---

## The gate before building anything else

After `codeskate report`, read the top 20 and answer honestly:

> **Would I actually apply to 15 of these?**

- **No** → the scoring is wrong. Tune `config/targets.yaml` and the prompt in
  `agents/fit_scoring.py`. Building a tailoring agent on top of bad matching just
  produces polished applications to jobs you don't want.
- **Yes** → matching works. Move to Agent 8.

Then record your **baseline callback rate** before using the system. Without it
you cannot tell whether this works or whether you got lucky — and that number is
also the entire pitch when this becomes a company.

One more caution: testing on yourself is sample size 1. Once your own loop runs,
onboard 3-5 friends with different profiles. That's what reveals whether you built
a product or a personal script.

---

## Layout

```
config/
  targets.yaml       roles, locations, hard constraints -> drives the free prefilter
  companies.yaml     ATS slugs -> your job feed
data/inbox/          your resume + brag doc (gitignored)
data/out/            profile.json, matches.md
src/codeskate/
  settings.py        model routing, price table, spend limit
  llm.py             provider-agnostic client, caching, cost accounting
  models.py          pydantic schemas — also the LLM's JSON contract
  db.py              sqlite: jobs, profile, fit_scores, llm_calls
  cli.py             typer commands
  agents/            one module per agent
```

Works with Anthropic or OpenAI — set `CODESKATE_PROVIDER`. Model IDs are
env-configurable because they change often; `codeskate doctor` pings both tiers
to confirm yours are valid.

No LangChain, no vector DB, no Docker, no Postgres. Plain functions and SQLite
are the right call at this size; frameworks earn their place at Phase 2.
