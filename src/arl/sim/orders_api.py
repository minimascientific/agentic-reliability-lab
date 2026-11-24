"""orders-api: a small synthetic internal application backed by app-db.

It exists so the agent has a real service to diagnose. When app-db is stopped, /health
really fails with the real connection error, and requests really return 503.

Run inside Docker Compose (see docker-compose.yml). Logs are JSON lines on stdout.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

SERVICE = "orders-api"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "service": SERVICE,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry)


log = logging.getLogger(SERVICE)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
log.addHandler(_handler)
log.setLevel(logging.INFO)

app = FastAPI(title="orders-api (synthetic)")


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["APP_DATABASE_URL"], connect_timeout=2)


@app.get("/health")
def health() -> JSONResponse:
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        error = str(exc).strip().splitlines()[0]
        log.error("database health check failed", extra={"fields": {"dependency": "app-db", "error": error}})
        return JSONResponse(
            status_code=503, content={"status": "degraded", "database": "unreachable", "error": error}
        )
    return JSONResponse(content={"status": "ok", "database": "ok"})


@app.get("/orders/{order_number}")
def get_order(order_number: str) -> dict:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT order_number, customer, status, total_usd FROM orders WHERE order_number = %s",
                (order_number,),
            ).fetchone()
    except psycopg.Error as exc:
        log.error("order lookup failed", extra={"fields": {"order": order_number, "error": str(exc).strip()}})
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_number": row[0], "customer": row[1], "status": row[2], "total_usd": float(row[3])}
