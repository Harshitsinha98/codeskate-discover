"""Pydantic schemas. These double as the JSON contract handed to the LLM."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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
