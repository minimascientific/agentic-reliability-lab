-- ops-db: the agent's own records. Separate from app-db so a broken application database
-- can never take the audit trail, approvals or workflow checkpoints down with it.
CREATE EXTENSION IF NOT EXISTS vector;   -- used by runbook retrieval in a later increment
CREATE SCHEMA ops;

-- Append-only audit trail, hash-chained per run (see src/arl/audit.py).
CREATE TABLE ops.audit_event (
    run_id      TEXT        NOT NULL,
    seq         INTEGER     NOT NULL CHECK (seq >= 1),
    ts          TIMESTAMPTZ NOT NULL,
    actor       TEXT        NOT NULL CHECK (actor IN ('system', 'llm', 'tool', 'policy', 'human', 'executor')),
    event_type  TEXT        NOT NULL,
    data        JSONB       NOT NULL,
    prev_hash   CHAR(64)    NOT NULL,
    hash        CHAR(64)    NOT NULL UNIQUE,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX audit_event_type_idx ON ops.audit_event (event_type, ts DESC);

-- Approval requests and their single-use consumption.
CREATE TABLE ops.approval (
    approval_id     TEXT PRIMARY KEY,
    run_id          TEXT        NOT NULL,
    incident_number TEXT        NOT NULL,
    action_id       TEXT        NOT NULL,
    target          TEXT        NOT NULL,
    action_hash     CHAR(64)    NOT NULL,
    request         JSONB       NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    consumed_at     TIMESTAMPTZ,
    CONSTRAINT approval_expiry_after_request CHECK (expires_at > requested_at)
);
