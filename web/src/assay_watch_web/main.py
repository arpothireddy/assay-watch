"""The FastAPI app: serves the search page and the search API it calls.

Two search endpoints intentionally:
- POST /api/search returns the finished result in one shot. Simple, and
  what the deploy pipeline's smoke test exercises.
- POST /api/search/stream emits the same result, preceded by the pipeline
  stages as they actually happen, so the UI can report real progress
  instead of animating a guess. It also emits the pricing on its own the
  moment it is final ("partial") and the explanation a token at a time
  ("text"), so the page fills in as the work completes rather than all at
  once at the end.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import orchestrator
from .mcp_client import (
    CatalogueRow,
    CheapestListing,
    WatchListing,
    catalogue_overview,
    list_listings,
    search_live_listings,
)
from .search import SearchResult, run_search
from .settings import get_settings

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="assay-watch")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


class SearchRequest(BaseModel):
    query: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/catalogue")
async def catalogue() -> list[CatalogueRow]:
    """The tracked catalogue with curated attributes and live prices -- one
    call, because the filter panel needs all of it to decide what to show."""
    settings = get_settings()
    return await catalogue_overview(settings.mcp_server_url)


@app.get("/api/listings/{reference:path}")
async def listings(reference: str) -> list[CheapestListing]:
    """The listings behind a reference's headline numbers. Path is declared
    ``:path`` because references contain slashes -- Patek's 5711/1A would
    otherwise 404 as a two-segment route."""
    settings = get_settings()
    return await list_listings(settings.mcp_server_url, reference)


_LIVE_SOURCES = {"google_shopping", "web_search"}
_MAX_QUERY_LEN = 120


@app.get("/api/watches/search")
async def watches_search(
    q: str = Query(..., min_length=1, max_length=_MAX_QUERY_LEN),
    source: str = Query("google_shopping"),
) -> list[WatchListing]:
    """Live listings for a free-text watch query.

    Separate from /api/search on purpose: that one answers "what is this
    worth" from data we crawled and verified, this one answers "what is
    currently listed" from a search API. Keeping them apart is what stops a
    caller treating an asking price as a fair price.
    """
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query must not be empty.")
    if source not in _LIVE_SOURCES:
        # 400 rather than a silent empty list: an unknown source is a caller
        # bug, and returning [] would read as "nothing found".
        raise HTTPException(
            status_code=400,
            detail=f"source must be one of: {', '.join(sorted(_LIVE_SOURCES))}",
        )
    settings = get_settings()
    return await search_live_listings(settings.mcp_server_url, query, source)


@app.post("/api/ask")
async def ask(request: SearchRequest) -> dict[str, Any]:
    """The orchestrator: the model picks its own lookups and answers.

    For questions the deterministic path cannot serve -- a budget, a
    category, a comparison. /api/search remains the fast path for "what is
    this one watch worth".
    """
    settings = get_settings()
    result = await orchestrator.run(
        request.query,
        mcp_url=settings.mcp_server_url,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    return {"answer": result.text, "tools_used": result.calls, "truncated": result.truncated}


@app.post("/api/search")
async def search(request: SearchRequest) -> SearchResult:
    settings = get_settings()
    return await run_search(
        request.query,
        mcp_url=settings.mcp_server_url,
        gemini_api_key=settings.gemini_api_key,
        gemini_model=settings.gemini_model,
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/api/search/stream")
async def search_stream(request: SearchRequest) -> StreamingResponse:
    settings = get_settings()

    async def events() -> AsyncIterator[str]:
        # The search pushes stages in as it goes while this generator drains
        # them out to the client; the sentinel None marks the end so the
        # loop terminates on completion rather than on an empty queue.
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def on_stage(stage: str, detail: str) -> None:
            await queue.put({"type": "stage", "stage": stage, "detail": detail})

        async def on_text(chunk: str) -> None:
            await queue.put({"type": "text", "chunk": chunk})

        async def on_partial(result: SearchResult) -> None:
            await queue.put({"type": "partial", "result": result.model_dump(mode="json")})

        async def run() -> None:
            try:
                result = await run_search(
                    request.query,
                    mcp_url=settings.mcp_server_url,
                    gemini_api_key=settings.gemini_api_key,
                    gemini_model=settings.gemini_model,
                    on_stage=on_stage,
                    on_text=on_text,
                    on_partial=on_partial,
                )
                await queue.put({"type": "result", "result": result.model_dump(mode="json")})
            except Exception:
                # Logged server-side with the traceback; the client gets a
                # flat message, since exception text here can carry internal
                # hostnames and query fragments.
                logger.exception("search failed for query %r", request.query)
                await queue.put({"type": "error", "message": "Search failed. Please try again."})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield _sse(item)
        finally:
            await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
