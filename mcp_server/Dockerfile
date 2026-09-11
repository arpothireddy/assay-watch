# Build from the repo root (`docker build -f mcp_server/Dockerfile .`), not
# from mcp_server/ itself -- this image needs config/references.yaml from
# the repo root, and settings.py's REPO_ROOT computation
# (Path(__file__).resolve().parents[3]) depends on the mcp_server/ nesting
# being preserved exactly as it is in the local checkout: parents[3] from
# .../mcp_server/src/assay_watch_mcp/settings.py is the directory one level
# above mcp_server/ -- which must be /app here, matching the local layout
# where it's the repo root.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.17

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/mcp_server/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies first (cached until the lockfile changes). --no-dev
# skips the dev-only path dependency on the main assay-watch package (used
# only to reuse its schema for tests, never needed at runtime).
COPY mcp_server/pyproject.toml mcp_server/uv.lock ./mcp_server/
COPY mcp_server/src ./mcp_server/src
WORKDIR /app/mcp_server
RUN uv sync --frozen --no-dev

# The tracked-reference catalog, read at runtime -- same content the crawler
# uses to decide what to fetch, bundled here as the single source of truth
# for what this service can look up.
WORKDIR /app
COPY config ./config

# Non-root runtime user.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8080
ENTRYPOINT ["assay-watch-mcp"]
