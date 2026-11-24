"""Remediation: the only code path that can change anything, and its verification.

The executor does not trust the workflow that called it. Before acting it re-runs the policy
engine and re-validates the human approval (defence in depth), then consumes the approval so
it cannot be replayed. If any check fails it raises UnauthorizedAction and nothing is sent.

The executor itself has no Docker or shell access. It can only call the platform-ops
service's narrow API, and only for actions that have an entry in ACTION_HANDLERS.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx

from arl.models import (
    ApprovalDecision,
    ApprovalRequest,
    CheckResult,
    Evidence,
    RemediationProposal,
    VerificationResult,
)
from arl.policy.approvals import ApprovalInvalid, ApprovalStore, action_hash, check_decision
from arl.policy.engine import PolicyEngine
from arl.tools.catalog import Action
from arl.tools.diagnostics import DiagnosticToolbox


class UnauthorizedAction(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class PlatformOps(Protocol):
    def restart_service(self, target: str, idempotency_key: str) -> dict: ...


class HttpPlatformOps:
    """Client for the platform-ops service (the only component allowed to restart containers)."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self._base = (base_url or os.environ.get("PLATFORM_OPS_URL", "http://localhost:8190")).rstrip("/")
        self._token = token or os.environ.get("PLATFORM_OPS_TOKEN", "")
        self._timeout = timeout

    def restart_service(self, target: str, idempotency_key: str) -> dict:
        response = httpx.post(
            f"{self._base}/v1/services/{target}/restart",
            headers={"Authorization": f"Bearer {self._token}", "Idempotency-Key": idempotency_key},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()


# action_id -> how to perform it. An action in the catalog without a handler cannot run.
ACTION_HANDLERS: dict[str, Callable[[PlatformOps, str, str], dict]] = {
    "restart_service": lambda ops, target, key: ops.restart_service(target, idempotency_key=key),
}


class RemediationExecutor:
    def __init__(self, policy_engine: PolicyEngine, approvals: ApprovalStore, platform: PlatformOps):
        self.policy_engine = policy_engine
        self.approvals = approvals
        self.platform = platform

    def execute(
        self,
        *,
        run_id: str,
        incident_number: str,
        proposal: RemediationProposal,
        evidence: list[Evidence],
        recent_actions: list[tuple[str, str, datetime]],
        approval_request: ApprovalRequest | None,
        approval_decision: ApprovalDecision | None,
        now: datetime,
    ) -> dict:
        decision = self.policy_engine.evaluate(proposal, evidence, recent_actions, now)
        if decision.outcome == "deny":
            raise UnauthorizedAction("policy_denied", ",".join(decision.reasons))

        action_id, target = proposal.action_id, proposal.target
        if not (action_id and target):  # unreachable after a non-deny decision; never assume
            raise UnauthorizedAction("no_action_proposed")
        if action_id not in ACTION_HANDLERS:
            raise UnauthorizedAction("no_handler_for_action", action_id)

        if decision.outcome == "requires_approval":
            if approval_request is None or approval_decision is None:
                raise UnauthorizedAction("approval_missing")
            stored = self.approvals.get_request(approval_request.approval_id)
            if stored is None or stored != approval_request:
                raise UnauthorizedAction("approval_not_on_record")
            try:
                check_decision(
                    stored,
                    approval_decision,
                    run_id=run_id,
                    incident_number=incident_number,
                    action_id=action_id,
                    target=target,
                    now=now,
                )
            except ApprovalInvalid as exc:
                raise UnauthorizedAction(exc.reason, str(exc)) from exc
            if not self.approvals.consume(stored.approval_id):
                raise UnauthorizedAction("approval_already_used")

        key = action_hash(run_id, incident_number, action_id, target)
        result = ACTION_HANDLERS[action_id](self.platform, target, key)
        return {"action_id": action_id, "target": target, "idempotency_key": key, "result": result}


class Verifier:
    """Checks the system after remediation. Success is measured, not assumed.

    Industrial analogy: confirm the valve moved by its limit switch, not by the output bit.
    """

    def __init__(
        self,
        toolbox: DiagnosticToolbox,
        attempts: int,
        interval_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.toolbox = toolbox
        self.attempts = attempts
        self.interval = interval_seconds
        self.sleep = sleep

    def verify(self, action: Action) -> VerificationResult:
        checks: list[CheckResult] = []
        for attempt in range(1, self.attempts + 1):
            checks = []
            for c in action.verification:
                ok, summary = self.toolbox.check(c.check, c.service)
                checks.append(CheckResult(name=f"{c.check}:{c.service}", passed=ok, detail=summary))
            if checks and all(c.passed for c in checks):
                return VerificationResult(passed=True, attempts=attempt, checks=checks)
            if attempt < self.attempts:
                self.sleep(self.interval)
        return VerificationResult(passed=False, attempts=self.attempts, checks=checks)
