"""Runtime configuration, model routing and price table."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
INBOX_DIR = DATA_DIR / "inbox"
OUT_DIR = DATA_DIR / "out"
CONFIG_DIR = ROOT / "config"
DB_PATH = DATA_DIR / "codeskate.db"

# USD per 1M tokens (input, output).
# Rates as of 2026-08-30 — update when your provider changes pricing.
PRICES: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    # OpenAI
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (5.00, 30.00),
}

# Charged at 25% of input rate to write, 10% to read (Anthropic prompt caching).
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


@dataclass(frozen=True)
class Settings:
    provider: str
    api_key: str
    model_cheap: str
    model_smart: str
    spend_limit_usd: float

    def model(self, tier: str) -> str:
        if tier == "cheap":
            return self.model_cheap
        if tier == "smart":
            return self.model_smart
        raise ValueError(f"unknown tier: {tier!r} (use 'cheap' or 'smart')")

    def price(self, model: str) -> tuple[float, float]:
        if model not in PRICES:
            # Unknown model: assume a mid rate so cost tracking still functions.
            return (3.00, 15.00)
        return PRICES[model]


def load_settings() -> Settings:
    provider = os.getenv("CODESKATE_PROVIDER", "anthropic").strip().lower()
    if provider not in ("anthropic", "openai"):
        raise SystemExit(f"CODESKATE_PROVIDER must be 'anthropic' or 'openai', got {provider!r}")

    key_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    api_key = os.getenv(key_var, "").strip()

    defaults = {
        "anthropic": ("claude-haiku-4-5", "claude-sonnet-5"),
        "openai": ("gpt-5.6-luna", "gpt-5.6-terra"),
    }[provider]

    return Settings(
        provider=provider,
        api_key=api_key,
        model_cheap=os.getenv("CODESKATE_MODEL_CHEAP", defaults[0]).strip(),
        model_smart=os.getenv("CODESKATE_MODEL_SMART", defaults[1]).strip(),
        spend_limit_usd=float(os.getenv("CODESKATE_SPEND_LIMIT_USD", "20.00")),
    )
