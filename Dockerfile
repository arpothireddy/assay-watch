# NOTE: pin this base image by digest (python:3.12-slim@sha256:...) before the
# first real deploy — see PHASE0 security requirements. Left as a tag here so the
# image builds in any environment; pinning is a follow-up commit.
FROM python:3.12-slim

# uv for fast, locked installs.
RUN pip install --no-cache-dir uv==0.8.17

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies first (cached until the lockfile changes).
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Application files needed at runtime.
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config

# Non-root runtime user. Root filesystem is read-only in compose; writable paths
# (metrics, tmp) are mounted explicitly.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

ENTRYPOINT ["assay"]
CMD ["crawl"]
