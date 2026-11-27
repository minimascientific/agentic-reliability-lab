"""Typed contracts shared by the workflow, tools, policy engine and services.

Two kinds of model live here:

* **LLM outputs** (Classification, DiagnosticPlan, RemediationProposal). Claude must return
  JSON matching these schemas. They are *proposals*: nothing in them is trusted until
  deterministic code has checked it.
* **System records** (Evidence, PolicyDecision, ApprovalRequest, ...). Only application code
  creates these. The LLM never produces them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["database_connectivity", "integration_failure", "account_access", "unknown"]


class Incident(BaseModel):
    """A ticket as received from the ITSM system. Its text is untrusted user input."""

    model_config = ConfigDict(frozen=True)

    number: str = Field(pattern=r"^INC\d{7}$")
    short_description: str = Field(max_length=200)
    description: str = Field(max_length=4000)
    caller: str
    opened_at: datetime
    configuration_item: str | None = Field(
        default=None,
        max_length=64,
        description="Affected service as recorded on the ticket (ITSM 'CI'), if the caller knew it",
    )


# ---------------------------------------------------------------------------
# LLM outputs: proposals, validated against these schemas, never trusted directly
# ---------------------------------------------------------------------------


class Classification(BaseModel):
    category: Category
    severity: int = Field(ge=1, le=4, description="1 = critical, 4 = low")
    affected_service: str = Field(description="Service name from the service catalog, or 'unknown'")
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=600)


class ToolRequest(BaseModel):
    tool: str = Field(description="Name of a diagnostic tool from the list provided")
    service: str = Field(description="Target service name from the service catalog")


class DiagnosticPlan(BaseModel):
    requests: list[ToolRequest] = Field(max_length=5)
    done: bool = Field(description="True when the evidence is sufficient to propose a fix")
    rationale: str = Field(max_length=600)


class RemediationProposal(BaseModel):
    escalate: bool = Field(description="True if no safe, evidence-backed action exists")
    action_id: str | None = Field(default=None, description="Action name from the action catalog")
    target: str | None = Field(default=None, description="Target service for the action")
    evidence_ids: list[str] = Field(default_factory=list, description="IDs of evidence relied on")
    runbook_ids: list[str] = Field(default_factory=list)
    diagnosis: str = Field(max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# System records: produced only by deterministic code
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    id: str
    tool: str
    service: str
    ok: bool = Field(description="True if the check passed (service healthy, port open, ...)")
    rejected: bool = Field(default=False, description="True if the request was refused by the allowlist")
    summary: str
    data: dict = Field(default_factory=dict)
    collected_at: datetime
    phase: Literal["baseline", "diagnostic"] = Field(
        default="diagnostic",
        description="baseline = chosen by code before classification; diagnostic = requested by the model",
    )


class BaselineScope(BaseModel):
    """Which services the deterministic baseline checks covered, and why."""

    basis: Literal["configuration_item", "fallback_user_facing"]
    services: list[str]
    notes: list[str] = Field(default_factory=list, description="Machine-readable notes, e.g. scope_truncated")


class RunbookRef(BaseModel):
    id: str
    title: str
    excerpt: str
    score: float


PolicyOutcome = Literal["allow", "requires_approval", "deny"]


class PolicyDecision(BaseModel):
    outcome: PolicyOutcome
    reasons: list[str] = Field(description="Machine-readable reason codes")
    action_id: str | None
    target: str | None
    change_type: Literal["standard", "normal"] | None = None


class ApprovalRequest(BaseModel):
    """What a human is asked to approve. The hash binds the approval to exactly this action."""

    approval_id: str
    run_id: str
    incident_number: str
    action_id: str
    target: str
    action_hash: str
    requested_at: datetime
    expires_at: datetime
    diagnosis: str
    evidence_ids: list[str]


class ApprovalDecision(BaseModel):
    approval_id: str
    action_hash: str = Field(description="Must equal the hash shown to the approver")
    approved: bool
    approver: str
    comment: str = ""
    decided_at: datetime


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str


class VerificationResult(BaseModel):
    passed: bool
    attempts: int
    checks: list[CheckResult]


class Escalation(BaseModel):
    reason: str = Field(description="Machine-readable reason code")
    detail: str
    evidence: list[Evidence] = Field(
        default_factory=list, description="Everything collected before escalating: the human's starting point"
    )


class LLMUsage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
        )
