# Brag document template

Copy this to `data/inbox/brag.md` and fill it in. Everything downstream — skill
levels, fit scores, and later your tailored resumes — is built from this file.
A vague brag doc produces a vague system. This is the highest-return hour of work
in the whole project.

**The rule: every entry needs a number.** If you can't quantify it, state the
scope instead (how many users, services, teammates, requests/day).

---

## Achievements

Use this shape. One block per accomplishment, aim for 15-25 total.

### <Short headline of what you did>
- **Where:** company / project / course
- **When:** 2025-06 to 2025-11
- **Problem:** what was broken or missing, and why it mattered
- **What I did:** your specific contribution — "I", not "we"
- **Impact:** the number. Latency, cost, revenue, users, error rate, time saved.
- **Skills used:** Python, Postgres, Redis, Docker

Worked example:

### Cut checkout API latency by 85%
- **Where:** Acme Retail, payments team
- **When:** 2025-03 to 2025-05
- **Problem:** p99 checkout latency was 800ms; ~4% of carts abandoned at payment.
- **What I did:** Profiled the request path, found 3 N+1 queries, added a Redis
  read-through cache and batched the inventory lookups.
- **Impact:** p99 800ms -> 120ms. Cart abandonment 4% -> 1.6%. ~₹40L/yr recovered
  revenue by the finance team's estimate.
- **Skills used:** Python, FastAPI, Postgres, Redis, profiling

---

## Skills self-audit

Be harsh here. An inflated level produces bad fit scores, which wastes your
applications — you're the one who pays for lying to yourself.

| Skill | Level 1-5 | Years | Last used | Evidence (which achievement above?) |
|---|---|---|---|---|
| Python | 4 | 3 | 2026 | Checkout latency, internal CLI tooling |
| Kubernetes | 2 | 0.5 | 2025 | Side project only — never owned in prod |

**Levels:** 1 = read about it · 2 = side project or coursework · 3 = shipped to
production · 4 = owned a significant production system · 5 = recognised expert
(talks, OSS ownership).

Anything at level 1 with no evidence will be excluded from tailored resumes by
design. That's correct behaviour, not a bug — either build proof, or drop it.

---

## Targets

- **Role titles I want:** e.g. Backend Engineer, Platform Engineer
- **Locations:** Bengaluru, remote-India
- **Minimum salary:** ₹__ LPA
- **Notice period:** __ days
- **Hard nos:** e.g. no on-site support rotation, no <20 person companies

Mirror these into `config/targets.yaml` — that file drives the free prefilter.

---

## Baseline (fill this in BEFORE you use the system)

Without this you'll never know whether CodeSkate worked or you just got lucky.
It's also your entire pitch if this becomes a company.

- Applications sent in the last 3 months: __
- Recruiter screens / callbacks: __
- **Callback rate: __%**
- Onsites / final rounds: __
- Offers: __
