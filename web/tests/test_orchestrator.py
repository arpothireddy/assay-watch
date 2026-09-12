"""Tests for the orchestrator loop.

The genai client is stubbed with scripted turns, so the loop itself -- tool
dispatch, feeding results back, the turn limit, failure handling -- is
exercised without a live model.
"""

from __future__ import annotations

from typing import Any

import pytest

from assay_watch_web import mcp_client, orchestrator
from assay_watch_web.mcp_client import CatalogEntry, FairPrice, ReferenceMatch, WatchListing

_SUB = CatalogEntry(ref="126610LN", brand="Rolex", model_name="Submariner Date")
_FAIR = FairPrice(
    median_price="13000.00",
    min_price="12500.00",
    max_price="14000.00",
    n_listings=3,
    excluded_other_currency=0,
    excluded_implausible=0,
)
_LIVE = WatchListing(
    id="1",
    title="Rolex Submariner 126610LN",
    brand="Rolex",
    price=12900.0,
    currency="USD",
    merchant="A Shop",
    link="https://x.test/1",
    source="google_shopping",
)


class _Call:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name, self.args = name, args


class _Part:
    def __init__(self, call: _Call | None = None) -> None:
        self.function_call = call


class _Content:
    def __init__(self, parts: list[_Part]) -> None:
        self.parts = parts


class _Candidate:
    def __init__(self, parts: list[_Part]) -> None:
        self.content = _Content(parts)


class _Resp:
    def __init__(self, *, calls: list[_Call] | None = None, text: str = "") -> None:
        self.candidates = [_Candidate([_Part(c) for c in (calls or [])])]
        self.text = text


def _responses_in(contents: Any) -> list[Any]:
    """The function-response payloads inside one turn's contents.

    Read out of the Part rather than off str(contents): the SDK's repr elides
    a nested dict as "<dict len=6>", so stringifying would pass whatever the
    payload actually held.
    """
    out: list[Any] = []
    for content in contents:
        for part in content.parts or []:
            fr = getattr(part, "function_response", None)
            if fr is not None:
                out.append(fr.response)
    return out


def _stub_model(monkeypatch: pytest.MonkeyPatch, turns: list[Any]) -> list[Any]:
    """Script the model's turns. Returns the list of contents it was sent, so
    a test can assert what the tool results looked like going back in."""
    sent: list[Any] = []
    queue = list(turns)

    class _Models:
        async def generate_content(self, **kwargs: Any) -> Any:
            sent.append(kwargs["contents"])
            nxt = queue.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

    class _Aio:
        models = _Models()

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            self.aio = _Aio()

    monkeypatch.setattr("assay_watch_web.orchestrator.genai.Client", _Client)
    return sent


async def _returns(value: object) -> object:
    return value


async def test_the_model_chooses_its_own_lookups_and_they_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the layer: our code does not decide the sequence."""
    _stub_model(
        monkeypatch,
        [
            _Resp(calls=[_Call("find_reference", {"query": "submariner"})]),
            _Resp(calls=[_Call("get_fair_price", {"reference": "126610LN"})]),
            _Resp(text="The median sits at $13,000 across three listings."),
        ],
    )

    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return [ReferenceMatch(**_SUB.model_dump(), confidence="exact")]

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(_FAIR))

    out = await orchestrator.run("what's a submariner worth", mcp_url="u", api_key="k", model="m")
    assert out.calls == ["find_reference", "get_fair_price"]
    assert out.text == "The median sits at $13,000 across three listings."
    assert out.truncated is False


async def test_a_live_lookup_is_available_as_a_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_model(
        monkeypatch,
        [
            _Resp(calls=[_Call("search_live_listings", {"query": "Rolex 126610LN"})]),
            _Resp(text="One shop is asking $12,900, which is an asking price, not a sale."),
        ],
    )
    seen: dict[str, str] = {}

    async def live(url: str, query: str, source: str = "google_shopping") -> list[WatchListing]:
        seen["query"], seen["source"] = query, source
        return [_LIVE]

    monkeypatch.setattr(mcp_client, "search_live_listings", live)

    out = await orchestrator.run("price check", mcp_url="u", api_key="k", model="m")
    assert out.calls == ["search_live_listings"]
    # Defaulted rather than dropped when the model omits it.
    assert seen == {"query": "Rolex 126610LN", "source": "google_shopping"}


async def test_tool_results_are_fed_back_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop that calls tools but never returns their output is just a
    slower way of answering from memory."""
    sent = _stub_model(
        monkeypatch,
        [
            _Resp(calls=[_Call("get_fair_price", {"reference": "126610LN"})]),
            _Resp(text="done"),
        ],
    )
    monkeypatch.setattr(mcp_client, "get_fair_price", lambda *a, **k: _returns(_FAIR))

    await orchestrator.run("q", mcp_url="u", api_key="k", model="m")

    # Second turn's contents carry the function response with the real median.
    payloads = _responses_in(sent[1])
    assert payloads == [
        {
            "result": {
                "median_price": "13000.00",
                "min_price": "12500.00",
                "max_price": "14000.00",
                "n_listings": 3,
                "excluded_other_currency": 0,
                "excluded_implausible": 0,
            }
        }
    ]


async def test_a_failing_tool_is_reported_to_the_model_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model can route around a failed lookup; an exception here would
    throw away every turn already spent."""
    sent = _stub_model(
        monkeypatch,
        [
            _Resp(calls=[_Call("get_fair_price", {"reference": "nope"})]),
            _Resp(text="I couldn't price that one."),
        ],
    )

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("mcp unreachable")

    monkeypatch.setattr(mcp_client, "get_fair_price", boom)

    out = await orchestrator.run("q", mcp_url="u", api_key="k", model="m")
    assert out.text == "I couldn't price that one."
    assert "mcp unreachable" in str(_responses_in(sent[1]))


async def test_an_unknown_tool_name_does_not_crash_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _stub_model(
        monkeypatch,
        [_Resp(calls=[_Call("delete_everything", {})]), _Resp(text="no such tool")],
    )
    out = await orchestrator.run("q", mcp_url="u", api_key="k", model="m")
    assert out.text == "no such tool"
    assert "unknown tool" in str(_responses_in(sent[1]))


async def test_the_loop_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that keeps calling tools without concluding must be stopped,
    not left to run until the request times out."""
    _stub_model(
        monkeypatch,
        [_Resp(calls=[_Call("get_catalogue_overview", {})]) for _ in range(50)],
    )
    monkeypatch.setattr(mcp_client, "catalogue_overview", lambda *a, **k: _returns([]))

    out = await orchestrator.run("q", mcp_url="u", api_key="k", model="m")
    assert out.truncated is True
    assert len(out.calls) == orchestrator.MAX_TOOL_TURNS


async def test_a_generation_failure_ends_the_run_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_model(monkeypatch, [RuntimeError("quota exhausted")])
    out = await orchestrator.run("q", mcp_url="u", api_key="k", model="m")
    assert out.text == ""
    assert out.truncated is True


async def test_stages_report_what_the_model_actually_chose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The progress line should say what is happening, and here that is not
    knowable in advance -- the model picks it."""
    _stub_model(
        monkeypatch,
        [
            _Resp(calls=[_Call("find_reference", {"query": "GMT"})]),
            _Resp(text="ok"),
        ],
    )

    async def find_reference(url: str, query: str) -> list[ReferenceMatch]:
        return []

    monkeypatch.setattr(mcp_client, "find_reference", find_reference)

    stages: list[tuple[str, str]] = []

    async def on_stage(name: str, detail: str) -> None:
        stages.append((name, detail))

    await orchestrator.run("GMT", mcp_url="u", api_key="k", model="m", on_stage=on_stage)
    assert stages == [("find_reference", "Identifying “GMT”")]
