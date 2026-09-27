"""A polite HTTP client: identifies itself, throttles per host, honours robots.txt,
retries transient errors and supports conditional GETs."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 10 * 1024 * 1024  # pages larger than this are not news articles
RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RobotsDisallowed(FetchError):
    pass


class RateLimited(FetchError):
    """The site answered 429 Too Many Requests even after waiting."""


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        robots_agent: str = "GUHeadlinesBot",
        timeout: float = 25.0,
        per_host_delay: float = 1.5,
        respect_robots: bool = True,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.user_agent = user_agent
        # robots.txt rules are matched on a product token, not the full UA string.
        self.robots_agent = robots_agent
        self.per_host_delay = per_host_delay
        self.respect_robots = respect_robots
        self.retries = retries
        self._sleep = sleep
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/rss+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
            follow_redirects=True,
            max_redirects=8,
            transport=transport,
        )
        self._host_lock = threading.Lock()
        self._host_next: dict[str, float] = {}
        self._host_delay: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- politeness ---------------------------------------------------------

    def set_host_delay(self, url_or_host: str, seconds: float) -> None:
        """Use a longer pause than per_host_delay for one site (e.g. one that rate-limits)."""
        host = urlsplit(url_or_host).netloc or url_or_host
        with self._host_lock:
            self._host_delay[host] = max(seconds, self.per_host_delay)

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        with self._host_lock:
            now = time.monotonic()
            wait_until = self._host_next.get(host, now)
            start = max(now, wait_until)
            self._host_next[host] = start + self._host_delay.get(host, self.per_host_delay)
        delay = start - now
        if delay > 0:
            self._sleep(delay)

    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
        parser: robotparser.RobotFileParser | None = None
        try:
            result = self._request("GET", origin + "/robots.txt", check_robots=False)
            if result.status == 200:
                parser = robotparser.RobotFileParser()
                parser.parse(result.content.decode("utf-8", errors="replace").splitlines())
        except FetchError as exc:
            # No readable robots.txt: nothing is disallowed.
            log.debug("robots.txt unavailable for %s: %s", origin, exc)
        with self._robots_lock:
            self._robots[origin] = parser
        return parser

    def robots_permits(self, url: str) -> bool:
        """What robots.txt says about this URL, whether or not we honour it."""
        parser = self._robots_for(url)
        return parser is None or parser.can_fetch(self.robots_agent, url)

    def allowed(self, url: str) -> bool:
        return not self.respect_robots or self.robots_permits(url)

    # -- requests -----------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        referer: str | None = None,
        max_bytes: int = MAX_BODY_BYTES,
    ) -> FetchResult:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if referer:
            headers["Referer"] = referer
        return self._request("GET", url, headers=headers, max_bytes=max_bytes)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int = MAX_BODY_BYTES,
        check_robots: bool = True,
    ) -> FetchResult:
        if check_robots and not self.allowed(url):
            raise RobotsDisallowed(f"blocked by robots.txt: {url}")

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(url)
            try:
                with self._client.stream(method, url, headers=headers) as response:
                    status = response.status_code
                    if status in RETRY_STATUSES and attempt < self.retries:
                        last_error = FetchError(f"HTTP {status}", status)
                        self._sleep(self._retry_delay(response, attempt))
                        continue
                    body = b""
                    if status != 304:
                        chunks = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise FetchError(f"response larger than {max_bytes} bytes")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                    result = FetchResult(
                        url=str(response.url),
                        status=status,
                        content=body,
                        headers={k.lower(): v for k, v in response.headers.items()},
                    )
                    if status == 429:
                        raise RateLimited(f"HTTP 429 (rate limited) for {url}", status)
                    if status >= 400:
                        raise FetchError(f"HTTP {status} for {url}", status)
                    return result
            except (httpx.TransportError, httpx.TooManyRedirects) as exc:
                last_error = FetchError(f"{type(exc).__name__}: {exc}")
                if attempt < self.retries:
                    self._sleep(2.0 * (attempt + 1))
                    continue
        assert last_error is not None
        raise last_error

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit():
            return min(float(retry_after), 60.0)
        if response.status_code == 429:
            return 15.0 * (attempt + 1)  # rate limiters need a real pause
        return 2.0 * (attempt + 1)
