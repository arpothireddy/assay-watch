from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from assay_watch_web import main
from assay_watch_web.search import SearchResult


def test_index_serves_the_search_page() -> None:
    client = TestClient(main.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "assay-watch" in resp.text


def test_healthz() -> None:
    client = TestClient(main.app)
    assert client.get("/healthz").json() == {"status": "ok"}


def test_search_endpoint_calls_orchestration_and_returns_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = SearchResult(query="126610LN", message="stubbed")

    async def fake_run_search(query: str, **kwargs: object) -> SearchResult:
        assert query == "126610LN"
        return expected

    monkeypatch.setattr(main, "run_search", fake_run_search)

    client = TestClient(main.app)
    resp = client.post("/api/search", json={"query": "126610LN"})
    assert resp.status_code == 200
    assert resp.json()["message"] == "stubbed"
