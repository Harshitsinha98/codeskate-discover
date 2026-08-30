"""Per-user LLM runtime, on the platform's own API key.

Bring-your-own-key is gone. Asking a job seeker to create an OpenAI account, add
a card and paste a key was a wall most would never climb, and if they were paying
the model provider directly there was little left to charge a subscription for.
The two models contradicted each other.

The consequence is that usage now spends the operator's money, so the guard rails
move from "a dollar ceiling the user sets" to "a run quota the plan sets". Cost is
the operator's concern; the user sees runs, not dollars.

The spend recorder still writes every call to the database. It is no longer a
budget the user controls, but it is how quotas are counted and how per-user unit
economics stay visible.
"""

from __future__ import annotations

import os
from typing import Any

from codeskate.llm import LLM
from codeskate.settings import Settings

from . import store

DEFAULT_MODELS = {
    "openai": ("gpt-5.6-luna", "gpt-5.6-terra"),
    "anthropic": ("claude-haiku-4-5", "claude-sonnet-5"),
}


class PlatformKeyMissing(RuntimeError):
    """Configuration fault, not a user error. The message is for the operator."""


class UsageRecorder:
    """Records every call against a user. Implements codeskate.llm.SpendRecorder."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    def spent(self) -> float:
        return store.total_spend(self.user_id)

    def record(self, **kw: Any) -> None:
        store.log_call(self.user_id, **kw)


def platform_provider() -> str:
    return os.getenv("LLM_PROVIDER", "openai").strip().lower()


def platform_key() -> str:
    provider = platform_provider()
    var = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    key = os.getenv(var, "").strip()
    if not key:
        raise PlatformKeyMissing(
            f"{var} is not set on the server. Agents cannot run until the operator "
            "configures the platform API key."
        )
    return key


def configured() -> bool:
    try:
        platform_key()
        return True
    except PlatformKeyMissing:
        return False


def user_llm(user_id: int) -> LLM:
    """An LLM client that bills the platform and records usage against one user.

    spend_limit_usd is set high on purpose: the real ceiling is the plan's run
    quota, checked in quota.check() before work is queued and again before each
    unit runs. Leaving a dollar guard here too means a pricing mistake or a
    pathologically expensive prompt still cannot run unbounded.
    """
    provider = platform_provider()
    cheap_default, smart_default = DEFAULT_MODELS.get(provider, DEFAULT_MODELS["openai"])

    settings = Settings(
        provider=provider,
        api_key=platform_key(),
        model_cheap=os.getenv("LLM_MODEL_CHEAP", cheap_default).strip(),
        model_smart=os.getenv("LLM_MODEL_SMART", smart_default).strip(),
        spend_limit_usd=float(os.getenv("PER_USER_HARD_USD_CEILING", "25")),
    )
    return LLM(settings, UsageRecorder(user_id))
