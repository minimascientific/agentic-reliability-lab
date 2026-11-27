# ADR 0005: Deterministic baseline diagnostics before AI classification

**Status:** accepted, 2026-11-27

## Context
The first live run with Claude (manual acceptance test 03, 2026-11-27) escalated
immediately: `classification_uncertain: category=unknown confidence=0.4`, after one model
call costing $0.0076. All 59 unit and 4 integration tests had passed, as had the manual
tests for approved and rejected remediation with the scripted model.

The ticket, INC0010001, describes only a symptom: "every order lookup returns 'service
unavailable'". It names no service and mentions no database. Given only that text,
"unknown, 0.4" is a well-calibrated answer, and escalating was the correct, safe outcome.

The flaw was in the design, in two parts:

1. **Order of operations.** The workflow asked for a confident category *before* collecting
   any evidence, and the category gated every diagnostic tool. A low-confidence guess
   therefore blocked even read-only checks.
2. **Test fixtures with hindsight.** The scripted model classified the same ticket as
   `database_connectivity` with confidence 0.9, which no model could justify from the
   ticket alone. Deterministic tests passed because the fixture assumed the answer.

## Decision
Add a `baseline_diagnostics` node between `intake` and `classify`. It is deterministic code;
the model has no say in it.

- **Scope** (`arl/tools/baseline.py`): start from the ticket's optional
  `configuration_item` if it is in the service catalog; otherwise fall back to the
  catalog's `user_facing` services. Add `depends_on` dependencies breadth first. Nothing
  about INC-001 is encoded; the scope comes from catalog data.
- **Bounds** (`config/policy.yaml` `baseline`): `max_dependency_depth: 2`,
  `max_services: 3`, its own tool allowlist (health and TCP checks only), and every check
  counts against the existing `max_tool_calls`. Truncation and fallbacks are recorded as
  notes in a `baseline_scope` audit event.
- **Classification** sees the ticket, the catalog with dependencies, and the baseline
  evidence (prompt `classify` v2), and must cite evidence IDs.
- **Unchanged controls:** the 0.7 classification threshold, the category-based diagnostic
  allowlist, the remediation policy, approval binding and executor re-checks.
- **Escalations carry evidence:** `Escalation.evidence` and the `run_escalated` audit
  event hold everything collected, so a human starts from evidence rather than from zero.
- **Honest stand-in:** the scripted backend (`arl/llm/stand_in.py`) now answers each stage
  only from the content it receives. With healthy evidence it returns `unknown`.
- **Debuggability:** `arl-demo --show-model-output` prints each structured model output,
  passed through `arl/redaction.py` (known secret values, API keys, bearer tokens, URL
  passwords, `password=`-style pairs and e-mail addresses are masked).

Principle: **improve the information available to the model rather than weaken the
safeguards.** Confidence gates what the agent may *change*, not what it may *look at*.

## Alternatives rejected
- **Lower the threshold or reword the prompt to steer the answer.** This hides the problem
  for one ticket's wording, and the next ticket is worded differently. It also weakens a
  control to make a test pass.
- **Model-led triage for low confidence.** Give `unknown` a generic read-only toolset, let
  the model pick checks, then classify again. This is more flexible, but it costs more model
  calls, hands tool selection to the model at its least certain point, and needs a bounded
  reclassification loop. It can be revisited if catalog-driven scope proves too narrow.
- **Remove the classification gate.** This is the simplest change, but the category selects
  toolsets and runbooks, so an unchecked guess would only move the risk downstream.

## Consequences
- Every run makes up to `max_services` x 2 read-only probes before the first model call.
  That is cheap here, but in a large estate it must stay scoped. That is why the scope
  needs a configuration item or user-facing services, and is capped.
- The fallback depends on catalog quality: a missing `user_facing` flag or `depends_on`
  edge means a missing check. Like the action allowlist, the catalog is configuration
  under review.
- Evidence IDs shift: baseline evidence comes first (EV-001...), and model-requested
  diagnostics follow.
- The original symptom-only ticket is kept unchanged as the evaluation case
  `inc001-symptom-only` (`evals/cases.yaml`).
- Not yet verified: a repeat of acceptance test 03 against live Claude with this change.
