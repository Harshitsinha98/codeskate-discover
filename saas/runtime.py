"""Per-user LLM runtime.

Builds an LLM client bound to one user: their decrypted key, their chosen models,
their spend ceiling, and cost recorded against their row. The spend guard from the
single-user tool carries over unchanged — it just reads and writes Postgres now,
through the recorder protocol rather than a SQLite connection.

Per-user limits matter more in a hosted product than locally. One user's runaway
loop must not be able to exhaust anyone else's budget, and since keys are BYOK the
blast radius of a bug lands on the account that triggered it.
"""

from __future__ import annotations

from typing import Any

from codeskate.llm import LLM, SpendRecorder
from codeskate.settings import Settings

from . import crypto, store


class PostgresRecorder:
    """Records spend against one user. Implements codeskate.llm.SpendRecorder."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    def spent(self) -> float:
        return store.total_spend(self.user_id)

    def record(self, **kw: Any) -> None:
        store.log_call(self.user_id, **kw)


class MissingKey(RuntimeError):
    pass


DEFAULT_MODELS = {
    "openai": ("gpt-5.6-luna", "gpt-5.6-terra"),
    "anthropic": ("claude-haiku-4-5", "claude-sonnet-5"),
}


def user_llm(user_id: int) -> LLM:
    """Construct the client for one user, or explain what is missing."""
    record = store.get_user_key(user_id)
    if not record:
        raise MissingKey(
            "Add your OpenAI or Anthropic API key in Settings before running an agent."
        )

    user = store.user_by_id(user_id)
    if user is None:
        raise MissingKey("Account not found")

    provider = record["provider"]
    cheap_default, smart_default = DEFAULT_MODELS.get(provider, DEFAULT_MODELS["openai"])

    settings = Settings(
        provider=provider,
        api_key=crypto.decrypt(record["key_ciphertext"]),
        model_cheap=record.get("model_cheap") or cheap_default,
        model_smart=record.get("model_smart") or smart_default,
        spend_limit_usd=float(user["spend_limit_usd"]),
    )
    return LLM(settings, PostgresRecorder(user_id))
