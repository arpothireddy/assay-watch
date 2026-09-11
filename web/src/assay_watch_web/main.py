"""The FastAPI app: serves the search page and the search API it calls."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .search import SearchResult, run_search
from .settings import get_settings

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


@app.post("/api/search")
async def search(request: SearchRequest) -> SearchResult:
    settings = get_settings()
    return await run_search(
        request.query,
        mcp_url=settings.mcp_server_url,
        gemini_api_key=settings.gemini_api_key,
        gemini_model=settings.gemini_model,
    )
