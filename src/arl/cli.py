"""`uv run arl-demo`: run the INC-001 workflow against the live sandbox from the terminal.

    make up && make fault-db-down
    uv run arl-demo                      # scripted model: free, deterministic
    ARL_LLM_BACKEND=anthropic uv run arl-demo   # real Claude (uses your API key and budget)

Diagnostics run for real against the sandbox. The workflow pauses at the approval gate and
asks you in the terminal. Increment 1 has no platform-ops service yet, so an approved restart
fails safely and the run escalates; increment 2 adds the restart path.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from langgraph.types import Command

from arl.audit import InMemoryAuditLog, verify_chain
from arl.config import Settings
from arl.graph.checkpointing import memory_checkpointer
from arl.graph.workflow import Deps, build_workflow, new_run
from arl.knowledge import KeywordRetriever
from arl.llm import ScriptedLLM
from arl.models import (
    Classification,
    DiagnosticPlan,
    Incident,
    RemediationProposal,
    ToolRequest,
)
from arl.policy.approvals import InMemoryApprovalStore
from arl.policy.engine import PolicyEngine
from arl.remediation import HttpPlatformOps, RemediationExecutor, Verifier
from arl.tools.catalog import load_actions, load_policy, load_services
from arl.tools.diagnostics import DiagnosticToolbox
from arl.tools.probes import NetworkProbe

DEMO_INCIDENT = Incident(
    number="INC0010001",
    short_description="Orders page shows errors for the whole Sales Ops team",
    description="Since about 09:10 every order lookup returns 'service unavailable'. "
    "Nothing changed on our side. Affects about 40 users.",
    caller="dana.whitfield@corp.example",
    opened_at=datetime(2026, 11, 25, 9, 14, tzinfo=UTC),
)


def scripted_model() -> ScriptedLLM:
    """What a well-behaved model would answer for INC-001. Used when no API key is involved."""
    return ScriptedLLM(
        {
            "classify": [
                Classification(
                    category="database_connectivity",
                    severity=2,
                    affected_service="orders-api",
                    confidence=0.9,
                    rationale="(scripted) Application errors point at its database.",
                )
            ],
            "plan_diagnostics": [
                DiagnosticPlan(
                    requests=[
                        ToolRequest(tool="check_service_health", service="orders-api"),
                        ToolRequest(tool="check_tcp_port", service="app-db"),
                    ],
                    done=False,
                    rationale="(scripted) Check the app, then its database port.",
                ),
                DiagnosticPlan(requests=[], done=True, rationale="(scripted) Enough evidence."),
            ],
            "propose_remediation": [
                RemediationProposal(
                    escalate=False,
                    action_id="restart_service",
                    target="app-db",
                    evidence_ids=["EV-001", "EV-002"],
                    runbook_ids=["rb-db-connectivity"],
                    diagnosis="(scripted) app-db refuses connections; orders-api health fails.",
                    confidence=0.85,
                )
            ],
        }
    )


def build(settings: Settings, audit: InMemoryAuditLog):
    if settings.llm_backend == "anthropic":
        from arl.llm.claude import ClaudeLLM

        llm = ClaudeLLM(settings.model, settings.limits.max_output_tokens_per_call)
    else:
        llm = scripted_model()
    actions, policy = load_actions(), load_policy()
    engine = PolicyEngine(actions, policy)
    approvals = InMemoryApprovalStore()
    toolbox = DiagnosticToolbox(load_services(), policy, NetworkProbe())
    deps = Deps(
        llm=llm,
        toolbox=toolbox,
        retriever=KeywordRetriever(),
        policy_engine=engine,
        approvals=approvals,
        executor=RemediationExecutor(engine, approvals, HttpPlatformOps(timeout=5)),
        verifier=Verifier(toolbox, settings.limits.verify_attempts, settings.limits.verify_interval_seconds),
        audit=audit,
        actions=actions,
        policy=policy,
        limits=settings.limits,
    )
    return build_workflow(deps).compile(checkpointer=memory_checkpointer())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--approver", default="alison.gorman", help="name recorded on the approval")
    parser.add_argument(
        "--auto", choices=["approve", "reject"], help="answer the approval prompt automatically"
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    print(f"LLM backend: {settings.llm_backend} ({settings.model})")
    audit = InMemoryAuditLog()
    graph = build(settings, audit)
    state = new_run(DEMO_INCIDENT)
    config = {"configurable": {"thread_id": state["run_id"]}, "recursion_limit": 60}

    # stream_mode="updates" yields one item per node as it finishes: a live view of the graph.
    for update in graph.stream(state, config, stream_mode="updates"):
        for node, change in update.items():
            if node == "__interrupt__":
                continue
            print(f"  [{node}] {', '.join(k for k in (change or {}) if k != 'steps') or '-'}")

    snapshot = graph.get_state(config)
    if snapshot.interrupts:
        request = snapshot.interrupts[0].value["approval_request"]
        print("\nAPPROVAL REQUIRED")
        print(
            json.dumps(
                {
                    k: request[k]
                    for k in (
                        "incident_number",
                        "action_id",
                        "target",
                        "diagnosis",
                        "evidence_ids",
                        "expires_at",
                        "action_hash",
                    )
                },
                indent=2,
            )
        )
        answer = args.auto or input("Approve this action? [y/N] ").strip().lower()
        approved = answer in ("y", "yes", "approve")
        decision = {
            "approval_id": request["approval_id"],
            "action_hash": request["action_hash"],
            "approved": approved,
            "approver": args.approver,
            "comment": "" if approved else "rejected in demo CLI",
            "decided_at": datetime.now(UTC).isoformat(),
        }
        for update in graph.stream(Command(resume=decision), config, stream_mode="updates"):
            for node, change in update.items():
                print(f"  [{node}] {', '.join(k for k in (change or {}) if k != 'steps') or '-'}")

    final = graph.get_state(config).values
    esc = final.get("escalation")
    print(f"\nRESULT: {final['status']}" + (f" ({esc.reason}: {esc.detail})" if esc else ""))
    print(f"LLM usage: {final['usage'].model_dump()}")
    events = audit.events(final["run_id"])
    print(f"Audit: {len(events)} events, hash chain valid = {verify_chain(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
