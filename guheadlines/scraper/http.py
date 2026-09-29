"""A polite HTTP client: identifies itself, throttles per host, honours robots.txt,
retries transient errors and supports conditional GETs."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 10 * 1024 * 1024  # pages larger than this are not news articles
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_CRAWL_DELAY = 15.0


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RobotsDisallowed(FetchError):
    pass


class Blocked(FetchError):
    """The site answered with a bot check ("Just a moment...") instead of the page."""

    def __init__(self, message: str):
        super().__init__(message, 403)  # handled like the 403 many bot walls send


class RateLimited(FetchError):
    """The site answered 429 Too Many Requests even after waiting."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, 429)
        self.retry_after = retry_after  # seconds the site asked us to wait, if it said


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


# Titles of the pages bot-protection services show instead of the real page.
_BOT_CHECK_TITLE_RE = re.compile(
    rb"<title[^>]*>\s*(just a moment|one moment, please|attention required|"
    rb"please wait|checking your browser|ddos-guard|access denied)",
    re.IGNORECASE,
)
_BOT_CHECK_MAX_BYTES = 200_000  # challenge pages are small; articles often are not


def bot_check_service(result: FetchResult) -> str | None:
    """The bot-protection service that answered for the site, if it can be told."""
    headers = result.headers
    server = headers.get("server", "").lower()
    head = result.content[:50_000].lower()
    if "cf-ray" in headers or server == "cloudflare":
        return "Cloudflare"
    if b"sgcaptcha" in head:
        return "SiteGround"
    if "akamai" in server:
        return "Akamai"
    if "x-sucuri-id" in headers or b"sucuri" in head:
        return "Sucuri"
    if b"_incapsula_resource" in head or "x-iinfo" in headers:
        return "Imperva"
    if b"ddos-guard" in head:
        return "DDoS-Guard"
    return None


def is_bot_check(result: FetchResult) -> bool:
    """A successful-looking answer that is really a bot check page."""
    if result.content_type not in ("", "text/html") or not result.content:
        return False
    if len(result.content) > _BOT_CHECK_MAX_BYTES:
        return False
    return bool(_BOT_CHECK_TITLE_RE.search(result.content[:50_000]))


def retry_after_seconds(value: str | None) -> float | None:
    """Parse a Retry-After header: either seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


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
        self._robots_ignored: set[str] = set()
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
                delay = parser.crawl_delay(self.robots_agent)
                if delay and self.respect_robots:
                    # Honour Crawl-delay (capped so one site cannot stall a run).
                    self.set_host_delay(parts.netloc, min(float(delay), MAX_CRAWL_DELAY))
        except FetchError as exc:
            # No readable robots.txt: nothing is disallowed.
            log.debug("robots.txt unavailable for %s: %s", origin, exc)
        with self._robots_lock:
            self._robots[origin] = parser
        return parser

    def ignore_robots_for(self, url_or_host: str) -> None:
        """Do not apply robots.txt to this one site (a per-source owner's choice)."""
        host = urlsplit(url_or_host).netloc or url_or_host
        with self._host_lock:
            self._robots_ignored.add(host)

    def robots_permits(self, url: str) -> bool:
        """What robots.txt says about this URL, whether or not we honour it."""
        parser = self._robots_for(url)
        return parser is None or parser.can_fetch(self.robots_agent, url)

    def allowed(self, url: str) -> bool:
        if not self.respect_robots or urlsplit(url).netloc in self._robots_ignored:
            return True
        return self.robots_permits(url)

    # -- requests -----------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        referer: str | None = None,
        max_bytes: int = MAX_BODY_BYTES,
        check_robots: bool = True,
    ) -> FetchResult:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if referer:
            headers["Referer"] = referer
        return self._request(
            "GET", url, headers=headers, max_bytes=max_bytes, check_robots=check_robots
        )

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
                        raise RateLimited(
                            f"HTTP 429 (rate limited) for {url}",
                            retry_after=retry_after_seconds(result.headers.get("retry-after")),
                        )
                    if status >= 400:
                        note = ""
                        if status in (401, 403, 451):
                            service = bot_check_service(result)
                            if service:
                                note = f" (blocked by {service})"
                            elif is_bot_check(result):
                                note = " (blocked by a bot check)"
                        raise FetchError(f"HTTP {status} for {url}{note}", status)
                    if is_bot_check(result):
                        service = bot_check_service(result) or "The site"
                        raise Blocked(f"{service} showed a bot check instead of the page: {url}")
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
        retry_after = retry_after_seconds(response.headers.get("retry-after"))
        if retry_after is not None:
            return min(retry_after, 60.0)
        if response.status_code == 429:
            return 15.0 * (attempt + 1)  # rate limiters need a real pause
        return 2.0 * (attempt + 1)
