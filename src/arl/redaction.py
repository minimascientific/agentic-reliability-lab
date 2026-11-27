"""Redact credentials and personal data before anything is printed for debugging.

The audit trail keeps the full record; this is applied to what leaves the process (the
terminal, a pasted bug report). Two layers: known secret values from the environment are
replaced exactly, and common credential and e-mail patterns are masked.
"""

from __future__ import annotations

import os
import re

_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)$")
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+@"), r"\1[REDACTED]@"),
    (
        re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\s*[=:]\s*)[^\s,;\"']+"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
]


def _env_secrets() -> list[str]:
    values = [v for k, v in os.environ.items() if _SECRET_ENV.search(k) and len(v) >= 8]
    return sorted(values, key=len, reverse=True)


def redact_text(text: str, secrets: list[str] | None = None) -> str:
    for value in _env_secrets() if secrets is None else secrets:
        text = text.replace(value, "[REDACTED]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(obj, secrets: list[str] | None = None):
    """Redact every string inside a JSON-like structure (dicts, lists, scalars)."""
    secrets = _env_secrets() if secrets is None else secrets
    if isinstance(obj, str):
        return redact_text(obj, secrets)
    if isinstance(obj, dict):
        return {k: redact(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact(v, secrets) for v in obj]
    return obj
