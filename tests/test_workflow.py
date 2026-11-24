"""Workflow transition tests: the LangGraph routes, end to end, with the scripted LLM.

Each test names the path through the graph it proves. None of them call a real model.
"""

from __future__ import annotations

from arl.audit import verify_chain
from arl.llm import LLMError
from arl.models import Classification, DiagnosticPlan, RemediationProposal, ToolRequest
from tests.conftest import Harness, happy_script


def test_happy_path_pauses_for_approval_then_restarts_and_verifies():
    h = Harness(happy_script())
    paused = h.start()

    # The graph stopped at the approval gate; nothing has been executed yet.
    assert paused["status"] == "awaiting_approval"
    assert h.platform.calls == []
    request = h.pending_approval()
    assert request["action_id"] == "restart_service" and request["target"] == "app-db"
    assert request["incident_number"] == "INC0010001"

    final = h.decide(approved=True)

    assert final["status"] == "resolved"
    assert [target for target, _ in h.platform.calls] == ["app-db"]
    assert final["verification"].passed
    ev = {e.id: e for e in final["evidence"]}
    assert not ev["EV-001"].ok and "503" in ev["EV-001"].summary  # orders-api unhealthy
    assert not ev["EV-002"].ok and "refused" in ev["EV-002"].summary.lower()  # app-db down
    assert final["runbooks"][0].id == "rb-db-connectivity"


def test_audit_trail_records_every_stage_and_chain_verifies():
    h = Harness(happy_script())
    h.start()
    h.decide(approved=True)
    types = h.event_types()
    for expected in (
        "run_started",
        "llm_classify",
        "llm_plan_diagnostics",
        "tool_call",
        "runbooks_retrieved",
        "llm_propose_remediation",
        "policy_decision",
        "approval_requested",
        "approval_decision",
        "remediation_executed",
        "verification",
        "run_resolved",
    ):
        assert expected in types, expected
    assert verify_chain(h.audit.events(h.run_id))


def test_rejected_approval_never_executes():
    h = Harness(happy_script())
    h.start()
    final = h.decide(approved=False, comment="Change freeze in effect")
    assert final["status"] == "escalated"
    assert final["escalation"].reason == "approval_rejected"
    assert h.platform.calls == []
    assert "remediation_executed" not in h.event_types()


def test_approval_for_a_different_action_hash_is_refused_by_the_executor():
    h = Harness(happy_script())
    h.start()
    final = h.decide(approved=True, action_hash="0" * 64)
    assert final["status"] == "escalated"
    assert final["escalation"].reason == "unauthorized_action"
    assert h.platform.calls == []


def test_agent_cannot_approve_its_own_action():
    h = Harness(happy_script())
    h.start()
    final = h.decide(approved=True, approver="agent")
    assert final["escalation"].reason == "unauthorized_action"
    assert h.platform.calls == []


def test_model_proposing_a_protected_target_is_denied_by_policy():
    bad = RemediationProposal(
        escalate=False,
        action_id="restart_service",
        target="ops-db",
        evidence_ids=["EV-001"],
        diagnosis="restart everything",
        confidence=0.99,
    )
    h = Harness(happy_script(propose_remediation=[bad]))
    final = h.start()
    assert final["status"] == "escalated"
    assert final["escalation"].reason == "policy_denied"
    assert "target_protected" in final["policy_decision"].reasons
    assert h.platform.calls == []
    assert h.pending_approval() is None  # never even asked a human


def test_model_inventing_an_action_is_denied_by_policy():
    bad = RemediationProposal(
        escalate=False,
        action_id="drop_database",
        target="app-db",
        evidence_ids=["EV-002"],
        diagnosis="drop and recreate",
        confidence=0.95,
    )
    h = Harness(happy_script(propose_remediation=[bad]))
    final = h.start()
    assert final["escalation"].reason == "policy_denied"
    assert "action_not_in_catalog" in final["policy_decision"].reasons
    assert h.platform.calls == []


def test_proposal_without_evidence_is_denied():
    bad = RemediationProposal(
        escalate=False,
        action_id="restart_service",
        target="app-db",
        evidence_ids=[],
        diagnosis="probably the database",
        confidence=0.9,
    )
    final = Harness(happy_script(propose_remediation=[bad])).start()
    assert "evidence_missing" in final["policy_decision"].reasons


def test_low_confidence_classification_escalates_before_any_tool_runs():
    unsure = Classification(
        category="database_connectivity",
        severity=3,
        affected_service="unknown",
        confidence=0.4,
        rationale="Ticket is vague.",
    )
    h = Harness(happy_script(classify=[unsure]))
    final = h.start()
    assert final["escalation"].reason == "classification_uncertain"
    assert final["evidence"] == []
    assert h.platform.calls == []


def test_model_choosing_to_escalate_is_respected():
    esc = RemediationProposal(
        escalate=True, diagnosis="Database reachable but app still failing.", confidence=0.6
    )
    final = Harness(happy_script(propose_remediation=[esc])).start()
    assert final["escalation"].reason == "model_escalated"


def test_disallowed_tool_request_is_refused_and_recorded():
    plan = [
        DiagnosticPlan(
            requests=[
                ToolRequest(tool="run_shell", service="app-db"),
                ToolRequest(tool="check_tcp_port", service="payroll-db"),
            ],
            done=True,
            rationale="try things",
        )
    ]
    h = Harness(
        happy_script(
            plan_diagnostics=plan,
            propose_remediation=[
                RemediationProposal(escalate=True, diagnosis="no usable evidence", confidence=0.2)
            ],
        )
    )
    final = h.start()
    refused = [e for e in final["evidence"] if e.rejected]
    assert {e.data["reason"] for e in refused} == {"tool_not_in_registry", "service_not_in_catalog"}


def test_failed_verification_escalates():
    h = Harness(happy_script(), fixes=False)  # restart "succeeds" but the database stays down
    h.start()
    final = h.decide(approved=True)
    assert len(h.platform.calls) == 1
    assert final["status"] == "escalated"
    assert final["escalation"].reason == "verification_failed"


def test_invalid_model_output_escalates():
    h = Harness(happy_script(classify=[{"category": "database_connectivity", "severity": 9}]))
    final = h.start()
    assert final["escalation"].reason == "invalid_model_output"


def test_model_refusal_escalates():
    h = Harness(happy_script(classify=[LLMError("model refused the request")]))
    assert h.start()["escalation"].reason == "invalid_model_output"


def test_llm_call_budget_stops_the_run():
    from arl.config import Limits

    h = Harness(happy_script(), limits=Limits(max_llm_calls=2, verify_attempts=1, verify_interval_seconds=0))
    final = h.start()
    assert final["escalation"].reason == "budget_exceeded"
    assert final["usage"].calls == 2
    assert h.platform.calls == []


def test_step_limit_stops_the_run():
    from arl.config import Limits

    h = Harness(
        happy_script(), limits=Limits(max_graph_steps=3, verify_attempts=1, verify_interval_seconds=0)
    )
    final = h.start()
    assert final["status"] == "escalated"
    assert final["escalation"].reason == "step_limit_reached"


def test_diagnostic_loop_is_bounded_even_if_model_never_says_done():
    endless = [
        DiagnosticPlan(
            requests=[ToolRequest(tool="check_tcp_port", service="app-db")],
            done=False,
            rationale="check again",
        )
    ] * 10
    h = Harness(happy_script(plan_diagnostics=endless))
    final = h.start()
    assert final["diagnostic_rounds"] == h.limits.max_diagnostic_rounds
    assert final["status"] == "awaiting_approval"


def test_ticket_text_is_passed_to_the_model_inside_untrusted_tags():
    h = Harness(happy_script())
    h.start()
    prompt_id, content = h.llm.calls[0]
    assert prompt_id == "classify@v1"
    assert "<ticket>" in content and "</ticket>" in content
