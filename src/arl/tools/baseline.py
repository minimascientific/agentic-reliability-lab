"""Deterministic baseline checks: evidence gathered by code *before* the model classifies.

The first live run showed that a symptom-only ticket ("orders page shows errors") gives the
model nothing to classify on, so it correctly answered "unknown". The fix is not a lower
threshold but better information: before classification, code runs a fixed set of read-only
checks on services chosen from the catalog, and the model classifies with that evidence.

Scope (no knowledge of any particular incident is encoded here):

1. If the ticket names a configuration item that is in the catalog, start from it.
2. Otherwise (missing, or not in the catalog) fall back to the catalog's user-facing
   services: a symptom-only ticket is reported by users, so it is about something they use.
3. Add dependencies from `depends_on`, breadth first, up to `max_dependency_depth`.
4. Stop at `max_services`; anything beyond is dropped and noted as `scope_truncated`.

Industrial analogy: the first-out alarm summary an operator reads before diagnosing a trip.
"""

from __future__ import annotations

from arl.models import BaselineScope
from arl.tools.catalog import BaselinePolicy, Service


def baseline_scope(
    services: dict[str, Service], configuration_item: str | None, policy: BaselinePolicy
) -> BaselineScope:
    notes: list[str] = []
    if configuration_item and configuration_item in services:
        basis, start = "configuration_item", [configuration_item]
    else:
        notes.append("configuration_item_missing" if not configuration_item else "configuration_item_unknown")
        basis, start = "fallback_user_facing", sorted(n for n, s in services.items() if s.user_facing)
        if not start:
            notes.append("no_user_facing_services")

    ordered: list[str] = []
    frontier = list(start)
    for depth in range(policy.max_dependency_depth + 1):
        next_frontier: list[str] = []
        for name in frontier:
            if name in ordered or name not in services:
                continue
            ordered.append(name)
            if depth < policy.max_dependency_depth:
                next_frontier.extend(services[name].depends_on)
        frontier = next_frontier

    if len(ordered) > policy.max_services:
        notes.append("scope_truncated")
        ordered = ordered[: policy.max_services]
    return BaselineScope(basis=basis, services=ordered, notes=notes)


def applicable_tools(service: Service, tools: list[str]) -> list[str]:
    """Only the checks a service supports: health needs a URL, TCP needs an endpoint."""
    supported = {
        "check_service_health": service.health_url is not None,
        "check_tcp_port": service.tcp is not None,
    }
    return [t for t in tools if supported.get(t, False)]
