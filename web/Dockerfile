# Build from the repo root (`docker build -f web/Dockerfile .`) -- kept
# consistent with mcp_server/Dockerfile's build context even though this
# image doesn't need anything outside web/ itself (no repo-root config, no
# path dependency on the main package: it talks to Postgres only through
# the MCP server, never directly).
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.17

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/web/.venv/bin:$PATH"

WORKDIR /app

COPY web/pyproject.toml web/uv.lock ./web/
COPY web/src ./web/src
WORKDIR /app/web
RUN uv sync --frozen --no-dev

# Non-root runtime user.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8080
ENTRYPOINT ["uvicorn", "assay_watch_web.main:app", "--host", "0.0.0.0", "--port", "8080"]
