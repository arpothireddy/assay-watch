from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from assay_watch_web import main
from assay_watch_web.mcp_client import CatalogueRow, CheapestListing, Specs
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


def test_listings_endpoint_returns_the_rows_behind_the_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_listings(url: str, reference: str) -> list[CheapestListing]:
        assert reference == "126610LN"
        return [
            CheapestListing(
                seller_name="dealer",
                price_amount="12500.00",
                price_currency="USD",
                raw_title="Rolex Submariner",
                url="https://dealer.test/x",
                seen_at="2026-01-01T00:00:00Z",
            )
        ]

    monkeypatch.setattr(main, "list_listings", fake_listings)

    client = TestClient(main.app)
    resp = client.get("/api/listings/126610LN")
    assert resp.status_code == 200
    assert resp.json()[0]["price_amount"] == "12500.00"


def test_listings_endpoint_handles_a_reference_containing_a_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patek references look like 5711/1A -- without a :path route they
    would 404 as a two-segment URL."""
    seen: list[str] = []

    async def fake_listings(url: str, reference: str) -> list[CheapestListing]:
        seen.append(reference)
        return []

    monkeypatch.setattr(main, "list_listings", fake_listings)

    client = TestClient(main.app)
    resp = client.get("/api/listings/5711%2F1A")
    assert resp.status_code == 200
    assert seen == ["5711/1A"]


def test_catalogue_endpoint_carries_specs_and_live_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_overview(url: str) -> list[CatalogueRow]:
        return [
            CatalogueRow(
                ref="126610LN",
                brand="Rolex",
                model_name="Submariner Date",
                specs=Specs(
                    case_mm=41, movement="automatic", category="dive", integrated_bracelet=False
                ),
                n_listings=3,
                min_price="11850.00",
                median_price="13400.00",
            )
        ]

    monkeypatch.setattr(main, "catalogue_overview", fake_overview)

    client = TestClient(main.app)
    resp = client.get("/api/catalogue")
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["specs"]["case_mm"] == 41
    assert row["n_listings"] == 3
    assert row["min_price"] == "11850.00"


def test_catalogue_row_tolerates_a_reference_with_no_specs_or_listings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Specs are hand-curated and may be blank, and plenty of references have
    nothing on file -- neither is an error."""

    async def fake_overview(url: str) -> list[CatalogueRow]:
        return [CatalogueRow(ref="WSSA0009", brand="Cartier", model_name="Santos", n_listings=0)]

    monkeypatch.setattr(main, "catalogue_overview", fake_overview)

    client = TestClient(main.app)
    row = client.get("/api/catalogue").json()[0]
    assert row["specs"]["case_mm"] is None
    assert row["min_price"] is None
