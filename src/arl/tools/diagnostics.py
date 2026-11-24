"""Read-only diagnostic tools and the allowlist that guards them.

The model *requests* tools by name; this module decides whether each request is allowed and
runs it. A request for an unknown tool, a tool not allowed for the incident category, or a
service outside the catalog is refused and recorded as rejected evidence. None of these tools
can change anything.

Industrial analogy: read-only monitoring tags. The HMI can display them; it cannot write them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from arl.models import Evidence, ToolRequest
from arl.tools.catalog import Policy, Service
from arl.tools.probes import Probe


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    read_only: bool
    run: Callable[[Service], tuple[bool, str, dict]]  # -> (ok, summary, data)


class DiagnosticToolbox:
    def __init__(self, services: dict[str, Service], policy: Policy, probe: Probe):
        self.services = services
        self.policy = policy
        self.probe = probe
        self.tools: dict[str, ToolSpec] = {
            spec.name: spec
            for spec in (
                ToolSpec(
                    "check_service_health",
                    "HTTP GET the service's /health endpoint and report its status and body.",
                    True,
                    self._check_health,
                ),
                ToolSpec(
                    "check_tcp_port",
                    "Open a TCP connection to the service's port to see if it is listening.",
                    True,
                    self._check_tcp,
                ),
            )
        }
        if not all(t.read_only for t in self.tools.values()):
            raise RuntimeError("diagnostic tools must be read-only")

    # -- what the model is told it may use -------------------------------------------------

    def allowed_tools(self, category: str) -> list[ToolSpec]:
        names = self.policy.diagnostic_tools.get(category, [])
        return [self.tools[n] for n in names if n in self.tools]

    def describe(self, category: str) -> str:
        tools = "\n".join(f"- {t.name}: {t.description}" for t in self.allowed_tools(category))
        services = "\n".join(f"- {s.name}: {s.description}" for s in self.services.values())
        return f"Diagnostic tools:\n{tools or '- (none for this category)'}\n\nServices:\n{services}"

    # -- execution, with the allowlist enforced here, not in the prompt ----------------------

    def run(self, request: ToolRequest, category: str, evidence_id: str) -> Evidence:
        now = datetime.now(UTC)
        allowed = {t.name for t in self.allowed_tools(category)}
        if request.tool not in self.tools:
            return self._rejected(evidence_id, request, "tool_not_in_registry", now)
        if request.tool not in allowed:
            return self._rejected(evidence_id, request, "tool_not_allowed_for_category", now)
        service = self.services.get(request.service)
        if service is None:
            return self._rejected(evidence_id, request, "service_not_in_catalog", now)
        ok, summary, data = self.tools[request.tool].run(service)
        return Evidence(
            id=evidence_id,
            tool=request.tool,
            service=service.name,
            ok=ok,
            summary=summary,
            data=data,
            collected_at=now,
        )

    @staticmethod
    def _rejected(evidence_id: str, request: ToolRequest, reason: str, now: datetime) -> Evidence:
        return Evidence(
            id=evidence_id,
            tool=request.tool,
            service=request.service,
            ok=False,
            rejected=True,
            summary=f"request refused: {reason}",
            data={"reason": reason},
            collected_at=now,
        )

    # -- deterministic checks reused by post-remediation verification ----------------------

    def check(self, kind: str, service_name: str) -> tuple[bool, str]:
        service = self.services[service_name]
        runner = {"tcp": self._check_tcp, "health": self._check_health}[kind]
        ok, summary, _ = runner(service)
        return ok, summary

    # -- the tools themselves ------------------------------------------------------------------

    def _check_health(self, service: Service) -> tuple[bool, str, dict]:
        if not service.health_url:
            return False, f"{service.name} has no health endpoint", {}
        result = self.probe.http_get(service.health_url)
        if result["error"]:
            return False, f"{service.name} /health unreachable: {result['error']}", dict(result)
        healthy = result["status_code"] == 200
        return (
            healthy,
            f"{service.name} /health returned {result['status_code']}: {result['body']}",
            dict(result),
        )

    def _check_tcp(self, service: Service) -> tuple[bool, str, dict]:
        if not service.tcp:
            return False, f"{service.name} has no TCP endpoint", {}
        error = self.probe.tcp_connect(service.tcp.host, service.tcp.port)
        where = f"{service.tcp.host}:{service.tcp.port}"
        if error:
            return False, f"{service.name} TCP {where} failed: {error}", {"error": error}
        return True, f"{service.name} TCP {where} accepting connections", {}
