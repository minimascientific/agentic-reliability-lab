"""The workflow's shared state.

LangGraph passes one state object from node to node. Each node returns only the keys it wants
to change, and LangGraph merges them in. For most keys the new value simply replaces the old
one. Keys annotated with a *reducer* are merged differently:

* `evidence` uses `operator.add`, so each node's new evidence is appended, never overwritten.
* `usage` and `steps` are summed, so token spend and step count accumulate across nodes.

Industrial analogy: a UDT shared by every routine, where some members are latched or totalised
instead of overwritten each scan.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from arl.models import (
    ApprovalDecision,
    ApprovalRequest,
    BaselineScope,
    Classification,
    DiagnosticPlan,
    Escalation,
    Evidence,
    Incident,
    LLMUsage,
    PolicyDecision,
    RemediationProposal,
    RunbookRef,
    VerificationResult,
)

Status = Literal["running", "awaiting_approval", "resolved", "escalated"]


class RunState(TypedDict, total=False):
    run_id: str
    incident: Incident
    status: Status

    # LLM outputs (proposals)
    classification: Classification
    diagnostic_plan: DiagnosticPlan
    proposal: RemediationProposal

    # Deterministic records
    baseline_scope: BaselineScope
    evidence: Annotated[list[Evidence], operator.add]
    runbooks: list[RunbookRef]
    diagnostic_rounds: int
    tool_calls: int
    policy_decision: PolicyDecision
    approval_request: ApprovalRequest
    approval_decision: ApprovalDecision
    remediation: dict
    verification: VerificationResult
    escalation: Escalation

    # Accounting
    usage: Annotated[LLMUsage, operator.add]
    steps: Annotated[int, operator.add]
