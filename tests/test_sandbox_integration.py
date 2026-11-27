"""Integration tests against the real Docker Compose sandbox (run `make up` first).

These prove the diagnostics see a *real* outage: app-db is actually stopped, connections are
actually refused, and orders-api actually reports it. They also check the audit table's
database permissions. Run with: make test-integration
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
from langgraph.types import Command

from arl.audit import InMemoryAuditLog, PostgresAuditLog, verify_chain
from arl.config import load_dotenv
from arl.models import ToolRequest
from arl.tools.catalog import load_policy, load_services
from arl.tools.diagnostics import DiagnosticToolbox
from arl.tools.probes import NetworkProbe

pytestmark = pytest.mark.integration


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)  # noqa: S603, S607


def wait_for(predicate, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(1)
    return False


@pytest.fixture(scope="module")
def toolbox() -> DiagnosticToolbox:
    return DiagnosticToolbox(load_services(), load_policy(), NetworkProbe(timeout_seconds=3))


def run(toolbox: DiagnosticToolbox, tool: str, service: str):
    return toolbox.run(ToolRequest(tool=tool, service=service), "database_connectivity", "EV-T")


def test_real_outage_is_detected_and_clears_after_restart(toolbox):
    assert wait_for(lambda: run(toolbox, "check_service_health", "orders-api").ok), "sandbox not healthy"

    compose("stop", "app-db")  # INC-001 fault injection
    try:
        tcp = run(toolbox, "check_tcp_port", "app-db")
        health = run(toolbox, "check_service_health", "orders-api")
        assert not tcp.ok and "refused" in tcp.summary.lower()
        assert not health.ok and health.data["status_code"] == 503
        assert health.data["body"]["database"] == "unreachable"
    finally:
        compose("start", "app-db")

    assert wait_for(lambda: run(toolbox, "check_service_health", "orders-api").ok)


@pytest.fixture
def agent_conn():
    load_dotenv()
    url = os.environ.get("OPS_DATABASE_URL")
    if not url:
        pytest.skip("OPS_DATABASE_URL not set")
    with psycopg.connect(url, autocommit=True) as conn:
        yield conn


def test_postgres_audit_log_is_append_only_for_the_agent_role(agent_conn):
    log = PostgresAuditLog(agent_conn)
    run_id = f"RUN-it-{time.time_ns()}"
    for i in range(3):
        log.append(run_id, "system", "integration_event", {"i": i})
    assert verify_chain(log.events(run_id))

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        agent_conn.execute("UPDATE ops.audit_event SET data = '{}' WHERE run_id = %s", (run_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        agent_conn.execute("DELETE FROM ops.audit_event WHERE run_id = %s", (run_id,))


# --- platform-ops: the real restart path -------------------------------------------------


def platform_post(target: str, token: str | None, key: str = "k" * 64) -> httpx.Response:
    headers = {"Idempotency-Key": key}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(
        f"{os.environ.get('PLATFORM_OPS_URL', 'http://localhost:8190')}/v1/services/{target}/restart",
        headers=headers,
        timeout=30,
    )


def test_platform_ops_refuses_bad_token_and_unlisted_targets():
    load_dotenv()
    token = os.environ["PLATFORM_OPS_TOKEN"]
    assert platform_post("app-db", token=None).status_code == 401
    assert platform_post("app-db", token="x" * 40).status_code == 401
    for target in ("ops-db", "orders-api", "platform-ops", "docker-proxy"):
        assert platform_post(target, token=token).status_code == 403


def test_inc001_end_to_end_with_real_restart(toolbox):
    """Stop app-db for real, run the workflow (scripted model), approve, and expect resolved.

    Note: platform-ops allows one restart of app-db per minute, so running this right after
    a demo restart returns 429 and the run escalates with remediation_failed. Wait a minute.
    """
    from arl.cli import DEMO_INCIDENT, build
    from arl.config import Settings
    from arl.graph.workflow import new_run

    settings = Settings.from_env()
    if settings.llm_backend != "scripted":
        pytest.skip("integration tests never call a paid model")
    assert wait_for(lambda: run(toolbox, "check_service_health", "orders-api").ok), "sandbox not healthy"

    compose("stop", "app-db")
    try:
        audit = InMemoryAuditLog()
        graph = build(settings, audit)
        state = new_run(DEMO_INCIDENT)
        config = {"configurable": {"thread_id": state["run_id"]}, "recursion_limit": 60}
        graph.invoke(state, config)
        request = graph.get_state(config).interrupts[0].value["approval_request"]
        decision = {
            "approval_id": request["approval_id"],
            "action_hash": request["action_hash"],
            "approved": True,
            "approver": "integration.tester",
            "comment": "",
            "decided_at": datetime.now(UTC).isoformat(),
        }
        graph.invoke(Command(resume=decision), config)
        final = graph.get_state(config).values
    finally:
        if not run(toolbox, "check_tcp_port", "app-db").ok:
            compose("start", "app-db")

    assert final["status"] == "resolved", final.get("escalation")
    assert final["remediation"]["result"]["status"] == "restarted"
    assert final["verification"].passed
    assert verify_chain(audit.events(final["run_id"]))
