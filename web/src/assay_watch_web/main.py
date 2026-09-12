"""The FastAPI app: serves the search page and the search API it calls.

Two search endpoints intentionally:
- POST /api/search returns the finished result in one shot. Simple, and
  what the deploy pipeline's smoke test exercises.
- POST /api/search/stream emits the same result, preceded by the pipeline
  stages as they actually happen, so the UI can report real progress
  instead of animating a guess.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .mcp_client import CatalogEntry, list_tracked_references
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


@app.get("/api/references")
async def references() -> list[CatalogEntry]:
    """The tracked catalogue, for the browsable grid on the page. Every
    entry here is something the crawler actually looks for."""
    settings = get_settings()
    return await list_tracked_references(settings.mcp_server_url)


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

        async def run() -> None:
            try:
                result = await run_search(
                    request.query,
                    mcp_url=settings.mcp_server_url,
                    gemini_api_key=settings.gemini_api_key,
                    gemini_model=settings.gemini_model,
                    on_stage=on_stage,
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
