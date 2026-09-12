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


def _stub_client(monkeypatch: pytest.MonkeyPatch, resp: Any, extracted: Any = None) -> None:
    """``resp`` answers the grounded search; ``extracted`` answers the
    follow-up extraction call, which only happens if the first was grounded."""
    calls = {"n": 0}

    class _Models:
        async def generate_content(self, **kwargs: object) -> Any:
            calls["n"] += 1
            if calls["n"] > 1:
                return extracted if extracted is not None else _Parsed(_Offers([]))
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


async def test_each_discard_reason_is_logged_distinctly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """All three ways this returns None look identical from the outside, and
    they want completely different fixes -- an unsupported tool, a model that
    answered from memory, and a search that genuinely found nothing. The log
    is the only place they can be told apart."""
    caplog.set_level("WARNING", logger="assay_watch_web.gemini")

    _stub_client(monkeypatch, RuntimeError("tool not supported for this model"))
    assert await gemini.search_web_market(api_key="k", model="m", reference=_SUB) is None

    _stub_client(monkeypatch, _Response("  ", [_Candidate(_Meta([], []))]))
    assert await gemini.search_web_market(api_key="k", model="m", reference=_SUB) is None

    _stub_client(monkeypatch, _Response("About $13k.", [_Candidate(_Meta([], ["q"]))]))
    assert await gemini.search_web_market(api_key="k", model="m", reference=_SUB) is None

    text = caplog.text
    assert "web market search failed" in text
    assert "no text" in text
    assert "ungrounded" in text


def _Offers(
    offers: list[gemini.WebOffer], guidance: list[gemini.ConditionGuide] | None = None
) -> Any:
    """The real structured-output model -- the production code
    isinstance-checks what came back, so a stand-in would pass a test the
    real path would reject."""
    return gemini._Extracted(offers=offers, guidance=guidance or [])


def _grounded(text: str, url: str = "https://x.test/a") -> _Response:
    return _Response(text, [_Candidate(_Meta([_Chunk(_Web(url, "A listing", "x.test"))], ["q"]))])


async def test_offers_are_extracted_from_the_grounded_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_client(
        monkeypatch,
        _grounded("One dealer lists it at $12,500; another asks $14,000."),
        _Parsed(
            _Offers(
                [
                    gemini.WebOffer(
                        price_text="$12,500",
                        merchant="A Dealer",
                        url="https://x.test/a",
                        condition="pre-owned",
                    )
                ]
            )
        ),
    )

    out = await gemini.search_web_market(api_key="k", model="m", reference=_SUB)
    assert out is not None
    assert [o.price_text for o in out.offers] == ["$12,500"]
    assert out.offers[0].merchant == "A Dealer"


async def test_an_offer_priced_outside_the_grounded_text_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extraction step runs without tools, so a plausible invented figure
    with a real URL attached is exactly the failure this must not ship."""
    _stub_client(
        monkeypatch,
        _grounded("One dealer lists it at $12,500."),
        _Parsed(
            _Offers(
                [
                    gemini.WebOffer(price_text="$12,500", url="https://x.test/a"),
                    gemini.WebOffer(price_text="$13,750", url="https://x.test/a"),
                ]
            )
        ),
    )

    out = await gemini.search_web_market(api_key="k", model="m", reference=_SUB)
    assert out is not None
    assert [o.price_text for o in out.offers] == ["$12,500"]


async def test_an_offer_citing_a_page_the_search_never_returned_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_client(
        monkeypatch,
        _grounded("One dealer lists it at $12,500.", url="https://x.test/a"),
        _Parsed(_Offers([gemini.WebOffer(price_text="$12,500", url="https://invented.test/deal")])),
    )

    out = await gemini.search_web_market(api_key="k", model="m", reference=_SUB)
    assert out is not None
    assert out.offers == []


async def test_extraction_failing_still_leaves_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offers are an enrichment of a snapshot that is already useful."""
    _stub_client(
        monkeypatch,
        _grounded("Asking prices cluster near $12,500."),
        _Parsed("not the schema"),
    )

    out = await gemini.search_web_market(api_key="k", model="m", reference=_SUB)
    assert out is not None
    assert out.summary == "Asking prices cluster near $12,500."
    assert out.offers == []


async def test_no_extraction_call_is_made_for_an_ungrounded_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer is about to be discarded; paying for a second call to
    structure it would be spending money on something nobody will see."""
    calls = {"n": 0}

    class _Models:
        async def generate_content(self, **kwargs: object) -> Any:
            calls["n"] += 1
            return _Response("About $13,000.", [_Candidate(_Meta([], ["q"]))])

    class _Aio:
        models = _Models()

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            self.aio = _Aio()

    monkeypatch.setattr("assay_watch_web.gemini.genai.Client", _Client)

    assert await gemini.search_web_market(api_key="k", model="m", reference=_SUB) is None
    assert calls["n"] == 1


async def test_guidance_is_extracted_per_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completeness moves a used watch's price more than anything else, so a
    single market figure is the one answer a buyer cannot act on."""
    _stub_client(
        monkeypatch,
        _grounded("Full sets ask about $14,000; the watch alone trades nearer $12,500."),
        _Parsed(
            _Offers(
                [],
                [
                    gemini.ConditionGuide(
                        label="Full set (box & papers)",
                        price_text="$14,000",
                        note="Papers carry a premium on this reference.",
                    ),
                    gemini.ConditionGuide(label="Watch only", price_text="$12,500"),
                ],
            )
        ),
    )

    out = await gemini.search_web_market(api_key="k", model="m", reference=_SUB)
    assert out is not None
    assert [(g.label, g.price_text) for g in out.guidance] == [
        ("Full set (box & papers)", "$14,000"),
        ("Watch only", "$12,500"),
    ]


async def test_guidance_priced_outside_the_grounded_text_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guidance carries no URL to check against, so the price check is the
    only thing between a buyer and an invented number."""
    _stub_client(
        monkeypatch,
        _grounded("Full sets ask about $14,000."),
        _Parsed(
            _Offers(
                [],
                [
                    gemini.ConditionGuide(label="Full set (box & papers)", price_text="$14,000"),
                    gemini.ConditionGuide(label="Watch only", price_text="$11,200"),
                ],
            )
        ),
    )

    out = await gemini.search_web_market(api_key="k", model="m", reference=_SUB)
    assert out is not None
    assert [g.label for g in out.guidance] == ["Full set (box & papers)"]
