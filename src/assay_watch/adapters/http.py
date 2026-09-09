"""A polite, legal HTTP client shared by adapters.

Enforces the crawling policy in one place: honour ``robots.txt``, identify with a
descriptive User-Agent, rate-limit per host, and back off with jitter on
``429``/``5xx``. Never does anything a residential-proxy scraper would do.
"""

from __future__ import annotations

import random
import time
import urllib.robotparser
from types import TracebackType

import httpx

from ..logging import get_logger

log = get_logger(__name__)

# Status codes worth retrying: rate limiting and transient server errors.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# Synthetic robots.txt body that forbids everything, used when robots is
# unreachable, forbidden (401/403), or returns a server error.
_DISALLOW_ALL = ["User-agent: *", "Disallow: /"]


class RobotsDisallowed(Exception):
    """Raised when ``robots.txt`` forbids fetching a URL."""


class PoliteClient:
    """A thin wrapper over ``httpx.Client`` implementing the crawling policy."""

    def __init__(
        self,
        *,
        user_agent: str,
        min_interval_seconds: float = 3.0,
        max_retries: int = 4,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._min_interval = min_interval_seconds
        self._max_retries = max_retries
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    # ── robots.txt ────────────────────────────────────────────────────────────
    def _robots_for(self, url: httpx.URL) -> urllib.robotparser.RobotFileParser:
        origin = f"{url.scheme}://{url.host}"
        cached = self._robots.get(origin)
        if cached is not None:
            return cached

        parser = urllib.robotparser.RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        try:
            resp = self._client.get(robots_url)
        except httpx.HTTPError as exc:
            # Unreachable robots -> assume disallow (conservative, RFC 9309).
            log.warning("robots.fetch_failed", origin=origin, error=str(exc))
            parser.parse(_DISALLOW_ALL)
        else:
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif resp.status_code in (401, 403):
                parser.parse(_DISALLOW_ALL)  # forbidden robots -> full disallow
            elif 400 <= resp.status_code < 500:
                parser.parse([])  # e.g. 404 -> no restrictions, allow all
            else:
                parser.parse(_DISALLOW_ALL)  # 5xx -> conservative

        self._robots[origin] = parser
        return parser

    def _allowed(self, url: httpx.URL) -> bool:
        return self._robots_for(url).can_fetch(self._user_agent, str(url))

    # ── rate limiting ───────────────────────────────────────────────────────
    def _respect_rate_limit(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            wait = self._min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    # ── backoff ───────────────────────────────────────────────────────────────
    def _backoff_seconds(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return float(retry_after)
        return (2.0**attempt) + random.uniform(0.0, 1.0)

    # ── public API ────────────────────────────────────────────────────────────
    def get(self, url: str, params: dict[str, str | int] | None = None) -> httpx.Response:
        """GET ``url``, honouring robots, rate limits, and backoff.

        Raises :class:`RobotsDisallowed` if ``robots.txt`` forbids the URL.
        """
        target = httpx.URL(url)
        if not self._allowed(target):
            raise RobotsDisallowed(str(target))

        host = target.host
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            self._respect_rate_limit(host)
            response = self._client.get(url, params=params)
            if response.status_code in _RETRY_STATUS and attempt < self._max_retries:
                delay = self._backoff_seconds(attempt, response)
                log.warning(
                    "http.retry",
                    url=url,
                    status=response.status_code,
                    attempt=attempt,
                    delay=round(delay, 2),
                )
                time.sleep(delay)
                continue
            return response
        assert response is not None  # loop always assigns at least once
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
