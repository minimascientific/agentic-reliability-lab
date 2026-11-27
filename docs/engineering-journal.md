# Engineering journal

A chronological record of how this project was built: milestones, discoveries, manual
acceptance tests, decisions and lessons. It complements the formal decision records in
[`docs/adr/`](adr/), which state *what* was decided; this journal records *how we got there*.

Conventions: **Verified** means observed in a test run or manual test. **Planned** means
not yet done. Dates are UTC. Every system, ticket and user here is synthetic.

---

## 2026-11-24 — Scope and architecture

- Chose a single end-to-end milestone, INC-001: a real stopped PostgreSQL container
  (`app-db`) that a real application (`orders-api`) depends on. The agent has to diagnose an
  actual failure, not a mocked one.
- Core rule: the language model proposes, and deterministic code decides and acts
  ([ADR 0001](adr/0001-llm-proposes-code-decides.md)).
- LangGraph for orchestration, with the Anthropic SDK called directly from nodes
  ([ADR 0002](adr/0002-langgraph-with-anthropic-sdk.md)).
- The agent's own records live in a separate database, so the outage cannot erase the
  evidence of its own handling ([ADR 0003](adr/0003-separate-ops-database.md)).

## 2026-11-24 — Increment 1: foundation and first workflow (commit `3af29ee`)

- **Built:** Docker Compose sandbox (`app-db`, `orders-api`, `ops-db`). A 13-node LangGraph
  workflow with an approval interrupt and guarded routing to `escalate`. Policy engine,
  hash-bound single-use approvals, executor re-checks, verifier, hash-chained audit log, and
  per-run limits on steps, tool calls, model calls, tokens and cost.
- **Verified (development environment):** 37 unit and workflow tests and 2 integration tests
  passed. Stopping `app-db` produced a real connection refusal and a 503 from `orders-api`.
  The agent's database role could not UPDATE or DELETE audit rows.
- **Challenges:**
  - `assert`-based security checks were replaced with explicit `raise`, because Python strips
    asserts under `-O`.
  - Checkpoint deserialisation was limited to an allowlist of the project's own types.
  - Inline comments in `.env` broke parsing, so they moved onto their own lines.
- **Known gap at the time:** there was no restart service, so an approved restart failed
  safely and escalated. The Docker image build could not be checked in the development
  environment, because its network blocked the image registry.

## 2026-11-27 — Increment 1 verified on macOS

- **Verified** on Apple Silicon with Colima:
  - the `orders-api` image built;
  - 37 unit and 2 integration tests passed, and lint was clean;
  - an approved demo run escalated with `remediation_failed`, as expected, because no
    restart service existed yet;
  - a rejected run escalated with `approval_rejected`;
  - the audit hash chain was valid on both runs.
- **Observation:** `orders-api` had no container healthcheck. One was added in increment 2a.

## 2026-11-27 — Increment 2a: the restart path ([PR #1](https://github.com/minimascientific/agentic-reliability-lab/pull/1), commit `5027e2f`)

- **Built:** a `platform-ops` service with one restart endpoint. It checks a bearer token,
  its own target allowlist, an idempotency key and a per-target rate limit. Behind it is a
  Docker socket proxy that forwards only container stop, restart and kill
  ([ADR 0004](adr/0004-restarts-through-platform-ops.md)).
- **Verified:**
  - 59 unit and 4 integration tests passed. CI passed too, including the first full Compose
    build and a real restart of a stopped container.
  - By hand, the proxy returned 403 for listing and creating containers, exec, images, start,
    delete and volume creation.
- **Challenge:** the socket proxy crash-looped on start. HAProxy binds IPv6 by default and
  the host had none, so `DISABLE_IPV6=1` was added. The client had first shown only
  "connection refused"; the cause was in the container logs.
- **Status:** open for review, not merged.

## 2026-11-27 — Live AI testing exposed a design flaw

Manual acceptance tests on macOS, against the PR #1 branch:

| Test | Backend | Result |
| --- | --- | --- |
| 01: approved remediation | Scripted | **Pass.** Database restarted, recovery independently verified, 14 audit events, hash chain valid |
| 02: rejected remediation | Scripted | **Pass.** Escalated, database stayed stopped, 12 audit events, hash chain valid |
| 03: live model | Claude (`claude-opus-5-5`) | Escalated after classification: `classification_uncertain: category=unknown confidence=0.4`. 1 call, 1,064 input and 166 output tokens, $0.007576, 3 audit events, hash chain valid |

- **Observation:** all 59 unit and 4 integration tests passed, and so did manual tests 01
  and 02.
- **Discovery:** the live model classified the incident as `unknown` with confidence 0.4,
  and the workflow correctly escalated.
- **Root cause:** the architecture required a confident classification before collecting
  any evidence, and the category gated even read-only checks. The ticket describes only a
  symptom, so the model's answer was well calibrated. The scripted backend had classified
  the same ticket at 0.9, assuming knowledge it could not reasonably have had.
- **Also learned:** the model's rationale was recorded in the in-memory audit trail, but it
  was lost when the CLI exited. Evidence that is not persisted cannot be inspected later.
- **Engineering decision:** introduce deterministic baseline diagnostics before AI
  classification ([ADR 0005](adr/0005-baseline-diagnostics-before-classification.md)).
- **Alternatives considered:** lowering the confidence threshold, generic AI-driven
  triage, and removing the classification gate. The ADR explains why each was rejected.
- **Safety principle:** improve the information available to the AI rather than weaken
  the safeguards.
- **Next steps:**
  - Implement the revised architecture (in progress, below).
  - Repeat all three acceptance tests against a branch that contains both changes.
  - Keep the original failure as a regression evaluation.

## 2026-11-27 — Baseline diagnostics implemented (pending review)

- **Built (branch `baseline-diagnostics-before-classification`):**
  - an optional `configuration_item` on incidents;
  - a catalog-derived baseline scope with a bounded fallback for tickets without one;
  - a `baseline_diagnostics` node, with escalations that carry their evidence;
  - redacted `--show-model-output`;
  - an evidence-aware stand-in model;
  - evaluation cases in `evals/cases.yaml`, including `inc001-symptom-only` unchanged.
- **Verified:** 61 unit and workflow tests passed with the stand-in model. They cover a
  known, missing and unknown configuration item, healthy and failed dependencies,
  inconclusive evidence, tool limits and safe escalation.
- **Planned:**
  - repeat acceptance tests 01 to 03, including live Claude, before either PR merges;
  - persistent, inspectable audit records for CLI runs, as a separate increment.
