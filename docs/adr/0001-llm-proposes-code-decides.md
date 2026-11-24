# ADR 0001: The model proposes; deterministic code decides and acts

**Status:** accepted, 2026-11-24

## Context
An LLM is good at reading messy tickets and evidence, and at choosing what to check next. It
is not a reliable security boundary: its output can be wrong, and text in a ticket or document
can try to steer it (prompt injection).

## Decision
The model only ever returns typed proposals (classification, diagnostic plan, remediation
proposal). Code enforces tool allowlists, evaluates policy, requests human approval, re-checks
before execution, and verifies the outcome. The model has no credentials, no shell, and no
direct tool execution.

## Consequences
- Every allow, deny and escalation has a machine-readable reason that does not depend on the
  model's explanation.
- Policy is unit-testable without a model.
- The agent is less flexible than one with open-ended tools. That is intended: the job is
  bounded operational tasks.

Industrial analogy: the model is the basic process control system; the policy engine is the
separate safety instrumented system.
