"""Agent implementations. One module per agent, plain functions, no framework.

Pod 1 — Intake & Profiling
  1  intake.py            Agent 1  — read resume/brag docs           (no LLM)
  2  skill_graph.py       Agent 2  — evidence-backed skill inventory
  3  gap_analysis.py      Agent 3  — what is actually blocking you

Pod 2 — Market Intelligence
  4  discovery.py         Agent 4  — live jobs from public ATS feeds  (no LLM)
  5  fit_scoring.py       Agent 5  — free prefilter + LLM scoring
  6  company_intel.py     Agent 6  — pre-interview company briefing
  7  compensation.py      Agent 7  — salary band, JD range extraction first

Pod 3 — Application Execution
  8  resume_tailoring.py  Agent 8  — per-JD rewrite + fabrication check
  9  outreach.py          Agent 9  — cover letter, recruiter DM, HM email
  10 referral.py          Agent 10 — warm intros from your own network file
  11 submission.py        Agent 11 — application packet, human submits

Pod 4 — Interview & Close
  12 interview_prep.py    Agent 12 — questions, STAR stories, weak spots
  13 mock_interview.py    Agent 13 — multi-turn practice with scoring
  14 negotiation.py       Agent 14 — offer assessment + counter script
  15 upskilling.py        Agent 15 — artefact-producing plan to close gaps

Pod 5 — Orchestration
  16 career_manager.py    Agent 16 — next-action engine (rules, free)
  17 pipeline.py          Agent 17 — state machine + follow-up   (no LLM)
  18 learning_loop.py     Agent 18 — what is actually working (SQL + LLM)

Agents 1, 4, 17 and the action engine in 16 cost nothing to run. That is
deliberate: the expensive models are reserved for work that genuinely needs
judgement.
"""
