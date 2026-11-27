"""A deterministic stand-in for the LLM, used by CI and unit tests. Costs nothing."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable

from pydantic import BaseModel

from arl.llm.base import LLMError, LLMResult, Prompt, T


class ScriptedLLM:
    """Returns pre-written outputs, keyed by prompt name, in the order they were scripted.

    Each scripted item may be a Pydantic object (returned as-is), a dict (validated against
    the requested schema, so tests exercise the same validation path as real output), an
    Exception (raised, to simulate refusals or malformed output), or a callable that receives
    the prompt's user content and returns one of those. Callables let a stand-in answer only
    from the information it was actually given at that stage.
    """

    model = "scripted"

    def __init__(
        self,
        script: dict[str, list[BaseModel | dict | Exception | Callable[[str], object]]],
        tokens_per_call: tuple[int, int] = (1_000, 200),
    ):
        self._queues: dict[str, deque] = defaultdict(deque)
        for prompt_name, outputs in script.items():
            self._queues[prompt_name].extend(outputs)
        self._tokens = tokens_per_call
        self.calls: list[tuple[str, str]] = []  # (prompt_id, user_content) for assertions

    def structured(self, *, prompt: Prompt, user_content: str, output_type: type[T]) -> LLMResult[T]:
        self.calls.append((prompt.id, user_content))
        queue = self._queues[prompt.name]
        if not queue:
            raise LLMError(f"ScriptedLLM has no scripted output left for prompt {prompt.name!r}")
        item = queue.popleft()
        if callable(item) and not isinstance(item, type):
            item = item(user_content)
        if isinstance(item, Exception):
            raise item
        try:
            output = output_type.model_validate(item.model_dump() if isinstance(item, BaseModel) else item)
        except Exception as exc:  # same path a malformed real response would take
            raise LLMError(f"scripted output failed schema validation: {exc}") from exc
        return LLMResult(
            output=output,
            prompt_id=prompt.id,
            model=self.model,
            input_tokens=self._tokens[0],
            output_tokens=self._tokens[1],
        )
