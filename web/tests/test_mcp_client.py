"""Confirmed live against a real running server (not assumed): every MCP
tool's structured_content comes back wrapped as {"result": <value>} --
including when the value itself is null. This is the bug that broke the
first live end-to-end test: a bare `if data else None` on the still-wrapped
dict is truthy even when the actual result is null, so it tried to build a
model from {"result": None} and blew up. These tests lock the fix in
without needing a real server running."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from assay_watch_web import mcp_client


class _FakeToolResult:
    def __init__(self, structured_content: dict[str, Any] | None, *, is_error: bool = False):
        self.structured_content = structured_content
        self.is_error = is_error
        self.content: list[Any] = []


def _patched_session(call_tool_return: _FakeToolResult) -> Any:
    """Patches the streamable_http_client/ClientSession pair _call_tool uses,
    so no real network connection is needed."""
    fake_session = AsyncMock()
    fake_session.initialize = AsyncMock()
    fake_session.call_tool = AsyncMock(return_value=call_tool_return)

    @asynccontextmanager
    async def fake_streamable_http_client(url: str) -> Any:
        yield (None, None)

    @asynccontextmanager
    async def fake_client_session(read: Any, write: Any) -> Any:
        yield fake_session

    return patch.multiple(
        mcp_client,
        streamable_http_client=fake_streamable_http_client,
        ClientSession=fake_client_session,
    )


async def test_null_result_unwraps_to_none_not_a_validation_error() -> None:
    with _patched_session(_FakeToolResult({"result": None})):
        result = await mcp_client.get_cheapest_listing("http://mcp.test/mcp", "126610LN")
    assert result is None


async def test_real_object_result_unwraps_correctly() -> None:
    payload = {
        "seller_name": "dealer",
        "price_amount": "12500.00",
        "price_currency": "USD",
        "raw_title": "Rolex Submariner",
        "url": "https://dealer.test/x",
        "seen_at": "2026-01-01T00:00:00Z",
    }
    with _patched_session(_FakeToolResult({"result": payload})):
        result = await mcp_client.get_cheapest_listing("http://mcp.test/mcp", "126610LN")
    assert result is not None
    assert result.price_amount == "12500.00"


async def test_list_result_unwraps_correctly() -> None:
    payload = [{"ref": "126610LN", "brand": "Rolex", "model_name": "Submariner Date"}]
    with _patched_session(_FakeToolResult({"result": payload})):
        result = await mcp_client.list_tracked_references("http://mcp.test/mcp")
    assert len(result) == 1
    assert result[0].ref == "126610LN"


async def test_empty_list_result_unwraps_to_empty_list() -> None:
    with _patched_session(_FakeToolResult({"result": []})):
        result = await mcp_client.find_reference("http://mcp.test/mcp", "nonsense query")
    assert result == []


async def test_tool_error_raises_mcp_error() -> None:
    with (
        _patched_session(_FakeToolResult(None, is_error=True)),
        pytest.raises(mcp_client.MCPError),
    ):
        await mcp_client.get_cheapest_listing("http://mcp.test/mcp", "126610LN")
