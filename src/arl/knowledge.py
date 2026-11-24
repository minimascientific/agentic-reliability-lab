"""Runbook retrieval.

Increment 1 uses simple keyword scoring over docs/runbooks/*.md so the workflow is complete
and deterministic. Increment 3 replaces it with pgvector + full-text search behind the same
`retrieve()` interface, and the retrieval evaluations measure the difference.
"""

from __future__ import annotations

import re
from pathlib import Path

from arl.config import PROJECT_ROOT
from arl.models import RunbookRef

RUNBOOK_DIR = PROJECT_ROOT / "docs" / "runbooks"
_WORD = re.compile(r"[a-z0-9][a-z0-9_-]+")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "not", "can", "its", "into"}


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


class KeywordRetriever:
    def __init__(self, directory: Path = RUNBOOK_DIR):
        self.docs: list[tuple[str, str, str]] = []  # (id, title, text)
        for path in sorted(directory.glob("*.md")):
            text = path.read_text()
            title = next(
                (ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem
            )
            self.docs.append((path.stem, title, text))

    def retrieve(self, query: str, k: int = 2) -> list[RunbookRef]:
        q = _terms(query)
        scored = []
        for doc_id, title, text in self.docs:
            overlap = q & _terms(text)
            if overlap:
                score = len(overlap) / len(q) if q else 0.0
                scored.append((score, doc_id, title, text))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [RunbookRef(id=d, title=t, excerpt=x[:1500], score=round(s, 3)) for s, d, t, x in scored[:k]]
