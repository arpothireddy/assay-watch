from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from assay_watch_web import main
from assay_watch_web.mcp_client import CatalogEntry
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


def _sse_messages(body: str) -> list[dict[str, object]]:
    return [
        json.loads(line[len("data: ") :])
        for block in body.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]


def test_stream_emits_stages_then_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_search(query: str, **kwargs: object) -> SearchResult:
        on_stage = kwargs["on_stage"]
        await on_stage("find_reference", "matching")  # type: ignore[operator]
        await on_stage("done", "complete")  # type: ignore[operator]
        return SearchResult(query=query, message="stubbed")

    monkeypatch.setattr(main, "run_search", fake_run_search)

    client = TestClient(main.app)
    resp = client.post("/api/search/stream", json={"query": "126610LN"})
    assert resp.status_code == 200

    msgs = _sse_messages(resp.text)
    assert [m["type"] for m in msgs] == ["stage", "stage", "result"]
    assert msgs[0]["stage"] == "find_reference"
    assert msgs[-1]["result"]["message"] == "stubbed"  # type: ignore[index]


def test_stream_reports_a_failure_without_leaking_internals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(query: str, **kwargs: object) -> SearchResult:
        raise RuntimeError("postgres://user:pw@internal-host/db is unreachable")

    monkeypatch.setattr(main, "run_search", boom)

    client = TestClient(main.app)
    resp = client.post("/api/search/stream", json={"query": "x"})
    assert resp.status_code == 200

    msgs = _sse_messages(resp.text)
    assert msgs[-1]["type"] == "error"
    assert "internal-host" not in resp.text
    assert "postgres" not in resp.text


def test_references_endpoint_returns_the_tracked_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list(url: str) -> list[CatalogEntry]:
        return [CatalogEntry(ref="126610LN", brand="Rolex", model_name="Submariner Date")]

    monkeypatch.setattr(main, "list_tracked_references", fake_list)

    client = TestClient(main.app)
    resp = client.get("/api/references")
    assert resp.status_code == 200
    assert resp.json() == [{"ref": "126610LN", "brand": "Rolex", "model_name": "Submariner Date"}]
