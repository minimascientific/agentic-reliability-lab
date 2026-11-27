"""Load evaluation cases from evals/cases.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from arl.config import PROJECT_ROOT
from arl.models import Incident


class EvalCase(BaseModel):
    id: str
    description: str
    fault: Literal["app_db_stopped", "none"]
    incident: Incident
    expect: dict


def load_cases(path: Path = PROJECT_ROOT / "evals" / "cases.yaml") -> dict[str, EvalCase]:
    raw = yaml.safe_load(path.read_text())["cases"]
    return {c["id"]: EvalCase.model_validate(c) for c in raw}
