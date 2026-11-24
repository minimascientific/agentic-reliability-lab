"""The single interface every workflow node uses to talk to a language model.

Nodes never import the Anthropic SDK. They call `LLMClient.structured(...)` and get back a
validated Pydantic object plus token counts. That gives us two implementations behind one
interface:

* `ScriptedLLM` returns pre-written answers: free, deterministic, used by CI and unit tests.
* `ClaudeLLM` calls the real Anthropic API: used for demos and the evaluation suite.

Industrial analogy: the same PLC logic runs against a plant simulator or real I/O, because the
I/O layer is swapped behind a fixed interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


class LLMError(Exception):
    """The model could not produce a valid answer (refusal, truncation, schema failure)."""


@dataclass(frozen=True)
class Prompt:
    """A versioned system prompt. `id` (name@version) is recorded with every call."""

    name: str
    version: str
    effort: str
    system: str

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def load(cls, name: str) -> Prompt:
        """Read prompts/<name>.md. The first lines are `key: value` headers, then `---`."""
        text = (PROMPTS_DIR / f"{name}.md").read_text()
        header, _, body = text.partition("\n---\n")
        meta = dict(line.split(":", 1) for line in header.strip().splitlines())
        meta = {k.strip(): v.strip() for k, v in meta.items()}
        return cls(name=name, version=meta["version"], effort=meta["effort"], system=body.strip())


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    output: T
    prompt_id: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    model: str

    def structured(self, *, prompt: Prompt, user_content: str, output_type: type[T]) -> LLMResult[T]:
        """Return the model's answer parsed into `output_type`, or raise LLMError."""
        ...
