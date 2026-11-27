"""Baseline diagnostics: code-chosen, read-only checks that run before the model classifies.

Covers scope selection (known, missing and unknown configuration item; dependency depth and
size bounds), the baseline allowlist, and the workflow paths: failed dependency, healthy
dependencies, inconclusive evidence, tool limits and safe escalation with evidence attached.
"""

from __future__ import annotations

import pytest

from arl.config import Limits
from arl.evals import load_cases
from arl.models import Classification
from arl.tools.baseline import applicable_tools, baseline_scope
from arl.tools.catalog import BaselinePolicy, Service, TcpEndpoint, load_policy, load_services
from arl.tools.diagnostics import DiagnosticToolbox
from arl.tools.probes import HttpResult
from tests.conftest import FakeSandbox, Harness, happy_script, inc001

POLICY = BaselinePolicy(
    tools=["check_service_health", "check_tcp_port"], max_dependency_depth=2, max_services=3
)


def svc(name: str, deps: list[str] | None = None, user_facing: bool = False) -> Service:
    return Service(
        name=name,
        description=f"{name} service",
        tcp=TcpEndpoint(host="localhost", port=1),
        depends_on=deps or [],
        user_facing=user_facing,
    )


# --- scope selection (pure, no workflow) ---------------------------------------------------


def test_known_configuration_item_scopes_to_it_and_its_dependencies():
    scope = baseline_scope(load_services(), "orders-api", POLICY)
    assert scope.basis == "configuration_item"
    assert scope.services == ["orders-api", "app-db"]
    assert scope.notes == []


def test_configuration_item_without_dependencies_checks_only_itself():
    assert baseline_scope(load_services(), "app-db", POLICY).services == ["app-db"]


def test_missing_configuration_item_falls_back_to_user_facing_services():
    scope = baseline_scope(load_services(), None, POLICY)
    assert scope.basis == "fallback_user_facing"
    assert scope.services == ["orders-api", "app-db"]
    assert "ops-db" not in scope.services  # not user facing, nothing depends on it
    assert scope.notes == ["configuration_item_missing"]


def test_configuration_item_not_in_catalog_is_noted_and_falls_back():
    scope = baseline_scope(load_services(), "payroll-db", POLICY)
    assert scope.basis == "fallback_user_facing"
    assert scope.notes == ["configuration_item_unknown"]
    assert "payroll-db" not in scope.services


def test_dependency_depth_is_bounded():
    services = {n: svc(n, d) for n, d in {"a": ["b"], "b": ["c"], "c": ["d"], "d": []}.items()}
    policy = POLICY.model_copy(update={"max_services": 10})
    assert baseline_scope(services, "a", policy).services == ["a", "b", "c"]


def test_scope_size_is_bounded_and_truncation_is_noted():
    services = {"a": svc("a", ["b", "c", "d"]), **{n: svc(n) for n in "bcd"}}
    scope = baseline_scope(services, "a", POLICY)
    assert scope.services == ["a", "b", "c"]
    assert "scope_truncated" in scope.notes


def test_dependency_cycles_terminate():
    services = {"a": svc("a", ["b"]), "b": svc("b", ["a"])}
    assert baseline_scope(services, "a", POLICY).services == ["a", "b"]


def test_catalog_without_user_facing_services_gives_empty_scope():
    scope = baseline_scope({"x": svc("x")}, None, POLICY)
    assert scope.services == [] and "no_user_facing_services" in scope.notes


def test_only_checks_a_service_supports_are_run():
    services = load_services()
    tools = POLICY.tools
    assert applicable_tools(services["orders-api"], tools) == ["check_service_health", "check_tcp_port"]
    assert applicable_tools(services["app-db"], tools) == ["check_tcp_port"]


def test_baseline_has_its_own_allowlist():
    policy = load_policy()
    policy.baseline.tools = ["check_tcp_port"]
    toolbox = DiagnosticToolbox(load_services(), policy, FakeSandbox())
    ev = toolbox.run_baseline("check_service_health", "orders-api", "EV-001")
    assert ev.rejected and ev.data["reason"] == "tool_not_allowed_for_baseline"
    assert toolbox.run_baseline("check_tcp_port", "payroll-db", "EV-002").data["reason"] == (
        "service_not_in_catalog"
    )


# --- workflow paths ---------------------------------------------------------------------


def classify_prompt(h: Harness) -> str:
    return next(content for prompt_id, content in h.llm.calls if prompt_id.startswith("classify@"))


def scope_event(h: Harness) -> dict:
    return next(e.data for e in h.audit.events(h.run_id) if e.event_type == "baseline_scope")


def test_failed_dependency_with_known_configuration_item_resolves():
    incident = inc001().model_copy(update={"configuration_item": "orders-api"})
    h = Harness(happy_script(), incident=incident)
    h.start()
    assert scope_event(h)["basis"] == "configuration_item"
    content = classify_prompt(h)
    assert "EV-003 (baseline) [check_tcp_port on app-db] FAILED" in content  # model sees evidence
    assert "Depends on: app-db" in content  # and the dependency it needs to reason
    assert h.decide(approved=True)["status"] == "resolved"


def test_failed_dependency_without_configuration_item_resolves_via_fallback():
    h = Harness(happy_script())  # the original symptom-only ticket
    h.start()
    assert scope_event(h)["basis"] == "fallback_user_facing"
    assert h.decide(approved=True)["status"] == "resolved"


def test_healthy_dependencies_escalate_with_evidence_and_never_ask_for_approval():
    h = Harness(happy_script(), app_db_up=True)
    final = h.start()
    assert final["status"] == "escalated"
    assert final["escalation"].reason == "classification_uncertain"
    assert final["escalation"].evidence and all(e.ok for e in final["escalation"].evidence)
    assert h.pending_approval() is None and h.platform.calls == []
    assert final["usage"].calls == 1  # stopped after classification


class AppBrokenSandbox(FakeSandbox):
    """orders-api fails but its database is fine: the evidence does not explain the symptom."""

    def http_get(self, url: str) -> HttpResult:
        return HttpResult(status_code=500, body={"status": "error"}, error=None)


def test_inconclusive_evidence_escalates():
    h = Harness(happy_script(), app_db_up=True)
    h.sandbox.__class__ = AppBrokenSandbox
    final = h.start()
    assert final["classification"].category == "unknown"
    assert final["escalation"].reason == "classification_uncertain"
    assert any(not e.ok for e in final["escalation"].evidence)
    assert h.platform.calls == []


def test_confidence_threshold_is_unchanged_even_with_failing_evidence():
    """Evidence improves what the model knows; it never lowers the bar it must clear."""
    almost = Classification(
        category="database_connectivity",
        severity=2,
        affected_service="orders-api",
        confidence=0.69,
        rationale="EV-003 suggests the database, but not certain.",
    )
    h = Harness(happy_script(classify=[almost]))
    final = h.start()
    assert h.deps.limits.min_confidence == 0.7
    assert final["escalation"].reason == "classification_uncertain"
    assert h.platform.calls == []


def test_baseline_checks_count_against_the_tool_call_limit():
    limits = Limits(max_tool_calls=2, verify_attempts=1, verify_interval_seconds=0)
    h = Harness(happy_script(), limits=limits)
    final = h.start()
    baseline = [e for e in final["evidence"] if e.phase == "baseline"]
    assert len(baseline) == 2
    assert "tool_limit_reached" in h.event_types()
    assert final["tool_calls"] <= 2


def test_escalation_audit_record_carries_the_evidence():
    h = Harness(happy_script(), app_db_up=True)
    h.start()
    record = next(e.data for e in h.audit.events(h.run_id) if e.event_type == "run_escalated")
    assert [e["id"] for e in record["evidence"]] == ["EV-001", "EV-002", "EV-003"]
    assert record["classification"]["category"] == "unknown"


# --- evaluation cases (regression set, stand-in model) ------------------------------------


@pytest.mark.parametrize("case", load_cases().values(), ids=lambda c: c.id)
def test_evaluation_cases_with_stand_in_model(case):
    h = Harness(happy_script(), incident=case.incident, app_db_up=case.fault == "none")
    final = h.start()
    expect = case.expect
    assert scope_event(h)["basis"] == expect["baseline_basis"]
    assert final["classification"].category == expect["category"]
    if "status_after_approval" in expect:
        assert final["proposal"].target == expect["target"]
        assert h.decide(approved=True)["status"] == expect["status_after_approval"]
    else:
        assert final["status"] == expect["status"]
        assert final["escalation"].reason == expect["escalation"]
        assert h.platform.calls == []


def test_original_symptom_only_ticket_is_preserved_unchanged():
    case = load_cases()["inc001-symptom-only"]
    assert case.incident == inc001()
    assert case.incident.configuration_item is None
