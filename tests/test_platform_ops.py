"""platform-ops refuses everything except an authenticated, allowlisted, idempotent restart."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arl.services.platform_ops import (
    DockerError,
    PlatformOpsConfig,
    Target,
    create_app,
    load_config,
)

TOKEN = "t" * 40
KEY = "a" * 64


class FakeEngine:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.restarts: list[tuple[str, int]] = []

    def restart(self, container: str, stop_timeout_seconds: int) -> None:
        if self.fail:
            raise DockerError("docker returned 500")
        self.restarts.append((container, stop_timeout_seconds))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(engine: FakeEngine | None = None, clock: Clock | None = None, min_interval: int = 60):
    engine = engine or FakeEngine()
    config = PlatformOpsConfig(
        targets={"app-db": Target(container="arl-app-db")}, min_interval_seconds=min_interval
    )
    app = create_app(token=TOKEN, config=config, engine=engine, clock=clock or Clock())
    return TestClient(app), engine


def post(client, target="app-db", token=TOKEN, key=KEY):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(f"/v1/services/{target}/restart", headers=headers)


def test_shipped_config_allows_only_app_db():
    assert set(load_config().targets) == {"app-db"}


def test_short_token_is_refused_at_startup():
    with pytest.raises(ValueError):
        create_app(token="short", config=load_config(), engine=FakeEngine())


def test_authorized_restart_of_allowlisted_target():
    client, engine = make()
    response = post(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "restarted" and body["container"] == "arl-app-db"
    assert body["replayed"] is False
    assert engine.restarts == [("arl-app-db", 10)]


@pytest.mark.parametrize("token", [None, "", "wrong" * 10, TOKEN + "x"])
def test_missing_or_wrong_token_is_refused(token):
    client, engine = make()
    assert post(client, token=token).status_code == 401
    assert engine.restarts == []


def test_non_bearer_scheme_is_refused():
    client, engine = make()
    response = client.post(
        "/v1/services/app-db/restart", headers={"Authorization": f"Basic {TOKEN}", "Idempotency-Key": KEY}
    )
    assert response.status_code == 401 and engine.restarts == []


@pytest.mark.parametrize("target", ["ops-db", "orders-api", "platform-ops", "docker-proxy", "APP-DB"])
def test_target_outside_own_allowlist_is_refused(target):
    client, engine = make()
    response = post(client, target=target)
    assert response.status_code == 403 and response.json()["detail"] == "target_not_allowed"
    assert engine.restarts == []


@pytest.mark.parametrize("key", [None, "", "short", "has spaces in it!!", "x" * 129])
def test_missing_or_malformed_idempotency_key_is_refused(key):
    client, engine = make()
    assert post(client, key=key).status_code == 400
    assert engine.restarts == []


def test_retry_with_same_key_does_not_restart_twice():
    client, engine = make()
    first = post(client).json()
    second = post(client)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["restarted_at"] == first["restarted_at"]
    assert len(engine.restarts) == 1


def test_second_restart_within_interval_is_rate_limited():
    clock = Clock()
    client, engine = make(clock=clock)
    assert post(client, key="k" * 32).status_code == 200
    clock.now += 30
    assert post(client, key="j" * 32).status_code == 429
    clock.now += 31
    assert post(client, key="i" * 32).status_code == 200
    assert len(engine.restarts) == 2


def test_docker_failure_is_reported_and_not_cached():
    engine = FakeEngine(fail=True)
    client, _ = make(engine=engine)
    assert post(client).status_code == 502
    engine.fail = False
    assert post(client).status_code == 200  # same key may be retried after a failure
    assert len(engine.restarts) == 1


def test_key_reused_for_another_target_is_refused():
    config = PlatformOpsConfig(
        targets={"app-db": Target(container="arl-app-db"), "cache": Target(container="arl-cache")},
        min_interval_seconds=0,
    )
    engine = FakeEngine()
    client = TestClient(create_app(token=TOKEN, config=config, engine=engine, clock=Clock()))
    assert post(client, target="app-db").status_code == 200
    assert post(client, target="cache").status_code == 409
    assert engine.restarts == [("arl-app-db", 10)]
