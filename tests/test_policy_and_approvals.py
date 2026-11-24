"""Unit tests for the deterministic controls: policy engine, approval binding, executor, audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arl.audit import InMemoryAuditLog, verify_chain
from arl.models import ApprovalDecision, Evidence, RemediationProposal
from arl.policy.approvals import InMemoryApprovalStore, new_request
from arl.policy.engine import PolicyEngine
from arl.remediation import RemediationExecutor, UnauthorizedAction
from arl.tools.catalog import load_actions, load_policy
from tests.conftest import FakePlatform, FakeSandbox

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
EVIDENCE = [
    Evidence(
        id="EV-001", tool="check_tcp_port", service="app-db", ok=False, summary="refused", collected_at=NOW
    )
]


def proposal(**kw) -> RemediationProposal:
    base = dict(
        escalate=False,
        action_id="restart_service",
        target="app-db",
        evidence_ids=["EV-001"],
        diagnosis="app-db down",
        confidence=0.9,
    )
    base.update(kw)
    return RemediationProposal(**base)


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(load_actions(), load_policy())


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"action_id": "drop_database"}, "action_not_in_catalog"),
        ({"target": "ops-db"}, "target_protected"),
        ({"target": "orders-api"}, "target_not_allowlisted"),
        ({"confidence": 0.5}, "confidence_below_threshold"),
        ({"evidence_ids": []}, "evidence_missing"),
        ({"evidence_ids": ["EV-999"]}, "evidence_unknown"),
    ],
)
def test_policy_denies_with_a_reason(engine, kw, reason):
    decision = engine.evaluate(proposal(**kw), EVIDENCE, [], NOW)
    assert decision.outcome == "deny"
    assert reason in decision.reasons


def test_policy_requires_approval_for_normal_change(engine):
    decision = engine.evaluate(proposal(), EVIDENCE, [], NOW)
    assert decision.outcome == "requires_approval"
    assert decision.reasons == ["normal_change_requires_approval"]


def test_policy_enforces_cooldown(engine):
    recent = [("restart_service", "app-db", NOW - timedelta(minutes=10))]
    assert "cooldown_active" in engine.evaluate(proposal(), EVIDENCE, recent, NOW).reasons
    old = [("restart_service", "app-db", NOW - timedelta(minutes=45))]
    assert engine.evaluate(proposal(), EVIDENCE, old, NOW).outcome == "requires_approval"


def test_rejected_evidence_cannot_be_cited(engine):
    refused = [EVIDENCE[0].model_copy(update={"rejected": True})]
    assert "evidence_unknown" in engine.evaluate(proposal(), refused, [], NOW).reasons


# ---------------------------------------------------------------- executor -------------


class Setup:
    def __init__(self):
        self.store = InMemoryApprovalStore()
        self.platform = FakePlatform(FakeSandbox())
        engine = PolicyEngine(load_actions(), load_policy())
        self.executor = RemediationExecutor(engine, self.store, self.platform)
        self.request = new_request("RUN-1", "INC0010001", proposal(), NOW, ttl_minutes=15)
        self.store.save_request(self.request)

    def decision(self, **kw) -> ApprovalDecision:
        base = dict(
            approval_id=self.request.approval_id,
            action_hash=self.request.action_hash,
            approved=True,
            approver="alison.gorman",
            decided_at=NOW,
        )
        base.update(kw)
        return ApprovalDecision(**base)

    def run(self, decision, prop=None, now=NOW, request=None):
        return self.executor.execute(
            run_id="RUN-1",
            incident_number="INC0010001",
            proposal=prop or proposal(),
            evidence=EVIDENCE,
            recent_actions=[],
            approval_request=request or self.request,
            approval_decision=decision,
            now=now,
        )


def test_executor_runs_with_valid_approval_exactly_once():
    s = Setup()
    result = s.run(s.decision())
    assert result["target"] == "app-db" and len(s.platform.calls) == 1
    with pytest.raises(UnauthorizedAction, match="approval_already_used"):
        s.run(s.decision())
    assert len(s.platform.calls) == 1


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"approved": False}, "rejected_by_human"),
        ({"approver": "agent"}, "approver_not_human"),
        ({"action_hash": "f" * 64}, "action_hash_mismatch"),
    ],
)
def test_executor_refuses_invalid_decisions(kw, reason):
    s = Setup()
    with pytest.raises(UnauthorizedAction) as exc:
        s.run(s.decision(**kw))
    assert exc.value.reason == reason
    assert s.platform.calls == []


def test_executor_refuses_missing_approval():
    s = Setup()
    with pytest.raises(UnauthorizedAction, match="approval_missing"):
        s.executor.execute(
            run_id="RUN-1",
            incident_number="INC0010001",
            proposal=proposal(),
            evidence=EVIDENCE,
            recent_actions=[],
            approval_request=None,
            approval_decision=None,
            now=NOW,
        )
    assert s.platform.calls == []


def test_executor_refuses_expired_approval():
    s = Setup()
    with pytest.raises(UnauthorizedAction, match="approval_expired"):
        s.run(s.decision(), now=NOW + timedelta(minutes=16))
    assert s.platform.calls == []


def test_approval_cannot_be_reused_for_a_different_incident():
    s = Setup()
    with pytest.raises(UnauthorizedAction, match="action_hash_mismatch"):
        s.executor.execute(
            run_id="RUN-1",
            incident_number="INC0099999",
            proposal=proposal(),
            evidence=EVIDENCE,
            recent_actions=[],
            approval_request=s.request,
            approval_decision=s.decision(),
            now=NOW,
        )
    assert s.platform.calls == []


def test_executor_rechecks_policy_even_with_an_approval():
    s = Setup()
    with pytest.raises(UnauthorizedAction, match="policy_denied"):
        s.run(s.decision(), prop=proposal(target="ops-db"))
    assert s.platform.calls == []


def test_forged_request_not_on_record_is_refused():
    s = Setup()
    forged = s.request.model_copy(update={"approval_id": "APR-forged"})
    with pytest.raises(UnauthorizedAction, match="approval_not_on_record"):
        s.run(s.decision(approval_id="APR-forged"), request=forged)


# ---------------------------------------------------------------- audit ----------------


def test_audit_chain_detects_tampering():
    log = InMemoryAuditLog()
    for i in range(3):
        log.append("RUN-1", "system", f"event_{i}", {"i": i})
    events = log.events("RUN-1")
    assert verify_chain(events)

    edited = [e.model_copy() for e in events]
    edited[1] = edited[1].model_copy(update={"data": {"i": 99}})
    assert not verify_chain(edited)

    assert not verify_chain([events[0], events[2]])  # deletion
