"""Low-level, read-only network probes. These touch real services.

Kept behind a small interface (`Probe`) so unit tests can substitute a fake environment,
while integration tests run the real probes against the Docker Compose stack.
"""

from __future__ import annotations

import socket
from typing import Protocol

import httpx


class HttpResult(dict):
    """{'status_code': int | None, 'body': dict | str | None, 'error': str | None}"""


class Probe(Protocol):
    def http_get(self, url: str) -> HttpResult: ...
    def tcp_connect(self, host: str, port: int) -> str | None:
        """Return None if a TCP connection succeeds, else the error text."""
        ...


class NetworkProbe:
    """The real thing: HTTP GET with a timeout, and a plain TCP connect."""

    def __init__(self, timeout_seconds: float = 3.0):
        self._timeout = timeout_seconds

    def http_get(self, url: str) -> HttpResult:
        try:
            response = httpx.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            return HttpResult(status_code=None, body=None, error=f"{type(exc).__name__}: {exc}")
        try:
            body = response.json()
        except ValueError:
            body = response.text[:500]
        return HttpResult(status_code=response.status_code, body=body, error=None)

    def tcp_connect(self, host: str, port: int) -> str | None:
        try:
            with socket.create_connection((host, port), timeout=self._timeout):
                return None
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"
