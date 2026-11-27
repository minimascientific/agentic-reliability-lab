# ADR 0004: Restarts go through platform-ops and a Docker socket proxy

**Status:** accepted, 2026-11-27

## Context
INC-001 is fixed by restarting the stopped `app-db` container. Whatever performs that restart
needs some access to Docker, and raw Docker socket access is equivalent to root on the host:
it can create privileged containers, exec into any container, or delete volumes.

## Decision
Three layers, each with its own allowlist, so no single bug or edited file widens access:

1. **The agent's executor** (`arl/remediation.py`) re-checks policy and the human approval,
   then calls one HTTP endpoint: `POST /v1/services/{target}/restart` on platform-ops, with
   a bearer token and the approved action's hash as the idempotency key. It has no Docker
   access of its own.
2. **platform-ops** (`arl/services/platform_ops.py`) checks the token in constant time,
   checks the target against its *own* allowlist (`config/platform-ops.yaml`, today only
   `app-db` -> container `arl-app-db`), rate-limits to one restart per target per minute,
   and replays the first result for a repeated idempotency key instead of restarting again.
3. **docker-socket-proxy** (`tecnativa/docker-socket-proxy`, pinned) is the only container
   that mounts the socket. It forwards container `stop`/`restart`/`kill` and read-only
   ping/version/events; everything else (create, exec, images, volumes, listing containers)
   returns 403. It sits on an `internal` network with platform-ops and publishes no port.

## Consequences
- The proxy filters by *operation*, not by container: anything that can reach it could
  restart or stop any container. That is why it has no published port and why the target
  allowlist lives in platform-ops. A custom proxy filtering by container name would close
  this gap; not justified for a local sandbox.
- `POST=1` is required for the restart call. With every other write section left at its
  default (denied), the only POST paths that pass are container stop/restart/kill.
- Idempotency records are kept in memory and reset when platform-ops restarts. The agent's
  own 30-minute cooldown and single-use approvals still prevent a repeat restart; persisting
  the records in `ops-db` is a later step.
- The proxy needs `DISABLE_IPV6=1` on hosts without IPv6 (found in the cloud sandbox).
