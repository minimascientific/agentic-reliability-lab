"""An evidence-aware stand-in for the model, used by the free demo and the workflow tests.

The first live run showed the old scripted answers assumed knowledge the model could not have
had: they classified a symptom-only ticket as a database problem with 0.9 confidence. These
functions answer only from what is in the prompt content they receive, using simple, visible
rules. They are not a model and are not evaluated as one; their job is to keep deterministic
tests honest about what information exists at each stage.

Rules (generic, not tied to one incident):
- classify: a failed, non-refused check on a catalog service described as a database, while a
  service that depends on it is also failing -> database_connectivity. Failures without that
  pattern -> unknown, 0.5. No failures -> unknown, 0.3.
- plan_diagnostics: if the evidence already shows a failed dependency, stop; otherwise ask for
  the health and TCP checks of the affected service once.
- propose_remediation: restart the failed dependency, citing the failing evidence; escalate
  if there is no failed dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from arl.models import Classification, DiagnosticPlan, RemediationProposal, ToolRequest

_EVIDENCE = re.compile(
    r"^- (?P<id>EV-\d+) \((?P<phase>\w+)\) \[(?P<tool>\w+) on (?P<service>[\w-]+)\] "
    r"(?P<refused>REFUSED: )?(?P<status>ok|FAILED): ",
    re.MULTILINE,
)
_CATALOG = re.compile(
    r"^- (?P<name>[\w-]+): (?P<desc>.*?)(?: Depends on: (?P<deps>[\w, -]+)\.)?$", re.MULTILINE
)


@dataclass
class _Facts:
    failed: dict[str, list[str]]  # service -> failing evidence ids
    databases: set[str]
    dependants: dict[str, set[str]]  # dependency -> services that depend on it

    def failed_dependency(self) -> tuple[str, list[str]] | None:
        for db in sorted(self.databases):
            users = self.dependants.get(db, set())
            failing_users = [u for u in sorted(users) if u in self.failed]
            if db in self.failed and failing_users:
                return db, self.failed[failing_users[0]] + self.failed[db]
        return None


def _facts(content: str) -> _Facts:
    failed: dict[str, list[str]] = {}
    for m in _EVIDENCE.finditer(content):
        if m["status"] == "FAILED" and not m["refused"]:
            failed.setdefault(m["service"], []).append(m["id"])
    databases, dependants = set(), {}
    for m in _CATALOG.finditer(content):
        if "database" in m["desc"].lower() and not m["deps"]:
            databases.add(m["name"])
        for dep in (m["deps"] or "").split(","):
            if dep.strip():
                dependants.setdefault(dep.strip(), set()).add(m["name"])
    return _Facts(failed, databases, dependants)


def classify(content: str) -> Classification:
    facts = _facts(content)
    found = facts.failed_dependency()
    if found:
        db, ids = found
        users = sorted(facts.dependants[db])
        return Classification(
            category="database_connectivity",
            severity=2,
            affected_service=users[0],
            confidence=0.85,
            rationale=f"(stand-in) {', '.join(ids)}: {users[0]} is failing and its database {db} fails too.",
        )
    if facts.failed:
        ids = [i for v in facts.failed.values() for i in v]
        return Classification(
            category="unknown",
            severity=3,
            affected_service=sorted(facts.failed)[0],
            confidence=0.5,
            rationale=f"(stand-in) {', '.join(ids)} fail, but no failing dependency explains them.",
        )
    return Classification(
        category="unknown",
        severity=3,
        affected_service="unknown",
        confidence=0.3,
        rationale="(stand-in) Baseline evidence is healthy or absent; the ticket alone is not enough.",
    )


def plan_diagnostics(content: str) -> DiagnosticPlan:
    if _facts(content).failed_dependency():
        return DiagnosticPlan(requests=[], done=True, rationale="(stand-in) Baseline evidence is sufficient.")
    m = re.search(r'"affected_service":\s*"([\w-]+)"', content)
    service = m.group(1) if m else "unknown"
    return DiagnosticPlan(
        requests=[
            ToolRequest(tool="check_service_health", service=service),
            ToolRequest(tool="check_tcp_port", service=service),
        ],
        done=False,
        rationale="(stand-in) Re-check the affected service.",
    )


def propose_remediation(content: str) -> RemediationProposal:
    found = _facts(content).failed_dependency()
    if not found:
        return RemediationProposal(
            escalate=True, diagnosis="(stand-in) No failed dependency to act on.", confidence=0.3
        )
    db, ids = found
    return RemediationProposal(
        escalate=False,
        action_id="restart_service",
        target=db,
        evidence_ids=ids,
        runbook_ids=["rb-db-connectivity"],
        diagnosis=f"(stand-in) {db} refuses connections ({', '.join(ids)}), so its dependants fail.",
        confidence=0.85,
    )


def script(rounds: int = 3) -> dict:
    """A ScriptedLLM script that answers every stage from its input (enough for one run)."""
    return {
        "classify": [classify],
        "plan_diagnostics": [plan_diagnostics] * rounds,
        "propose_remediation": [propose_remediation],
    }
