# ADR 0002: LangGraph for orchestration, Anthropic SDK called directly from nodes

**Status:** accepted, 2026-11-24

## Context
We need explicit steps, conditional routing, durable pause and resume for approvals, and a
model layer that is easy to mock and meter.

## Decision
- LangGraph `StateGraph` for the workflow, with a checkpointer and `interrupt()` for approvals.
- Nodes call a small `LLMClient` interface. `ClaudeLLM` uses the Anthropic SDK's
  `messages.parse(output_format=...)` for schema-constrained output; `ScriptedLLM` serves tests.
- No `langchain-anthropic` wrapper and no agent-loop abstraction.

## Consequences
- Every model call is one typed request whose token usage we record directly.
- CI runs the whole graph without network access or cost.
- We write a little more glue code than with a prebuilt agent, and can explain all of it.
- A refusal or truncated answer escalates; we do not silently fall back to another model, so
  each run and evaluation is attributable to one model.
