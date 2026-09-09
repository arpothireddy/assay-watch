from __future__ import annotations

import httpx
import pytest

from assay_watch.adapters.http import PoliteClient, RobotsDisallowed
from tests.conftest import make_transport


def test_robots_disallow_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, json={})

    with (
        PoliteClient(
            user_agent="assay-test", min_interval_seconds=0.0, transport=make_transport(handler)
        ) as client,
        pytest.raises(RobotsDisallowed),
    ):
        client.get("https://dealer.test/products.json")


def test_missing_robots_allows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, json={"ok": True})

    with PoliteClient(
        user_agent="assay-test", min_interval_seconds=0.0, transport=make_transport(handler)
    ) as client:
        resp = client.get("https://dealer.test/products.json")
    assert resp.status_code == 200


def test_server_error_robots_disallows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(500)
        return httpx.Response(200)

    with (
        PoliteClient(
            user_agent="assay-test", min_interval_seconds=0.0, transport=make_transport(handler)
        ) as client,
        pytest.raises(RobotsDisallowed),
    ):
        client.get("https://dealer.test/x")


def test_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    with PoliteClient(
        user_agent="assay-test",
        min_interval_seconds=0.0,
        max_retries=3,
        transport=make_transport(handler),
    ) as client:
        resp = client.get("https://dealer.test/a")
    assert resp.status_code == 200
    assert calls["n"] == 2
