"""Tests for the one Gemini call that reaches the open web.

The genai client is stubbed: these assert how we read the SDK's grounding
metadata and, more importantly, that an answer with no grounding behind it
is thrown away rather than shown as sourced pricing.
"""

from __future__ import annotations

from typing import Any

import pytest

from assay_watch_web import gemini
from assay_watch_web.mcp_client import CatalogEntry

_SUB = CatalogEntry(ref="126610LN", brand="Rolex", model_name="Submariner Date")


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

    monkeypatch.setattr(gemini.genai, "Client", _Client)


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
