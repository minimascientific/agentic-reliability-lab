"""Human approvals bound to one specific action, target and incident.

An approval is valid only if all of these hold:
* the decision says approved, by a named human (not the agent),
* its action_hash matches the hash of exactly the action that is about to run,
* it has not expired, and
* it has not been used before (single use).

Industrial analogy: a permit to work. It names one job on one piece of equipment, is signed
by an authorised person, expires, and cannot be reused for a different job.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Protocol

from arl.models import ApprovalDecision, ApprovalRequest, RemediationProposal

AGENT_IDENTITIES = {"agent", "arl-agent", "system"}


class ApprovalInvalid(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def action_hash(run_id: str, incident_number: str, action_id: str, target: str) -> str:
    """SHA-256 over a canonical JSON of what will run. Any change gives a different hash."""
    payload = {"run_id": run_id, "incident": incident_number, "action_id": action_id, "target": target}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ApprovalStore(Protocol):
    def save_request(self, request: ApprovalRequest) -> None: ...
    def get_request(self, approval_id: str) -> ApprovalRequest | None: ...
    def consume(self, approval_id: str) -> bool:
        """Mark the approval used. Return False if it was already used (atomic in Postgres)."""
        ...


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._used: set[str] = set()

    def save_request(self, request: ApprovalRequest) -> None:
        self._requests[request.approval_id] = request

    def get_request(self, approval_id: str) -> ApprovalRequest | None:
        return self._requests.get(approval_id)

    def consume(self, approval_id: str) -> bool:
        if approval_id in self._used:
            return False
        self._used.add(approval_id)
        return True


def new_request(
    run_id: str, incident_number: str, proposal: RemediationProposal, now: datetime, ttl_minutes: int
) -> ApprovalRequest:
    if not (proposal.action_id and proposal.target):
        raise ValueError("cannot request approval for a proposal without an action and target")
    return ApprovalRequest(
        approval_id=f"APR-{uuid.uuid4().hex[:10]}",
        run_id=run_id,
        incident_number=incident_number,
        action_id=proposal.action_id,
        target=proposal.target,
        action_hash=action_hash(run_id, incident_number, proposal.action_id, proposal.target),
        requested_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
        diagnosis=proposal.diagnosis,
        evidence_ids=proposal.evidence_ids,
    )


def check_decision(
    request: ApprovalRequest,
    decision: ApprovalDecision,
    *,
    run_id: str,
    incident_number: str,
    action_id: str,
    target: str,
    now: datetime,
) -> None:
    """Raise ApprovalInvalid unless `decision` authorises exactly this action, right now.

    Does not consume the approval; the executor does that atomically just before acting.
    """
    if decision.approval_id != request.approval_id:
        raise ApprovalInvalid("approval_id_mismatch")
    if not decision.approved:
        raise ApprovalInvalid("rejected_by_human", decision.comment)
    if not decision.approver or decision.approver.lower() in AGENT_IDENTITIES:
        raise ApprovalInvalid("approver_not_human", decision.approver)
    expected = action_hash(run_id, incident_number, action_id, target)
    if decision.action_hash != expected or request.action_hash != expected:
        raise ApprovalInvalid("action_hash_mismatch", "approval was for a different action")
    if now > request.expires_at:
        raise ApprovalInvalid("approval_expired", f"expired at {request.expires_at.isoformat()}")
