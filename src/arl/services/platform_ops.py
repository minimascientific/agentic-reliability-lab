"""platform-ops: a narrow restart API in front of a Docker socket proxy.

The agent never touches Docker. Its executor can call exactly one endpoint here:

    POST /v1/services/{target}/restart
    Authorization: Bearer <PLATFORM_OPS_TOKEN>
    Idempotency-Key: <action hash of the approved action>

Controls, each independent of the agent:

- **Bearer token**, compared in constant time; a token shorter than 32 characters is
  refused at startup.
- **Own target allowlist** (`config/platform-ops.yaml`), mapping a service name to a fixed
  container name. Anything else is refused, whatever the agent's catalog says.
- **Idempotency key.** Retrying a request with the same key returns the first result without
  restarting again. Reusing a key for a different target is refused.
- **Rate limit** per target (`min_interval_seconds`).
- **Least-privilege Docker access.** Requests go to a socket proxy that only forwards
  container stop/restart/kill (plus ping/version/events); create, exec, images and volumes
  are denied. The proxy has no published port; only this service can reach it.

Industrial analogy: the agent is the HMI requesting a motor restart; platform-ops is the
PLC interlock logic that decides whether the command reaches the starter.

Run in Compose with: uvicorn --factory arl.services.platform_ops:app_from_env
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from arl.config import CONFIG_DIR

SERVICE = "platform-ops"
MIN_TOKEN_LENGTH = 32
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{16,128}$")

log = logging.getLogger(SERVICE)
if not log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)


def _log(event: str, **fields) -> None:
    log.info(json.dumps({"ts": datetime.now(UTC).isoformat(), "service": SERVICE, "event": event, **fields}))


class Target(BaseModel):
    container: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
    stop_timeout_seconds: int = Field(default=10, ge=1, le=60)


class PlatformOpsConfig(BaseModel):
    targets: dict[str, Target]
    min_interval_seconds: int = Field(default=60, ge=0)


def load_config(path: Path = CONFIG_DIR / "platform-ops.yaml") -> PlatformOpsConfig:
    return PlatformOpsConfig.model_validate(yaml.safe_load(path.read_text()))


class DockerError(Exception):
    pass


class DockerEngine(Protocol):
    def restart(self, container: str, stop_timeout_seconds: int) -> None: ...


class ProxyDockerEngine:
    """Calls the Docker Engine API through the socket proxy (never the raw socket)."""

    def __init__(self, base_url: str, timeout: float = 60.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def restart(self, container: str, stop_timeout_seconds: int) -> None:
        try:
            response = httpx.post(
                f"{self._base}/containers/{container}/restart",
                params={"t": stop_timeout_seconds},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise DockerError(f"docker proxy unreachable: {type(exc).__name__}") from exc
        if response.status_code != 204:
            raise DockerError(f"docker returned {response.status_code}: {response.text[:200]}")


@dataclass
class _Done:
    target: str
    result: dict


class RestartResult(BaseModel):
    target: str
    container: str
    status: str
    idempotency_key: str
    replayed: bool
    restarted_at: str


def create_app(
    *,
    token: str,
    config: PlatformOpsConfig,
    engine: DockerEngine,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"PLATFORM_OPS_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")

    app = FastAPI(title="platform-ops (synthetic sandbox)", docs_url=None, redoc_url=None)
    lock = threading.Lock()  # one restart decision at a time: no double restart on a race
    done: dict[str, _Done] = {}  # idempotency key -> first result (in memory; see README)
    last_restart: dict[str, float] = {}

    def authorize(header: str | None) -> None:
        scheme, _, presented = (header or "").partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(presented.encode(), token.encode()):
            _log("refused", reason="unauthorized")
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/services/{target}/restart", response_model=RestartResult)
    def restart(
        target: str,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> dict:
        authorize(authorization)
        if not idempotency_key or not _KEY.match(idempotency_key):
            _log("refused", reason="idempotency_key_missing_or_invalid", target=target[:64])
            raise HTTPException(status_code=400, detail="idempotency_key_missing_or_invalid")
        if not _NAME.match(target) or target not in config.targets:
            _log("refused", reason="target_not_allowed", target=target[:64])
            raise HTTPException(status_code=403, detail="target_not_allowed")

        with lock:
            previous = done.get(idempotency_key)
            if previous is not None:
                if previous.target != target:
                    _log("refused", reason="idempotency_key_reused", target=target)
                    raise HTTPException(status_code=409, detail="idempotency_key_reused")
                _log("replayed", target=target, idempotency_key=idempotency_key)
                return {**previous.result, "replayed": True}

            now = clock()
            since = now - last_restart.get(target, float("-inf"))
            if since < config.min_interval_seconds:
                _log("refused", reason="rate_limited", target=target)
                raise HTTPException(status_code=429, detail="rate_limited")

            spec = config.targets[target]
            try:
                engine.restart(spec.container, spec.stop_timeout_seconds)
            except DockerError as exc:
                _log("failed", target=target, error=str(exc))
                raise HTTPException(status_code=502, detail="restart_failed") from exc

            last_restart[target] = now
            result = {
                "target": target,
                "container": spec.container,
                "status": "restarted",
                "idempotency_key": idempotency_key,
                "replayed": False,
                "restarted_at": datetime.now(UTC).isoformat(),
            }
            done[idempotency_key] = _Done(target=target, result=result)
            _log("restarted", target=target, container=spec.container, idempotency_key=idempotency_key)
            return result

    return app


def app_from_env() -> FastAPI:
    return create_app(
        token=os.environ.get("PLATFORM_OPS_TOKEN", ""),
        config=load_config(),
        engine=ProxyDockerEngine(os.environ.get("DOCKER_PROXY_URL", "http://docker-proxy:2375")),
    )
