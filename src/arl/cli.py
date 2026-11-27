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
from arl.evals import load_cases
from arl.graph.checkpointing import memory_checkpointer
from arl.graph.workflow import Deps, build_workflow, new_run
from arl.knowledge import KeywordRetriever
from arl.llm import ScriptedLLM, stand_in
from arl.policy.approvals import InMemoryApprovalStore
from arl.policy.engine import PolicyEngine
from arl.redaction import redact
from arl.remediation import HttpPlatformOps, RemediationExecutor, Verifier
from arl.tools.catalog import load_actions, load_policy, load_services
from arl.tools.diagnostics import DiagnosticToolbox
from arl.tools.probes import NetworkProbe

CASES = load_cases()
DEFAULT_CASE = "inc001-symptom-only"
DEMO_INCIDENT = CASES[DEFAULT_CASE].incident  # the original, symptom-only INC-001 ticket


def scripted_model() -> ScriptedLLM:
    """The free stand-in: answers each stage only from the content it is given (see stand_in.py)."""
    return ScriptedLLM(stand_in.script())


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
    parser.add_argument(
        "--case", default=DEFAULT_CASE, choices=sorted(CASES), help="which evaluation case's ticket to run"
    )
    parser.add_argument(
        "--show-model-output",
        action="store_true",
        help="print each structured model output (redacted) after the run",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    print(f"LLM backend: {settings.llm_backend} ({settings.model})")
    audit = InMemoryAuditLog()
    graph = build(settings, audit)
    incident = CASES[args.case].incident
    ci = incident.configuration_item or "none"
    print(f"Case: {args.case} ({incident.number}, configuration item: {ci})")
    state = new_run(incident)
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
    events = audit.events(final["run_id"])
    if args.show_model_output:
        print("\nMODEL OUTPUTS (redacted)")
        for e in events:
            if e.event_type.startswith("llm_"):
                print(json.dumps(redact({"event": e.event_type, **e.data}), indent=2, default=str))
    scope = final.get("baseline_scope")
    if scope:
        covered = ", ".join(scope.services) or "(none)"
        print(f"\nBaseline scope: {scope.basis} -> {covered} {scope.notes or ''}")
    print(f"\nRESULT: {final['status']}" + (f" ({esc.reason}: {esc.detail})" if esc else ""))
    if esc:
        print("Evidence handed to the human:")
        for ev in esc.evidence:
            status = "REFUSED" if ev.rejected else ("ok" if ev.ok else "FAILED")
            print(f"  {ev.id} ({ev.phase}) {ev.tool} on {ev.service}: {status}: {redact(ev.summary)}")
    print(f"LLM usage: {final['usage'].model_dump()}")
    print(f"Audit: {len(events)} events, hash chain valid = {verify_chain(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
