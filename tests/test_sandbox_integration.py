"""Integration tests against the real Docker Compose sandbox (run `make up` first).

These prove the diagnostics see a *real* outage: app-db is actually stopped, connections are
actually refused, and orders-api actually reports it. They also check the audit table's
database permissions. Run with: make test-integration
"""

from __future__ import annotations

import os
import subprocess
import time

import psycopg
import pytest

from arl.audit import PostgresAuditLog, verify_chain
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
