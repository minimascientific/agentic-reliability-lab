"""Load the service catalog, action catalog and policy from config/*.yaml."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from arl.config import CONFIG_DIR


class TcpEndpoint(BaseModel):
    host: str
    port: int


class Service(BaseModel):
    name: str
    description: str
    health_url: str | None = None
    tcp: TcpEndpoint | None = None
    depends_on: list[str] = Field(default_factory=list)


class VerificationCheck(BaseModel):
    check: str  # "tcp" or "health"
    service: str


class Action(BaseModel):
    name: str
    description: str
    change_type: str
    allowed_targets: list[str]
    cooldown_minutes: int = 0
    verification: list[VerificationCheck] = Field(default_factory=list)


class ApprovalPolicy(BaseModel):
    ttl_minutes: int
    auto_approve_change_types: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    min_confidence: float
    require_cited_evidence: bool
    protected_targets: list[str]
    approval: ApprovalPolicy
    diagnostic_tools: dict[str, list[str]]


def _env_key(*parts: str) -> str:
    return "ARL_SVC_" + "_".join(p.upper().replace("-", "_") for p in parts)


def load_services(path: Path = CONFIG_DIR / "services.yaml") -> dict[str, Service]:
    raw = yaml.safe_load(path.read_text())["services"]
    services = {}
    for name, spec in raw.items():
        svc = Service(name=name, **spec)
        # Allow deployment-specific addresses without editing the committed file.
        if url := os.environ.get(_env_key(name, "health_url")):
            svc.health_url = url
        if svc.tcp and (host := os.environ.get(_env_key(name, "tcp_host"))):
            svc.tcp.host = host
        if svc.tcp and (port := os.environ.get(_env_key(name, "tcp_port"))):
            svc.tcp.port = int(port)
        services[name] = svc
    return services


def load_actions(path: Path = CONFIG_DIR / "actions.yaml") -> dict[str, Action]:
    raw = yaml.safe_load(path.read_text())["actions"]
    return {name: Action(name=name, **spec) for name, spec in raw.items()}


def load_policy(path: Path = CONFIG_DIR / "policy.yaml") -> Policy:
    return Policy(**yaml.safe_load(path.read_text()))
