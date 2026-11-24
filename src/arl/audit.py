"""Append-only, hash-chained audit trail.

Every significant event in a run (model output, tool call, policy decision, approval,
remediation, verification, escalation) becomes one AuditEvent. Each event stores the hash of
the previous event in the same run, so any edit or deletion breaks the chain and is detected
by `verify_chain`. In Postgres the agent's role also has INSERT but not UPDATE or DELETE.

Industrial analogy: the batch record / electronic signature trail in a regulated plant.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel

Actor = Literal["system", "llm", "tool", "policy", "human", "executor"]
GENESIS = "0" * 64


class AuditEvent(BaseModel):
    run_id: str
    seq: int
    ts: datetime
    actor: Actor
    event_type: str
    data: dict
    prev_hash: str
    hash: str


def _event_hash(
    run_id: str, seq: int, ts: datetime, actor: str, event_type: str, data: dict, prev_hash: str
) -> str:
    body = json.dumps(
        {
            "run_id": run_id,
            "seq": seq,
            "ts": ts.isoformat(),
            "actor": actor,
            "event_type": event_type,
            "data": data,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def make_event(
    run_id: str,
    seq: int,
    actor: Actor,
    event_type: str,
    data: dict,
    prev_hash: str,
    ts: datetime | None = None,
) -> AuditEvent:
    ts = ts or datetime.now(UTC)
    data = json.loads(json.dumps(data, default=str))  # store only JSON-safe values
    return AuditEvent(
        run_id=run_id,
        seq=seq,
        ts=ts,
        actor=actor,
        event_type=event_type,
        data=data,
        prev_hash=prev_hash,
        hash=_event_hash(run_id, seq, ts, actor, event_type, data, prev_hash),
    )


def verify_chain(events: list[AuditEvent]) -> bool:
    prev = GENESIS
    for expected_seq, e in enumerate(events, start=1):
        if e.seq != expected_seq or e.prev_hash != prev:
            return False
        if e.hash != _event_hash(e.run_id, e.seq, e.ts, e.actor, e.event_type, e.data, e.prev_hash):
            return False
        prev = e.hash
    return True


class AuditLog(Protocol):
    def append(self, run_id: str, actor: Actor, event_type: str, data: dict) -> AuditEvent: ...
    def events(self, run_id: str) -> list[AuditEvent]: ...


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._events: dict[str, list[AuditEvent]] = {}

    def append(self, run_id: str, actor: Actor, event_type: str, data: dict) -> AuditEvent:
        chain = self._events.setdefault(run_id, [])
        prev = chain[-1].hash if chain else GENESIS
        event = make_event(run_id, len(chain) + 1, actor, event_type, data, prev)
        chain.append(event)
        return event

    def events(self, run_id: str) -> list[AuditEvent]:
        return list(self._events.get(run_id, []))


class PostgresAuditLog:
    """Writes to ops.audit_event. Serialises appends per run with an advisory lock."""

    def __init__(self, conn) -> None:  # psycopg.Connection with autocommit=True
        self._conn = conn

    def append(self, run_id: str, actor: Actor, event_type: str, data: dict) -> AuditEvent:
        with self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
            row = self._conn.execute(
                "SELECT seq, hash FROM ops.audit_event WHERE run_id = %s ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
            event = make_event(run_id, seq, actor, event_type, data, prev)
            self._conn.execute(
                "INSERT INTO ops.audit_event (run_id, seq, ts, actor, event_type, data, prev_hash, hash)"
                " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (
                    event.run_id,
                    event.seq,
                    event.ts,
                    event.actor,
                    event.event_type,
                    json.dumps(event.data),
                    event.prev_hash,
                    event.hash,
                ),
            )
        return event

    def events(self, run_id: str) -> list[AuditEvent]:
        rows = self._conn.execute(
            "SELECT run_id, seq, ts, actor, event_type, data, prev_hash, hash"
            " FROM ops.audit_event WHERE run_id = %s ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            AuditEvent(
                run_id=r[0],
                seq=r[1],
                ts=r[2],
                actor=r[3],
                event_type=r[4],
                data=r[5],
                prev_hash=r[6],
                hash=r[7],
            )
            for r in rows
        ]
