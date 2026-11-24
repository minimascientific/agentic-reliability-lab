"""Deterministic policy engine: decides whether a proposed action may run.

This is the most important module in the project. The model proposes; this code disposes.
It is a pure function of its inputs (proposal, catalog, policy, evidence, recent actions), so
every decision is repeatable, unit-testable, and explainable through its reason codes.

Industrial analogy: the safety instrumented system. The basic control layer (the model) can
ask for anything; interlocks and permissives here decide what actually reaches the plant.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from arl.models import Evidence, PolicyDecision, RemediationProposal
from arl.tools.catalog import Action, Policy

# Reason codes. Each deny/approval decision cites at least one.
ACTION_NOT_IN_CATALOG = "action_not_in_catalog"
TARGET_PROTECTED = "target_protected"
TARGET_NOT_ALLOWLISTED = "target_not_allowlisted"
CONFIDENCE_BELOW_THRESHOLD = "confidence_below_threshold"
EVIDENCE_MISSING = "evidence_missing"
EVIDENCE_UNKNOWN = "evidence_unknown"
COOLDOWN_ACTIVE = "cooldown_active"
NORMAL_CHANGE_REQUIRES_APPROVAL = "normal_change_requires_approval"
STANDARD_CHANGE_PREAPPROVED = "standard_change_preapproved"
NO_ACTION_PROPOSED = "no_action_proposed"


class PolicyEngine:
    def __init__(self, actions: dict[str, Action], policy: Policy):
        self.actions = actions
        self.policy = policy

    def evaluate(
        self,
        proposal: RemediationProposal,
        evidence: list[Evidence],
        recent_actions: list[tuple[str, str, datetime]],
        now: datetime,
    ) -> PolicyDecision:
        """Return allow, requires_approval, or deny, with every reason that applied.

        recent_actions: (action_id, target, executed_at) for cooldown checks.
        All deny rules are evaluated (not just the first) so the audit record explains fully.
        """
        action_id, target = proposal.action_id, proposal.target
        if proposal.escalate or not action_id or not target:
            return PolicyDecision(
                outcome="deny", reasons=[NO_ACTION_PROPOSED], action_id=action_id, target=target
            )

        deny: list[str] = []
        action = self.actions.get(action_id)
        if action is None:
            deny.append(ACTION_NOT_IN_CATALOG)
        if target in self.policy.protected_targets:
            deny.append(TARGET_PROTECTED)
        if action is not None and target not in action.allowed_targets:
            deny.append(TARGET_NOT_ALLOWLISTED)
        if proposal.confidence < self.policy.min_confidence:
            deny.append(CONFIDENCE_BELOW_THRESHOLD)

        if self.policy.require_cited_evidence:
            usable = {e.id for e in evidence if not e.rejected}
            if not proposal.evidence_ids:
                deny.append(EVIDENCE_MISSING)
            elif not set(proposal.evidence_ids) <= usable:
                deny.append(EVIDENCE_UNKNOWN)

        if action is not None and action.cooldown_minutes:
            window = timedelta(minutes=action.cooldown_minutes)
            if any(a == action_id and t == target and now - at < window for a, t, at in recent_actions):
                deny.append(COOLDOWN_ACTIVE)

        if deny:
            return PolicyDecision(
                outcome="deny",
                reasons=deny,
                action_id=action_id,
                target=target,
                change_type=action.change_type if action else None,
            )

        if action is None:  # unreachable: a missing action always adds a deny reason
            raise RuntimeError("policy invariant violated")
        if action.change_type in self.policy.approval.auto_approve_change_types:
            return PolicyDecision(
                outcome="allow",
                reasons=[STANDARD_CHANGE_PREAPPROVED],
                action_id=action_id,
                target=target,
                change_type=action.change_type,
            )
        return PolicyDecision(
            outcome="requires_approval",
            reasons=[NORMAL_CHANGE_REQUIRES_APPROVAL],
            action_id=action_id,
            target=target,
            change_type=action.change_type,
        )
