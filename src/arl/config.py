"""Runtime settings, read from environment variables (and a local, git-ignored .env file).

Every limit that bounds the agent lives here, so a reviewer can see the whole safety
envelope in one place. Secrets (ANTHROPIC_API_KEY) are read by the Anthropic SDK from the
environment and are never stored on this object, logged, or written to the audit trail.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"

# USD per million tokens (input, output). Thinking tokens bill as output tokens.
# Source: Anthropic pricing, checked 2026-11-24. Update when models or prices change.
MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "scripted": (0.0, 0.0),
}


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load KEY=VALUE lines from .env into os.environ without overriding real env vars."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Limits:
    """Hard limits on one agent run. Exceeding any of them ends the run in escalation."""

    max_graph_steps: int = 25  # nodes executed, like a watchdog on scan count
    max_diagnostic_rounds: int = 3  # plan -> run tool loops
    max_tool_calls: int = 10  # diagnostic tool executions per run
    max_llm_calls: int = 6  # model calls per run
    max_output_tokens_per_call: int = 8_000  # includes thinking tokens
    max_input_tokens_per_run: int = 60_000
    max_output_tokens_per_run: int = 24_000
    max_cost_usd_per_run: float = 0.75
    min_confidence: float = 0.7  # below this, the run escalates instead of acting
    verify_attempts: int = 10
    verify_interval_seconds: float = 3.0


@dataclass(frozen=True)
class Settings:
    llm_backend: str  # "scripted" (default, free, deterministic) or "anthropic"
    model: str
    limits: Limits
    ops_database_url: str | None

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        defaults = Limits()
        limits = Limits(
            max_graph_steps=_int("ARL_MAX_GRAPH_STEPS", defaults.max_graph_steps),
            max_diagnostic_rounds=_int("ARL_MAX_DIAGNOSTIC_ROUNDS", defaults.max_diagnostic_rounds),
            max_tool_calls=_int("ARL_MAX_TOOL_CALLS", defaults.max_tool_calls),
            max_llm_calls=_int("ARL_MAX_LLM_CALLS", defaults.max_llm_calls),
            max_output_tokens_per_call=_int(
                "ARL_MAX_OUTPUT_TOKENS_PER_CALL", defaults.max_output_tokens_per_call
            ),
            max_input_tokens_per_run=_int("ARL_MAX_INPUT_TOKENS_PER_RUN", defaults.max_input_tokens_per_run),
            max_output_tokens_per_run=_int(
                "ARL_MAX_OUTPUT_TOKENS_PER_RUN", defaults.max_output_tokens_per_run
            ),
            max_cost_usd_per_run=_float("ARL_MAX_COST_USD_PER_RUN", defaults.max_cost_usd_per_run),
            min_confidence=_float("ARL_MIN_CONFIDENCE", defaults.min_confidence),
            verify_attempts=_int("ARL_VERIFY_ATTEMPTS", defaults.verify_attempts),
            verify_interval_seconds=_float("ARL_VERIFY_INTERVAL_SECONDS", defaults.verify_interval_seconds),
        )
        backend = os.environ.get("ARL_LLM_BACKEND", "scripted")
        if backend not in ("scripted", "anthropic"):
            raise ValueError(f"ARL_LLM_BACKEND must be 'scripted' or 'anthropic', not {backend!r}")
        model = os.environ.get("ARL_MODEL", "claude-opus-5-5") if backend == "anthropic" else "scripted"
        if model not in MODEL_PRICING_USD_PER_MTOK:
            raise ValueError(f"ARL_MODEL={model!r} has no price entry; add it to MODEL_PRICING_USD_PER_MTOK")
        return cls(
            llm_backend=backend,
            model=model,
            limits=limits,
            ops_database_url=os.environ.get("OPS_DATABASE_URL"),
        )


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = MODEL_PRICING_USD_PER_MTOK[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
