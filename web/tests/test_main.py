from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from assay_watch_web import main, orchestrator
from assay_watch_web.mcp_client import CatalogueRow, CheapestListing, Specs, WatchListing
from assay_watch_web.search import SearchResult


def test_index_serves_the_search_page() -> None:
    client = TestClient(main.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "assay-watch" in resp.text


def test_healthz_reports_the_build_it_is_serving() -> None:
    client = TestClient(main.app)
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    # "dev" is the honest default for a process started from a working tree
    # rather than a deployed image.
    assert body["build"] == "dev"


def test_version_endpoint_answers_which_build_is_live() -> None:
    """Exists so "is my change deployed?" is a fetch, not an argument."""
    client = TestClient(main.app)
    assert client.get("/api/version").json() == {"build": "dev"}


def test_the_page_is_served_uncached() -> None:
    """A browser holding a stale index.html is indistinguishable from a
    deploy that never happened -- which has cost us real time."""
    client = TestClient(main.app)
    resp = client.get("/")
    assert "no-cache" in resp.headers.get("cache-control", "")


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


def test_stream_emits_the_partial_and_the_text_it_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The front end renders the card off the partial and appends the text
    chunks into it, so the order here is the contract: partial before any
    text, and a final result carrying the assembled explanation."""

    async def fake_run_search(query: str, **kwargs: object) -> SearchResult:
        on_partial = kwargs["on_partial"]
        on_text = kwargs["on_text"]
        await on_partial(SearchResult(query=query))  # type: ignore[operator]
        await on_text("Half ")  # type: ignore[operator]
        await on_text("a thought.")  # type: ignore[operator]
        return SearchResult(query=query, explanation="Half a thought.")

    monkeypatch.setattr(main, "run_search", fake_run_search)

    client = TestClient(main.app)
    resp = client.post("/api/search/stream", json={"query": "126610LN"})
    assert resp.status_code == 200

    msgs = _sse_messages(resp.text)
    assert [m["type"] for m in msgs] == ["partial", "text", "text", "result"]
    assert msgs[1]["chunk"] == "Half "
    assert msgs[-1]["result"]["explanation"] == "Half a thought."  # type: ignore[index]


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


def test_live_search_endpoint_returns_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(url: str, query: str, source: str) -> list[WatchListing]:
        return [
            WatchListing(
                id="1",
                title="Rolex Submariner 126610LN",
                brand="Rolex",
                price=12900.0,
                currency="USD",
                merchant="A Shop",
                link="https://x.test/1",
                image_url="https://img.test/1.jpg",
                condition="pre-owned",
                source="google_shopping",
            )
        ]

    monkeypatch.setattr(main, "search_live_listings", fake)

    client = TestClient(main.app)
    resp = client.get("/api/watches/search", params={"q": "Rolex Submariner 126610LN"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["merchant"] == "A Shop"
    # Provenance rides on every row, so a consumer cannot mistake an asking
    # price for a fair price we computed.
    assert body[0]["source"] == "google_shopping"


def test_live_search_passes_the_requested_source(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    async def fake(url: str, query: str, source: str) -> list[WatchListing]:
        seen["query"], seen["source"] = query, source
        return []

    monkeypatch.setattr(main, "search_live_listings", fake)

    client = TestClient(main.app)
    client.get("/api/watches/search", params={"q": "  Omega Speedmaster  ", "source": "web_search"})
    # Trimmed before it reaches the paid API, so "x" and " x " are one query.
    assert seen == {"query": "Omega Speedmaster", "source": "web_search"}


def test_live_search_rejects_an_unknown_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 rather than an empty list: an unknown source is a caller bug, and
    [] would read as 'nothing found'."""

    async def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not reach the API with an invalid source")

    monkeypatch.setattr(main, "search_live_listings", fail)

    client = TestClient(main.app)
    resp = client.get("/api/watches/search", params={"q": "rolex", "source": "chrono24"})
    assert resp.status_code == 400
    assert "google_shopping" in resp.json()["detail"]


def test_live_search_rejects_a_missing_or_oversized_query() -> None:
    client = TestClient(main.app)
    assert client.get("/api/watches/search").status_code == 422
    assert client.get("/api/watches/search", params={"q": ""}).status_code == 422
    assert client.get("/api/watches/search", params={"q": "x" * 400}).status_code == 422


def test_live_search_rejects_a_whitespace_only_query(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not spend a paid call on whitespace")

    monkeypatch.setattr(main, "search_live_listings", fail)

    client = TestClient(main.app)
    resp = client.get("/api/watches/search", params={"q": "   "})
    assert resp.status_code == 400


def test_ask_endpoint_reports_the_tools_the_model_chose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(query: str, **kwargs: object) -> orchestrator.OrchestratorResult:
        return orchestrator.OrchestratorResult("Two good options under $15k.", ["x", "y"], False)

    monkeypatch.setattr(orchestrator, "run", fake_run)

    client = TestClient(main.app)
    resp = client.post("/api/ask", json={"query": "steel sports watch under 15k"})
    assert resp.status_code == 200
    assert resp.json() == {
        "answer": "Two good options under $15k.",
        "tools_used": ["x", "y"],
        "truncated": False,
    }
