"""The real model: Claude via the Anthropic Python SDK, with structured outputs.

Only used when ARL_LLM_BACKEND=anthropic. The API key is read by the SDK from the
ANTHROPIC_API_KEY environment variable (loaded from your local .env); this module never
touches it directly.

Design notes:
* `messages.parse(output_format=Model)` constrains Claude to the Pydantic schema and returns
  a validated object, so no free text is ever parsed or executed.
* Claude Opus 5.5 always thinks; `effort` (low/medium/high) controls how much. Thinking
  tokens are billed as output tokens and count against our per-run budget.
* A refusal or truncated answer raises LLMError, and the workflow escalates. We do not
  silently fall back to another model, so every run and eval is attributable to one model.
"""

from __future__ import annotations

import anthropic

from arl.llm.base import LLMError, LLMResult, Prompt, T


class ClaudeLLM:
    def __init__(self, model: str, max_output_tokens_per_call: int, timeout_seconds: float = 120.0):
        self.model = model
        self._max_tokens = max_output_tokens_per_call
        self._client = anthropic.Anthropic(timeout=timeout_seconds, max_retries=2)

    def structured(self, *, prompt: Prompt, user_content: str, output_type: type[T]) -> LLMResult[T]:
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=self._max_tokens,
                system=prompt.system,
                messages=[{"role": "user", "content": user_content}],
                output_format=output_type,
                output_config={"effort": prompt.effort},
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Anthropic API unreachable: {exc}") from exc
        except Exception as exc:  # schema validation of the returned JSON
            raise LLMError(f"model output could not be parsed: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMError("model refused the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("model output truncated at max_tokens")
        if response.parsed_output is None:
            raise LLMError("model returned no structured output")

        return LLMResult(
            output=response.parsed_output,
            prompt_id=prompt.id,
            model=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
