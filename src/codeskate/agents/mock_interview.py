"""Agent 13 — Mock Interview.

Asks a question, takes your typed answer, scores it, and rewrites it using only
the facts you gave. Repeats. The transcript and scores are stored so you can see
whether you are actually improving between sessions.

Text, not voice. Voice needs a realtime audio pipeline plus latency work to feel
like an interview rather than a walkie-talkie, and it adds nothing to the part
that matters right now — whether your answers have structure and evidence. Voice
is a Phase 3.5 upgrade on top of this same scoring logic.
"""

from __future__ import annotations

from ..llm import LLM
from ..models import MockFeedback, MockQuestion, PrepBrief, SkillGraph
from pydantic import BaseModel


class QuestionSet(BaseModel):
    questions: list[MockQuestion]


QUESTION_SYSTEM = """You are an interviewer generating questions for a mock session.

Rules:
* Weight questions to what the job description emphasises and to the candidate's
  weak spots — the point is to find the cracks before the real interview does.
* Mix kinds appropriately for the stage: a recruiter screen is mostly behavioural
  and motivation; a technical round is mostly technical and system design.
* Ask them one at a time, standalone. No multi-part compound questions.
* Do not telegraph the answer in the question."""

FEEDBACK_SYSTEM = """You are an interviewer scoring one answer, honestly.

Scoring:
  80-100 pass       — an interviewer would move them forward on this answer
  55-79  borderline — recoverable, but visibly weaker than a strong candidate
  0-54   fail       — this answer would count against them

Rules:
1. Score what they actually said, not their potential. Do not be kind.
2. `problems`: be specific. "Rambled" is useless — "spent 40 words on background
   before naming the problem, and never stated the outcome" is useful.
3. `missing_from_answer`: facts from their own profile they should have used and
   didn't. This is usually the biggest win available.
4. `model_answer`: rewrite THEIR answer, stronger. Use only facts they stated or
   that appear in their profile. Do not invent achievements to make it sound good
   — that would teach them to lie in the real interview.
5. If the answer is empty or evasive, say so and score it accordingly."""


def generate_questions(
    llm: LLM,
    job_title: str,
    company: str,
    jd: str,
    stage: str = "screen",
    count: int = 5,
    brief: PrepBrief | None = None,
) -> list[MockQuestion]:
    weak = ", ".join(brief.weak_spots) if brief and brief.weak_spots else "not yet analysed"
    user = (
        f"STAGE: {stage}\nROLE: {job_title} at {company}\n"
        f"NUMBER OF QUESTIONS: exactly {count}\n"
        f"CANDIDATE'S KNOWN WEAK SPOTS: {weak}\n\n"
        f"=== JOB DESCRIPTION ===\n{jd[:3000]}"
    )
    result = llm.json_call(
        agent="mock_interview",
        tier="cheap",
        system=QUESTION_SYSTEM,
        user=user,
        schema=QuestionSet,
        max_tokens=1500,
    )
    return result.questions[:count]


def evaluate(
    llm: LLM,
    graph: SkillGraph,
    question: MockQuestion,
    answer: str,
    job_title: str,
    company: str,
) -> MockFeedback:
    from .skill_graph import profile_brief

    user = (
        f"ROLE: {job_title} at {company}\n\n"
        f"QUESTION [{question.kind}]: {question.question}\n\n"
        f"THEIR ANSWER:\n{answer.strip() or '(no answer given)'}\n\n"
        f"=== THEIR VERIFIED PROFILE (the only facts you may add) ===\n"
        f"{profile_brief(graph)}"
    )
    return llm.json_call(
        agent="mock_interview",
        tier="smart",
        system=FEEDBACK_SYSTEM,
        user=user,
        schema=MockFeedback,
        max_tokens=2000,
    )


def session_markdown(
    turns: list[dict], job_title: str, company: str, avg: float
) -> str:
    lines = [
        f"# Mock interview — {job_title} @ {company}",
        f"\n**Average score: {avg:.0f}/100** across {len(turns)} question(s)\n",
    ]
    for i, t in enumerate(turns, 1):
        fb = t["feedback"]
        lines += [
            f"\n## Q{i} [{t['kind']}] — scored {fb['score']}/100 ({fb['verdict']})",
            f"\n**Question:** {t['question']}",
            f"\n**Your answer:**\n\n> {t['answer'] or '(none)'}",
        ]
        if fb.get("strengths"):
            lines += ["\n**Worked:**", *(f"- {s}" for s in fb["strengths"])]
        if fb.get("problems"):
            lines += ["\n**Problems:**", *(f"- {p}" for p in fb["problems"])]
        if fb.get("missing_from_answer"):
            lines += [
                "\n**You had this evidence and didn't use it:**",
                *(f"- {m}" for m in fb["missing_from_answer"]),
            ]
        lines += ["\n**Stronger version of your answer:**\n", fb.get("model_answer", "")]
    return "\n".join(lines)
