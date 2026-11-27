"""The INC-001 incident workflow as a LangGraph StateGraph.

    intake -> baseline_diagnostics -> classify -> plan_diagnostics <-> run_diagnostics
      -> retrieve_runbook -> propose
      -> policy_gate -> request_approval -> await_approval -> execute -> verify -> resolve
    Any step can route to `escalate` instead.

Who decides what (the core design rule):

    LLM (proposes)                        Deterministic code (decides and acts)
    ------------------------------------  ----------------------------------------------------
    classify: category, severity          intake, routing between every pair of nodes
                                          baseline_diagnostics: which services to check first
    plan_diagnostics: which checks next   run_diagnostics: allowlist enforcement, execution
    propose: action, target, diagnosis    policy_gate: allow / requires approval / deny
                                          request/await_approval, execute, verify, escalate
                                          all limits: steps, rounds, tool calls, tokens, cost

Every conditional edge goes through `_guard`, which sends the run to `escalate` if an earlier
node recorded an escalation or the step limit is reached, before any other routing rule.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from arl.audit import AuditLog
from arl.config import Limits, cost_usd
from arl.graph.state import RunState
from arl.knowledge import KeywordRetriever
from arl.llm.base import LLMClient, LLMError, Prompt, T
from arl.models import (
    ApprovalDecision,
    Classification,
    DiagnosticPlan,
    Escalation,
    Incident,
    LLMUsage,
    RemediationProposal,
)
from arl.policy import approvals as approval_mod
from arl.policy.approvals import ApprovalStore
from arl.policy.engine import PolicyEngine
from arl.remediation import RemediationExecutor, UnauthorizedAction, Verifier
from arl.tools.baseline import applicable_tools, baseline_scope
from arl.tools.catalog import Action, Policy
from arl.tools.diagnostics import DiagnosticToolbox, describe_services


@dataclass
class Deps:
    """Everything the nodes need, injected so tests can swap in fakes."""

    llm: LLMClient
    toolbox: DiagnosticToolbox
    retriever: KeywordRetriever
    policy_engine: PolicyEngine
    approvals: ApprovalStore
    executor: RemediationExecutor
    verifier: Verifier
    audit: AuditLog
    actions: dict[str, Action]
    policy: Policy
    limits: Limits
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    action_history: list[tuple[str, str, datetime]] = field(default_factory=list)


def new_run(incident: Incident) -> RunState:
    return {
        "run_id": f"RUN-{uuid.uuid4().hex[:12]}",
        "incident": incident,
        "status": "running",
        "evidence": [],
        "diagnostic_rounds": 0,
        "tool_calls": 0,
        "usage": LLMUsage(),
        "steps": 0,
    }


def _ticket_block(incident: Incident) -> str:
    """Ticket text is untrusted: wrapped in tags the prompts tell the model never to obey."""
    return f"<ticket>\n{incident.model_dump_json(indent=2)}\n</ticket>"


def _evidence_block(state: RunState) -> str:
    lines = [
        f"- {e.id} ({e.phase}) [{e.tool} on {e.service}] {'REFUSED: ' if e.rejected else ''}"
        f"{'ok' if e.ok else 'FAILED'}: {e.summary}"
        for e in state.get("evidence", [])
    ]
    return "<evidence>\n" + ("\n".join(lines) or "(none yet)") + "\n</evidence>"


def build_workflow(deps: Deps):
    """Return an uncompiled StateGraph; the caller compiles it with a checkpointer."""
    limits = deps.limits
    prompts = {name: Prompt.load(name) for name in ("classify", "plan_diagnostics", "propose_remediation")}

    def audit(state: RunState, actor, event_type: str, data: dict) -> None:
        deps.audit.append(state["run_id"], actor, event_type, data)

    def escalation(reason: str, detail: str) -> dict:
        return {"escalation": Escalation(reason=reason, detail=detail)}

    def call_llm(
        state: RunState, prompt_name: str, content: str, output_type: type[T]
    ) -> tuple[T | None, dict]:
        """Budget check, model call, usage accounting, audit. Returns (output, state update)."""
        used: LLMUsage = state.get("usage", LLMUsage())
        over = [
            name
            for name, value, cap in (
                ("llm_calls", used.calls, limits.max_llm_calls),
                ("input_tokens", used.input_tokens, limits.max_input_tokens_per_run),
                ("output_tokens", used.output_tokens, limits.max_output_tokens_per_run),
                ("cost_usd", used.cost_usd, limits.max_cost_usd_per_run),
            )
            if value >= cap
        ]
        if over:
            audit(state, "system", "budget_exceeded", {"limits": over, "usage": used.model_dump()})
            return None, escalation("budget_exceeded", f"run budget exhausted: {', '.join(over)}")

        prompt = prompts[prompt_name]
        try:
            result = deps.llm.structured(prompt=prompt, user_content=content, output_type=output_type)
        except LLMError as exc:
            audit(state, "llm", "llm_error", {"prompt": prompt.id, "error": str(exc)})
            return None, escalation("invalid_model_output", str(exc))

        delta = LLMUsage(
            calls=1,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost_usd(result.model, result.input_tokens, result.output_tokens),
        )
        audit(
            state,
            "llm",
            f"llm_{prompt_name}",
            {
                "prompt": result.prompt_id,
                "model": result.model,
                "output": result.output.model_dump(),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )
        return result.output, {"usage": delta}

    # ---------------------------------------------------------------- nodes ----------

    def intake(state: RunState) -> dict:
        audit(
            state,
            "system",
            "run_started",
            {"incident": state["incident"].model_dump(mode="json"), "limits": vars(limits)},
        )
        return {"steps": 1}

    def baseline_diagnostics(state: RunState) -> dict:
        """Code, not the model, gathers first evidence: read-only, scoped, within max_tool_calls."""
        scope = baseline_scope(
            deps.toolbox.services, state["incident"].configuration_item, deps.policy.baseline
        )
        audit(state, "system", "baseline_scope", scope.model_dump())
        new, calls = [], state.get("tool_calls", 0)
        next_id = len(state.get("evidence", [])) + 1
        for name in scope.services:
            for tool in applicable_tools(deps.toolbox.services[name], deps.policy.baseline.tools):
                if calls >= limits.max_tool_calls:
                    audit(state, "system", "tool_limit_reached", {"max_tool_calls": limits.max_tool_calls})
                    return {"baseline_scope": scope, "evidence": new, "tool_calls": calls, "steps": 1}
                ev = deps.toolbox.run_baseline(tool, name, evidence_id=f"EV-{next_id:03d}")
                next_id += 1
                calls += 1
                audit(state, "tool", "tool_call", ev.model_dump(mode="json"))
                new.append(ev)
        return {"baseline_scope": scope, "evidence": new, "tool_calls": calls, "steps": 1}

    def classify(state: RunState) -> dict:
        content = (
            f"{_ticket_block(state['incident'])}\n\n"
            f"Service catalog:\n{describe_services(deps.toolbox.services)}\n\n"
            f"Baseline checks were chosen by code, not by you: {state['baseline_scope'].model_dump_json()}\n"
            f"{_evidence_block(state)}"
        )
        output, update = call_llm(state, "classify", content, Classification)
        update["steps"] = 1
        if output is None:
            return update
        update["classification"] = output
        if output.category == "unknown" or output.confidence < limits.min_confidence:
            update |= escalation(
                "classification_uncertain", f"category={output.category} confidence={output.confidence}"
            )
        return update

    def plan_diagnostics(state: RunState) -> dict:
        category = state["classification"].category
        if not deps.toolbox.allowed_tools(category):
            return {"steps": 1, **escalation("no_diagnostics_for_category", category)}
        remaining = limits.max_diagnostic_rounds - state.get("diagnostic_rounds", 0)
        content = (
            f"{_ticket_block(state['incident'])}\n\n"
            f"Classification: {state['classification'].model_dump_json()}\n\n"
            f"{deps.toolbox.describe(category)}\n\n{_evidence_block(state)}\n\n"
            f"Diagnostic rounds remaining including this one: {remaining}"
        )
        output, update = call_llm(state, "plan_diagnostics", content, DiagnosticPlan)
        update["steps"] = 1
        if output is not None:
            update["diagnostic_plan"] = output
        return update

    def run_diagnostics(state: RunState) -> dict:
        category = state["classification"].category
        new, calls = [], state.get("tool_calls", 0)
        next_id = len(state.get("evidence", [])) + 1
        for request in state["diagnostic_plan"].requests:
            if calls >= limits.max_tool_calls:
                audit(state, "system", "tool_limit_reached", {"max_tool_calls": limits.max_tool_calls})
                break
            ev = deps.toolbox.run(request, category, evidence_id=f"EV-{next_id:03d}")
            next_id += 1
            calls += 1
            audit(state, "tool", "tool_call", ev.model_dump(mode="json"))
            new.append(ev)
        return {
            "evidence": new,
            "tool_calls": calls,
            "diagnostic_rounds": state.get("diagnostic_rounds", 0) + 1,
            "steps": 1,
        }

    def retrieve_runbook(state: RunState) -> dict:
        query = " ".join(
            [
                state["classification"].category.replace("_", " "),
                state["incident"].short_description,
                *[e.summary for e in state.get("evidence", []) if not e.rejected],
            ]
        )
        refs = deps.retriever.retrieve(query)
        audit(
            state,
            "system",
            "runbooks_retrieved",
            {"ids": [r.id for r in refs], "scores": [r.score for r in refs]},
        )
        return {"runbooks": refs, "steps": 1}

    def propose(state: RunState) -> dict:
        catalog = "\n".join(
            f"- {a.name}: {a.description} (allowed targets: {', '.join(a.allowed_targets)})"
            for a in deps.actions.values()
        )
        runbooks = "\n\n".join(
            f'<runbook id="{r.id}">\n{r.excerpt}\n</runbook>' for r in state.get("runbooks", [])
        )
        content = (
            f"{_ticket_block(state['incident'])}\n\n"
            f"Classification: {state['classification'].model_dump_json()}\n\n"
            f"Service catalog:\n{describe_services(deps.toolbox.services)}\n\n"
            f"{_evidence_block(state)}\n\n{runbooks or '(no runbook found)'}\n\n"
            f"Action catalog:\n{catalog}"
        )
        output, update = call_llm(state, "propose_remediation", content, RemediationProposal)
        update["steps"] = 1
        if output is None:
            return update
        update["proposal"] = output
        if output.escalate:
            update |= escalation("model_escalated", output.diagnosis)
        return update

    def policy_gate(state: RunState) -> dict:
        decision = deps.policy_engine.evaluate(
            state["proposal"], state.get("evidence", []), deps.action_history, deps.clock()
        )
        audit(state, "policy", "policy_decision", decision.model_dump())
        update: dict = {"policy_decision": decision, "steps": 1}
        if decision.outcome == "deny":
            update |= escalation("policy_denied", ",".join(decision.reasons))
        return update

    def request_approval(state: RunState) -> dict:
        req = approval_mod.new_request(
            state["run_id"],
            state["incident"].number,
            state["proposal"],
            deps.clock(),
            deps.policy.approval.ttl_minutes,
        )
        deps.approvals.save_request(req)
        audit(state, "system", "approval_requested", req.model_dump(mode="json"))
        return {"approval_request": req, "status": "awaiting_approval", "steps": 1}

    def await_approval(state: RunState) -> dict:
        # interrupt() pauses the graph here and saves state through the checkpointer. The run
        # resumes when the caller invokes the graph with Command(resume=<decision dict>).
        # On resume this node runs again from the top, so it must have no side effects
        # before interrupt(); that is why the request is created in the previous node.
        raw = interrupt({"approval_request": state["approval_request"].model_dump(mode="json")})
        decision = ApprovalDecision.model_validate(raw)
        audit(state, "human", "approval_decision", decision.model_dump(mode="json"))
        update: dict = {"approval_decision": decision, "status": "running", "steps": 1}
        if not decision.approved:
            update |= escalation("approval_rejected", decision.comment or "rejected by approver")
        return update

    def execute(state: RunState) -> dict:
        proposal = state["proposal"]
        now = deps.clock()
        try:
            result = deps.executor.execute(
                run_id=state["run_id"],
                incident_number=state["incident"].number,
                proposal=proposal,
                evidence=state.get("evidence", []),
                recent_actions=deps.action_history,
                approval_request=state.get("approval_request"),
                approval_decision=state.get("approval_decision"),
                now=now,
            )
        except UnauthorizedAction as exc:
            audit(state, "executor", "remediation_refused", {"reason": exc.reason, "detail": str(exc)})
            return {"steps": 1, **escalation("unauthorized_action", exc.reason)}
        except Exception as exc:  # platform-ops error, timeout, ...
            audit(state, "executor", "remediation_failed", {"error": f"{type(exc).__name__}: {exc}"})
            return {"steps": 1, **escalation("remediation_failed", str(exc))}
        deps.action_history.append((proposal.action_id, proposal.target, now))
        audit(state, "executor", "remediation_executed", result)
        return {"remediation": result, "steps": 1}

    def verify(state: RunState) -> dict:
        action = deps.actions[state["proposal"].action_id]
        result = deps.verifier.verify(action)
        audit(state, "system", "verification", result.model_dump())
        update: dict = {"verification": result, "steps": 1}
        if not result.passed:
            failed = [c.name for c in result.checks if not c.passed]
            update |= escalation("verification_failed", ", ".join(failed))
        return update

    def resolve(state: RunState) -> dict:
        audit(state, "system", "run_resolved", {"usage": state.get("usage", LLMUsage()).model_dump()})
        return {"status": "resolved", "steps": 1}

    def escalate(state: RunState) -> dict:
        esc = state.get("escalation") or Escalation(
            reason="step_limit_reached", detail=f"max_graph_steps={limits.max_graph_steps}"
        )
        # Hand the human everything collected so far, so they start from evidence, not from zero.
        esc = esc.model_copy(update={"evidence": list(state.get("evidence", []))})
        audit(
            state,
            "system",
            "run_escalated",
            {
                "reason": esc.reason,
                "detail": esc.detail,
                "classification": c.model_dump() if (c := state.get("classification")) else None,
                "evidence": [
                    {"id": e.id, "phase": e.phase, "service": e.service, "ok": e.ok, "summary": e.summary}
                    for e in esc.evidence
                ],
                "usage": state.get("usage", LLMUsage()).model_dump(),
            },
        )
        return {"status": "escalated", "escalation": esc, "steps": 1}

    # ---------------------------------------------------------------- routing ----------

    def _guard(route: Callable[[RunState], str]) -> Callable[[RunState], str]:
        def guarded(state: RunState) -> str:
            if state.get("escalation") is not None:
                return "escalate"
            if state.get("steps", 0) >= limits.max_graph_steps:
                return "escalate"
            return route(state)

        guarded.__name__ = f"route_after_{route.__name__}"
        return guarded

    def after_run_diagnostics(state: RunState) -> str:
        done = (
            state["diagnostic_plan"].done
            or state.get("diagnostic_rounds", 0) >= limits.max_diagnostic_rounds
            or state.get("tool_calls", 0) >= limits.max_tool_calls
        )
        return "retrieve_runbook" if done else "plan_diagnostics"

    def after_policy_gate(state: RunState) -> str:
        outcome = state["policy_decision"].outcome
        return {"allow": "execute", "requires_approval": "request_approval"}.get(outcome, "escalate")

    def to(node: str) -> Callable[[RunState], str]:
        def route(_: RunState) -> str:
            return node

        route.__name__ = node
        return route

    graph = StateGraph(RunState)
    for fn in (
        intake,
        baseline_diagnostics,
        classify,
        plan_diagnostics,
        run_diagnostics,
        retrieve_runbook,
        propose,
        policy_gate,
        request_approval,
        await_approval,
        execute,
        verify,
        resolve,
        escalate,
    ):
        graph.add_node(fn.__name__, fn)

    graph.add_edge(START, "intake")
    edges = {
        "intake": (to("baseline_diagnostics"), ["baseline_diagnostics"]),
        "baseline_diagnostics": (to("classify"), ["classify"]),
        "classify": (to("plan_diagnostics"), ["plan_diagnostics"]),
        "plan_diagnostics": (to("run_diagnostics"), ["run_diagnostics"]),
        "run_diagnostics": (after_run_diagnostics, ["plan_diagnostics", "retrieve_runbook"]),
        "retrieve_runbook": (to("propose"), ["propose"]),
        "propose": (to("policy_gate"), ["policy_gate"]),
        "policy_gate": (after_policy_gate, ["execute", "request_approval"]),
        "request_approval": (to("await_approval"), ["await_approval"]),
        "await_approval": (to("execute"), ["execute"]),
        "execute": (to("verify"), ["verify"]),
        "verify": (to("resolve"), ["resolve"]),
    }
    for source, (route, targets) in edges.items():
        graph.add_conditional_edges(source, _guard(route), [*targets, "escalate"])
    graph.add_edge("resolve", END)
    graph.add_edge("escalate", END)
    return graph


def summarize(state: RunState) -> str:
    """One-line JSON summary of a finished (or paused) run, for logs and the CLI."""
    esc = state.get("escalation")
    return json.dumps(
        {
            "run_id": state.get("run_id"),
            "status": state.get("status"),
            "escalation": esc.reason if esc else None,
            "evidence": len(state.get("evidence", [])),
            "usage": state.get("usage", LLMUsage()).model_dump(),
        }
    )
