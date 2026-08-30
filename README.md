# CodeSkate — career agents

A team of 18 AI agents that finds and wins you a better job.

Most job tools are single-shot: generate one resume, run one search. A job hunt is
a **long-running pipeline** — profile, match, apply, referral, interview, offer.
The advantage isn't a clever prompt; it's orchestration, memory, and a feedback
loop that learns which of *your* applications actually got callbacks.

All 18 agents are implemented. Single-user, CLI, runs on a laptop. No auth, no
server, no payments — deliberately absent until the matching quality is proven on
one real person.

---

## Start here

```bash
uv venv --python 3.11
uv pip install -e .

cp .env.example .env          # add your API key
codeskate doctor              # verify config + model IDs before spending

# put resume.pdf and brag.md in data/inbox/  (see docs/brag-template.md)
codeskate next                # <- the only command you need to remember
```

`codeskate next` is Agent 16. It inspects every piece of state and tells you the
one thing to do now, with the exact command to run. Everything else exists so it
has something to recommend. It costs nothing unless you pass `--brief`.

---

## The 18 agents

### Pod 1 — Intake & Profiling

| # | Agent | Command | Cost |
|---|---|---|---|
1 | Intake | `profile` | **$0** |
2 | Skill Graph | `profile` | ~$0.06 |
3 | Gap Analysis | `gaps --role '<role>'` | ~$0.03 |

Agent 2 builds an **evidence-backed** inventory: level 0-5, years, and proof for
every skill. Agent 3 reports what's actually blocking you, and for each gap the
*smallest concrete thing you could build* to make the claim truthful.

### Pod 2 — Market Intelligence

| # | Agent | Command | Cost |
|---|---|---|---|
4 | Discovery | `discover` | **$0** |
5 | Fit Scoring | `score` | ~$0.006/job |
6 | Company Intel | `intel <job>` | ~$0.02 |
7 | Compensation | `comp <job>` | ~$0.004 |

Agent 4 pulls from public ATS endpoints — **27 verified boards returned ~5,800
postings for $0**. Agent 5 runs a free rule-based prefilter that dropped **98% of
5,797 jobs** before any LLM call. Agent 7 extracts a stated salary range with regex
first, and only estimates when the posting doesn't say.

### Pod 3 — Application Execution

| # | Agent | Command | Cost |
|---|---|---|---|
8 | Resume Tailoring | `tailor <job>` | ~$0.06 |
9 | Cover Letter & Outreach | `outreach <job>` | ~$0.04 |
10 | Referral | `refer <job>` | ~$0.02/contact |
11 | Submission | `submit <job>` | ~$0.01 |

### Pod 4 — Interview & Close

| # | Agent | Command | Cost |
|---|---|---|---|
12 | Interview Prep | `prep <job>` | ~$0.10 |
13 | Mock Interview | `mock <job>` | ~$0.04/question |
14 | Negotiation | `negotiate <job> --offer '...'` | ~$0.05 |
15 | Upskilling | `upskill` | ~$0.05 |

Agent 12's most valuable output is `weak_spots` — where an interviewer will press
and find nothing. Agent 13 rewrites *your* answer using only *your* facts, so it
can't teach you to lie. Agent 15 produces milestones that end in artefacts, which
close the loop: finish one, add it to your brag doc, re-run `profile`, and the
skill becomes claimable.

### Pod 5 — Orchestration

| # | Agent | Command | Cost |
|---|---|---|---|
16 | Career Manager | `next` | **$0** (`--brief`: ~$0.001) |
17 | Pipeline & Follow-up | `pipeline`, `stage`, `followup` | **$0** |
18 | Learning Loop | `learn` | **$0** under 10 applications |

Agents 1, 4, 17 and the action engine in 16 cost nothing. The expensive models are
reserved for work that genuinely needs judgement.

---

## Three design decisions that matter

### 1. A skill without evidence does not exist

`Skill.is_claimable` requires a non-empty `evidence` list. The tailoring agent only
receives claimable skills, and its output is **checked in code** — not just in the
prompt:

```
$ codeskate tailor gh:databricks:1
FABRICATION CHECK FAILED — 4 issue(s)
  ! bullet 1: source_achievement 'Scaled platform to 90 million users'
    does not match any supplied achievement
  ! skills_line: 'Kubernetes' is not an evidence-backed skill
  ! bullet 1: number '12' does not appear in any source achievement
```

Every bullet must trace to a supplied achievement, every claimed skill must be
evidence-backed, and every number in a bullet must appear in the source facts.
`verify()` is independent of the prompt, so it still catches drift when the model
stops obeying. One fabricated line surviving to a background check ends the
product — that risk belongs in the type system, not in a paragraph of instructions.

### 2. The pipeline state machine is the spine

`applications` has one row per job you're pursuing, and every agent from Phase 1
onward reads or advances it. Illegal transitions are refused:

```
$ codeskate stage gh:databricks:1 offer
cannot go applied -> offer. Allowed: ghosted, rejected, screen, withdrawn
```

Ghosting is modelled explicitly rather than left as "no news", because a pipeline
full of permanently-open applications produces a flattering, useless funnel.

### 3. Cost control is a feature, not an afterthought

- **Spend guard** — cumulative spend is checked *before* every call, including
  between schema-retry attempts. A buggy loop stops instead of draining credits.
- **Model routing** — `tier="cheap"` for volume, `tier="smart"` for judgement.
- **Prompt caching** — scoring N jobs re-sends the same profile, so that block is
  marked cacheable (~90% off repeated input).
- **Per-agent logging** — `codeskate spend` shows real cost by agent.

Measured Phase 0 budget for testing on yourself: **under $1**, because the free
prefilter does 98% of the filtering. A full loop through all 18 agents on ~20
applications lands around **$5-8**.

Projected steady state as a product: **~$2.20/user/month**. At ₹999/mo that's ~80%
gross margin — which is why the routing habit starts now rather than at 1,000 users.

---

## Honest limitations

Three agents are narrower than the marketing version of this idea would suggest.
Each is a deliberate trade, not an unfinished feature.

**Agent 10 (Referral) does not touch LinkedIn.** It reads `config/network.yaml`,
a file you maintain. Scraping their graph breaks their terms, gets accounts
restricted, and isn't a foundation for a company. Twenty accurate contacts you
actually know beat a large scraped graph you can't legally use.

**Agent 11 (Submission) does not click submit.** It assembles a packet
(`resume.md`, `outreach.md`, `answers.md`, `checklist.md`) and stops. Auto-submitting
needs your logged-in session on each board — exactly what those terms prohibit —
and every ATS form differs, so blind field-filling silently mangles applications.
One badly targeted auto-application costs you that company for a year. The honest
product version is assisted submission inside the user's own browser with
per-application confirmation, not a background blaster.

**Agent 13 (Mock Interview) is text, not voice.** Voice needs a realtime audio
pipeline and latency work to feel like an interview rather than a walkie-talkie,
and it adds nothing to what matters now: whether your answers have structure and
evidence. Voice is an upgrade on top of this same scoring logic.

**Agents 6 and 7 are low-confidence by construction.** Company intel runs on the
job description plus model knowledge, so anything time-sensitive may be stale.
Compensation without a licensed dataset is a prior, not market data. Both schemas
force the model to state its confidence rather than sounding authoritative.

---

## Full command list

```
codeskate next [--brief]              Agent 16 — what to do now
codeskate doctor                      verify config, keys, model IDs

codeskate profile [--show]            Agents 1+2 — skill graph
codeskate gaps --role '<role>'        Agent 3
codeskate discover                    Agent 4 — free
codeskate score [--limit N]           Agent 5
codeskate report [--min-score N]      top matches
codeskate intel <job>                 Agent 6
codeskate comp <job>                  Agent 7

codeskate shortlist [--min-score N]   add to pipeline — free
codeskate tailor <job>                Agent 8 + fabrication check
codeskate outreach <job>              Agent 9
codeskate refer <job>                 Agent 10
codeskate submit <job>                Agent 11 — builds packet

codeskate prep <job> [--stage]        Agent 12
codeskate mock <job> [--count N]      Agent 13
codeskate negotiate <job> --offer '.' Agent 14
codeskate upskill [--weeks N]         Agent 15

codeskate pipeline                    Agent 17 — funnel, stale, ghosts
codeskate stage <job> <stage>         Agent 17 — advance
codeskate followup <job>              Agent 17 — free
codeskate learn                       Agent 18
codeskate spend                       cost per agent
```

`<job>` accepts a full id, a prefix, or a company/title fragment — `codeskate
tailor databricks` works if it's unambiguous.

---

## The gate before scaling any of this

After `codeskate report`, read the top 20 and answer honestly:

> **Would I actually apply to 15 of these?**

- **No** → scoring is wrong. Tune `config/targets.yaml` and the prompt in
  `agents/fit_scoring.py`. Polished applications to jobs you don't want are worth
  nothing.
- **Yes** → `codeskate shortlist` and work the pipeline.

Record your **baseline callback rate** before using the system. Without it you
can't tell whether this worked or you got lucky — and that number is the entire
pitch when this becomes a company. `codeskate learn` refuses to interpret fewer
than 10 applications, for the same reason.

Testing on yourself is sample size 1. Once your own loop runs, onboard 3-5 friends
with different profiles. That's what reveals whether you built a product or a
personal script.

---

## Layout

```
config/
  targets.yaml       roles, locations, constraints -> drives the free prefilter
  companies.yaml     27 verified ATS slugs -> your job feed
  network.yaml       your contacts -> Agent 10
data/inbox/          your resume + brag doc (gitignored)
data/out/            profile.json, matches.md, resumes/, prep/, applications/
docs/
  brag-template.md   the highest-return hour of work in the project
src/codeskate/
  settings.py        model routing, price table, spend limit
  llm.py             provider-agnostic client, caching, cost accounting
  models.py          pydantic schemas — also the LLM's JSON contract
  db.py              sqlite; `applications` is the pipeline spine
  cli.py             23 commands
  agents/            one module per agent, 18 total
```

Works with Anthropic or OpenAI via `CODESKATE_PROVIDER`. Model IDs are
env-configurable because they change often; `codeskate doctor` pings both tiers to
confirm yours are valid.

No LangChain, no vector DB, no Docker, no Postgres. Plain functions and SQLite are
correct at this size; frameworks earn their place when there are users.
