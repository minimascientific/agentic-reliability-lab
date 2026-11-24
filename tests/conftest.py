"""Shared fixtures: a fake sandbox (no Docker), a fake platform-ops, and the scripted LLM.

Routine tests never touch the network or the Anthropic API, so they are free and repeatable.
The fake sandbox behaves like the real one: app-db is down until platform-ops restarts it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langgraph.types import Command

from arl.audit import InMemoryAuditLog
from arl.config import Limits
from arl.graph.checkpointing import memory_checkpointer
from arl.graph.workflow import Deps, build_workflow, new_run
from arl.knowledge import KeywordRetriever
from arl.llm import ScriptedLLM
from arl.models import Classification, DiagnosticPlan, Incident, RemediationProposal, ToolRequest
from arl.policy.approvals import InMemoryApprovalStore
from arl.policy.engine import PolicyEngine
from arl.remediation import RemediationExecutor, Verifier
from arl.tools.catalog import load_actions, load_policy, load_services
from arl.tools.diagnostics import DiagnosticToolbox
from arl.tools.probes import HttpResult

REFUSED = "ConnectionRefusedError: [Errno 111] Connection refused"


class FakeSandbox:
    """Stands in for the Docker Compose stack. `app_db_up` is the injected fault."""

    def __init__(self, app_db_up: bool = False):
        self.app_db_up = app_db_up

    def http_get(self, url: str) -> HttpResult:
        if "8101" in url:  # orders-api /health
            if self.app_db_up:
                return HttpResult(status_code=200, body={"status": "ok", "database": "ok"}, error=None)
            return HttpResult(
                status_code=503,
                error=None,
                body={
                    "status": "degraded",
                    "database": "unreachable",
                    "error": 'connection failed: connection to server at "app-db" (172.18.0.3), port 5432 '
                    "failed: Connection refused",
                },
            )
        return HttpResult(status_code=None, body=None, error="ConnectError: unknown host")

    def tcp_connect(self, host: str, port: int) -> str | None:
        if port == 5434:
            return None if self.app_db_up else REFUSED
        return None


class FakePlatform:
    """Records every restart request. Restarting app-db clears the fault unless `fixes` is False."""

    def __init__(self, sandbox: FakeSandbox, fixes: bool = True):
        self.sandbox = sandbox
        self.fixes = fixes
        self.calls: list[tuple[str, str]] = []

    def restart_service(self, target: str, idempotency_key: str) -> dict:
        self.calls.append((target, idempotency_key))
        if target == "app-db" and self.fixes:
            self.sandbox.app_db_up = True
        return {"target": target, "status": "restarted"}


def inc001() -> Incident:
    return Incident(
        number="INC0010001",
        short_description="Orders page shows errors for the whole Sales Ops team",
        description="Since about 09:10 every order lookup returns 'service unavailable'. "
        "Nothing changed on our side. Affects about 40 users.",
        caller="dana.whitfield@corp.example",
        opened_at=datetime(2026, 11, 25, 9, 14, tzinfo=UTC),
    )


def happy_script(**overrides) -> dict:
    """The model outputs for a clean INC-001 run. Override any prompt's list to vary it."""
    script = {
        "classify": [
            Classification(
                category="database_connectivity",
                severity=2,
                affected_service="orders-api",
                confidence=0.9,
                rationale="Application errors suggest its database is unreachable.",
            )
        ],
        "plan_diagnostics": [
            DiagnosticPlan(
                requests=[
                    ToolRequest(tool="check_service_health", service="orders-api"),
                    ToolRequest(tool="check_tcp_port", service="app-db"),
                ],
                done=False,
                rationale="Confirm the app is unhealthy and test the database port.",
            ),
            DiagnosticPlan(requests=[], done=True, rationale="Evidence is sufficient."),
        ],
        "propose_remediation": [
            RemediationProposal(
                escalate=False,
                action_id="restart_service",
                target="app-db",
                evidence_ids=["EV-001", "EV-002"],
                runbook_ids=["rb-db-connectivity"],
                diagnosis="app-db refuses TCP connections, so orders-api health fails.",
                confidence=0.85,
            )
        ],
    }
    script.update(overrides)
    return script


class Harness:
    """Builds the graph with fakes and drives it like the CLI or UI would."""

    def __init__(
        self, script: dict, *, app_db_up: bool = False, fixes: bool = True, limits: Limits | None = None
    ):
        self.sandbox = FakeSandbox(app_db_up=app_db_up)
        self.platform = FakePlatform(self.sandbox, fixes=fixes)
        self.llm = ScriptedLLM(script)
        self.audit = InMemoryAuditLog()
        self.approvals = InMemoryApprovalStore()
        actions, policy = load_actions(), load_policy()
        engine = PolicyEngine(actions, policy)
        toolbox = DiagnosticToolbox(load_services(), policy, self.sandbox)
        self.limits = limits or Limits(verify_attempts=2, verify_interval_seconds=0)
        self.deps = Deps(
            llm=self.llm,
            toolbox=toolbox,
            retriever=KeywordRetriever(),
            policy_engine=engine,
            approvals=self.approvals,
            executor=RemediationExecutor(engine, self.approvals, self.platform),
            verifier=Verifier(
                toolbox,
                self.limits.verify_attempts,
                self.limits.verify_interval_seconds,
                sleep=lambda _: None,
            ),
            audit=self.audit,
            actions=actions,
            policy=policy,
            limits=self.limits,
        )
        self.graph = build_workflow(self.deps).compile(checkpointer=memory_checkpointer())
        self.state = new_run(inc001())
        self.config = {"configurable": {"thread_id": self.state["run_id"]}, "recursion_limit": 50}

    @property
    def run_id(self) -> str:
        return self.state["run_id"]

    def start(self) -> dict:
        return self.graph.invoke(self.state, self.config)

    def pending_approval(self) -> dict | None:
        snapshot = self.graph.get_state(self.config)
        return snapshot.interrupts[0].value["approval_request"] if snapshot.interrupts else None

    def decide(self, approved: bool, approver: str = "alison.gorman", **override) -> dict:
        request = self.pending_approval()
        assert request is not None, "graph is not waiting for approval"
        decision = {
            "approval_id": request["approval_id"],
            "action_hash": request["action_hash"],
            "approved": approved,
            "approver": approver,
            "comment": "",
            "decided_at": datetime.now(UTC).isoformat(),
        }
        decision.update(override)
        return self.graph.invoke(Command(resume=decision), self.config)

    def event_types(self) -> list[str]:
        return [e.event_type for e in self.audit.events(self.run_id)]


@pytest.fixture
def harness():
    return Harness
