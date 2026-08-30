"""Provider-agnostic LLM client with a hard spend guard and cost accounting.

Design notes
------------
* Two model tiers ("cheap" / "smart") so high-volume work never hits an
  expensive model. At 1000 users this is the difference between a viable
  business and a dead one, so the habit starts on day one.
* The system prompt is marked cacheable. Scoring 500 jobs re-sends the same
  profile every call; caching that block cuts repeated input cost by ~90%.
* Every call is logged to SQLite and checked against CODESKATE_SPEND_LIMIT_USD
  *before* firing. A runaway loop stops instead of draining your credits.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import db
from .settings import CACHE_READ_MULT, CACHE_WRITE_MULT, Settings

T = TypeVar("T", bound=BaseModel)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = httpx.Timeout(120.0)


class SpendLimitExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    """Models sometimes wrap JSON in prose or fences. Recover the object."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise LLMError(f"no JSON found in response: {text[:300]}")
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start : end + 1])


class LLM:
    def __init__(self, settings: Settings, conn: sqlite3.Connection) -> None:
        if not settings.api_key:
            key = "ANTHROPIC_API_KEY" if settings.provider == "anthropic" else "OPENAI_API_KEY"
            raise SystemExit(f"{key} is not set. Copy .env.example to .env and fill it in.")
        self.s = settings
        self.conn = conn
        self.client = httpx.Client(timeout=TIMEOUT)

    # ---------- cost ----------

    def _cost(self, model: str, usage: dict[str, int]) -> float:
        in_rate, out_rate = self.s.price(model)
        return (
            usage["input_tokens"] * in_rate
            + usage["output_tokens"] * out_rate
            + usage["cache_write"] * in_rate * CACHE_WRITE_MULT
            + usage["cache_read"] * in_rate * CACHE_READ_MULT
        ) / 1_000_000

    def _assert_budget(self) -> None:
        spent = db.total_spend(self.conn)
        if spent >= self.s.spend_limit_usd:
            raise SpendLimitExceeded(
                f"Spend guard tripped: ${spent:.2f} of ${self.s.spend_limit_usd:.2f} used. "
                f"Raise CODESKATE_SPEND_LIMIT_USD in .env if this is intentional."
            )

    # ---------- transport ----------

    def _call_anthropic(
        self, model: str, system: str, user: str, max_tokens: int, cache_system: bool
    ) -> tuple[str, dict[str, int]]:
        system_block: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}

        r = self.client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": self.s.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": [system_block],
                "messages": [{"role": "user", "content": user}],
            },
        )
        if r.status_code >= 400:
            raise LLMError(f"anthropic {r.status_code}: {r.text[:500]}")
        body = r.json()
        u = body.get("usage", {})
        text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        return text, {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cache_write": u.get("cache_creation_input_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0),
        }

    def _call_openai(
        self, model: str, system: str, user: str, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        r = self.client.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {self.s.api_key}",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_completion_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        if r.status_code >= 400:
            raise LLMError(f"openai {r.status_code}: {r.text[:500]}")
        body = r.json()
        u = body.get("usage", {})
        cached = u.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        return body["choices"][0]["message"]["content"] or "", {
            "input_tokens": max(u.get("prompt_tokens", 0) - cached, 0),
            "output_tokens": u.get("completion_tokens", 0),
            "cache_write": 0,
            "cache_read": cached,
        }

    # ---------- public ----------

    def json_call(
        self,
        *,
        agent: str,
        tier: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 4096,
        cache_system: bool = True,
        retries: int = 2,
    ) -> T:
        """Call the model and validate the reply against a pydantic schema."""
        self._assert_budget()
        model = self.s.model(tier)

        instruction = (
            f"{system}\n\n"
            "Reply with a single JSON object and nothing else. "
            "It must validate against this JSON Schema:\n"
            f"{json.dumps(schema.model_json_schema(), separators=(',', ':'))}"
        )

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            if self.s.provider == "anthropic":
                text, usage = self._call_anthropic(
                    model, instruction, user, max_tokens, cache_system
                )
            else:
                text, usage = self._call_openai(model, instruction, user, max_tokens)

            cost = self._cost(model, usage)
            db.log_call(self.conn, agent=agent, model=model, cost_usd=cost, **usage)

            try:
                return schema.model_validate(_extract_json(text))
            except (ValidationError, LLMError, json.JSONDecodeError) as e:
                last_err = e
                # Re-check budget so a schema-fighting model can't loop us broke.
                self._assert_budget()
                user = (
                    f"{user}\n\n[Your previous reply was rejected: {str(e)[:300]}. "
                    "Return only valid JSON matching the schema.]"
                )

        raise LLMError(f"{agent}: schema validation failed after {retries + 1} tries: {last_err}")

    def ping(self, tier: str) -> str:
        """Cheap liveness + model-id check used by `codeskate doctor`."""
        model = self.s.model(tier)
        if self.s.provider == "anthropic":
            text, usage = self._call_anthropic(model, "Reply with the word: ok", "ping", 16, False)
        else:
            text, usage = self._call_openai(
                model, 'Reply with JSON {"status":"ok"}', "ping", 32
            )
        db.log_call(
            self.conn, agent="doctor", model=model, cost_usd=self._cost(model, usage), **usage
        )
        return text.strip()[:80]
