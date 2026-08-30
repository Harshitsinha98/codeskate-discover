"""Pydantic schemas. These double as the JSON contract handed to the LLM.

Grouped by the agent that produces them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Agent 2 — Skill Graph
# --------------------------------------------------------------------------- #


class Evidence(BaseModel):
    """Proof that a skill is real. No evidence -> the skill does not exist."""

    source: str = Field(description="Which role, project or cert this comes from")
    detail: str = Field(description="What was actually built or done, with metrics if available")


class Skill(BaseModel):
    name: str
    level: int = Field(ge=0, le=5, description="0=none, 3=production-capable, 5=deep expert")
    years: float = Field(ge=0, default=0.0)
    last_used_year: int | None = None
    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def is_claimable(self) -> bool:
        """Only evidence-backed skills may appear on a tailored resume."""
        return bool(self.evidence) and self.level >= 2


class Achievement(BaseModel):
    """A reusable, quantified accomplishment — the raw material for tailoring."""

    headline: str
    metric: str | None = Field(default=None, description="Quantified impact, if stated")
    skills_used: list[str] = Field(default_factory=list)
    source: str


class SkillGraph(BaseModel):
    candidate_name: str | None = None
    headline: str | None = None
    total_years_experience: float = 0.0
    skills: list[Skill] = Field(default_factory=list)
    achievements: list[Achievement] = Field(default_factory=list)
    seniority: Literal["intern", "junior", "mid", "senior", "staff", "lead"] | None = None

    def claimable_skills(self) -> list[Skill]:
        return [s for s in self.skills if s.is_claimable]


# --------------------------------------------------------------------------- #
# Agent 4 — Discovery / Agent 5 — Fit Scoring
# --------------------------------------------------------------------------- #


class Job(BaseModel):
    external_id: str
    source: str
    company: str
    title: str
    location: str | None = None
    url: str
    description: str = ""


class FitScore(BaseModel):
    score: int = Field(ge=0, le=100)
    verdict: Literal["strong", "stretch", "weak"]
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reasoning: str = Field(description="2-3 sentences, concrete, no filler")


# --------------------------------------------------------------------------- #
# Agent 3 — Gap Analysis
# --------------------------------------------------------------------------- #


class Gap(BaseModel):
    skill: str
    severity: Literal["blocking", "important", "nice_to_have"]
    why_it_matters: str
    fastest_proof: str = Field(
        description="The smallest concrete thing to build or do that would evidence this skill"
    )


class GapReport(BaseModel):
    target_role: str
    readiness_pct: int = Field(ge=0, le=100)
    verdict: str = Field(description="2-3 sentences: are they ready, and what is the one blocker")
    gaps: list[Gap] = Field(default_factory=list)
    strengths_to_lead_with: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Agent 8 — Resume Tailoring
# --------------------------------------------------------------------------- #


class ResumeBullet(BaseModel):
    text: str
    source_achievement: str = Field(
        description="Headline of the achievement this came from. Must match one that was supplied."
    )
    keywords_hit: list[str] = Field(default_factory=list)


class TailoredResume(BaseModel):
    headline: str
    summary: str
    bullets: list[ResumeBullet] = Field(default_factory=list)
    skills_line: list[str] = Field(default_factory=list)
    omitted_and_why: list[str] = Field(
        default_factory=list, description="What you left out for this role, and the reason"
    )
    fabrication_check: str = Field(
        description="State explicitly that every bullet traces to a supplied achievement"
    )


# --------------------------------------------------------------------------- #
# Agent 9 — Outreach / Agent 10 — Referral
# --------------------------------------------------------------------------- #


class OutreachPack(BaseModel):
    cover_letter: str
    recruiter_dm: str = Field(description="Under 400 characters, LinkedIn-safe")
    hm_email_subject: str
    hm_email: str
    followup_message: str = Field(description="Sent 5-7 days later if no reply")


class ReferralRequest(BaseModel):
    contact_name: str
    angle: str = Field(description="Why this person is the right asker, in one line")
    message: str = Field(description="The actual message to send. Short. Easy to say yes to.")
    what_to_attach: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Agent 6 — Company Intel / Agent 7 — Compensation
# --------------------------------------------------------------------------- #


class CompanyIntel(BaseModel):
    company: str
    what_they_do: str
    business_model: str | None = None
    likely_tech_stack: list[str] = Field(default_factory=list)
    likely_interview_process: list[str] = Field(default_factory=list)
    talking_points: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list, description="Layoffs, funding, churn signals")
    questions_to_ask_them: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"


class CompEstimate(BaseModel):
    currency: str = "INR"
    unit: str = Field(default="LPA", description="LPA for India, annual otherwise")
    base_low: float
    base_high: float
    total_low: float
    total_high: float
    recommended_ask: float
    confidence: Literal["low", "medium", "high"]
    basis: str = Field(description="What this estimate is derived from. Be honest about weakness.")


# --------------------------------------------------------------------------- #
# Agent 12 — Interview Prep / Agent 13 — Mock Interview
# --------------------------------------------------------------------------- #


class InterviewQuestion(BaseModel):
    question: str
    kind: Literal["behavioral", "technical", "system_design", "coding", "role_specific"]
    why_asked: str
    answer_outline: str


class StarStory(BaseModel):
    title: str
    situation: str
    task: str
    action: str
    result: str
    use_for: list[str] = Field(default_factory=list)


class PrepBrief(BaseModel):
    role_focus: list[str] = Field(default_factory=list)
    likely_questions: list[InterviewQuestion] = Field(default_factory=list)
    star_stories: list[StarStory] = Field(default_factory=list)
    weak_spots: list[str] = Field(
        default_factory=list, description="Where this candidate will get caught out"
    )
    questions_to_ask_them: list[str] = Field(default_factory=list)


class MockQuestion(BaseModel):
    question: str
    kind: Literal["behavioral", "technical", "system_design", "coding", "role_specific"]


class MockFeedback(BaseModel):
    score: int = Field(ge=0, le=100)
    verdict: Literal["pass", "borderline", "fail"]
    strengths: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    missing_from_answer: list[str] = Field(default_factory=list)
    model_answer: str = Field(description="A stronger version of THEIR answer, same facts")


# --------------------------------------------------------------------------- #
# Agent 14 — Negotiation / Agent 15 — Upskilling
# --------------------------------------------------------------------------- #


class NegotiationPlan(BaseModel):
    assessment: str = Field(description="Is this offer good, fair or weak, and why")
    target: str = Field(description="The specific number/terms to aim for")
    leverage: list[str] = Field(default_factory=list)
    counter_script: str = Field(description="Word-for-word what to say or write")
    concessions: list[str] = Field(default_factory=list, description="What to trade if pushed")
    walk_away: str
    risks: list[str] = Field(default_factory=list)


class Milestone(BaseModel):
    week: int
    focus: str
    deliverable: str
    proof: str = Field(description="The artefact that becomes evidence in the skill graph")


class UpskillPlan(BaseModel):
    target_role: str
    weeks: int
    milestones: list[Milestone] = Field(default_factory=list)
    resume_lines_unlocked: list[str] = Field(
        default_factory=list, description="Bullets that become truthfully claimable on completion"
    )


# --------------------------------------------------------------------------- #
# Agent 18 — Learning Loop
# --------------------------------------------------------------------------- #


class LearningInsight(BaseModel):
    sample_size: int
    what_worked: list[str] = Field(default_factory=list)
    what_failed: list[str] = Field(default_factory=list)
    recommended_changes: list[str] = Field(
        default_factory=list, description="Concrete config or prompt changes to make next"
    )
    confidence_warning: str = Field(
        description="State plainly how little can be concluded at this sample size"
    )
