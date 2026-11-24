# ADR 0003: The agent's records live in a separate database

**Status:** accepted, 2026-11-24

## Context
INC-001 deliberately breaks the application database. If audit events, approvals and
workflow checkpoints lived there, the incident would erase the evidence of its own handling.

## Decision
Two PostgreSQL instances: `app-db` (synthetic business data, the fault target) and `ops-db`
(audit, approvals, checkpoints, later the runbook index with pgvector). `ops-db` is a
protected target in policy and cannot be remediated by the agent.

The agent connects to `ops-db` as `arl_agent`, which may INSERT audit events but not UPDATE or
DELETE them. `orders-api` connects to `app-db` as `orders_app`, which may only SELECT.

## Consequences
- One more container. Worth it: the audit trail survives the failure it describes.
