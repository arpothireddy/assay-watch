"""Tests for the one Gemini call that reaches the open web.

The genai client is stubbed: these assert how we read the SDK's grounding
metadata and, more importantly, that an answer with no grounding behind it
is thrown away rather than shown as sourced pricing.
"""

from __future__ import annotations

from typing import Any

import pytest

from assay_watch_web import gemini
from assay_watch_web.mcp_client import CatalogEntry, CheapestListing, FairPrice

_SUB = CatalogEntry(ref="126610LN", brand="Rolex", model_name="Submariner Date")
_CHEAPEST = CheapestListing(
    seller_name="dealer",
    price_amount="12500.00",
    price_currency="USD",
    raw_title="Rolex Submariner",
    url="https://dealer.test/x",
    seen_at="2026-01-01T00:00:00Z",
)
_FAIR = FairPrice(
    median_price="13000.00",
    min_price="12500.00",
    max_price="14000.00",
    n_listings=3,
    excluded_other_currency=0,
    excluded_implausible=0,
)


class _Web:
    def __init__(self, uri: str, title: str, domain: str | None = None) -> None:
        self.uri, self.title, self.domain = uri, title, domain


class _Chunk:
    def __init__(self, web: _Web | None) -> None:
        self.web = web


class _Meta:
    def __init__(self, chunks: list[_Chunk], queries: list[str]) -> None:
        self.grounding_chunks = chunks
        self.web_search_queries = queries


class _Candidate:
    def __init__(self, meta: _Meta | None) -> None:
        self.grounding_metadata = meta


class _Response:
    def __init__(self, text: str, candidates: list[_Candidate]) -> None:
        self.text = text
        self.candidates = candidates


def _stub_client(monkeypatch: pytest.MonkeyPatch, resp: Any) -> None:
    class _Models:
        async def generate_content(self, **kwargs: object) -> Any:
            if isinstance(resp, Exception):
                raise resp
            return resp

    class _Aio:
        models = _Models()

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            self.aio = _Aio()

    # Addressed by path rather than by attribute: genai is an import inside
    # gemini, not something gemini re-exports, and mypy is right to say so.
    monkeypatch.setattr("assay_watch_web.gemini.genai.Client", _Client)


async def test_returns_summary_with_sources_and_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_client(
        monkeypatch,
        _Response(
            "Asking prices cluster around $13,000.",
            [_Candidate(_Meta([_Chunk(_Web("https://x.test/a", "A listing", "x.test"))], ["q1"]))],
        ),
    )

    out = await gemini.search_web_market(api_key="k", model="m", reference=_SUB)
    assert out is not None
    assert out.summary == "Asking prices cluster around $13,000."
    assert [s.url for s in out.sources] == ["https://x.test/a"]
    assert out.sources[0].domain == "x.test"
    assert out.queries == ["q1"]


async def test_ungrounded_answer_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """No grounding chunks means the model answered from memory. A price
    figure with nothing behind it is worse than saying nothing."""
    _stub_client(
        monkeypatch,
        _Response("They go for about $13,000.", [_Candidate(_Meta([], []))]),
    )

    assert await gemini.search_web_market(api_key="k", model="m", reference=_SUB) is None


async def test_chunk_without_a_usable_uri_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_client(
        monkeypatch,
        _Response(
            "Prices vary.",
            [
                _Candidate(
                    _Meta(
                        [
                            _Chunk(None),
                            _Chunk(_Web("", "empty")),
                            _Chunk(_Web("https://y.test", "Y")),
                        ],
                        [],
                    )
                )
            ],
        ),
    )

    out = await gemini.search_web_market(api_key="k", model="m", reference=_SUB)
    assert out is not None
    assert [s.url for s in out.sources] == ["https://y.test"]


async def test_empty_text_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_client(
        monkeypatch,
        _Response("   ", [_Candidate(_Meta([_Chunk(_Web("https://x.test", "A"))], []))]),
    )

    assert await gemini.search_web_market(api_key="k", model="m", reference=_SUB) is None


async def test_api_failure_returns_none_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is an optional extra on top of a search that already has an
    answer -- it must never take the whole request down with it."""
    _stub_client(monkeypatch, RuntimeError("quota exhausted"))

    assert await gemini.search_web_market(api_key="k", model="m", reference=_SUB) is None


class _Parsed:
    """A response whose ``parsed`` is what the structured-output config
    produced -- the shape resolve_reference actually reads."""

    def __init__(self, parsed: object) -> None:
        self.parsed = parsed
        self.text = ""
        self.candidates: list[_Candidate] = []


_PEPSI = CatalogEntry(ref="126710BLRO", brand="Rolex", model_name="GMT-Master II Pepsi")
_BATMAN = CatalogEntry(ref="126710BLNR", brand="Rolex", model_name="GMT-Master II Batman")


async def test_a_reference_not_on_the_candidate_list_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller looks these up by exact reference. A ref the model made up
    would sail through as a lookup for a watch we do not track."""
    _stub_client(
        monkeypatch,
        _Parsed(
            gemini.ResolvedPick(refs=["126710BLRO", "6538"], reasoning="one real, one invented")
        ),
    )

    out = await gemini.resolve_reference(
        api_key="k", model="m", query="GMT", candidates=[_PEPSI, _BATMAN]
    )
    assert out.refs == ["126710BLRO"]


async def test_several_matches_come_back_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "GMT" matching both is the answer, not a failure to answer."""
    _stub_client(
        monkeypatch,
        _Parsed(
            gemini.ResolvedPick(
                refs=["126710BLRO", "126710BLNR"], reasoning="both are GMT-Master IIs"
            )
        ),
    )

    out = await gemini.resolve_reference(
        api_key="k", model="m", query="GMT", candidates=[_PEPSI, _BATMAN]
    )
    assert out.refs == ["126710BLRO", "126710BLNR"]


async def test_an_unparseable_response_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_client(monkeypatch, _Parsed(None))

    out = await gemini.resolve_reference(
        api_key="k", model="m", query="GMT", candidates=[_PEPSI, _BATMAN]
    )
    assert out.refs == []


async def test_a_failing_explanation_stream_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prices are already on screen by the time this runs. A failure
    here costs the commentary, not the answer."""

    class _Models:
        async def generate_content_stream(self, **kwargs: object) -> object:
            raise RuntimeError("stream died mid-flight")

    class _Aio:
        models = _Models()

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            self.aio = _Aio()

    monkeypatch.setattr("assay_watch_web.gemini.genai.Client", _Client)

    chunks = [
        c
        async for c in gemini.stream_fair_price_explanation(
            api_key="k",
            model="m",
            reference=_SUB,
            cheapest=_CHEAPEST,
            fair=_FAIR,
        )
    ]
    assert chunks == []
