"""Checkpointer setup.

A checkpointer saves the graph state after every node. That is what lets a run pause at the
approval gate (interrupt) and resume later, even after a process restart when the Postgres
checkpointer is used.

Industrial analogy: retentive memory. The controller can lose power mid-sequence and resume
at the same step with the same data.

Security note: checkpoints are deserialised when a run resumes. We allowlist exactly which
Python types may be rebuilt from a checkpoint, so a tampered checkpoint cannot make the
process construct arbitrary objects.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from arl import models

STATE_TYPES = [
    "Incident",
    "Classification",
    "DiagnosticPlan",
    "ToolRequest",
    "RemediationProposal",
    "Evidence",
    "RunbookRef",
    "PolicyDecision",
    "ApprovalRequest",
    "ApprovalDecision",
    "CheckResult",
    "VerificationResult",
    "Escalation",
    "BaselineScope",
    "LLMUsage",
]


def serializer() -> JsonPlusSerializer:
    for name in STATE_TYPES:
        if not hasattr(models, name):
            raise RuntimeError(f"checkpoint allowlist names unknown type {name}")
    return JsonPlusSerializer(allowed_msgpack_modules=[("arl.models", n) for n in STATE_TYPES])


def memory_checkpointer() -> InMemorySaver:
    """For tests and the terminal demo. State lives only as long as the process."""
    return InMemorySaver(serde=serializer())
