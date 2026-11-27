"""The website: server-rendered pages (fast, no JavaScript needed, bookmarkable URLs)."""

from __future__ import annotations

import base64
import hashlib
import logging
import random
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import func, select, update
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import (
    ads,
    alerts,
    gas,
    guide_packs,
    guides,
    health,
    listings,
    mail,
    paypal,
    renewals,
    sendfox,
)
from ..bids import alerts as bid_alerts
from ..config import (
    AppConfig,
    CalendarConfig,
    ConfigError,
    load_bids_config,
    load_config,
    load_events_config,
    load_jobs_config,
)
from ..db import session_scope
from ..events import ical
from ..events.pipeline import DAYS_AHEAD as EVENT_DAYS_AHEAD
from ..models import (
    CATCH_UP_NOTE,
    FEATURED_CALENDAR,
    AdBooking,
    Article,
    Guide,
    GuidePack,
    GuidePass,
    Listing,
    Source,
)
from ..newsletter import schedule_text
from ..outages import EPA_URL, GPA_TROUBLE_PHONE, GPA_URL, GWA_DISPATCH_PHONE, GWA_URL, outages_page
from ..scraper.images import SMALL_WIDTH, ImageRejected, small_image_path
from ..scraper.text import GUAM_TZ, truncate
from ..settings import DAY_NAMES, get_settings
from ..settings import _web_address as web_address
from ..weather import (
    AIRPORT_SCHEDULE_URL,
    BEACH_REPORT_URL,
    CLIMATE_URL,
    GUAM_RADAR_URL,
    JTWC_URL,
    MARINE_PAGE,
    NWS_GUAM_URL,
    SURF_PAGE,
    for_page,
    weather_page,
)
from . import backups, event_calendar, queries, seo, sponsor, stats
from .queries import MARK_END, MARK_START, BidFilters, EventFilters, Filters, JobFilters

log = logging.getLogger(__name__)
settings = get_settings()
HERE = Path(__file__).resolve().parent

STATIC_DIR = HERE / "static"

# --------------------------------------------------------- static files


def _file_version(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


# Each static file's address carries a hash of its contents (style.css?v=1a2b3c...),
# so browsers can keep it for a year and still get a new copy after any edit.
_static_versions = {p.name: _file_version(p) for p in STATIC_DIR.iterdir() if p.is_file()}


def static_url(name: str) -> str:
    return f"/static/{name}?v={_static_versions[name]}"


class CachedStaticFiles(StaticFiles):
    """StaticFiles that also tells browsers how long they may keep each file."""

    def __init__(
        self,
        *args,
        cache_control: Callable[[Scope], str],
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._cache_control = cache_control
        self._headers = headers or {}

    def file_response(self, full_path, stat_result, scope: Scope, status_code: int = 200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = self._cache_control(scope)
        response.headers.update(self._headers)
        return response


def _static_cache(scope: Scope) -> str:
    # Addresses made by static_url() change whenever the file does.
    if b"v=" in scope.get("query_string", b""):
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600"


def _media_cache(scope: Scope) -> str:
    # Stored photos are named after a hash of their source address and never
    # rewritten in place.
    return "public, max-age=2592000"


# ------------------------------------------------------ security headers

# The light/dark script is inlined in every page (see base.html); the policy
# allows exactly that script and nothing else inline.
THEME_SCRIPT = (STATIC_DIR / "theme.js").read_text(encoding="utf-8")
_theme_hash = base64.b64encode(hashlib.sha256(THEME_SCRIPT.encode("utf-8")).digest()).decode()

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        f"script-src 'sha256-{_theme_hash}'",
        "style-src 'self'",
        # Stories without a stored copy show the photo from the news site itself.
        "img-src 'self' https: http: data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def ad_page_policy(nonce: str) -> str:
    """The policy for a page that shows the ad block (_ad.html).

    Google AdSense loads scripts, frames and requests from many addresses that
    Google does not list, so AdSense supports only a nonce-based policy: the
    AdSense code carries this page's nonce and may then load what it needs
    ('strict-dynamic'). Google's supported policy also allows 'unsafe-eval',
    and limits nothing else, so ads (and Google's consent message for European
    visitors) can use frames, styles, fonts and requests from any https address.
    """
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'nonce-{nonce}' 'sha256-{_theme_hash}' 'strict-dynamic' 'unsafe-eval'",
            "style-src 'self' 'unsafe-inline' https:",
            "img-src 'self' https: http: data:",
            "font-src 'self' https: data:",
            "connect-src 'self' https:",
            "frame-src https:",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


SECURITY_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    # A site this one links to cannot reach back into this page's window.
    (b"cross-origin-opener-policy", b"same-origin"),
]


# Sent only on visits over https (through Caddy, once SITE_DOMAIN is set):
# browsers then use https for this site for a year, even when a visitor types
# or follows a plain http:// address. Browsers ignore it over http.
HSTS_HEADER = (b"strict-transport-security", b"max-age=31536000")


class SecurityHeadersMiddleware:
    """Adds SECURITY_HEADERS to every response (pages, files and errors alike),
    and HSTS_HEADER to those sent over https."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Uvicorn takes the scheme from Caddy's X-Forwarded-Proto header.
        headers = SECURITY_HEADERS
        if scope["scheme"] == "https":
            headers = [*SECURITY_HEADERS, HSTS_HEADER]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in headers if n not in names]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RejectNulMiddleware:
    """Answers 400 to an address that contains a NUL character. PostgreSQL does
    not accept NUL in text, so such a search or name would fail with a server error."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and ("\x00" in scope["path"] or b"%00" in scope["query_string"]):
            await PlainTextResponse("Bad request", status_code=400)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# The largest request the site takes: the guide editor's form with two photos
# (guides.MAX_FORM, about 16 MB), and room to spare. Caddy refuses larger ones
# too (caddy/Caddyfile).
MAX_REQUEST_BYTES = 20 * 1024 * 1024


class LimitRequestSizeMiddleware:
    """Answers 413 at once to a request that says it is larger than
    MAX_REQUEST_BYTES, before any of it is read. (The forms also stop
    reading at their own, smaller limits.)"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"content-length" and (
                    not value.strip().isdigit() or int(value) > MAX_REQUEST_BYTES
                ):
                    answer = PlainTextResponse("Too large", status_code=413)
                    await answer(scope, receive, send)
                    return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Counts that this worker has not written to the database yet.
    stats.counts.flush()


app = FastAPI(
    title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)
app.add_middleware(RejectNulMiddleware)
app.add_middleware(LimitRequestSizeMiddleware)
# Pages are mostly text and shrink to a fraction of their size; photos (and
# backup downloads, which are mostly photos) are already compressed and left
# alone.
app.add_middleware(
    GZipMiddleware,
    minimum_size=1024,
    compresslevel=6,
    exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/x-tar"),
)
app.add_middleware(SecurityHeadersMiddleware)
app.mount(
    "/static",
    CachedStaticFiles(directory=STATIC_DIR, cache_control=_static_cache),
    name="static",
)
settings.media_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    "/media",
    CachedStaticFiles(
        directory=settings.media_dir,
        cache_control=_media_cache,
        # The stored photos belong to the news sites; keep these copies out of
        # image search, so searches lead to the publishers' own pages.
        headers={"X-Robots-Tag": "noindex"},
    ),
    name="media",
)
templates = Jinja2Templates(directory=HERE / "templates")

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip


# ------------------------------------------------------------------ config


CONFIG_RELOAD_SECONDS = 60


class _ConfigCache:
    """Re-reads the YAML config at most once a minute, keeping the last good copy."""

    def __init__(self) -> None:
        self._config: AppConfig | None = None
        self._loaded = 0.0

    def get(self) -> AppConfig:
        if self._config is None or time.monotonic() - self._loaded > CONFIG_RELOAD_SECONDS:
            try:
                self._config = load_config(settings.config_dir)
            except ConfigError as exc:
                log.error("config error: %s", exc)
                if self._config is None:
                    raise  # no good copy to fall back on
            self._loaded = time.monotonic()
        return self._config


config_cache = _ConfigCache()


class _ListConfigCache:
    """jobs.yaml, events.yaml or bids.yaml, re-read at most once a minute (a broken edit
    keeps the last good copy; a broken file at startup means an empty list
    rather than a broken site)."""

    def __init__(self, load: Callable[[Path], list], name: str) -> None:
        self._load = load
        self._name = name
        self._items: list = []
        self._loaded: float | None = None

    def get(self) -> list:
        if self._loaded is None or time.monotonic() - self._loaded > CONFIG_RELOAD_SECONDS:
            try:
                self._items = self._load(settings.config_dir)
            except ConfigError as exc:
                log.error("%s config error: %s", self._name, exc)
            self._loaded = time.monotonic()
        return self._items


jobs_config_cache: _ListConfigCache = _ListConfigCache(load_jobs_config, "jobs")
events_config_cache: _ListConfigCache = _ListConfigCache(load_events_config, "events")
bids_config_cache: _ListConfigCache = _ListConfigCache(load_bids_config, "bids")


class _TTLCache:
    """Keeps computed values for `ttl` seconds (per web worker process)."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, object]] = {}

    def get(self, key: str, compute: Callable[[], object]):
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        value = compute()
        self._data[key] = (time.monotonic(), value)
        return value


# The sidebar's counts run several queries; every page shows them, so they are
# shared between requests for two minutes.
sidebar_cache = _TTLCache(ttl=120)
# The weather is read on every page, for the warning banner; the worker
# updates it every few minutes. The outage notices (outages.py) are kept in
# the same table and come with it.
weather_cache = _TTLCache(ttl=60)


def weather_reports() -> dict[str, tuple]:
    """The saved weather (see weather.py) and outage notices (outages.py)."""

    def load():
        with session_scope() as session:
            return queries.weather_reports(session)

    return weather_cache.get("weather", load)


# Paid featured events (listings.py), shown on the home page and the Events page.
featured_cache = _TTLCache(ttl=60)
FEATURED_SHOWN = 4


def featured_events() -> list[dict]:
    """The next featured events, soonest first."""

    def load():
        today = datetime.now(GUAM_TZ).date()
        since = datetime.combine(today, datetime.min.time(), tzinfo=GUAM_TZ)
        with session_scope() as session:
            found = queries.list_events(
                session, EventFilters(calendar=FEATURED_CALENDAR), 1, FEATURED_SHOWN, since
            )
        return found.items

    return featured_cache.get("featured", load)


# Paid deals (listings.py), on the home page and the Deals page. The home page
# shows a few, picked again each minute, so that each deal gets its turn.
deals_cache = _TTLCache(ttl=60)
DEALS_SHOWN = 3


def current_deals() -> dict:
    """A few of the deals on the site now, and how many there are."""

    def load():
        with session_scope() as session:
            found = queries.listings(session, "deal", datetime.now(timezone.utc), per_page=60)
        return {"items": found.items, "total": found.total}

    found = deals_cache.get("deals", load)
    items = found["items"]
    picked = random.sample(items, DEALS_SHOWN) if len(items) > DEALS_SHOWN else items
    return {"items": picked, "total": found["total"]}


# --------------------------------------------------------- template helpers


def to_local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(GUAM_TZ) if dt else None


def fmt_datetime(dt: datetime | None) -> str:
    local = to_local(dt)
    if not local:
        return ""
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {local.year}, {hour}:{local:%M} {local:%p} ChST"


def fmt_until(dt: datetime) -> str:
    """When a weather alert ends: Thu, Nov 26, 3:00 PM ChST"""
    local = to_local(dt)
    return f"{local:%a, %b} {local.day}, {local.hour % 12 or 12}:{local:%M %p} ChST"


def fmt_date_long(value: date | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = to_local(value).date()
    return f"{value:%A}, {value:%B} {value.day}, {value.year}"


def fmt_day(value: date | datetime | None) -> str:
    """Oct 13, 2026"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = to_local(value).date()
    return f"{value:%b} {value.day}, {value.year}"


def rel_time(dt: datetime | None) -> str:
    if not dt:
        return ""
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hr ago" if hours == 1 else f"{hours} hrs ago"
    local = to_local(dt)
    today = datetime.now(GUAM_TZ).date()
    if (today - local.date()).days == 1:
        return "yesterday"
    if local.year == today.year:
        return f"{local:%b} {local.day}"
    return f"{local:%b} {local.day}, {local.year}"


def day_label(day: date) -> str:
    today = datetime.now(GUAM_TZ).date()
    delta = (today - day).days
    prefix = {0: "Today", 1: "Yesterday", -1: "Tomorrow"}.get(delta)
    long = fmt_date_long(day)
    return f"{prefix} · {long}" if prefix else long


def fmt_time(dt: datetime | None) -> str:
    """9:30 AM (Guam time)"""
    local = to_local(dt)
    if not local:
        return ""
    return f"{local.hour % 12 or 12}:{local:%M} {local:%p}"


def fmt_hour(dt: datetime) -> str:
    """2 PM (Guam time)"""
    local = to_local(dt)
    return f"{local.hour % 12 or 12} {local:%p}"


def _short_day(value: date) -> str:
    return f"{value:%a}, {value:%b} {value.day}"


def event_when(event) -> str:
    """When an event is, in full: "Tue, Dec 1, 9:00 AM – 12:00 PM", "Mon, Dec 21 –
    Wed, Dec 23", "Sat, Dec 5, all day"."""
    start = to_local(event.starts_at)
    end = to_local(event.ends_at)
    if event.all_day:
        last = (end - timedelta(days=1)).date() if end else start.date()
        if last > start.date():
            return f"{_short_day(start.date())} – {_short_day(last)}"
        return f"{_short_day(start.date())}, all day"
    text = f"{_short_day(start.date())}, {fmt_time(start)}"
    if end and end.date() == start.date():
        return f"{text} – {fmt_time(end)}"
    if end:
        return f"{text} – {_short_day(end.date())}, {fmt_time(end)}"
    return text


def bid_due(bid) -> str:
    """When a bid is due: "Tue, Dec 1, 2026, 10:00 AM ChST", or the day alone
    when the source gives no time."""
    local = to_local(bid.due_at)
    if not local:
        return ""
    day = f"{_short_day(local.date())}, {local.year}"
    return f"{day}, {fmt_time(local)} ChST" if bid.due_time else day


def due_in(dt: datetime | None) -> str:
    """How long until a bid is due, in Guam days: "today", "tomorrow", "in 6 days"."""
    local = to_local(dt)
    if not local:
        return ""
    days = (local.date() - datetime.now(GUAM_TZ).date()).days
    return "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"


def event_time(event, day: date) -> str:
    """The short label beside an event under `day`: its start time, "All day",
    or "Ongoing" for an event that began on an earlier day."""
    start = to_local(event.starts_at)
    if start.date() < day:
        return "Ongoing"
    return "All day" if event.all_day else fmt_time(event.starts_at)


def highlight(snippet: str | None) -> Markup:
    if not snippet:
        return Markup("")
    safe = str(escape(snippet))
    return Markup(safe.replace(MARK_START, "<mark>").replace(MARK_END, "</mark>"))


def paragraphs(text: str | None) -> list[str]:
    return [p for p in (text or "").split("\n\n") if p.strip()]


def is_note(message: str | None) -> bool:
    """A run message that only reports a normal catch-up pause, not a problem."""
    parts = [part.strip() for part in (message or "").split("; ")]
    return bool(message) and all(part.startswith(CATCH_UP_NOTE) for part in parts)


def safe_url(url: str | None) -> str:
    """The address itself if it is a web (http/https) link, else "".

    Links and photo addresses come from scraped pages and feeds; this keeps a
    javascript: or data: address from ever reaching an href or src.
    """
    url = (url or "").strip()
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in ("http", "https") else ""


def story_link(article) -> str:
    """Where a story's links lead: through /go/<id>, which counts the click,
    while visitor stats are on (STATS_PASSWORD); else straight to the story."""
    if settings.stats_password:
        return f"/go/{article.id}"
    return safe_url(article.url)


# How wide story photos are drawn (style.css, .story and .thumb): across the
# card on phones, 228 pixels beside the story on larger screens. The browser
# uses it to pick the small or the full copy.
PHOTO_SIZES = "(max-width: 640px) calc(100vw - 46px), 228px"


def story_photo(article) -> dict | None:
    """The photo of a story card: our saved copy, with its small copy for
    larger screens once that is made; the news site's own photo when we have
    no copy (never saved, deleted by IMAGE_STORAGE_GB, or the media folder
    was not moved with the database); None without a photo."""
    path = article.image_path
    if path:
        size = {"width": article.image_width, "height": article.image_height}
        small = small_image_path(path)
        if (article.image_width or 0) > SMALL_WIDTH and (settings.media_dir / small).is_file():
            srcset = f"/media/{small} {SMALL_WIDTH}w, /media/{path} {article.image_width}w"
            return {"src": f"/media/{path}", "srcset": srcset, **size}
        if (settings.media_dir / path).is_file():
            return {"src": f"/media/{path}", **size}
    url = safe_url(article.image_url)
    return {"src": url, "remote": True} if url else None


def domain(url: str | None) -> str:
    """https://www.guampdn.com/news/... -> guampdn.com"""
    host = (urlsplit(url or "").hostname or "").lower()
    return host.removeprefix("www.")


templates.env.filters.update(
    duration=health.duration,
    fmt_datetime=fmt_datetime,
    fmt_time=fmt_time,
    event_when=event_when,
    fmt_until=fmt_until,
    short_day=_short_day,
    fmt_hour=fmt_hour,
    fmt_date_long=fmt_date_long,
    fmt_day=fmt_day,
    rel_time=rel_time,
    highlight=highlight,
    paragraphs=paragraphs,
    to_local=to_local,
    domain=domain,
    safe_url=safe_url,
)
templates.env.tests.update(note=is_note)
templates.env.filters.update(money=listings.money, human_size=backups.human_size)
templates.env.globals.update(
    settings=settings,
    event_time=event_time,
    bid_due=bid_due,
    due_in=due_in,
    months=MONTHS,
    static_url=static_url,
    story_link=story_link,
    story_photo=story_photo,
    photo_sizes=PHOTO_SIZES,
    theme_script=Markup(THEME_SCRIPT),
    nws_guam_url=NWS_GUAM_URL,
    jtwc_url=JTWC_URL,
    guam_radar_url=GUAM_RADAR_URL,
    surf_page_url=SURF_PAGE,
    marine_page_url=MARINE_PAGE,
    beach_report_url=BEACH_REPORT_URL,
    airport_schedule_url=AIRPORT_SCHEDULE_URL,
    climate_url=CLIMATE_URL,
    upgraded=listings.upgraded,
    deal_last_day=listings.deal_last_day,
    deal_weeks=listings.paid_weeks,
    listing_kinds=listings.KINDS,
    featured_calendar=FEATURED_CALENDAR,
    guide_minutes=lambda guide: guides.reading_minutes(guide.body),
    guide_srcset=guides.photo_srcset,
    guide_photo_size=guides.photo_size,
    guide_photo_sizes=guides.PHOTO_SIZES,
    guide_pack_money=guide_packs.money,
    # Email is set up (mail.py): the "lost your link?" form of the guide packs.
    mail_on=lambda: mail.enabled(settings),
)


def group_by_day(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """[(day label, items)] in the given order; items are sorted newest or oldest first."""
    groups: list[tuple[str, list[dict]]] = []
    current = None
    for item in items:
        day = to_local(item["article"].published_at).date()
        if day != current:
            groups.append((day_label(day), []))
            current = day
        groups[-1][1].append(item)
    return groups


def group_events(items: list[dict], today: date) -> list[tuple[date, str, list[dict]]]:
    """[(day, label, items)] in order. An event that began before today and
    is still on is listed under today."""
    groups: list[tuple[date, str, list[dict]]] = []
    for item in items:
        day = max(to_local(item["event"].starts_at).date(), today)
        if not groups or groups[-1][0] != day:
            groups.append((day, day_label(day), []))
        groups[-1][2].append(item)
    return groups


def page_url(request: Request, **changes) -> str:
    """The current URL with some query parameters changed (used for pagination)."""
    params = {**request.query_params, **changes}
    if str(params.get("page")) == "1":
        del params["page"]
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{request.url.path}?{query}" if query else request.url.path


def gas_prices(config) -> list[gas.Price]:
    """Guam's newest regular, premium and diesel prices, read from the news
    (gas.py). Empty when GAS_PRICE_DAYS is 0 or no recent story gives one."""
    if not settings.gas_price_days:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=settings.gas_price_days)
    with session_scope() as session:
        rows = queries.gas_price_stories(
            session,
            since,
            hide_sources=config.hidden_sources(),
            hide_topics=config.separate_topics(),
            mute=config.muted,
        )
    return gas.latest_prices([gas.Story(**row) for row in rows])


def advertising() -> list[ads.Offer]:
    """What the Advertise page sells (ads.py), or [] while it is off. It needs
    a price, PayPal (paypal.py), and STATS_PASSWORD, which opens the owner's
    review page."""
    if not (settings.stats_password and paypal.enabled(settings)):
        return []
    return ads.offers(settings)


# This week's booked ad is looked for on every page, so it is kept for a minute.
ad_cache = _TTLCache(ttl=60)


def booked_site_ad() -> AdBooking | None:
    """The approved ad booked for this week's sponsor spot (ads.py), if any."""

    def find() -> AdBooking | None:
        with session_scope() as session:
            return ads.site_ad(session, datetime.now(GUAM_TZ).date())

    return ad_cache.get("site", find)


def sponsor_ad() -> dict | None:
    """The sponsor spot (_sponsor.html): this week's booked ad, else the
    picture in SPONSOR_IMAGE; None when there is neither."""
    booked = booked_site_ad()
    if booked is not None and booked.picture:
        return {
            "src": f"/sponsor.webp?v=b{booked.id}",
            "width": booked.picture_width,
            "height": booked.picture_height,
            "alt": booked.words,
            # /go/ad/<id> counts the click (while the visitor stats are on).
            "href": f"/go/ad/{booked.id}",
            "rel": "nofollow sponsored noopener",
        }
    picture = sponsor.picture(settings)
    if picture is None:
        return None
    link = settings.sponsor_link
    counted = bool(link and settings.stats_password)
    return {
        "src": f"/sponsor.webp?v={picture.version}",
        "width": picture.width,
        "height": picture.height,
        "alt": settings.sponsor_alt or (f"Sponsor: {domain(link)}" if link else "Sponsor"),
        # While the visitor stats are on, the click is counted on the way.
        "href": "/go/sponsor" if counted else link,
        "rel": "nofollow sponsored noopener" if counted else "sponsored noopener",
    }


# The lines of the "Plan a trip" box (_travel.html), each with the setting
# that holds its affiliate link.
TRAVEL_LINKS = (
    ("Hotels", "affiliate_hotels_link"),
    ("Flights", "affiliate_flights_link"),
    ("Tours and activities", "affiliate_tours_link"),
    ("Car rentals", "affiliate_cars_link"),
)


def travel_links() -> list[dict]:
    """The "Plan a trip" box: one line for each affiliate link that is set."""
    return [
        {"label": label, "href": getattr(settings, name)}
        for label, name in TRAVEL_LINKS
        if getattr(settings, name)
    ]


def _days_text(days: tuple[int, ...], word: str) -> str:
    """(1, 4), "and" -> "Tuesday and Friday"."""
    names = [DAY_NAMES[d].capitalize() for d in days]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" {word} {names[-1]}"


def signup_info() -> dict | None:
    """The newsletter sign-up box and page, or None while SENDFOX_API_TOKEN is empty."""
    if not settings.sendfox_api_token:
        return None
    days = settings.newsletter_days
    pitch = "The top Guam stories by email"
    if len(days) == 7:
        pitch += " every day"
    elif days:
        pitch += " every " + _days_text(days, "and")
    return {"pitch": pitch + "."}


def paid_listings() -> dict[str, dict]:
    """The kinds of paid listings that people can post now (listings.py),
    with their price and days, for the links that offer them."""
    return {
        kind: {
            "price": listings.money(listings.price_cents(settings, kind, featured=True)),
            "days": listings.days(settings, kind),
        }
        for kind in listings.KINDS
        if listings.is_on(settings, kind)
    }


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    """Render a page with what every page needs: the sections, the sidebar and
    the pagination helper."""
    config = config_cache.get()

    def sidebar():
        with session_scope() as session:
            week_ago = datetime.now(timezone.utc) - timedelta(days=7)
            facets = queries.facet_counts(session, week_ago, config.muted)
            last_updated = queries.last_updated(session)
        # News sources only: sites that can't be collected are listed apart, and
        # only_in_topic sources are reached through their topic, even when
        # they can't be collected (the Sources page still lists them).
        hidden = set(config.hidden_sources())
        left_out = {s.slug for s in config.uncollectable_sources} | hidden
        facets["sources"] = [s for s in facets["sources"] if s["slug"] not in left_out]
        return {
            "facets_week": facets,
            "last_updated": last_updated,
            "uncollectable": [s for s in config.uncollectable_sources if s.slug not in hidden],
            "gas": gas_prices(config),
        }

    context.setdefault("title", None)
    count_view = context.pop("count_view", True)
    show_signup = context.pop("show_signup", True)
    show_travel = context.pop("show_travel", False)
    # The ad block, on the pages that ask for it, once AdSense is set up.
    show_ad = context.pop("show_ad", False) and bool(
        settings.adsense_client and settings.adsense_slot
    )
    nonce = secrets.token_urlsafe(18) if show_ad else None
    meta = seo.page_meta(
        request,
        title=context["title"],
        description=context.pop("description", None),
        robots=context.pop("robots", None),
        canonical=context.pop("canonical", None),
        breadcrumbs=context.pop("breadcrumbs", None),
        structured=context.pop("structured", None),
        result=context.get("result"),
        static_url=static_url,
    )
    response = templates.TemplateResponse(
        request,
        template,
        {
            "categories": config.categories,
            "category_names": {c.slug: c.name for c in config.categories},
            "sidebar": sidebar_cache.get("sidebar", sidebar),
            "page_url": lambda **kw: page_url(request, **kw),
            "seo": meta,
            "show_ad": show_ad,
            "csp_nonce": nonce,
            "sponsor": sponsor_ad(),
            # What the Advertise page sells ("site", "newsletter"): its links show.
            "advertise": [offer.kind for offer in advertising()],
            "signup": signup_info(),
            "show_signup": show_signup,
            "bid_alerts": bid_alerts_info(),
            "offers": paid_listings(),
            "travel": travel_links() if show_travel else None,
            # The Guides link in the menu, once a guide is published.
            "guides_menu": bool(published_guides()),
            "weather": (
                for_page(
                    weather_reports(),
                    datetime.now(timezone.utc),
                    tide_station=settings.tide_station,
                )
                if settings.weather_interval_minutes
                else None
            ),
            **context,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "public, max-age=120"
    if nonce:
        # SecurityHeadersMiddleware keeps a policy that is already set.
        response.headers["Content-Security-Policy"] = ad_page_policy(nonce)
    if count_view and status_code == 200 and settings.stats_password and stats.is_reader(request):
        stats.counts.view(request.url.path)
        site = stats.came_from(request)
        if site is not None:  # a visit that started here, not a move between pages
            stats.counts.referrer(site)
    return response


# ------------------------------------------------------------------ routes


def _listing(request: Request, template: str, filters: Filters, page: int, **context):
    """A page of stories grouped by day (home, topic, source and archive day pages)."""
    filters.mute = _muted()
    with session_scope() as session:
        result = queries.list_articles(session, filters, page, settings.page_size)
    if page > 1 and not result.items:
        raise HTTPException(404)
    context.setdefault("canonical", {"page": page})
    if context.pop("noindex_if_empty", False) and not result.items:
        context["robots"] = "noindex"
    return render(
        request,
        template,
        result=result,
        groups=group_by_day(result.items),
        filters=filters,
        **context,
    )


def _hidden(topic: str | None = None) -> tuple[str, ...]:
    """Sources to leave out of a listing for `topic` (only_in_topic sources
    whose stories belong on another topic's page)."""
    return tuple(config_cache.get().hidden_sources(topic))


def _muted() -> tuple[str, ...]:
    """Words and phrases whose stories are never shown (muted: in sources.yaml)."""
    return config_cache.get().muted


def _separate(topic: str | None = None) -> tuple[str, ...]:
    """Topics to leave out of a listing for `topic` (separate topics, such as
    Obituaries, whose stories belong only on their own page)."""
    return tuple(config_cache.get().separate_topics(topic))


@app.get("/", response_class=HTMLResponse)
def home(request: Request, page: int = Query(1, ge=1, le=10000)):
    return _listing(
        request,
        "index.html",
        Filters(hide_sources=_hidden(), hide_topics=_separate()),
        page,
        active_nav="latest",
        show_ad=True,
        show_weather=page == 1,
        featured=featured_events() if page == 1 else [],
        deals=current_deals() if page == 1 else None,
    )


@app.get("/category/{slug}", response_class=HTMLResponse)
def category_page(request: Request, slug: str, page: int = Query(1, ge=1, le=10000)):
    category = config_cache.get().category(slug)
    if not category:
        raise HTTPException(404)
    return _listing(
        request,
        "index.html",
        Filters(category=slug, hide_sources=_hidden(slug), hide_topics=_separate(slug)),
        page,
        active_nav=slug,
        heading=category.name,
        subheading=category.description,
        title=category.page_title,
        description=(
            f"{category.description} The latest headlines on {settings.site_name}, "
            "each linked to the original."
        ).strip(),
        breadcrumbs=[(category.name, None)],
        show_ad=category.show_ad,
    )


@app.get("/source/{slug}", response_class=HTMLResponse)
def source_page(request: Request, slug: str, page: int = Query(1, ge=1, le=10000)):
    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.slug == slug))
    if not source:
        raise HTTPException(404)
    return _listing(
        request,
        "index.html",
        Filters(source=slug),
        page,
        active_nav=None,
        heading=source.name,
        subheading=source.homepage,
        subheading_link=source.homepage,
        title=f"{source.name} headlines",
        description=(
            f"The latest {source.name} headlines, with photos and intros, on {settings.site_name}."
            + (f" Every story links to {domain(source.homepage)}." if source.homepage else "")
        ),
        breadcrumbs=[(source.name, None)],
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = Query("", max_length=200),
    source: str = Query("", max_length=40),
    category: str = Query("", max_length=40),
    date_from: str = Query("", alias="from", max_length=10),
    date_to: str = Query("", alias="to", max_length=10),
    sort: str = Query("relevance", pattern="^(relevance|newest|oldest)$"),
    page: int = Query(1, ge=1, le=1000),
):
    filters = Filters(
        q=q.strip(),
        source=source or None,
        category=category or None,
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        sort=sort,
        hide_sources=_hidden(category or None),
        mute=_muted(),
    )
    fuzzy = False
    result = None
    with session_scope() as session:
        if filters.q:
            result, fuzzy = queries.search_articles(session, filters, page, settings.page_size)
        elif filters.active:
            if filters.sort == "relevance":
                filters.sort = "newest"
            result = queries.list_articles(session, filters, page, settings.page_size)
        config = config_cache.get()
        sources = queries.all_sources(session, keep={s.slug for s in config.sources})
    return render(
        request,
        "search.html",
        result=result,
        fuzzy=fuzzy,
        filters=filters,
        sources=sources,
        title=f"Search: {filters.q}" if filters.q else "Search",
        active_nav="search",
        # Result pages are endless and change hourly; robots.txt also keeps
        # crawlers out.
        robots="noindex, follow",
    )


@app.get("/archive", response_class=HTMLResponse)
def archive(request: Request):
    with session_scope() as session:
        years = queries.archive_months(session, list(_hidden()), list(_separate()), _muted())
    return render(
        request,
        "archive.html",
        years=years,
        title="Guam news archive",
        description=(
            f"Every Guam news story {settings.site_name} has collected, "
            "by year, month and day, with links to the original stories."
        ),
        active_nav="archive",
        canonical={},
        breadcrumbs=[("Archive", None)],
    )


@app.get("/archive/{year}/{month}", response_class=HTMLResponse)
def archive_month(request: Request, year: int, month: int):
    if not (1990 <= year <= 2200 and 1 <= month <= 12):
        raise HTTPException(404)
    with session_scope() as session:
        days = queries.archive_days(
            session, year, month, list(_hidden()), list(_separate()), _muted()
        )
        years = queries.archive_months(session, list(_hidden()), list(_separate()), _muted())
    # The nearest months with stories, so the month-to-month links never lead
    # crawlers (or readers) through endless empty months.
    stored = sorted((y["year"], m["month"]) for y in years for m in y["months"])
    earlier = [ym for ym in stored if ym < (year, month)]
    later = [ym for ym in stored if ym > (year, month)]
    name = f"{MONTHS[month - 1]} {year}"
    return render(
        request,
        "archive_month.html",
        year=year,
        month=month,
        days=days,
        prev_month=earlier[-1] if earlier else None,
        next_month=later[0] if later else None,
        title=f"Guam news archive: {name}",
        description=(
            f"Guam news from {name}, day by day: every headline {settings.site_name} "
            "collected that month, with links to the original stories."
        ),
        active_nav="archive",
        canonical={},
        robots=None if days else "noindex",
        breadcrumbs=[("Archive", "/archive"), (name, None)],
    )


@app.get("/archive/{year}/{month}/{day}", response_class=HTMLResponse)
def archive_day(
    request: Request, year: int, month: int, day: int, page: int = Query(1, ge=1, le=10000)
):
    if not 1990 <= year <= 2200:
        raise HTTPException(404)
    try:
        the_day = date(year, month, day)
    except (ValueError, OverflowError):
        raise HTTPException(404) from None
    hidden = _hidden()
    separate = _separate()
    with session_scope() as session:
        # The nearest days with stories, so the day-to-day links never lead
        # crawlers (or readers) through endless empty days.
        prev_day, next_day = queries.neighbor_days(
            session, the_day, list(hidden), list(separate), _muted()
        )
    long_date = fmt_date_long(the_day)
    month_name = f"{MONTHS[month - 1]} {year}"
    return _listing(
        request,
        "index.html",
        Filters(date_from=the_day, date_to=the_day, hide_sources=hidden, hide_topics=separate),
        page,
        active_nav="archive",
        heading=long_date,
        title=f"Guam news for {long_date}",
        description=(
            f"Guam news from {long_date}: every headline {settings.site_name} collected "
            "that day, with photos and links to the original stories."
        ),
        noindex_if_empty=True,
        breadcrumbs=[
            ("Archive", "/archive"),
            (month_name, f"/archive/{year}/{month}"),
            (long_date, None),
        ],
        archive_day=the_day,
        prev_day=prev_day,
        next_day=next_day,
    )


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    config = config_cache.get()
    with session_scope() as session:
        sources = queries.source_status(session)
        runs = queries.recent_runs(session)
    # Sites removed from sources.yaml are left out; those that can't be
    # collected get their own list.
    listed = {s.slug for s in config.sources if not s.cannot_scrape}
    sources = [row for row in sources if row["source"].slug in listed]
    return render(
        request,
        "status.html",
        sources=sources,
        uncollectable=config.uncollectable_sources,
        runs=runs,
        affiliate_links=bool(travel_links()),
        guide_packs_on=bool(packs_on_sale()),
        title="Sources",
        active_nav="status",
        canonical={},
        show_ad=True,
    )


@app.get("/weather", response_class=HTMLResponse)
def weather_view(request: Request):
    if not settings.weather_interval_minutes:
        raise HTTPException(404)
    shown = weather_page(weather_reports(), datetime.now(timezone.utc), settings.tide_station)
    return render(
        request,
        "weather.html",
        page_weather=shown,
        title="Guam weather and tides",
        description=(
            "Guam weather now: the reading at the airport, the National Weather Service"
            " forecast and alerts, typhoons in the western Pacific, sunrise and sunset,"
            " and the high and low tides for today, the week and the month."
        ),
        active_nav="weather",
        canonical={},
        breadcrumbs=[("Weather", None)],
        show_travel=True,
    )


@app.get("/outages", response_class=HTMLResponse)
def outages_view(request: Request):
    if not settings.outages_interval_minutes:
        raise HTTPException(404)
    return render(
        request,
        "outages.html",
        outages=outages_page(weather_reports(), datetime.now(timezone.utc)),
        sources={
            "gpa": GPA_URL,
            "gwa": GWA_URL,
            "epa": EPA_URL,
            "gpa_phone": GPA_TROUBLE_PHONE,
            "gwa_phone": GWA_DISPATCH_PHONE,
        },
        title="Guam power and water outages",
        description=(
            "Power outages from the Guam Power Authority and water outages and boil-water"
            " notices from the Guam Waterworks Authority and Guam EPA: today, coming up and"
            " the last few days, by village."
        ),
        active_nav="outages",
        canonical={},
        breadcrumbs=[("Outages", None)],
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs(
    request: Request,
    q: str = Query("", max_length=200),
    source: str = Query("", max_length=60),
    page: int = Query(1, ge=1, le=10000),
):
    boards_config = jobs_config_cache.get()
    job_filters = JobFilters(q=q.strip(), source=source.strip() or None)
    today = datetime.now(GUAM_TZ).date()
    with session_scope() as session:
        result = queries.list_jobs(session, job_filters, page, settings.page_size, today)
        boards = queries.job_boards(session, today)
        # Paid job posts (listings.py), pinned above the boards' postings.
        featured = (
            queries.listings(
                session, "job", datetime.now(timezone.utc), q=job_filters.q, per_page=20
            ).items
            if page == 1 and not job_filters.source
            else []
        )
    if page > 1 and not result.items:
        raise HTTPException(404)
    # Boards that can't be collected are listed apart, with the reason.
    collected = {b.slug for b in boards_config if b.collected}
    boards = [b for b in boards if b["board"].slug in collected]
    return render(
        request,
        "jobs.html",
        result=result,
        featured=featured,
        job_filters=job_filters,
        boards=boards,
        uncollectable=[b for b in boards_config if b.enabled and b.cannot_scrape],
        title="Jobs on Guam",
        description=_jobs_description([b["board"].name for b in boards]),
        active_nav="jobs",
        canonical={"source": job_filters.source, "page": page},
        # A word search is a search page; a board's own list is worth finding.
        robots="noindex, follow" if job_filters.q else None,
        breadcrumbs=[("Jobs", None)],
        show_ad=True,
    )


@app.get("/events", response_class=HTMLResponse)
def events(
    request: Request,
    q: str = Query("", max_length=200),
    calendar: str = Query("", max_length=60),
    month: str = Query("", max_length=7),
    day: str = Query("", max_length=10),
    page: int = Query(1, ge=1, le=10000),
):
    calendars_config: list[CalendarConfig] = events_config_cache.get()
    today = datetime.now(GUAM_TZ).date()
    last_day = today + timedelta(days=EVENT_DAYS_AHEAD)
    since = datetime.combine(today, datetime.min.time(), tzinfo=GUAM_TZ)
    # A day picked on the month calendar narrows the list to that day.
    try:
        picked = date.fromisoformat(day) if day else None
    except ValueError:
        picked = None
    if picked and not today <= picked <= last_day:
        picked = None
    event_filters = EventFilters(q=q.strip(), calendar=calendar.strip() or None, day=picked)
    shown = event_calendar.first_of(picked) if picked else event_calendar.parse_month(month)
    lowest, highest = event_calendar.first_of(today), event_calendar.first_of(last_day)
    shown = min(max(shown or lowest, lowest), highest)
    with session_scope() as session:
        result = queries.list_events(session, event_filters, page, settings.page_size, since)
        calendars = queries.event_calendars(session, since)
        in_month = queries.month_events(
            session,
            EventFilters(q=event_filters.q, calendar=event_filters.calendar),
            *event_calendar.month_span(shown),
            since,
        )
    if page > 1 and not result.items:
        raise HTTPException(404)
    # Calendars that can't be collected are not shown.
    collected = {c.slug for c in calendars_config if c.collected}
    calendars = [c for c in calendars if c["calendar"].slug in collected]
    colors = {c.slug: c.color for c in calendars_config if c.color}
    colors[FEATURED_CALENDAR] = event_calendar.FEATURED_COLOR
    # The calendar feed to offer: the calendar the page is narrowed to, or all.
    feed_calendar = next(
        (c for c in calendars_config if c.collected and c.slug == event_filters.calendar), None
    )
    month_view = event_calendar.build_month(
        shown,
        today,
        last_day,
        in_month,
        calendars_config,
        # Narrowed to one calendar or a word: every matching event.
        everything=bool(event_filters.q or event_filters.calendar),
        selected=picked,
    )
    return render(
        request,
        "events.html",
        result=result,
        featured=(
            featured_events() if page == 1 and not (picked or event_filters.calendar) else []
        ),
        groups=group_events(result.items, picked or today),
        event_filters=event_filters,
        calendars=calendars,
        month_view=month_view,
        colors=colors,
        legend=[c for c in calendars if c["calendar"].slug in colors],
        featured_legend=bool(featured_events()),
        feed=_feed_links(request, feed_calendar.slug if feed_calendar else None),
        feed_calendar=feed_calendar,
        title="Events on Guam",
        description=_events_description([c["calendar"].name for c in calendars]),
        active_nav="events",
        canonical={"calendar": event_filters.calendar, "page": page},
        # A word search is a search page, and another month or a picked day
        # is the same page again; a calendar's own list is worth finding.
        robots="noindex, follow" if event_filters.q or month or day else None,
        breadcrumbs=[("Events", None)],
        show_ad=True,
        show_travel=True,
    )


def _feed_links(request: Request, calendar: str | None) -> dict:
    """Where the events feed is, and the links that add it to a calendar app:
    webcal:// opens Apple Calendar (and Outlook) ready to subscribe, and Google
    Calendar takes the same address in its own link."""
    path = "/events.ics" + (f"?{urlencode({'calendar': calendar})}" if calendar else "")
    address = seo.absolute_url(request, path)
    webcal = "webcal://" + address.split("://", 1)[1]
    google = "https://calendar.google.com/calendar/render?" + urlencode({"cid": webcal})
    return {"address": address, "webcal": webcal, "google": google}


def _upcoming_event(event_id: int) -> dict:
    """An upcoming event and its calendar, or 404 (an event that is over,
    cancelled or no longer listed has no page)."""
    if not 1 <= event_id < 2**63:
        raise HTTPException(404)
    today = datetime.now(GUAM_TZ).date()
    since = datetime.combine(today, datetime.min.time(), tzinfo=GUAM_TZ)
    with session_scope() as session:
        item = queries.upcoming_event(session, event_id, since)
    if item is None:
        raise HTTPException(404)
    return item


def google_calendar_link(event) -> str:
    """A Google Calendar link that opens one event, filled in, ready to save."""
    start, end = ical.ical_times(event)
    url = safe_url(event.url)
    details = "\n\n".join(part for part in (event.description, url and f"Details: {url}") if part)
    params = {
        "action": "TEMPLATE",
        "text": event.title,
        "dates": f"{start}/{end or start}",
        "details": details,
        "location": event.location,
        "ctz": "Pacific/Guam",
    }
    query = urlencode({k: v for k, v in params.items() if v})
    return f"https://calendar.google.com/calendar/render?{query}"


def _host(request: Request) -> str:
    return urlsplit(seo.base_url(request)).hostname or "localhost"


@app.get("/events/{event_id:int}", response_class=HTMLResponse)
def event_page(request: Request, event_id: int):
    """One event's own page: what Google lists in its event results, and the
    links that add the event to a calendar app."""
    item = _upcoming_event(event_id)
    event = item["event"]
    colors = {c.slug: c.color for c in events_config_cache.get() if c.color}
    colors[FEATURED_CALENDAR] = event_calendar.FEATURED_COLOR
    when = event_when(event)
    summary = f"{when}, {event.location}" if event.location else when
    if event.description:
        summary += f". {event.description}"
    structured = seo.event_data(
        event,
        url=seo.absolute_url(request, f"/events/{event.id}"),
        calendar_name=item["calendar_name"],
        calendar_homepage=safe_url(item["calendar_homepage"]) or None,
    )
    return render(
        request,
        "event.html",
        item=item,
        color=colors.get(item["calendar_slug"]),
        google_link=google_calendar_link(event),
        # Many events share a name ("Public Hearing"); the day tells them apart.
        title=f"{event.title}, {_short_day(to_local(event.starts_at).date())}",
        description=truncate(summary, 160),
        active_nav="events",
        canonical={},
        breadcrumbs=[("Events", "/events"), (event.title, None)],
        structured=[structured] if structured else None,
        show_ad=True,
        show_travel=True,
    )


@app.get("/events/{event_id:int}.ics")
def event_file(request: Request, event_id: int):
    """One event as an iCal file, which calendar apps open to add the event."""
    item = _upcoming_event(event_id)
    return Response(
        ical.write_calendar([item], product=settings.site_name, host=_host(request)),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="event-{event_id}.ics"',
            "Cache-Control": "public, max-age=900",
            "X-Robots-Tag": "noindex",
        },
    )


# Events that ended this many days ago stay in the calendar feed, so that a
# calendar app that subscribed to it keeps the recent past.
FEED_DAYS_BEHIND = 30


@app.get("/events.ics")
def events_feed(request: Request, calendar: str = Query("", max_length=60)):
    """The events as a calendar that visitors subscribe to in their calendar
    app (Google Calendar, Apple Calendar, Outlook...), which checks it again by
    itself. With ?calendar=<slug>, every event of that calendar; without it,
    the main events of every calendar, as on the month calendar."""
    calendars_config = {c.slug: c for c in events_config_cache.get()}
    slug = calendar.strip()
    if slug and not (slug in calendars_config and calendars_config[slug].collected):
        raise HTTPException(404)
    today = datetime.now(GUAM_TZ).date()
    since = datetime.combine(
        today - timedelta(days=FEED_DAYS_BEHIND), datetime.min.time(), tzinfo=GUAM_TZ
    )
    until = datetime.combine(
        today + timedelta(days=EVENT_DAYS_AHEAD + 1), datetime.min.time(), tzinfo=GUAM_TZ
    )
    with session_scope() as session:
        items = queries.month_events(
            session, EventFilters(calendar=slug or None), since, until, since
        )
    if not slug:
        items = [
            item
            for item in items
            if item["calendar_slug"] not in calendars_config
            or calendars_config[item["calendar_slug"]].is_main(item["event"].title)
        ]
    name = calendars_config[slug].name if slug else "Guam events"
    return Response(
        ical.write_calendar(
            items,
            product=settings.site_name,
            host=_host(request),
            name=f"{name} · {settings.site_name}",
            refresh=timedelta(hours=settings.events_interval_hours),
        ),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="guam-events.ics"',
            "Cache-Control": "public, max-age=900",
            "X-Robots-Tag": "noindex",
        },
    )


@app.get("/bids", response_class=HTMLResponse)
def bids(
    request: Request,
    q: str = Query("", max_length=200),
    source: str = Query("", max_length=60),
    agency: str = Query("", max_length=200),
):
    sources_config = bids_config_cache.get()
    bid_filters = BidFilters(
        q=q.strip(), source=source.strip() or None, agency=agency.strip() or None
    )
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        listing = queries.list_bids(session, bid_filters, now)
        sources = queries.bid_sources(session, now)
    # Sources that can't be collected are not shown.
    collected = {s.slug for s in sources_config if s.collected}
    sources = [s for s in sources if s["source"].slug in collected]
    return render(
        request,
        "bids.html",
        bids=listing.items,
        agencies=listing.agencies,
        bid_filters=bid_filters,
        sources=sources,
        now=now,
        title="Bids and RFPs",
        description=_bids_description([s["source"].name for s in sources]),
        active_nav="bids",
        canonical={"source": bid_filters.source},
        # A word search or an agency is a search page; a source's own list
        # is worth finding.
        robots="noindex, follow" if bid_filters.q or bid_filters.agency else None,
        breadcrumbs=[("Bids and RFPs", None)],
        show_ad=True,
    )


def _bids_description(sources: list[str]) -> str:
    text = "Open Government of Guam bids and requests for proposals, with their due dates"
    if sources:
        text += f", from {_names(sources)}"
    return text + ". Every bid links to the agency or portal that published it."


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def _events_description(calendars: list[str]) -> str:
    text = "Upcoming events on Guam: sessions, hearings, public meetings and more"
    if calendars:
        text += f", from {_names(calendars)}"
    return text + ". Every event links to the calendar that published it."


def _jobs_description(boards: list[str]) -> str:
    text = "Job openings on Guam with pay, location and closing dates"
    if boards:
        text += f", from {_names(boards)}"
    return text + ". Every posting links to the board that published it."


@app.get("/go/sponsor")
def go_sponsor(request: Request):
    """Count a click on the sponsor spot, then send the reader on to the
    sponsor's website (SPONSOR_LINK)."""
    if not settings.sponsor_link:
        raise HTTPException(404)
    if settings.stats_password and stats.is_reader(request):
        stats.counts.sponsor_click()
    return RedirectResponse(
        settings.sponsor_link,
        status_code=302,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


@app.get("/sponsor.webp")
def sponsor_picture(v: str = Query("", max_length=20)):
    """The sponsor's picture: this week's booked ad (ads.py), else the small
    WebP copy of SPONSOR_IMAGE that web/sponsor.py makes."""
    booked = booked_site_ad()
    if booked is not None and booked.picture:
        version, data = f"b{booked.id}", booked.picture
    else:
        picture = sponsor.picture(settings)
        if picture is None:
            raise HTTPException(404)
        version, data = picture.version, picture.data
    # The page's address for it carries the version, so a new picture gets a new address.
    cache = "public, max-age=31536000, immutable" if v == version else "public, max-age=300"
    return Response(
        data,
        media_type="image/webp",
        headers={"Cache-Control": cache, "X-Robots-Tag": "noindex"},
    )


@app.get("/go/ad/{booking_id}")
def go_ad(request: Request, booking_id: int):
    """Count a click on a booked ad (the sponsor spot, or the sponsor line in
    the newsletter), then send the reader on to the business's website. Only
    an approved booking's own address is used."""
    if not 1 <= booking_id < 2**31:
        raise HTTPException(404)
    with session_scope() as session:
        booking = ads.approved(session, booking_id)
        link, kind = (safe_url(booking.link), booking.kind) if booking else ("", None)
    if not link:
        raise HTTPException(404)
    if settings.stats_password and stats.is_reader(request):
        stats.counts.ad_click(booking_id)
        if kind == ads.SITE:  # also in the stats page's "Sponsor spot" box
            stats.counts.sponsor_click()
    return RedirectResponse(
        link, status_code=302, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
    )


@app.get("/go/{article_id}")
def go(
    request: Request,
    article_id: int,
    from_: str = Query("", alias="from", max_length=20),
):
    """Count a click on a story's link, then send the reader on to the story.
    Only a stored story's own address is used, so nobody can make a link here
    that leads to another site. The links in the newsletter end in
    ?from=newsletter, so the clicks from the emails are counted too."""
    if not 1 <= article_id < 2**63:
        raise HTTPException(404)
    with session_scope() as session:
        link = safe_url(session.scalar(select(Article.url).where(Article.id == article_id)))
    if not link:
        raise HTTPException(404)
    if settings.stats_password and stats.is_reader(request):
        stats.counts.click(article_id)
        if from_ == "newsletter":
            stats.counts.newsletter_click()
    return RedirectResponse(
        link, status_code=302, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
    )


STATS_PERIODS = {1: "Today", 7: "7 days", 30: "30 days", 90: "90 days", 365: "1 year"}


class LoginLimit:
    """Keeps anyone from guessing STATS_PASSWORD, the password of the private
    pages: after `tries` wrong passwords from one IP address within `window`
    seconds, that address is refused for `block` seconds, even with the right
    password. Each web worker counts for itself. The addresses are kept in
    memory only, and only for as long as they count."""

    def __init__(self, tries: int = 10, window: float = 900, block: float = 3600) -> None:
        self.tries, self.window, self.block = tries, window, block
        self._lock = threading.Lock()
        self._wrong: dict[str, deque[float]] = {}
        self._blocked: dict[str, float] = {}  # address -> refused until (monotonic)

    def _forget_old(self, now: float) -> None:
        for address in [a for a, until in self._blocked.items() if until <= now]:
            del self._blocked[address]
        for address in list(self._wrong):
            times = self._wrong[address]
            while times and now - times[0] > self.window:
                times.popleft()
            if not times:
                del self._wrong[address]

    def blocked(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            until = self._blocked.get(address)
            return until is not None and until > now

    def wrong(self, address: str) -> bool:
        """Count a wrong password. True when it is the one that blocks the address."""
        now = time.monotonic()
        with self._lock:
            self._forget_old(now)
            if len(self._wrong) >= 10_000 and address not in self._wrong:
                # Very many addresses at once: forget the one that guessed
                # least recently, so the memory used stays small.
                oldest = min(self._wrong, key=lambda a: self._wrong[a][-1])
                del self._wrong[oldest]
            times = self._wrong.setdefault(address, deque())
            times.append(now)
            if len(times) < self.tries:
                return False
            del self._wrong[address]
            self._blocked[address] = now + self.block
            return True


login_limit = LoginLimit()
# When the last Discord alert about a blocked address went out (at most one
# an hour, in each web worker).
_blocked_alert = {"sent": float("-inf")}


def _given_password(request: Request) -> str | None:
    """The password typed into the browser's own login box (HTTP Basic: any
    user name, and the password), or None when the browser sent none."""
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        login = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except ValueError:  # not base64, or not UTF-8
        return ""
    return login.partition(":")[2]


def _password_prompt() -> PlainTextResponse:
    """The answer that makes the browser ask for STATS_PASSWORD. Every private
    page uses the same realm, so one login opens them all."""
    return PlainTextResponse(
        "This page needs a password.",
        status_code=401,
        headers={
            "WWW-Authenticate": 'Basic realm="Visitor stats", charset="UTF-8"',
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex",
        },
    )


def _too_many_tries() -> PlainTextResponse:
    minutes = round(login_limit.block / 60)
    return PlainTextResponse(
        f"Too many wrong passwords. Please try again in {minutes} minutes.",
        status_code=429,
        headers={
            "Retry-After": str(int(login_limit.block)),
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex",
        },
    )


def _alert_blocked(address: str) -> None:
    """Tell the owner (Discord) that an address was blocked for guessing."""
    now = time.monotonic()
    if not settings.discord_webhook_url or now - _blocked_alert["sent"] < 3600:
        return
    _blocked_alert["sent"] = now
    minutes = round(login_limit.block / 60)
    alert = alerts.Alert(
        title=f"{settings.site_name}: wrong passwords on the private pages",
        message=(
            f"The address {address} typed a wrong password {login_limit.tries} times on the"
            f" private pages (/stats, /admin), so it is blocked for {minutes} minutes. If it"
            " was you, wait and try again. If not, someone is guessing: make sure"
            " STATS_PASSWORD in .env is long and random (32 letters and digits, see"
            " .env.example). More alerts like this one come at most once an hour."
        ),
        emoji="🔐",
    )
    threading.Thread(target=alerts.send, args=(settings, alert), daemon=True).start()


def _owner_check(request: Request) -> Response | None:
    """None for the owner of the site (the right STATS_PASSWORD); else the
    answer to send: the browser's login box, or "too many wrong passwords"
    for an address that keeps guessing (LoginLimit). Without STATS_PASSWORD
    the private pages are not there at all (404)."""
    if not settings.stats_password:
        raise HTTPException(404)
    address = request.client.host if request.client else ""
    if login_limit.blocked(address):
        return _too_many_tries()
    password = _given_password(request)
    if password is None:  # the first visit: the browser asks for the password
        return _password_prompt()
    if secrets.compare_digest(password.encode(), settings.stats_password.encode()):
        return None
    if login_limit.wrong(address):
        log.warning("private pages: %s blocked after too many wrong passwords", address)
        _alert_blocked(address)
        return _too_many_tries()
    return _password_prompt()


@app.get("/stats", response_class=HTMLResponse)
def visitor_stats(request: Request, days: int = Query(7, ge=1, le=365)):
    if (answer := _owner_check(request)) is not None:
        return answer
    if days not in STATS_PERIODS:
        days = 7
    # This worker's newest counts. The other worker writes its own within 30 seconds.
    stats.counts.flush()
    config = config_cache.get()
    since = datetime.now(GUAM_TZ).date() - timedelta(days=days - 1)
    with session_scope() as session:
        data = queries.visitor_stats(session, since)
        to_review = session.scalar(select(func.count()).where(Listing.status == listings.REVIEW))
        news_health = health.news_health(
            session, [s.slug for s in config.sources if s.collected], datetime.now(timezone.utc)
        )
        feed_health = health.feed_health(
            session,
            [b.slug for b in jobs_config_cache.get() if b.collected],
            [c.slug for c in events_config_cache.get() if c.collected],
        )
    daily = [
        {
            "day": day,
            "views": data["views_by_day"].get(day, 0),
            "clicks": data["clicks_by_day"].get(day, 0),
        }
        for day in (since + timedelta(days=n) for n in range(days))
    ]
    page_views = dict(data["pages"])
    topics = [
        {
            "name": c.name,
            "views": page_views.get(f"/category/{c.slug}", 0),
            "clicks": data["topics"].get(c.slug, 0),
        }
        for c in config.categories
    ]
    page_names = {
        "/": "Home (Latest)",
        "/archive": "Archive",
        "/jobs": "Jobs",
        "/events": "Events",
        "/weather": "Weather",
        "/outages": "Outages",
        "/advertise": "Advertise",
        "/status": "Sources",
        "/search": "Search",
        "/classifieds": "Classifieds",
        "/directory": "Business directory",
        **{f"/category/{c.slug}": c.name for c in config.categories},
        **{f"/source/{s.slug}": s.name for s in config.sources},
    }
    response = render(
        request,
        "stats.html",
        title="Visitor stats",
        robots="noindex, nofollow",
        count_view=False,
        periods=STATS_PERIODS,
        days=days,
        daily=daily,
        views=sum(d["views"] for d in daily),
        clicks=sum(d["clicks"] for d in daily),
        pages=data["pages"][:20],
        page_names=page_names,
        stories=data["stories"],
        sources=data["sources"],
        referrers=data["referrers"][:20],
        visits=sum(row.visits for row in data["referrers"]),
        sponsor_clicks=data["sponsor_clicks"],
        topics=sorted(topics, key=lambda t: (-t["clicks"], -t["views"])),
        news_health=news_health,
        feed_health=feed_health,
        problems=[h for h in [*news_health, *feed_health] if h.status in ("failing", "quiet")],
        to_review=to_review,
        backups=backups.find(settings.backup_dir),
        backup_status=backups.read_status(settings.backup_dir),
        backup_problem=backups.download_problem(settings, request.url.scheme),
        weak_password=len(settings.stats_password) < backups.MIN_PASSWORD,
        backup_time_text=backups.clock_text(settings.backup_time),
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/stats/backups/{file_name}")
def backup_download(request: Request, file_name: str):
    """One nightly backup as a .tar file (STATS_PASSWORD, over https only)."""
    if (answer := _owner_check(request)) is not None:
        return answer
    problem = backups.download_problem(settings, request.url.scheme)
    if problem:
        return PlainTextResponse(problem, status_code=403, headers={"Cache-Control": "no-store"})
    found = backups.FILE_NAME.fullmatch(file_name)
    prepared = backups.prepare(settings.backup_dir, found[1]) if found else None
    if prepared is None:
        raise HTTPException(404)
    size, body = prepared
    who = request.client.host if request.client else "an unknown address"
    log.warning("backup %s downloaded from %s", found[1], who)
    # A backup holds every password of the site: say so in Discord each time,
    # so a download that wasn't you does not go unnoticed.
    alert = alerts.Alert(
        title=f"{settings.site_name}: a backup was downloaded",
        message=(
            f"The backup {file_name} was downloaded from the stats page, from the address {who}."
            " If that wasn't you, change STATS_PASSWORD in .env now, and the passwords and"
            " keys in .env (a backup holds them all)."
        ),
        emoji="📦",
    )
    threading.Thread(target=alerts.send, args=(settings, alert), daemon=True).start()
    return StreamingResponse(
        body,
        media_type="application/x-tar",
        headers={
            "Content-Length": str(size),
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Cache-Control": "private, no-store",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@app.get("/healthz")
def healthz():
    try:
        with session_scope() as session:
            ok = queries.db_ok(session)
            updated = queries.last_updated(session)
    except Exception as exc:
        return JSONResponse({"status": "error", "error": type(exc).__name__}, status_code=503)
    return {
        "status": "ok" if ok else "error",
        "last_scrape": updated.isoformat() if updated else None,
    }


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    # The AdSense crawler reads the pages that show ads, to choose ads that fit
    # them; the Sources page is one of them. It does not list pages in search.
    adsense = "User-agent: Mediapartners-Google\nDisallow:\n\n" if settings.adsense_client else ""
    return (
        f"{adsense}"
        "User-agent: *\n"
        "Disallow: /search\n"
        "Disallow: /healthz\n"
        # The counting stop in front of each story link (/go/<id>).
        "Disallow: /go/\n"
        # Each poster's own page of a paid listing, and the review page.
        "Disallow: /listing/\n"
        "Disallow: /admin/\n"
        # A booking's own page and the owner's review page (the Advertise page
        # itself, /advertise, is for everyone).
        "Disallow: /advertise/\n"
        # The PayPal link of a guide pack, and each buyer's private page.
        "Disallow: /guides/buy/\n"
        "Disallow: /guides/pass/\n"
        "\n"
        f"Sitemap: {seo.absolute_url(request, '/sitemap.xml')}\n"
    )


@app.get("/ads.txt", response_class=PlainTextResponse)
def ads_txt():
    # Tells ad buyers that Google may sell this site's ad space; AdSense warns
    # of lost earnings without it. f08c47fec0942fa0 is Google's ID, the same
    # for every publisher.
    if not settings.adsense_client:
        raise HTTPException(404)
    publisher = settings.adsense_client.removeprefix("ca-")
    return f"google.com, {publisher}, DIRECT, f08c47fec0942fa0\n"


# The sitemap's queries group every story; crawlers fetch it now and then, so
# their results are kept for ten minutes.
sitemap_cache = _TTLCache(ttl=600)


@app.get("/sitemap.xml")
def sitemap(request: Request):
    config = config_cache.get()
    hidden = list(_hidden())
    separate = list(_separate())

    def data() -> dict:
        today = datetime.now(GUAM_TZ).date()
        since = datetime.combine(today, datetime.min.time(), tzinfo=GUAM_TZ)
        with session_scope() as session:
            found = queries.sitemap_data(session, hidden, separate, config.muted)
            found["events"] = queries.upcoming_event_ids(session, since)
            found["press"] = [
                listing.id
                for listing in queries.listings(
                    session, "press", datetime.now(timezone.utc), per_page=1000
                ).items
            ]
            return found

    news_sources = [s.slug for s in config.sources if not s.cannot_scrape and s.slug not in hidden]
    found = sitemap_cache.get("sitemap", data)
    entries = seo.sitemap_entries(
        request,
        found,
        config.categories,
        news_sources,
        weather=bool(settings.weather_interval_minutes),
        outages=bool(settings.outages_interval_minutes),
        events=found["events"],
        bid_alerts=bid_alerts.is_on(settings),
        listings=[
            listings.KINDS[kind].page
            for kind in ("classified", "business", "press", "deal")
            if listings.is_on(settings, kind)
        ]
        + [f"/press/{listing_id}" for listing_id in found["press"]],
        advertise=bool(advertising()),
        guides=[(g.slug, g.updated_at) for g in published_guides()]
        + [(f"packs/{p['pack'].slug}", p["pack"].updated_at) for p in packs_on_sale()],
    )
    return Response(
        seo.sitemap_xml(entries),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=600"},
    )


# Characters that XML 1.0 does not allow. One of them in a scraped headline
# would make the whole feed unreadable.
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _xml_text(text: str | None) -> str:
    return _NOT_XML.sub("", text or "")


# Where paid press releases go in the newsletter: after this many stories.
PRESS_AFTER_STORIES = 3


@app.get("/feed.xml")
def newsletter_feed(request: Request):
    """The newsletter feed that an email service such as SendFox reads (see
    newsletter.py): the stories of the newest edition. No page links to it,
    and search engines are asked to leave it out."""
    if not settings.newsletter_days:
        raise HTTPException(404)
    with session_scope() as session:
        formed_at, rows = queries.newsletter_edition(session, _muted()) or (None, [])
        # The sponsor line that a business booked for this edition (ads.py).
        booked = None
        press = []
        if formed_at:
            booked = ads.newsletter_ad(session, formed_at.astimezone(GUAM_TZ).date())
            # Paid press releases approved since the edition before (listings.py).
            press = queries.press_for_edition(session, formed_at)
    base = seo.base_url(request)
    items = []
    for row in rows:
        a = row["article"]
        link = story_link(a)  # /go/<id> while visitor stats are on
        if not link:
            continue
        if link.startswith("/go/"):
            link += "?from=newsletter"  # counted as a click from the newsletter
        photo = f"{base}/media/{a.image_path}" if a.image_path else safe_url(a.image_url)
        items.append(
            {
                "title": _xml_text(a.title),
                "link": base + link if link.startswith("/") else link,
                "guid": safe_url(a.url),
                "summary": _xml_text(truncate(a.intro or "", 280)),
                "source": _xml_text(row["source_name"]),
                "source_homepage": safe_url(row["source_homepage"]),
                "domain": domain(a.url),
                "photo": photo,
                # Stored copies are WebP (see scraper/images.py).
                "photo_type": "image/webp" if a.image_path else None,
                "width": a.image_width if a.image_path else None,
                "height": a.image_height if a.image_path else None,
            }
        )
    # The press releases go after the first few stories, marked as paid.
    at = min(PRESS_AFTER_STORIES, len(items))
    items[at:at] = [
        {
            "title": _xml_text(f"Press release: {release.title}"),
            "link": f"{base}/press/{release.id}",
            "guid": f"{base}/press/{release.id}",
            "summary": _xml_text(truncate(release.description or "", 280)),
            "source": _xml_text(release.name),
            "sponsored": True,
            "photo": f"{base}/media/{release.photo}" if release.photo else None,
            "photo_type": "image/webp" if release.photo else None,
            "width": release.photo_width,
            "height": release.photo_height,
        }
        for release in press
    ]
    xml = templates.get_template("feed.xml").render(
        settings=settings,
        home=base + "/",
        self_url=base + "/feed.xml",
        schedule=schedule_text(settings.newsletter_days, settings.newsletter_time),
        formed=format_datetime(formed_at.astimezone(GUAM_TZ)) if formed_at else None,
        items=items,
        sponsor=(
            {
                "business": _xml_text(booked.business),
                "words": _xml_text(booked.words),
                "link": f"{base}/go/ad/{booked.id}",
                "domain": domain(booked.link),
                # One for each edition, so the email service sees it as new each time.
                "guid": f"{base}/advertise#ad-{booked.id}-{formed_at.astimezone(GUAM_TZ):%Y-%m-%d}",
            }
            if booked
            else None
        ),
    )
    return Response(
        xml,
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=600", "X-Robots-Tag": "noindex"},
    )


# ------------------------------------------------------ newsletter sign-up


class SignupLimit:
    """Keeps robots from adding many addresses: at most `per_address` sign-ups
    an hour from one IP address, and `total` an hour in all, in each web
    worker. The IP addresses are kept in memory for an hour at most."""

    def __init__(self, per_address: int = 5, total: int = 100, window: float = 3600) -> None:
        self.per_address, self.total, self.window = per_address, total, window
        self._lock = threading.Lock()
        self._all: deque[float] = deque()
        self._by_address: dict[str, deque[float]] = {}

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._all and now - self._all[0] > self.window:
                self._all.popleft()
            for key in list(self._by_address):
                times = self._by_address[key]
                while times and now - times[0] > self.window:
                    times.popleft()
                if not times:
                    del self._by_address[key]
            mine = self._by_address.setdefault(address, deque())
            if len(self._all) >= self.total or len(mine) >= self.per_address:
                return False
            self._all.append(now)
            mine.append(now)
            return True


signup_limit = SignupLimit()

# An address as people type it: something@domain.tld, without spaces or the
# characters that only odd quoted addresses use. The browser checks it too.
_EMAIL = re.compile(r"[^\s@<>()\[\],;:\\\"]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_MAX_SIGNUP_BODY = 4096


async def _small_body(request: Request, limit: int = _MAX_SIGNUP_BODY) -> bytes | None:
    """The body of a small form, read in pieces; None when it is larger than
    `limit`, so that a huge request never fills the server's memory."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return body


def _from_this_site(request: Request) -> bool:
    """True when the form was sent from a page of this site. Browsers say
    which site a form came from (the Origin header, else the Referer)."""
    sent_from = request.headers.get("origin") or request.headers.get("referer") or ""
    try:
        host = (urlsplit(sent_from).netloc or "").lower()
    except ValueError:
        return False
    return bool(host) and host == request.headers.get("host", "").lower()


def _signup_results() -> dict[str, tuple[bool, str]]:
    days = settings.newsletter_days
    when = f" on {_days_text(days, 'or')}" if 0 < len(days) < 7 else ""
    return {
        "ok": (
            True,
            "Almost done: check your inbox. We've sent you an email to confirm your address."
            f" Open it and confirm, and the newsletter starts with the next email{when}."
            " If it isn't there in a few minutes, look in your spam or promotions folder.",
        ),
        "invalid": (
            False,
            "That doesn't look like an email address. Please check it and try again.",
        ),
        "busy": (False, "Too many sign-ups in a short time. Please try again in an hour."),
        "error": (False, "Sorry, the sign-up didn't work this time. Please try again later."),
    }


@app.get("/newsletter", response_class=HTMLResponse)
def newsletter_page(request: Request, signup: str = Query("", max_length=10)):
    """What the newsletter is, with the sign-up form, and the result of a sign-up."""
    if not settings.sendfox_api_token:
        raise HTTPException(404)
    found = _signup_results().get(signup)
    return render(
        request,
        "newsletter.html",
        title="Newsletter",
        description=signup_info()["pitch"]
        + " Headlines, photos and the first lines of each story, with links to the news sites.",
        canonical={},
        # The page after a sign-up is not one to find in a search.
        robots="noindex, follow" if signup else None,
        breadcrumbs=[("Newsletter", None)],
        show_signup=False,
        result={"ok": found[0], "text": found[1]} if found else None,
    )


@app.post("/newsletter")
async def newsletter_signup(request: Request):
    """The sign-up form (_signup.html): add the address to the SendFox list
    (sendfox.py), then show the result on the newsletter page."""
    if not settings.sendfox_api_token:
        raise HTTPException(404)

    def result(name: str) -> RedirectResponse:
        return RedirectResponse(
            f"/newsletter?signup={name}", status_code=303, headers={"Cache-Control": "no-store"}
        )

    if not _from_this_site(request):
        return PlainTextResponse("Please use the sign-up form on the site.", status_code=403)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_SIGNUP_BODY:
            return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
    email = (form.get("email") or [""])[0].strip()
    if (form.get("website") or [""])[0]:
        # The field that people don't see: a robot filled it in. It is told
        # that all went well, so it does not try again.
        return result("ok")
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        return result("invalid")
    if not signup_limit.allow(request.client.host if request.client else ""):
        log.warning("newsletter sign-up refused: too many sign-ups in the last hour")
        return result("busy")
    try:
        await run_in_threadpool(sendfox.add_contact, settings, email)
    except sendfox.InvalidEmail:
        return result("invalid")
    except sendfox.SignupError as exc:
        log.warning("newsletter sign-up failed: %s", exc)
        return result("error")
    log.info("newsletter sign-up added to SendFox")
    stats.counts.signup()
    return result("ok")


# ------------------------------------------------------------ paid listings


listing_limit = SignupLimit(per_address=10, total=60)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _alert_review(request: Request, listing) -> None:
    """Tell the owner of the site (Discord) that a listing waits for review."""
    if settings.discord_webhook_url:
        kind = listings.KINDS[listing.kind].name
        alerts.send(
            settings,
            alerts.Alert(
                title=f"{kind} to review",
                message=f"{listing.title}\n{seo.absolute_url(request, '/admin/listings')}",
                emoji="🧾",
            ),
        )


def _listing_page(
    request: Request,
    kind: str,
    template: str,
    title: str,
    description: str,
    q: str,
    category: str,
    categories: tuple[str, ...],
    page: int,
):
    """The Classifieds, Directory, Press releases or Deals page."""
    if category and category not in categories:
        category = ""
    with session_scope() as session:
        result = queries.listings(
            session,
            kind,
            datetime.now(timezone.utc),
            q=q.strip(),
            category=category or None,
            page=page,
            per_page=settings.page_size,
        )
    # Listings that were paid for stay on the site until they end, also when
    # posting is off.
    if not listings.is_on(settings, kind) and not result.total and not (q or category):
        raise HTTPException(404)
    if page > 1 and not result.items:
        raise HTTPException(404)
    return render(
        request,
        template,
        result=result,
        q=q.strip(),
        category=category,
        choices=categories,
        now=datetime.now(timezone.utc),
        title=title,
        description=description,
        active_nav="listings",  # not a topic slug: "business" is one
        canonical={"category": category or None, "page": page},
        robots="noindex, follow" if q else None,
        breadcrumbs=[(title, None)],
        show_ad=True,
    )


@app.get("/classifieds", response_class=HTMLResponse)
def classifieds(
    request: Request,
    q: str = Query("", max_length=200),
    category: str = Query("", max_length=60),
    page: int = Query(1, ge=1, le=1000),
):
    return _listing_page(
        request,
        "classified",
        "classifieds.html",
        "Classifieds",
        "Guam classified ads: things for sale, cars and trucks, housing, services and wanted"
        f" ads from people on the island, on {settings.site_name}.",
        q,
        category,
        listings.CLASSIFIED_SECTIONS,
        page,
    )


@app.get("/press", response_class=HTMLResponse)
def press_releases(
    request: Request,
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1, le=1000),
):
    return _listing_page(
        request,
        "press",
        "press.html",
        "Press releases",
        "News and announcements from Guam businesses and groups, in their own words:"
        f" sponsored press releases on {settings.site_name}.",
        q,
        "",
        (),
        page,
    )


@app.get("/press/{listing_id:int}", response_class=HTMLResponse)
def press_release(request: Request, listing_id: int):
    """A press release's own page, with its whole text."""
    with session_scope() as session:
        release = session.get(Listing, listing_id) if 1 <= listing_id < 2**63 else None
    if release is None or release.kind != "press" or release.status != listings.LIVE:
        raise HTTPException(404)
    return render(
        request,
        "press_release.html",
        release=release,
        title=release.title,
        description=truncate(f"Press release from {release.name}: {release.description}", 160),
        active_nav="listings",
        canonical={},
        breadcrumbs=[("Press releases", "/press"), (release.title, None)],
        show_ad=True,
    )


@app.get("/deals", response_class=HTMLResponse)
def deals(
    request: Request,
    q: str = Query("", max_length=200),
    category: str = Query("", max_length=60),
    page: int = Query(1, ge=1, le=1000),
):
    return _listing_page(
        request,
        "deal",
        "deals.html",
        "Deals and offers",
        "Coupons, specials and offers from Guam businesses: restaurants, shops, services"
        f" and more, on {settings.site_name}.",
        q,
        category,
        listings.BUSINESS_CATEGORIES,
        page,
    )


@app.get("/directory", response_class=HTMLResponse)
def directory(
    request: Request,
    q: str = Query("", max_length=200),
    category: str = Query("", max_length=60),
    page: int = Query(1, ge=1, le=1000),
):
    return _listing_page(
        request,
        "business",
        "directory.html",
        "Business directory",
        "Guam businesses by category and village: restaurants, shops, services and more,"
        f" with their phone numbers and websites, on {settings.site_name}.",
        q,
        category,
        listings.BUSINESS_CATEGORIES,
        page,
    )


def _post_form(request: Request, kind: str, values: dict, errors: dict, status_code: int = 200):
    info = listings.KINDS[kind]
    title = {
        "press": "Post a press release",
        "deal": "Post a deal",
    }.get(kind, f"Post a {info.name.lower()}")
    return render(
        request,
        "listing_form.html",
        status_code=status_code,
        kind=info,
        values=values,
        errors=errors,
        today=datetime.now(GUAM_TZ).date(),
        last_day=datetime.now(GUAM_TZ).date() + timedelta(days=listings.EVENT_DAYS_AHEAD),
        deal_max_day=datetime.now(GUAM_TZ).date() + timedelta(weeks=listings.MAX_DEAL_WEEKS),
        max_deal_weeks=listings.MAX_DEAL_WEEKS,
        max_press_text=listings.MAX_PRESS_TEXT,
        sections=listings.CLASSIFIED_SECTIONS,
        business_categories=listings.BUSINESS_CATEGORIES,
        title=title,
        robots="noindex, follow",
        canonical={},
        breadcrumbs=[(title, None)],
        show_signup=False,
    )


@app.get("/post/{kind}", response_class=HTMLResponse)
def post_form(request: Request, kind: str):
    """The form for a new listing of this kind."""
    if not listings.is_on(settings, kind):
        raise HTTPException(404)
    return _post_form(request, kind, {"plan": "featured"}, {})


@app.post("/post/{kind}")
async def post_listing(request: Request, kind: str):
    """Save a new listing from its form, then open the poster's own page for
    it, which has the PayPal link."""
    if not listings.is_on(settings, kind):
        raise HTTPException(404)
    if not _from_this_site(request):
        return PlainTextResponse("Please use the form on the site.", status_code=403)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > listings.MAX_BODY:
            return PlainTextResponse("Too large: the photo can be at most 6 MB.", status_code=413)
    try:
        fields, photo = listings.parse_form(body, request.headers.get("content-type", ""))
    except listings.FormError:
        return PlainTextResponse("Please use the form on the site.", status_code=400)
    if fields.get("website"):
        # The field that people don't see: a robot filled it in.
        return RedirectResponse(f"/post/{kind}", status_code=303)
    values, errors = listings.check(
        kind, fields, datetime.now(GUAM_TZ).date(), config_cache.get().muted
    )
    if errors:
        return _post_form(request, kind, fields, errors, status_code=400)
    if not listing_limit.allow(request.client.host if request.client else ""):
        log.warning("listing refused: too many new listings in the last hour")
        errors = {"form": "Too many listings in a short time. Please try again in an hour."}
        return _post_form(request, kind, fields, errors, status_code=429)

    def save():
        stored = None
        if photo and listings.KINDS[kind].photo:
            stored = listings.save_photo(settings.media_dir, photo, settings.image_max_width)
        with session_scope() as session:
            listings.delete_stale(session, datetime.now(timezone.utc), settings.media_dir)
            listing = listings.create(session, settings, kind, values, stored)
        if listing.status == listings.REVIEW:  # free: nothing to pay
            _alert_review(request, listing)
        return listing

    try:
        listing = await run_in_threadpool(save)
    except ImageRejected as exc:
        log.info("listing photo refused: %s", exc)
        errors = {
            "photo": "The photo could not be used. Please choose a JPG, PNG or WebP photo of at"
            " least 200 by 120 pixels, and at most 6 MB."
        }
        return _post_form(request, kind, fields, errors, status_code=400)
    return RedirectResponse(
        f"/listing/{listing.id}/{listing.key}",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


def _own_listing(listing_id: int, key: str):
    with session_scope() as session:
        listing = listings.find(session, listing_id, key)
    if listing is None:
        raise HTTPException(404)
    return listing


PAYMENT_RESULTS = {
    "ok": (
        True,
        "Thank you, your payment went through. Your listing now waits for a quick"
        " review, and then shows on the site.",
    ),
    "pending": (
        True,
        "PayPal is still checking your payment. Your listing goes to review as soon"
        " as PayPal is done; this page shows it then.",
    ),
    "unfinished": (False, "The payment was not finished. Nothing was charged. You can pay below."),
    "declined": (
        False,
        "PayPal could not take the payment. Please try again, with another card or account.",
    ),
    "error": (
        False,
        "PayPal did not answer this time. Nothing was charged. Please try again in a few minutes.",
    ),
    "mismatch": (
        False,
        "Something did not match in this payment, so it was not used. Please try"
        " again; if you were charged, the payment will be given back.",
    ),
}


RENEWAL_RESULTS = {
    "ok": (True, "Thank you, your payment went through. See the new end date below."),
    "unfinished": (
        False,
        "The payment was not finished. Nothing was charged. You can renew below.",
    ),
    "declined": PAYMENT_RESULTS["declined"],
    "error": PAYMENT_RESULTS["error"],
    "mismatch": PAYMENT_RESULTS["mismatch"],
    "gone": (
        False,
        "This listing is no longer on the site, so it was not renewed. PayPal gives your"
        " payment back.",
    ),
}


@app.get("/listing/{listing_id}/{key}", response_class=HTMLResponse)
def listing_page(
    request: Request,
    listing_id: int,
    key: str,
    paid: str = Query("", max_length=12),
    renewed: str = Query("", max_length=12),
):
    """The poster's own page for a listing: how it looks, what it costs, the
    PayPal link, where it is (waiting for review, live, ...), and the link to
    renew it."""
    listing = _own_listing(listing_id, key)
    found = PAYMENT_RESULTS.get(paid) or RENEWAL_RESULTS.get(renewed)
    renewal = None
    if listings.renewable(settings, listing):
        renewal = {
            "cents": listings.price_cents(settings, listing.kind, featured=True),
            "days": listings.days(settings, listing.kind),
            "reminder": renewals.is_on(settings) and listing.email,
        }
    response = render(
        request,
        "listing.html",
        listing=listing,
        kind=listings.KINDS[listing.kind],
        result={"ok": found[0], "text": found[1]} if found else None,
        renewal=renewal,
        address=seo.absolute_url(request, f"/listing/{listing.id}/{listing.key}"),
        now=datetime.now(timezone.utc),
        title=f"Your {listings.KINDS[listing.kind].name.lower()}",
        robots="noindex, nofollow",
        count_view=False,
        show_signup=False,
    )
    return _no_store(response)


@app.get("/listing/{listing_id}/{key}/pay")
def listing_pay(request: Request, listing_id: int, key: str):
    """Make a PayPal order for the listing, and send the poster to PayPal to
    pay it. (A link, not a form: see paypal.py.)"""
    listing = _own_listing(listing_id, key)
    page = f"/listing/{listing.id}/{listing.key}"
    if listing.status != listings.UNPAID or not paypal.enabled(settings):
        return RedirectResponse(page, status_code=303, headers={"Cache-Control": "no-store"})
    try:
        order = paypal.create_order(
            settings,
            amount_cents=listing.amount_cents,
            description=f"{listings.KINDS[listing.kind].name}: {listing.title}",
            custom_id=f"listing-{listing.id}",
            return_url=seo.absolute_url(request, f"{page}/paid"),
            cancel_url=seo.absolute_url(request, page),
        )
    except paypal.PayPalError as exc:
        log.warning("PayPal order for listing %s failed: %s", listing.id, exc)
        return RedirectResponse(f"{page}?paid=error", status_code=303)
    with session_scope() as session:
        listings.set_order(session, listing.id, order.id)
    return RedirectResponse(
        order.approve_url, status_code=303, headers={"Cache-Control": "no-store"}
    )


def _settle(request: Request, listing, order_id: str) -> str:
    """Take the money of a PayPal order for the listing (see paypal.py), and
    send the listing to review. Returns a key of PAYMENT_RESULTS."""
    try:
        capture = paypal.capture_order(settings, order_id, custom_id=f"listing-{listing.id}")
    except paypal.PayPalError as exc:
        if exc.issue == paypal.WRONG_ORDER:
            log.error("PayPal payment not used for listing %s: %s", listing.id, exc)
            return "mismatch"
        log.warning("PayPal capture for listing %s failed: %s", listing.id, exc)
        return "declined" if exc.issue == "INSTRUMENT_DECLINED" else "error"
    if capture.status == "PENDING":
        with session_scope() as session:
            listings.set_order(session, listing.id, order_id)
        return "pending"
    if not capture.paid:
        return "unfinished"
    try:
        with session_scope() as session:
            recorded = listings.mark_paid(
                session, listing.id, order_id, capture, datetime.now(timezone.utc)
            )
    except listings.PaymentMismatch as exc:
        log.error(
            "PayPal payment %s not used for listing %s: %s", capture.capture_id, listing.id, exc
        )
        return "mismatch"
    if recorded:
        log.info("listing %s paid (PayPal capture %s)", listing.id, capture.capture_id)
        _alert_review(request, listing)
    return "ok"


@app.get("/listing/{listing_id}/{key}/paid")
def listing_paid(
    request: Request, listing_id: int, key: str, token: str = Query("", max_length=64)
):
    """Where PayPal sends the poster back after approving the payment, with
    ?token=<the order's ID>."""
    listing = _own_listing(listing_id, key)
    page = f"/listing/{listing.id}/{listing.key}"
    if listing.status != listings.UNPAID:
        return RedirectResponse(page, status_code=303, headers={"Cache-Control": "no-store"})
    if not re.fullmatch(r"[A-Za-z0-9-]{5,64}", token):
        return RedirectResponse(f"{page}?paid=unfinished", status_code=303)
    result = _settle(request, listing, token)
    return RedirectResponse(
        f"{page}?paid={result}", status_code=303, headers={"Cache-Control": "no-store"}
    )


@app.get("/listing/{listing_id}/{key}/renew")
def listing_renew(request: Request, listing_id: int, key: str):
    """Make a PayPal order that keeps a live listing on the site for more
    days, and send the poster to PayPal to pay it (see renewals.py)."""
    listing = _own_listing(listing_id, key)
    page = f"/listing/{listing.id}/{listing.key}"
    if not listings.renewable(settings, listing):
        return RedirectResponse(page, status_code=303, headers={"Cache-Control": "no-store"})
    amount = listings.price_cents(settings, listing.kind, featured=True)
    length = listings.days(settings, listing.kind)
    try:
        order = paypal.create_order(
            settings,
            amount_cents=amount,
            description=f"{listings.KINDS[listing.kind].name}, {length} days more: {listing.title}",
            custom_id=f"listing-{listing.id}-renewal",
            return_url=seo.absolute_url(request, f"{page}/renewed"),
            cancel_url=seo.absolute_url(request, page),
        )
    except paypal.PayPalError as exc:
        log.warning("PayPal order to renew listing %s failed: %s", listing.id, exc)
        return RedirectResponse(f"{page}?renewed=error", status_code=303)
    with session_scope() as session:
        listings.start_renewal(session, listing.id, order.id, amount, length)
    return RedirectResponse(
        order.approve_url, status_code=303, headers={"Cache-Control": "no-store"}
    )


@app.get("/listing/{listing_id}/{key}/renewed")
def listing_renewed(
    request: Request, listing_id: int, key: str, token: str = Query("", max_length=64)
):
    """Where PayPal sends the poster back after approving a renewal, with
    ?token=<the order's ID>."""
    listing = _own_listing(listing_id, key)
    page = f"/listing/{listing.id}/{listing.key}"
    if not re.fullmatch(r"[A-Za-z0-9-]{5,64}", token):
        return RedirectResponse(f"{page}?renewed=unfinished", status_code=303)
    result = renewals.settle(settings, listing.id, token)
    return RedirectResponse(
        f"{page}?renewed={result}", status_code=303, headers={"Cache-Control": "no-store"}
    )


REVIEW_RESULTS = {
    "approved": "Approved: the listing is on the site.",
    "rejected": "Rejected. Its payment, if any, was given back.",
    "removed": "Taken down: the listing is off the site. No money was given back.",
    "paid": "PayPal says it is paid: the listing now waits for review below.",
    "unpaid": "PayPal says it is not paid (yet).",
    "refund-failed": "PayPal could not give the money back, so the listing was not rejected."
    " See the web log for PayPal's reason, or refund it on paypal.com.",
    "gone": "That listing was not in that state any more. Nothing changed.",
}


@app.get("/admin/listings", response_class=HTMLResponse)
def review_listings(request: Request, done: str = Query("", max_length=20)):
    """The owner's page for the paid listings: approve or reject the new
    ones, take down live ones."""
    if (answer := _owner_check(request)) is not None:
        return answer
    with session_scope() as session:
        data = queries.listings_for_review(session, datetime.now(timezone.utc))
    response = render(
        request,
        "admin_listings.html",
        title="Paid listings",
        robots="noindex, nofollow",
        count_view=False,
        show_signup=False,
        message=REVIEW_RESULTS.get(done),
        now=datetime.now(timezone.utc),
        **data,
    )
    return _no_store(response)


def _review(request: Request, listing_id: int, action: str, note: str) -> str:
    """Do one action of the review page. Returns a key of REVIEW_RESULTS."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        listing = session.get(Listing, listing_id) if 1 <= listing_id < 2**63 else None
    if listing is None:
        return "gone"
    if action == "approve":
        with session_scope() as session:
            done = listings.approve(session, settings, listing_id, now)
        featured_cache._data.clear()
        deals_cache._data.clear()
        return "approved" if done else "gone"
    if action == "reject":
        if listing.status != listings.REVIEW:
            return "gone"
        refund = bool(listing.capture_id and not listing.refunded_at)
        if refund:
            try:
                paypal.refund_capture(settings, listing.capture_id)
            except paypal.PayPalError as exc:
                log.warning("refund of listing %s failed: %s", listing_id, exc)
                return "refund-failed"
        with session_scope() as session:
            done = listings.reject(session, listing_id, now, note, refunded=refund)
        if done:
            listings.delete_photo(settings.media_dir, listing.photo)
        return "rejected" if done else "gone"
    if action == "remove":
        with session_scope() as session:
            done = listings.take_down(session, listing_id, now)
        if done:
            listings.delete_photo(settings.media_dir, listing.photo)
            featured_cache._data.clear()
            deals_cache._data.clear()
        return "removed" if done else "gone"
    if action == "check" and listing.status == listings.UNPAID and listing.order_id:
        return "paid" if _settle(request, listing, listing.order_id) == "ok" else "unpaid"
    return "gone"


@app.post("/admin/listings/{listing_id}")
async def review_listing(request: Request, listing_id: int):
    """A button of the review page: approve, reject, remove or check."""
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request):
        return PlainTextResponse("Please use the buttons on the review page.", status_code=403)
    body = await _small_body(request)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
    action = (form.get("action") or [""])[0]
    note = (form.get("note") or [""])[0]
    result = await run_in_threadpool(_review, request, listing_id, action, note)
    return RedirectResponse(
        f"/admin/listings?done={result}", status_code=303, headers={"Cache-Control": "no-store"}
    )


# ------------------------------------------------------------ visitor guides
# Guides about visiting Guam that the owner writes on /admin/guides, behind
# STATS_PASSWORD (guides.py). Published guides are on /guides.

# The published guides are looked for on every page (the Guides link in the
# menu), so they are kept for a minute. Saving a guide empties this worker's
# copy; the other worker's copy is at most a minute old.
guide_cache = _TTLCache(ttl=60)


def published_guides() -> list[Guide]:
    def find() -> list[Guide]:
        with session_scope() as session:
            return guides.published(session)

    return guide_cache.get("published", find)


def packs_on_sale() -> list[dict]:
    """The guide packs for sale that hold a published guide, each with its
    number of guides ({"pack", "count"}), kept with the guides for a minute."""

    def find() -> list[dict]:
        if not guide_packs.on(settings):
            return []
        with session_scope() as session:
            found = [
                (p, len(guide_packs.contents(session, p))) for p in guide_packs.on_sale(session)
            ]
        return [{"pack": p, "count": n} for p, n in found if n]

    return guide_cache.get("packs", find)


def _reader_tokens(request: Request) -> list[str]:
    """The tokens of the guide packs bought in this browser (guide_packs.COOKIE)."""
    return guide_packs.cookie_tokens(request.cookies.get(guide_packs.COOKIE))


def _varies(response: Response) -> Response:
    """For a page that shows more to a reader with a guide pack: never kept
    for another reader, nor shown from the browser's copy after a purchase."""
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["Vary"] = "Cookie"
    return response


@app.get("/guides", response_class=HTMLResponse)
def guides_index(request: Request):
    found = published_guides()
    if not found:
        raise HTTPException(404)
    # Under their section's heading, the sections in the order of their first guide.
    sections: dict[str, list[Guide]] = {}
    for guide in found:
        sections.setdefault(guide.section, []).append(guide)
    return render(
        request,
        "guides.html",
        sections=list(sections.items()),
        packs=packs_on_sale(),
        title="Visitor guides",
        description=(
            f"Guides for visiting Guam from {settings.site_name}: getting around, the weather,"
            " beaches, food and what to see, kept up to date."
        ),
        active_nav="guides",
        canonical={},
        breadcrumbs=[("Visitor guides", None)],
        show_ad=True,
        show_travel=True,
    )


def _guide_page(request: Request, guide: Guide, preview: bool = False) -> HTMLResponse:
    """A guide's page; `preview` is the owner's look at it from the editor,
    also before it is published: not counted, no ads, not for search engines."""
    sponsor_box = None
    if guides.sponsor_showing(guide, datetime.now(GUAM_TZ).date()):
        link = safe_url(guide.sponsor_link)
        counted = bool(link and settings.stats_password and not preview)
        sponsor_box = {
            "name": guide.sponsor_name,
            "text": guide.sponsor_text,
            # While the visitor stats are on, /go/guide-sponsor/<id> counts the click.
            "href": f"/go/guide-sponsor/{guide.id}" if counted else link,
            "rel": "nofollow sponsored noopener" if counted else "sponsored noopener",
        }
    listed = published_guides()
    others = [g for g in listed if g.id != guide.id]
    others.sort(key=lambda g: g.section != guide.section)  # the same section first
    text = guides.unlink_unpublished(guides.render(guide.body), {g.slug for g in listed})
    paid = guide.access == guides.PAID
    # A paid guide: in full for a reader whose guide pack holds it (and in
    # the owner's preview); else its opening, and the packs that hold it.
    reading_with, offers = None, []
    if paid and not preview:
        with session_scope() as session:
            reading_with = guide_packs.unlocking(session, _reader_tokens(request), guide)
            if reading_with is None and guide_packs.on(settings):
                offers = guide_packs.packs_for(session, guide)
    locked = paid and not preview and reading_with is None
    response = render(
        request,
        "guide.html",
        guide=guide,
        text=guides.opening(text) if locked else text,
        # Where readers without a pack stop, for the owner's preview.
        free_parts=len(guides.opening(text).parts) if paid else None,
        locked=locked,
        offers=offers,
        reading_with=reading_with,
        others=others[:6],
        guide_sponsor=sponsor_box,
        preview=preview,
        title=guide.title,
        description=truncate(guide.summary, 160),
        active_nav="guides",
        canonical=None if preview else {},
        robots="noindex, nofollow" if preview else None,
        breadcrumbs=[("Visitor guides", "/guides"), (guide.title, None)],
        structured=None if preview else [seo.guide_data(request, guide)],
        # No ads for readers who paid for the guide.
        show_ad=not preview and reading_with is None,
        show_travel=True,
        show_signup=not preview,
        count_view=not preview,
    )
    return _varies(response) if paid else response


@app.get("/guides/{slug}", response_class=HTMLResponse)
def guide_page(request: Request, slug: str):
    guide = next((g for g in published_guides() if g.slug == slug), None)
    if guide is None:
        raise HTTPException(404)
    return _guide_page(request, guide)


@app.get("/go/guide-sponsor/{guide_id}")
def go_guide_sponsor(request: Request, guide_id: int):
    """Count a click on a guide's sponsor, then send the reader on to the
    sponsor's website."""
    guide = next((g for g in published_guides() if g.id == guide_id), None)
    today = datetime.now(GUAM_TZ).date()
    link = safe_url(guide.sponsor_link) if guide and guides.sponsor_showing(guide, today) else ""
    if not link:
        raise HTTPException(404)
    if settings.stats_password and stats.is_reader(request):
        stats.counts.guide_sponsor_click(guide_id)
    return RedirectResponse(
        link, status_code=302, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
    )


GUIDE_RESULTS = {
    "saved": "Saved.",
    "deleted": "The guide and its photos are deleted.",
    "starters": "The starter guides you ticked are added as drafts. Read each one, change"
    " what you like, and publish it.",
    "none": "Tick the starter guides you want first.",
    "confirm": "To delete the guide, tick the box beside the Delete button first.",
    "gone": "That guide is not there any more.",
    "packs": "The starter packs you ticked are added, not for sale yet, with their guides as"
    " drafts. Read and publish the guides, then open each pack and put it on sale.",
    "packs-none": "Tick the starter packs you want first.",
    "pack-deleted": "The guide pack is deleted.",
    "pass-off": "That pass is turned off: its link no longer opens the guides.",
    "pass-on": "That pass is turned on again.",
    "refunded": "The money is given back through PayPal, and the pass is turned off.",
    "refund-confirm": "To give the money back, tick the box beside the Refund button first.",
    "refund-failed": "PayPal could not give the money back, so nothing changed. See the web"
    " log for PayPal's reason, or refund it on paypal.com.",
    "pass-gone": "That pass was not in that state any more. Nothing changed.",
}


def _find_guide(guide_id: int) -> Guide:
    with session_scope() as session:
        guide = session.get(Guide, guide_id) if 1 <= guide_id < 2**31 else None
    if guide is None:
        raise HTTPException(404)
    return guide


@app.get("/admin/guides", response_class=HTMLResponse)
def admin_guides(request: Request, done: str = Query("", max_length=20)):
    """The owner's list of guides, drafts too, with their page views."""
    if (answer := _owner_check(request)) is not None:
        return answer
    today = datetime.now(GUAM_TZ).date()
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    with session_scope() as session:
        found = guides.everything(session)
        counts = guides.numbers(session, found, today - timedelta(days=29))
        starters = guides.missing_starters(session)
        packs = [
            {
                "pack": p,
                "count": len(guide_packs.contents(session, p, published_only=False)),
                "published": len(guide_packs.contents(session, p)),
                "sold": guide_packs.sold(session, p.id),
            }
            for p in guide_packs.everything(session)
        ]
        month = guide_packs.sales(session, month_ago)
        starter_packs = guide_packs.missing_starters(session)
        passes = guide_packs.recent_passes(session)
    response = render(
        request,
        "admin_guides.html",
        title="Visitor guides",
        robots="noindex, nofollow",
        count_view=False,
        show_signup=False,
        all_guides=found,
        counts=counts,
        starters=starters,
        today=today,
        message=GUIDE_RESULTS.get(done),
        packs=packs,
        month=month,
        month_total=sum(cents for _, cents in month.values()),
        starter_packs=starter_packs,
        passes=passes,
        sales_on=guide_packs.on(settings),
        paid_guides=sum(1 for g in found if g.access == guides.PAID),
        money=guide_packs.money,
    )
    return _no_store(response)


def _guide_values(guide: Guide) -> dict:
    """A guide's fields as the editor's form shows them."""
    return {
        "title": guide.title,
        "slug": guide.slug,
        "section": guide.section,
        "summary": guide.summary,
        "body": guide.body,
        "position": str(guide.position),
        "status": guide.status,
        "access": guide.access,
        "photo_alt": guide.photo_alt,
        "sponsor_name": guide.sponsor_name,
        "sponsor_link": guide.sponsor_link,
        "sponsor_text": guide.sponsor_text,
        "sponsor_until": guide.sponsor_until.isoformat() if guide.sponsor_until else "",
    }


def _guide_form(
    request: Request,
    guide: Guide | None,
    values: dict,
    errors: dict,
    status_code: int = 200,
    message: str | None = None,
) -> HTMLResponse:
    with session_scope() as session:
        sections = sorted({g.section for g in guides.everything(session) if g.section})
    response = render(
        request,
        "guide_form.html",
        status_code=status_code,
        title=f"Edit: {guide.title}" if guide else "New guide",
        robots="noindex, nofollow",
        count_view=False,
        show_signup=False,
        guide=guide,
        values=values,
        errors=errors,
        message=message,
        sections=sections,
        limits=guides,
        sales_on=guide_packs.on(settings),
    )
    return _no_store(response)


@app.get("/admin/guides/new", response_class=HTMLResponse)
def admin_guide_new(request: Request):
    if (answer := _owner_check(request)) is not None:
        return answer
    return _guide_form(
        request, None, {"position": "100", "status": guides.DRAFT, "access": guides.FREE}, {}
    )


@app.get("/admin/guides/{guide_id:int}", response_class=HTMLResponse)
def admin_guide_edit(request: Request, guide_id: int, done: str = Query("", max_length=20)):
    if (answer := _owner_check(request)) is not None:
        return answer
    guide = _find_guide(guide_id)
    return _guide_form(request, guide, _guide_values(guide), {}, message=GUIDE_RESULTS.get(done))


@app.get("/admin/guides/{guide_id:int}/view", response_class=HTMLResponse)
def admin_guide_view(request: Request, guide_id: int):
    """The guide as readers will see it, also while it is a draft."""
    if (answer := _owner_check(request)) is not None:
        return answer
    return _no_store(_guide_page(request, _find_guide(guide_id), preview=True))


class _PhotoRefused(Exception):
    pass


def _save_guide(
    guide_id: int | None, fields: dict[str, str], files: dict[str, bytes]
) -> tuple[Guide | None, dict[str, str]]:
    """Save the editor's form. Returns the guide and the problems found
    (nothing is saved when there are any)."""
    media = settings.media_dir
    stored: list[str] = []  # photos saved by this form, deleted again if it fails
    old_cover = None
    try:
        with session_scope() as session:
            guide = session.get(Guide, guide_id) if guide_id is not None else None
            if guide_id is not None and guide is None:
                return None, {"form": GUIDE_RESULTS["gone"]}
            remove = fields.get("remove_photo") == "yes"
            has_photo = "cover" in files or bool(guide and guide.photo and not remove)
            values, errors = guides.check(fields, has_photo)
            if "slug" not in errors and guides.slug_taken(session, values["slug"], guide_id):
                errors["slug"] = "Another guide has this address. Please choose another."
            if errors:
                return guide, errors
            now = datetime.now(timezone.utc)
            if guide is None:
                guide = Guide(created_at=now, access=guides.FREE)
                session.add(guide)
            guides.apply(guide, values, now)
            session.flush()  # a new guide's number, for its photos' names
            try:
                if "cover" in files:
                    stored.append(guides.save_photo(media, guide.id, files["cover"]))
                    old_cover, guide.photo = guide.photo, stored[-1]
                elif remove:
                    old_cover, guide.photo = guide.photo, None
                if "picture" in files:
                    stored.append(guides.save_photo(media, guide.id, files["picture"]))
                    alt = values["picture_alt"].replace("[", "(").replace("]", ")") or "Photo"
                    guide.body = f"{guide.body}\n\n![{alt}]({stored[-1]})"
            except ImageRejected as exc:
                log.info("guide photo refused: %s", exc)
                field = "cover" if not stored and "cover" in files else "picture"
                raise _PhotoRefused(field) from exc
    except _PhotoRefused as exc:
        for path in stored:
            guides.delete_photo(media, path)
        message = (
            "The photo could not be used. Please choose a JPG, PNG or WebP photo of at least"
            " 200 by 120 pixels, and at most 8 MB."
        )
        return guide, {str(exc): message}
    if old_cover and old_cover not in guide.body:
        guides.delete_photo(media, old_cover)
    guide_cache._data.clear()
    sitemap_cache._data.clear()
    return guide, {}


async def _read_guide_form(request: Request) -> tuple[dict[str, str], dict[str, bytes]] | Response:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > guides.MAX_FORM:
            return PlainTextResponse("Too large: each photo can be at most 8 MB.", status_code=413)
    try:
        return guides.read_form(body, request.headers.get("content-type", ""))
    except guides.FormError:
        return PlainTextResponse("Please use the form on the guide's page.", status_code=400)


@app.post("/admin/guides/new")
@app.post("/admin/guides/{guide_id:int}")
async def admin_guide_save(request: Request, guide_id: int | None = None):
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request):
        return PlainTextResponse("Please use the form on the guide's page.", status_code=403)
    form = await _read_guide_form(request)
    if isinstance(form, Response):
        return form
    fields, files = form
    guide, errors = await run_in_threadpool(_save_guide, guide_id, fields, files)
    if errors:
        if guide is None and guide_id is not None:
            raise HTTPException(404)
        return _guide_form(request, guide if guide_id else None, fields, errors, status_code=400)
    return RedirectResponse(
        f"/admin/guides/{guide.id}?done=saved",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/guides/{guide_id:int}/delete")
async def admin_guide_delete(request: Request, guide_id: int):
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request):
        return PlainTextResponse("Please use the button on the guide's page.", status_code=403)
    body = await _small_body(request)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
    if form.get("confirm") != ["yes"]:
        return RedirectResponse(
            f"/admin/guides/{guide_id}?done=confirm",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    def delete() -> bool:
        with session_scope() as session:
            guide = session.get(Guide, guide_id) if 1 <= guide_id < 2**31 else None
            if guide is None:
                return False
            session.delete(guide)
        guides.delete_photos(settings.media_dir, guide_id)
        guide_cache._data.clear()
        sitemap_cache._data.clear()
        return True

    done = await run_in_threadpool(delete)
    return RedirectResponse(
        f"/admin/guides?done={'deleted' if done else 'gone'}",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/guides/starters")
async def admin_guide_starters(request: Request):
    """Add the starter guides the owner ticked, as drafts."""
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request):
        return PlainTextResponse("Please use the button on the guides page.", status_code=403)
    body = await _small_body(request)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=100)

    def add() -> int:
        with session_scope() as session:
            return guides.add_starters(session, datetime.now(timezone.utc), form.get("slug", []))

    added = await run_in_threadpool(add)
    return RedirectResponse(
        f"/admin/guides?done={'starters' if added else 'none'}",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


# ------------------------------------------------------------- guide packs
# Visitor guides sold together through PayPal, read with a private link
# instead of an account (guide_packs.py). The owner makes the packs on
# /admin/guides; readers buy them on /guides/packs/<slug>.

guide_buy_limit = SignupLimit(per_address=10, total=100)
guide_link_limit = SignupLimit()
# The pack form: its text and a tick for each guide.
_MAX_PACK_BODY = 64 * 1024

PACK_PAGE_RESULTS = {
    "left": (False, "You left PayPal before the end, so nothing was paid. You can try again."),
    "unfinished": (False, "The payment was not finished. Nothing was charged."),
    "declined": PAYMENT_RESULTS["declined"],
    "error": PAYMENT_RESULTS["error"],
    "mismatch": (
        False,
        "Something did not match in this payment (the price may have just changed), so it was"
        " not used. Please try again; if you were charged, the payment is given back.",
    ),
    "used": (
        False,
        "This payment was used already. The link to your guides was emailed to the address you"
        " paid with; you can ask for it again below.",
    ),
    "busy": (False, "Too many tries in a short time. Please try again in an hour."),
    "sent": (
        True,
        "If guide packs were bought with that address, the link is on its way. Look in your"
        " inbox, and in your spam folder if it isn't there in a few minutes.",
    ),
    "invalid": (False, "That doesn't look like an email address. Please check it and try again."),
    "mailerror": (False, "Sorry, the email could not be sent this time. Please try again later."),
}


def _pack_for_sale(slug: str) -> tuple[GuidePack, list[Guide]] | None:
    """A pack that is for sale now, and its published guides (read fresh,
    not from the cache: its price may have just changed)."""
    if not guide_packs.on(settings) or not guides.SLUG.match(slug):
        return None
    with session_scope() as session:
        pack = guide_packs.by_slug(session, slug)
        if pack is None or pack.status != guide_packs.ON_SALE:
            return None
        inside = guide_packs.contents(session, pack)
    return (pack, inside) if inside else None


@app.get("/guides/packs/{slug}", response_class=HTMLResponse)
def guide_pack_page(request: Request, slug: str, result: str = Query("", max_length=12)):
    """A guide pack's page: what is in it, its price and the PayPal link."""
    found = _pack_for_sale(slug)
    if found is None:
        raise HTTPException(404)
    pack, inside = found
    with session_scope() as session:
        owned = guide_packs.owned(session, _reader_tokens(request))
    shown = PACK_PAGE_RESULTS.get(result)
    response = render(
        request,
        "guide_pack.html",
        pack=pack,
        inside=inside,
        about=guides.render(pack.about) if pack.about else None,
        price=guide_packs.money(pack.price_cents),
        owned_token=owned.get(pack.id),
        can_email=mail.enabled(settings),
        result={"ok": shown[0], "text": shown[1]} if shown else None,
        title=pack.title,
        description=truncate(pack.summary, 160),
        active_nav="guides",
        canonical={},
        robots="noindex, follow" if result else None,
        breadcrumbs=[("Visitor guides", "/guides"), (pack.title, None)],
        show_travel=True,
    )
    return _varies(response)


@app.get("/guides/buy/{slug}")
def guide_pack_buy(request: Request, slug: str):
    """Make a PayPal order for a pack, and send the buyer to PayPal to pay
    it. (A link, not a form: see paypal.py.) Nothing is stored until the
    payment is made."""
    found = _pack_for_sale(slug)
    if found is None:
        raise HTTPException(404)
    pack = found[0]
    page = f"/guides/packs/{pack.slug}"
    if not guide_buy_limit.allow(request.client.host if request.client else ""):
        log.warning("guide pack order refused: too many in the last hour")
        return _see_other(f"{page}?result=busy")
    try:
        order = paypal.create_order(
            settings,
            amount_cents=pack.price_cents,
            description=f"Guide pack: {pack.title}",
            custom_id=guide_packs.order_name(pack),
            return_url=seo.absolute_url(request, f"/guides/buy/{pack.slug}/paid"),
            cancel_url=seo.absolute_url(request, f"{page}?result=left"),
        )
    except paypal.PayPalError as exc:
        log.warning("PayPal order for guide pack %s failed: %s", pack.id, exc)
        return _see_other(f"{page}?result=error")
    return _see_other(order.approve_url)


def _after_guide_sale(request: Request, guide_pass: GuidePass, pack: GuidePack) -> None:
    """Email a new buyer their link, and tell the owner (Discord)."""
    link = seo.absolute_url(request, f"/guides/pass/{guide_pass.token}")
    if guide_pass.email and mail.enabled(settings):
        with session_scope() as session:
            titles = [g.title for g in guide_packs.contents(session, pack)]
        try:
            mail.send(
                settings,
                guide_pass.email,
                *guide_packs.bought_email(settings, pack.title, link, titles),
            )
        except mail.MailError as exc:
            log.warning("guide pack: the email to buyer %d failed: %s", guide_pass.id, exc)
        else:
            with session_scope() as session:
                session.execute(
                    update(GuidePass)
                    .where(GuidePass.id == guide_pass.id)
                    .values(emailed_at=datetime.now(timezone.utc))
                )
    if settings.discord_webhook_url:
        alerts.send(
            settings,
            alerts.Alert(
                title="Guide pack sold",
                message=f"{pack.title}, {guide_packs.money(guide_pass.amount_cents)}",
                emoji="📘",
                quiet=True,
            ),
        )


@app.get("/guides/buy/{slug}/paid")
def guide_pack_paid(request: Request, slug: str, token: str = Query("", max_length=64)):
    """Where PayPal sends the buyer back after approving the payment, with
    ?token=<the order's ID>: take the money, make the pass, and open the
    buyer's page."""
    if not guides.SLUG.match(slug):
        raise HTTPException(404)
    with session_scope() as session:
        pack = guide_packs.by_slug(session, slug)
    if pack is None:
        raise HTTPException(404)
    page = f"/guides/packs/{pack.slug}"
    if not re.fullmatch(r"[A-Za-z0-9-]{5,64}", token):
        return _see_other(f"{page}?result=unfinished")
    if not paypal.enabled(settings):
        return _see_other(f"{page}?result=error")
    bought = guide_packs.settle(settings, session_scope, pack, token, datetime.now(timezone.utc))
    if bought.guide_pass is None:
        return _see_other(f"{page}?result={bought.result}")
    if bought.new:
        log.info("guide pack %s sold (PayPal capture %s)", pack.id, bought.guide_pass.capture_id)
        _after_guide_sale(request, bought.guide_pass, pack)
    return _see_other(f"/guides/pass/{bought.guide_pass.token}?new=1")


@app.get("/guides/pass/{token}", response_class=HTMLResponse)
def guide_pass_page(request: Request, token: str, new: str = Query("", max_length=2)):
    """A buyer's own page: the guides of their pack. Opening it keeps the
    pass in this browser (a cookie for /guides pages only), so the guides
    open in full here from then on."""
    with session_scope() as session:
        found = guide_packs.by_token(session, token)
        if found is None:
            raise HTTPException(404)
        pack = session.get(GuidePack, found.pack_id)
        inside = guide_packs.contents(session, pack)
        readable = found.status in guide_packs.READABLE
        if readable:
            guide_packs.count_open(session, found.id)
    response = render(
        request,
        "guide_pass.html",
        guide_pass=found,
        pack=pack,
        inside=inside,
        readable=readable,
        new=new == "1" and readable,
        emailed=bool(found.email) and mail.enabled(settings),
        link=seo.absolute_url(request, f"/guides/pass/{found.token}"),
        title=f"Your guides: {pack.title}",
        robots="noindex, nofollow",
        breadcrumbs=[("Visitor guides", "/guides"), ("Your guides", None)],
        active_nav="guides",
        count_view=False,
        show_signup=False,
    )
    _private(response)
    if readable:
        response.set_cookie(
            guide_packs.COOKIE,
            guide_packs.with_token(request.cookies.get(guide_packs.COOKIE), found.token),
            max_age=guide_packs.COOKIE_DAYS * 86400,
            path=guide_packs.COOKIE_PATH,
            secure=seo.base_url(request).startswith("https://"),
            httponly=True,
            samesite="lax",
        )
    return response


@app.post("/guides/pass/link")
async def guide_pass_link(request: Request):
    """Email the links of the guide packs bought with an address again. The
    answer is the same whether or not there are any, so the form tells no
    one who bought what."""
    if not guide_packs.on(settings) or not mail.enabled(settings):
        raise HTTPException(404)
    if not _from_this_site(request):
        return PlainTextResponse("Please use the form on the site.", status_code=403)
    body = await _small_body(request)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
    slug = _field(form, "pack")
    back = f"/guides/packs/{slug}" if guides.SLUG.match(slug) else "/guides"
    if _field(form, "website"):
        return _see_other(f"{back}?result=sent#lost")  # the field only robots fill in
    email = _field(form, "email")
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        return _see_other(f"{back}?result=invalid#lost")
    if not guide_link_limit.allow(request.client.host if request.client else ""):
        return _see_other(f"{back}?result=busy#lost")

    def send() -> None:
        with session_scope() as session:
            found = guide_packs.passes_of(session, email)
        if not found:
            return
        links = [
            (title, seo.absolute_url(request, f"/guides/pass/{p.token}")) for p, title in found
        ]
        mail.send(settings, found[0][0].email, *guide_packs.links_email(settings, links))

    try:
        await run_in_threadpool(send)
    except mail.MailError as exc:
        log.warning("guide packs: could not email a link: %s", exc)
        return _see_other(f"{back}?result=mailerror#lost")
    return _see_other(f"{back}?result=sent#lost")


PACK_RESULTS = {
    "saved": "Saved.",
    "confirm": "To delete the pack, tick the box beside the Delete button first.",
    "sold": "This pack has buyers, so it can't be deleted: their links would stop working."
    " To stop selling it, choose Not for sale and save.",
}


def _pack_values(session, pack: GuidePack) -> dict:
    """A pack's fields as the owner's form shows them."""
    return {
        "title": pack.title,
        "slug": pack.slug,
        "summary": pack.summary,
        "about": pack.about,
        "price": guide_packs.money(pack.price_cents).removeprefix("$"),
        "position": str(pack.position),
        "status": pack.status,
        "every_paid": pack.every_paid,
        "guide_ids": guide_packs.guide_ids(session, pack.id),
    }


def _pack_form(
    request: Request,
    pack: GuidePack | None,
    values: dict,
    errors: dict,
    status_code: int = 200,
    message: str | None = None,
) -> HTMLResponse:
    with session_scope() as session:
        all_guides = guides.everything(session)
        sold = guide_packs.sold(session, pack.id) if pack else 0
    response = render(
        request,
        "guide_pack_form.html",
        status_code=status_code,
        title=f"Edit: {pack.title}" if pack else "New guide pack",
        robots="noindex, nofollow",
        count_view=False,
        show_signup=False,
        pack=pack,
        values=values,
        errors=errors,
        message=message,
        all_guides=all_guides,
        sold=sold,
        sales_on=guide_packs.on(settings),
        limits=guide_packs,
    )
    return _no_store(response)


def _find_pack(pack_id: int) -> tuple[GuidePack, dict]:
    with session_scope() as session:
        pack = session.get(GuidePack, pack_id) if 1 <= pack_id < 2**31 else None
        if pack is None:
            raise HTTPException(404)
        return pack, _pack_values(session, pack)


@app.get("/admin/guides/packs/new", response_class=HTMLResponse)
def admin_pack_new(request: Request):
    if (answer := _owner_check(request)) is not None:
        return answer
    values = {"position": "100", "status": guide_packs.DRAFT, "guide_ids": set()}
    return _pack_form(request, None, values, {})


@app.get("/admin/guides/packs/{pack_id:int}", response_class=HTMLResponse)
def admin_pack_edit(request: Request, pack_id: int, done: str = Query("", max_length=20)):
    if (answer := _owner_check(request)) is not None:
        return answer
    pack, values = _find_pack(pack_id)
    return _pack_form(request, pack, values, {}, message=PACK_RESULTS.get(done))


@app.post("/admin/guides/packs/new")
@app.post("/admin/guides/packs/{pack_id:int}")
async def admin_pack_save(request: Request, pack_id: int | None = None):
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request):
        return PlainTextResponse("Please use the form on the pack's page.", status_code=403)
    body = await _small_body(request, _MAX_PACK_BODY)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=400)
    values, errors = guide_packs.check(form)

    def save() -> tuple[GuidePack | None, dict]:
        with session_scope() as session:
            pack = session.get(GuidePack, pack_id) if pack_id is not None else None
            if pack_id is not None and pack is None:
                raise HTTPException(404)
            if "slug" not in errors and guide_packs.slug_taken(session, values["slug"], pack_id):
                errors["slug"] = "Another pack has this address. Please choose another."
            if errors:
                return pack, errors
            now = datetime.now(timezone.utc)
            if pack is None:
                pack = GuidePack(created_at=now)
                session.add(pack)
            guide_packs.apply(session, pack, values, now)
        guide_cache._data.clear()
        sitemap_cache._data.clear()
        return pack, {}

    pack, errors = await run_in_threadpool(save)
    if errors:
        shown = {
            **{k: (form.get(k) or [""])[0] for k in ("title", "slug", "summary", "about")},
            **{k: (form.get(k) or [""])[0] for k in ("price", "position", "status")},
            "every_paid": values["every_paid"],
            "guide_ids": set(values["guide_ids"]),
        }
        return _pack_form(request, pack, shown, errors, status_code=400)
    return _see_other(f"/admin/guides/packs/{pack.id}?done=saved")


@app.post("/admin/guides/packs/{pack_id:int}/delete")
async def admin_pack_delete(request: Request, pack_id: int):
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request):
        return PlainTextResponse("Please use the button on the pack's page.", status_code=403)
    body = await _small_body(request)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    if parse_qs(body.decode("utf-8", "replace")).get("confirm") != ["yes"]:
        return _see_other(f"/admin/guides/packs/{pack_id}?done=confirm")

    def delete() -> str:
        with session_scope() as session:
            pack = session.get(GuidePack, pack_id) if 1 <= pack_id < 2**31 else None
            if pack is None:
                return "gone"
            if guide_packs.sold(session, pack_id):
                return "sold"
            session.delete(pack)
        guide_cache._data.clear()
        sitemap_cache._data.clear()
        return "deleted"

    done = await run_in_threadpool(delete)
    if done == "sold":
        return _see_other(f"/admin/guides/packs/{pack_id}?done=sold")
    return _see_other(f"/admin/guides?done={'pack-deleted' if done == 'deleted' else 'gone'}")


@app.post("/admin/guides/packs/starters")
async def admin_pack_starters(request: Request):
    """Add the starter packs the owner ticked (not for sale), with their guides."""
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request):
        return PlainTextResponse("Please use the button on the guides page.", status_code=403)
    body = await _small_body(request)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=50)

    def add() -> int:
        with session_scope() as session:
            added = guide_packs.add_starters(
                session, datetime.now(timezone.utc), form.get("pack", [])
            )
        guide_cache._data.clear()
        return added

    added = await run_in_threadpool(add)
    return _see_other(f"/admin/guides?done={'packs' if added else 'packs-none'}#packs")


@app.post("/admin/guides/passes/{pass_id:int}")
async def admin_guide_pass(request: Request, pass_id: int):
    """Turn a buyer's pass off or on, or give the money back (which turns it off)."""
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request):
        return PlainTextResponse("Please use the buttons on the guides page.", status_code=403)
    body = await _small_body(request)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
    action = _field(form, "action")
    now = datetime.now(timezone.utc)

    def act() -> str:
        if action in ("off", "on"):
            status = guide_packs.OFF if action == "off" else guide_packs.PAID
            with session_scope() as session:
                done = guide_packs.set_status(session, pass_id, status, now)
            return f"pass-{action}" if done else "pass-gone"
        if action != "refund":
            return "pass-gone"
        if _field(form, "confirm") != "yes":
            return "refund-confirm"
        with session_scope() as session:
            found = session.get(GuidePass, pass_id) if 1 <= pass_id < 2**31 else None
        if found is None or found.status == guide_packs.REFUNDED or not found.capture_id:
            return "pass-gone"
        try:
            paypal.refund_capture(settings, found.capture_id)
        except paypal.PayPalError as exc:
            log.warning("guide pass %s: PayPal refund failed: %s", pass_id, exc)
            return "refund-failed"
        with session_scope() as session:
            guide_packs.set_status(session, pass_id, guide_packs.REFUNDED, now)
        return "refunded"

    done = await run_in_threadpool(act)
    return _see_other(f"/admin/guides?done={done}#sales")


# ------------------------------------------------------------ advertising
# The Advertise page: businesses book the sponsor spot by the week, or the
# newsletter's sponsor line by the edition, and pay through PayPal; the owner
# approves each ad on /advertise/review. See ads.py.

booking_limit = SignupLimit(per_address=10, total=60)
# The booking form with its picture, and a little room for the other fields.
MAX_BOOKING_BODY = ads.MAX_PICTURE_BYTES + 64 * 1024
# The numbers on the Advertise page are counted again at most every ten minutes.
audience_cache = _TTLCache(ttl=600)

AD_NAMES = {ads.SITE: "Sponsor spot", ads.NEWSLETTER: "Newsletter sponsor"}


def _list_words(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + f" and {words[-1]}"


def ad_when(booking: AdBooking, first: bool = False) -> str:
    """When a booked ad shows: "the weeks of Dec 7 and Dec 14, 2026", "the
    editions of Tue, Dec 8 and Fri, Dec 11" ("The ..." with `first`, to start
    a sentence)."""
    days = sorted(booking.days)
    if booking.kind == ads.SITE:
        words = [f"{d:%b} {d.day}" for d in days]
        text = f"the week{'s' if len(days) > 1 else ''} of {_list_words(words)}, {days[-1].year}"
    else:
        words = [_short_day(d) for d in days]
        text = f"the edition{'s' if len(days) > 1 else ''} of {_list_words(words)}"
    return "T" + text[1:] if first else text


templates.env.globals.update(ad_when=ad_when, money=ads.money, ad_names=AD_NAMES)


def _ad_form_values(form, offer: ads.Offer, now: datetime) -> tuple[dict, dict]:
    """The booking form's fields, cleaned, and the problems with them (words
    for the business, by field name)."""
    text = lambda name, limit: " ".join(str(form.get(name) or "").split())[:limit]  # noqa: E731
    link = str(form.get("link") or "").strip()[:2000]
    if link and "://" not in link and re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", link):
        link = "https://" + link  # "example.com" as most people type it
    values = {
        "business": text("business", 200),
        "email": str(form.get("email") or "").strip()[:300],
        "link": link,
        "words": text("words", 1000),
        "days": [str(d) for d in form.getlist("day")][:60],
    }
    allowed = {d.isoformat(): d for d in ads.choices(settings, offer.kind, now)}
    values["picked"] = sorted({allowed[d] for d in values["days"] if d in allowed})
    errors = {}
    if not values["picked"]:
        errors["days"] = f"Pick at least one {offer.unit}."
    if not 2 <= len(values["business"]) <= ads.MAX_BUSINESS:
        errors["business"] = f"Give the business's name (up to {ads.MAX_BUSINESS} letters)."
    if len(values["email"]) > 254 or not _EMAIL.fullmatch(values["email"]):
        errors["email"] = "That doesn't look like an email address."
    if not web_address(values["link"]):
        errors["link"] = "Give the full address of the website, such as https://example.com."
    longest = ads.MAX_WORDS[offer.kind]
    if offer.kind == ads.SITE and not 3 <= len(values["words"]) <= longest:
        errors["words"] = (
            f"Say what the picture shows, in up to {longest} letters, for people who can't see it."
        )
    if offer.kind == ads.NEWSLETTER and not 10 <= len(values["words"]) <= longest:
        errors["words"] = f"Write the sponsor line, from 10 to {longest} letters."
    if ads.refused([values["business"], values["link"], values["words"]], _muted()):
        errors["form"] = "Sorry, this ad can't be booked on this site."
    return values, errors


def _advertise_page(
    request: Request,
    *,
    status_code: int = 200,
    form_kind: str | None = None,
    values: dict | None = None,
    errors: dict | None = None,
) -> HTMLResponse:
    offers = advertising()
    now = datetime.now(timezone.utc)
    today = now.astimezone(GUAM_TZ).date()
    with session_scope() as session:
        ads.release_expired(session, now)
        numbers = audience_cache.get(str(today), lambda: ads.audience(session, today))
        forms = []
        for offer in offers:
            days = ads.choices(settings, offer.kind, now)
            held = ads.taken(session, offer.kind, days)
            # (day, last day of its week or None, taken)
            last = timedelta(days=6) if offer.kind == ads.SITE else None
            forms.append(
                {"offer": offer, "days": [(d, last and d + last, d in held) for d in days]}
            )
    response = render(
        request,
        "advertise.html",
        status_code=status_code,
        title="Advertise",
        description=(
            f"Put your business in front of Guam readers on {settings.site_name}: the sponsor"
            " spot on every page, or the sponsor line in the newsletter. See the real numbers,"
            " book online and pay with PayPal."
        ),
        canonical={},
        robots="noindex, follow" if status_code != 200 else None,
        breadcrumbs=[("Advertise", None)],
        numbers=numbers,
        forms=forms,
        form_kind=form_kind,
        values=values or {},
        errors=errors or {},
        newsletter_schedule=(
            schedule_text(settings.newsletter_days, settings.newsletter_time)
            if settings.newsletter_days
            else ""
        ),
        hold_minutes=ads.HOLD_MINUTES,
        max_picture_mb=ads.MAX_PICTURE_BYTES // (1024 * 1024),
    )
    # The open weeks change as businesses book them.
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/advertise", response_class=HTMLResponse)
def advertise(request: Request):
    """What a business can buy, the site's real numbers, and the booking forms."""
    if not advertising():
        raise HTTPException(404)
    return _advertise_page(request)


@app.post("/advertise")
async def advertise_book(request: Request):
    """The booking form: check it, hold the weeks, and send the business to
    its booking's own page, where it pays."""
    offers = {offer.kind: offer for offer in advertising()}
    if not offers:
        raise HTTPException(404)
    if not _from_this_site(request):
        return PlainTextResponse("Please use the booking form on the site.", status_code=403)
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > MAX_BOOKING_BODY:
        return PlainTextResponse(
            f"Too large: the picture can be at most {ads.MAX_PICTURE_BYTES // 2**20} MB.",
            status_code=413,
        )
    async with request.form(max_files=1, max_fields=40, max_part_size=16 * 1024) as form:
        offer = offers.get(str(form.get("kind") or ""))
        if offer is None:
            return RedirectResponse("/advertise", status_code=303)
        if form.get("website"):
            # The field that people don't see: a robot filled it in.
            return RedirectResponse("/advertise", status_code=303)
        now = datetime.now(timezone.utc)
        values, errors = _ad_form_values(form, offer, now)
        picture = None
        upload = form.get("picture")
        if offer.kind == ads.SITE:
            if not isinstance(upload, UploadFile) or not upload.filename:
                errors["picture"] = "Choose the picture for the sponsor spot."
            elif not errors:
                data = await upload.read(ads.MAX_PICTURE_BYTES + 1)
                try:
                    picture = await run_in_threadpool(ads.picture_copy, data)
                except ValueError as exc:
                    errors["picture"] = str(exc)
    if not errors and not booking_limit.allow(request.client.host if request.client else ""):
        log.warning("ad booking refused: too many bookings in the last hour")
        errors["form"] = "Too many bookings in a short time. Please try again in an hour."
    if errors:
        if offer.kind == ads.SITE and "picture" not in errors:
            errors["picture"] = "Please choose the picture again."
        return _advertise_page(
            request, status_code=400, form_kind=offer.kind, values=values, errors=errors
        )
    try:
        with session_scope() as session:
            booking = ads.book(
                session,
                kind=offer.kind,
                days=values["picked"],
                business=values["business"],
                email=values["email"],
                link=values["link"],
                words=values["words"],
                price_cents=offer.price_cents,
                picture=picture,
                now=now,
            )
            code = booking.code
            log.info("ad booking %d made (%s, not paid yet)", booking.id, offer.kind)
    except ads.NotAvailable as exc:
        names = [fmt_day(d) for d in exc.days]
        errors["days"] = (
            f"Sorry, someone else has just booked {_list_words(names)}. Please pick again."
        )
        return _advertise_page(
            request, status_code=409, form_kind=offer.kind, values=values, errors=errors
        )
    return RedirectResponse(
        f"/advertise/booking/{code}", status_code=303, headers={"Cache-Control": "no-store"}
    )


BOOKING_NOTES = {
    "paid": (
        True,
        "Thank you, your payment went through. Your ad is waiting for a quick look before it"
        " goes up. This page shows when it's approved.",
    ),
    "cancelled": (True, "The booking is cancelled, and nothing was charged."),
    "taken": (
        False,
        "Sorry, while you were paying, someone else booked one of your dates. Nothing was"
        " charged. Please book again on the Advertise page.",
    ),
    "stale": (
        False,
        "That payment was for an earlier try, so nothing was charged for it. Please press"
        " the PayPal button again.",
    ),
    "declined": (
        False,
        "PayPal didn't finish the payment, so nothing was charged. Please try again, or pay"
        " another way in PayPal.",
    ),
    "paypal": (
        False,
        "PayPal couldn't be reached just now, and nothing was charged. Please try again in a"
        " few minutes.",
    ),
    "pick": (False, "Pick at least one date to book your ad again."),
    "taken-again": (
        False,
        "Sorry, someone else has just booked one of those dates. Please pick again.",
    ),
    "refused": (False, "Sorry, this ad can't be booked again on this site."),
    "busy": (False, "Too many bookings in a short time. Please try again in an hour."),
}


def _private(response: Response) -> Response:
    """For a booking's page, the owner's review page and a bid alert
    subscriber's pages: never kept by a browser or a search engine, and their
    addresses (which hold a secret code) are not passed on."""
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _to_booking(code: str, note: str = "") -> RedirectResponse:
    query = f"?note={note}" if note else ""
    return RedirectResponse(
        f"/advertise/booking/{code}{query}",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/advertise/booking/{code}", response_class=HTMLResponse)
def ad_booking_page(request: Request, code: str, note: str = Query("", max_length=20)):
    """A booking's own page, which only the business that made it has the
    address of: what it booked, the PayPal button, where it stands, and how
    the ad has done."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        booking = ads.by_code(session, code)
        if booking is None:
            raise HTTPException(404)
        if booking.status == "new":
            ads.release_expired(session, now)
            session.refresh(booking)
        results = ads.results(session, booking, now.astimezone(GUAM_TZ).date())
        again = _book_again_choices(session, booking, now)
    found = BOOKING_NOTES.get(note)
    response = render(
        request,
        "ad_booking.html",
        title="Your ad booking",
        robots="noindex, nofollow",
        count_view=False,  # its address is a secret: it is not kept in the stats
        booking=booking,
        results=results,
        expires_at=booking.created_at + timedelta(minutes=ads.HOLD_MINUTES),
        can_pay=booking.status == "new" and paypal.enabled(settings),
        message={"ok": found[0], "text": found[1]} if found else None,
        again=again,
    )
    return _private(response)


def _book_again_choices(session, booking: AdBooking, now: datetime) -> dict | None:
    """For an approved ad whose kind is still sold: its offer, and the dates
    that can be booked, as (day, last day of its week or None, taken,
    ticked). The first free dates after the ad's own are ticked, as many as
    it booked."""
    offer = next((o for o in advertising() if o.kind == booking.kind), None)
    if booking.status != "approved" or offer is None:
        return None
    days = ads.choices(settings, booking.kind, now)
    held = ads.taken(session, booking.kind, days)
    free_after = [d for d in days if d not in held and d > max(booking.days)]
    ticked = set(free_after[: len(booking.days)])
    last = timedelta(days=6) if booking.kind == ads.SITE else None
    return {
        "offer": offer,
        "days": [(d, last and d + last, d in held, d in ticked) for d in days],
    }


@app.get("/advertise/booking/{code}/picture.webp")
def ad_booking_picture(code: str):
    with session_scope() as session:
        booking = ads.by_code(session, code)
        data = booking.picture if booking else None
    if not data:
        raise HTTPException(404)
    return Response(
        data,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=3600", "X-Robots-Tag": "noindex"},
    )


def _is_paypal_page(url: str) -> bool:
    """PayPal's own payment page (https, on paypal.com)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "paypal.com" or host.endswith(".paypal.com"))


@app.get("/advertise/booking/{code}/pay")
def ad_booking_pay(request: Request, code: str):
    """The PayPal button: make a PayPal order for the booking and send the
    business to PayPal to pay. A plain link, not a form, because the security
    rules let forms go only to this site."""
    now = datetime.now(timezone.utc)
    base = seo.base_url(request) + f"/advertise/booking/{code}"
    with session_scope() as session:
        booking = ads.by_code(session, code, lock=True)
        if booking is None:
            raise HTTPException(404)
        ads.release_expired(session, now)
        session.refresh(booking)
        if booking.status != "new" or not paypal.enabled(settings):
            return _to_booking(code)
        try:
            order = paypal.create_order(
                settings,
                amount_cents=booking.amount_cents,
                description=f"{settings.site_name}: {AD_NAMES[booking.kind]}, {ad_when(booking)}",
                custom_id=f"ad-booking-{booking.id}",
                return_url=f"{base}/paid",
                cancel_url=base,
            )
        except paypal.PayPalError as exc:
            log.warning("ad booking %d: PayPal made no order: %s", booking.id, exc)
            return _to_booking(code, "paypal")
        if not _is_paypal_page(order.approve_url):
            log.warning("ad booking %d: PayPal's page is not on paypal.com", booking.id)
            return _to_booking(code, "paypal")
        booking.paypal_order_id = order.id
    return RedirectResponse(
        order.approve_url, status_code=303, headers={"Cache-Control": "no-store"}
    )


def _booking_alert(booking: AdBooking, request: Request) -> alerts.Alert:
    pending = " PayPal is still checking the payment." if booking.note else ""
    return alerts.Alert(
        title=f"{settings.site_name}: a new ad to approve",
        message=(
            f"{booking.business} paid {ads.money(booking.amount_cents)} for the"
            f" {AD_NAMES[booking.kind].lower()}, {ad_when(booking)}.{pending} Approve it or turn"
            f" it down: {seo.absolute_url(request, '/advertise/review')}"
        ),
        emoji="💵",
    )


@app.get("/advertise/booking/{code}/paid")
def ad_booking_paid(request: Request, code: str, token: str = Query("", max_length=100)):
    """Where PayPal sends the business back after it approves the payment
    (?token=<the order>): take the money, and the booking waits for the owner."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        booking = ads.by_code(session, code, lock=True)
        if booking is None:
            raise HTTPException(404)
        if booking.status not in ("new", "cancelled"):
            return _to_booking(code)  # paid already (the page was loaded again)
        if not token or token != booking.paypal_order_id:
            return _to_booking(code, "stale")
        # Not paid within the hold: its dates again, if they are still free.
        if not ads.hold_again(session, booking):
            return _to_booking(code, "taken")
        try:
            capture = paypal.capture_order(settings, token, custom_id=f"ad-booking-{booking.id}")
        except paypal.PayPalError as exc:
            if exc.issue == paypal.WRONG_ORDER:
                log.error("ad booking %d: %s", booking.id, exc)
                return _to_booking(code, "stale")
            log.warning("ad booking %d: PayPal did not take the payment: %s", booking.id, exc)
            declined = exc.issue in ("INSTRUMENT_DECLINED", "ORDER_NOT_APPROVED")
            return _to_booking(code, "declined" if declined else "paypal")
        if capture.status not in ("COMPLETED", "PENDING"):
            log.warning("ad booking %d: PayPal says the payment is %s", booking.id, capture.status)
            return _to_booking(code, "declined")
        wrong_amount = capture.amount_cents not in (None, booking.amount_cents)
        wrong = wrong_amount or capture.currency not in (None, paypal.CURRENCY)
        if wrong:
            log.error(
                "ad booking %d: PayPal took %s %s, not %s; giving it back",
                booking.id,
                capture.amount_cents,
                capture.currency,
                booking.amount_cents,
            )
            if capture.capture_id:
                try:
                    paypal.refund_capture(settings, capture.capture_id)
                except paypal.PayPalError as exc:
                    log.error("ad booking %d: refund failed: %s", booking.id, exc)
            return _to_booking(code, "paypal")
        ads.mark_paid(session, booking, capture.capture_id, now)
        if capture.status == "PENDING":
            booking.note = "PayPal is still checking this payment (PENDING): look in PayPal first."
        log.info("ad booking %d paid: waiting for approval", booking.id)
        alert = _booking_alert(booking, request)
    threading.Thread(target=alerts.send, args=(settings, alert), daemon=True).start()
    return _to_booking(code, "paid")


@app.post("/advertise/booking/{code}/again")
async def ad_booking_again(request: Request, code: str):
    """Book the same ad (its picture or words, and its website) again for new
    weeks or editions. This makes a new booking, which is paid and approved
    the usual way, and opens its page."""
    if not _from_this_site(request):
        return PlainTextResponse("Please use the form on the booking's page.", status_code=403)
    body = await _small_body(request)
    if body is None:
        return PlainTextResponse("Too large", status_code=413)
    picked_text = parse_qs(body.decode("utf-8", "replace"), max_num_fields=80).get("day", [])
    now = datetime.now(timezone.utc)
    offers = {offer.kind: offer for offer in advertising()}
    with session_scope() as session:
        old = ads.by_code(session, code)
        if old is None:
            raise HTTPException(404)
    offer = offers.get(old.kind)
    if old.status != "approved" or offer is None:
        return _to_booking(code)
    if ads.refused([old.business, old.link, old.words], _muted()):
        return _to_booking(code, "refused")
    allowed = {d.isoformat(): d for d in ads.choices(settings, old.kind, now)}
    picked = sorted({allowed[d] for d in picked_text if d in allowed})
    if not picked:
        return _to_booking(code, "pick")
    if not booking_limit.allow(request.client.host if request.client else ""):
        log.warning("ad booking refused: too many bookings in the last hour")
        return _to_booking(code, "busy")
    picture = (old.picture, old.picture_width, old.picture_height) if old.picture else None
    try:
        with session_scope() as session:
            booking = ads.book(
                session,
                kind=old.kind,
                days=picked,
                business=old.business,
                email=old.email,
                link=old.link,
                words=old.words,
                price_cents=offer.price_cents,
                picture=picture,
                now=now,
            )
            new_code = booking.code
            log.info("ad booking %d made again from booking %d (not paid yet)", booking.id, old.id)
    except ads.NotAvailable:
        return _to_booking(code, "taken-again")
    return _to_booking(new_code)


@app.post("/advertise/booking/{code}/cancel")
def ad_booking_cancel(request: Request, code: str):
    """The business cancels a booking that it has not paid for."""
    if not _from_this_site(request):
        return PlainTextResponse("Please use the button on the booking's page.", status_code=403)
    with session_scope() as session:
        booking = ads.by_code(session, code, lock=True)
        if booking is None:
            raise HTTPException(404)
        if booking.status != "new":
            return _to_booking(code)
        ads.cancel(session, booking, "Cancelled by the business.")
    return _to_booking(code, "cancelled")


REVIEW_NOTES = {
    "approved": (True, "Approved. The ad shows in its weeks."),
    "rejected": (True, "Turned down. PayPal is giving the money back."),
    "rejected-kept": (True, "Turned down, without a refund through the site."),
    "refund-failed": (
        False,
        "PayPal could not refund the payment, so nothing was changed. Refund it in PayPal"
        ' (Activity, then the payment, then Issue a refund), then press "Turn down, refunded'
        ' in PayPal".',
    ),
    "unchanged": (False, "Nothing to change: the booking had already moved on."),
}


@app.get("/advertise/review", response_class=HTMLResponse)
def ad_review(
    request: Request,
    done: str = Query("", max_length=20),
    booking: int = Query(0, ge=0, le=2**31),
):
    """The owner's list of booked ads, to approve or turn down (STATS_PASSWORD)."""
    if (answer := _owner_check(request)) is not None:
        return answer
    stats.counts.flush()
    now = datetime.now(timezone.utc)
    today = now.astimezone(GUAM_TZ).date()
    with session_scope() as session:
        ads.release_expired(session, now)
        rows = ads.for_review(session)
        results = {b.id: ads.results(session, b, today) for b in rows}
    found = REVIEW_NOTES.get(done)
    response = render(
        request,
        "ad_review.html",
        title="Ad bookings",
        robots="noindex, nofollow",
        count_view=False,
        bookings=rows,
        results=results,
        today=today,
        selling=advertising(),
        last_day=ads.last_day,
        message={"ok": found[0], "text": found[1], "booking": booking} if found else None,
    )
    return _private(response)


@app.post("/advertise/review/{booking_id}")
async def ad_review_decide(request: Request, booking_id: int):
    """Approve a paid booking, or turn it down (PayPal refunds it)."""
    if (answer := _owner_check(request)) is not None:
        return answer
    if not _from_this_site(request) or not 1 <= booking_id < 2**31:
        return PlainTextResponse("Please use the buttons on the review page.", status_code=403)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_SIGNUP_BODY:
            return PlainTextResponse("Too large", status_code=413)
    form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
    action = (form.get("action") or [""])[0]
    note = " ".join((form.get("note") or [""])[0].split())[:500]

    def decide() -> str:
        now = datetime.now(timezone.utc)
        with session_scope() as session:
            found = session.get(AdBooking, booking_id, with_for_update=True)
            if found is None:
                raise HTTPException(404)
            if action == "approve" and found.status == "paid":
                ads.approve(found, now)
                log.info("ad booking %d approved", found.id)
                return "approved"
            if action in ("reject", "reject-kept") and found.status in ("paid", "approved"):
                if action == "reject" and found.paypal_capture_id:
                    try:
                        paypal.refund_capture(settings, found.paypal_capture_id)
                    except paypal.PayPalError as exc:
                        log.warning("ad booking %d: refund failed: %s", found.id, exc)
                        return "refund-failed"
                ads.reject(session, found, note, now)
                log.info("ad booking %d turned down", found.id)
                return "rejected" if action == "reject" else "rejected-kept"
            return "unchanged"

    result = await run_in_threadpool(decide)
    ad_cache._data.clear()  # this web worker shows the change at once
    return RedirectResponse(
        f"/advertise/review?done={result}&booking={booking_id}#booking-{booking_id}",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


# ------------------------------------------------------------ bid alerts


def bid_alerts_info() -> dict | None:
    """The bid alerts offer, or None while bid alerts are off (see bids/alerts.py)."""
    if not bid_alerts.is_on(settings):
        return None
    return {"price": bid_alerts.price_text(settings.bid_alerts_price_cents)}


bid_alert_limit = SignupLimit()


async def _bid_alert_form(request: Request) -> dict[str, list[str]] | Response:
    """The fields of a form sent from a page of this site, or the answer to
    give when it was not (another site's form) or is too large."""
    if not bid_alerts.is_on(settings):
        raise HTTPException(404)
    if not _from_this_site(request):
        return PlainTextResponse("Please use the form on the site.", status_code=403)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_SIGNUP_BODY:
            return PlainTextResponse("Too large", status_code=413)
    return parse_qs(body.decode("utf-8", "replace"), max_num_fields=30)


def _field(form: dict[str, list[str]], name: str) -> str:
    return (form.get(name) or [""])[0].strip()


def _choices(form: dict[str, list[str]]) -> tuple[str, list[str], list[str]] | None:
    """(email, trades, words) from the form, or None if the email address is
    not one."""
    email = _field(form, "email")
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        return None
    return (
        email,
        bid_alerts.parse_trades(form.get("trade", [])),
        bid_alerts.parse_words(_field(form, "words")),
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})


def _bid_alert_results() -> dict[str, tuple[bool, str]]:
    return {
        "sent": (
            True,
            "If that address gets bid alerts, the link to its page is on its way. Look in your"
            " inbox, and in your spam folder if it isn't there in a few minutes.",
        ),
        "invalid": (
            False,
            "That doesn't look like an email address. Please check it and try again.",
        ),
        "busy": (False, "Too many tries in a short time. Please try again in an hour."),
        "error": (False, "Sorry, that didn't work this time. Please try again later."),
    }


@app.get("/bids/alerts", response_class=HTMLResponse)
def bid_alerts_page(request: Request, result: str = Query("", max_length=10)):
    """What bid alerts are, the sign-up form, and the lost-link form."""
    alerts = bid_alerts_info()
    if alerts is None:
        raise HTTPException(404)
    found = _bid_alert_results().get(result)
    return render(
        request,
        "bid_alerts.html",
        title="Bid alerts by email",
        description="Get an email when a new Government of Guam bid or request for proposals"
        f" matches your trade. {alerts['price']} a month through PayPal; cancel any time.",
        canonical={},
        robots="noindex, follow" if result else None,
        breadcrumbs=[("Bids and RFPs", "/bids"), ("Bid alerts", None)],
        active_nav="bids",
        alerts=alerts,
        trades=bid_alerts.TRADES,
        result={"ok": found[0], "text": found[1]} if found else None,
    )


@app.post("/bids/alerts")
async def bid_alerts_signup(request: Request):
    """The sign-up form: keep a pending subscriber, then show the review page."""
    form = await _bid_alert_form(request)
    if isinstance(form, Response):
        return form
    if _field(form, "website"):
        return _see_other("/bids/alerts")  # the field only robots fill in
    choices = _choices(form)
    if choices is None:
        return _see_other("/bids/alerts?result=invalid")
    if not bid_alert_limit.allow(request.client.host if request.client else ""):
        log.warning("bid alerts sign-up refused: too many in the last hour")
        return _see_other("/bids/alerts?result=busy")
    token = await run_in_threadpool(bid_alerts.new_subscriber, *choices)
    return _see_other(f"/bids/alerts/review?t={token}")


def _bid_alert_review(request: Request, sub, result: dict | None = None) -> HTMLResponse:
    return _private(
        render(
            request,
            "bid_alerts_review.html",
            title="Check your bid alerts",
            robots="noindex, nofollow",
            breadcrumbs=[("Bids and RFPs", "/bids"), ("Bid alerts", "/bids/alerts")],
            active_nav="bids",
            count_view=False,
            alerts=bid_alerts_info(),
            sub=sub,
            choices=bid_alerts.choices_text(sub.trades, sub.keywords),
            already=bid_alerts.has_alerts(sub.email),
            result=result,
        )
    )


@app.get("/bids/alerts/review", response_class=HTMLResponse)
def bid_alerts_review(
    request: Request,
    t: str = Query("", max_length=64),
    left: str = Query("", alias="paypal", max_length=20),
):
    """The details of a new subscription, to check before paying."""
    if not bid_alerts.is_on(settings):
        raise HTTPException(404)
    sub = bid_alerts.by_token(t)
    if sub is None:
        raise HTTPException(404)
    if sub.status != "pending":
        return _see_other(f"/bids/alerts/manage?t={sub.token}")
    result = None
    if left == "cancelled":
        result = {
            "ok": False,
            "text": "You left PayPal before the end, so nothing was paid. You can try again.",
        }
    return _bid_alert_review(request, sub, result)


@app.get("/bids/alerts/pay")
def bid_alerts_pay(request: Request, t: str = Query("", max_length=64)):
    """Open PayPal's page for the monthly payment. A plain link leads here
    (not a form): Chrome applies a page's form-action rule, which allows only
    this site, also to the redirect after a form."""
    if not bid_alerts.is_on(settings):
        raise HTTPException(404)
    sub = bid_alerts.by_token(t)
    if sub is None:
        raise HTTPException(404)
    try:
        approve = bid_alerts.start_payment(settings, t)
    except (paypal.PayPalError, bid_alerts.AlertsError) as exc:
        log.warning("bid alerts: could not start the PayPal payment: %s", exc)
        return _bid_alert_review(
            request,
            sub,
            {"ok": False, "text": "PayPal could not be reached just now. Please try again soon."},
        )
    if approve is None:
        return _see_other(f"/bids/alerts/manage?t={t}")
    return _see_other(approve)


def _state(sub, now: datetime) -> dict:
    """The heading and words for a subscription's state on its own page."""
    price = bid_alerts.price_text(settings.bid_alerts_price_cents)
    until = fmt_date_long(sub.paid_until) if sub.paid_until else ""
    if sub.status == "pending":
        return {
            "title": "Not paid yet",
            "text": "The alerts start once PayPal has the payment. If you just approved it on"
            " PayPal's page, wait a minute and open this page again.",
            "ended": False,
        }
    if sub.status == "suspended":
        return {
            "title": "Paused",
            "text": "PayPal could not take the last payment, so the alerts are paused. Update"
            " your card or bank in your PayPal account: when PayPal takes the payment, the"
            " alerts start again.",
            "ended": False,
        }
    if sub.status == "cancelled" and bid_alerts.receiving(sub, now):
        return {
            "title": "Cancelled",
            "text": f"No more payments. Your alerts go on until {until}, the end of the time you"
            " paid for.",
            "ended": False,
        }
    if sub.status == "cancelled" or not bid_alerts.receiving(sub, now):
        return {"title": "Ended", "text": "These bid alerts have ended.", "ended": True}
    return {
        "title": "On",
        "text": f"You get an email when a new bid matches. PayPal takes {price} a month"
        + (f"; the next payment is due on {until}." if until else "."),
        "ended": False,
    }


def _manage_results() -> dict[str, tuple[bool, str]]:
    return {
        "saved": (True, "Saved. The next alert email uses these trades and words."),
        "cancelled": (True, "Your subscription is cancelled. PayPal takes no more payments."),
        "invalid": (False, "That doesn't look like an email address. Nothing was changed."),
        "error": (False, "PayPal could not be reached just now. Please try again soon."),
    }


@app.get("/bids/alerts/manage", response_class=HTMLResponse)
def bid_alerts_manage(
    request: Request, t: str = Query("", max_length=64), result: str = Query("", max_length=10)
):
    """A subscriber's own page. PayPal sends a new subscriber back here: a
    pending subscription is checked with PayPal at once."""
    if not bid_alerts.is_on(settings):
        raise HTTPException(404)
    sub = bid_alerts.by_token(t)
    if sub is None:
        raise HTTPException(404)
    now = datetime.now(timezone.utc)
    found = _manage_results().get(result)
    shown = {"ok": found[0], "text": found[1]} if found else None
    if sub.status == "pending" and sub.paypal_id:
        try:
            if bid_alerts.check(settings, sub.id, now) == "activated":
                shown = {
                    "ok": True,
                    "text": "Thank you, the payment is done. Your bid alerts are on, and a"
                    " welcome email with the link to this page is on its way.",
                }
        except paypal.PayPalError as exc:
            log.warning("bid alerts: could not check a new subscription with PayPal: %s", exc)
        sub = bid_alerts.by_token(t)
    return _private(
        render(
            request,
            "bid_alerts_manage.html",
            title="Your bid alerts",
            robots="noindex, nofollow",
            breadcrumbs=[("Bids and RFPs", "/bids"), ("Bid alerts", "/bids/alerts")],
            active_nav="bids",
            count_view=False,
            sub=sub,
            state=_state(sub, now),
            paid_until=fmt_date_long(sub.paid_until) if sub.paid_until else "",
            trades=bid_alerts.TRADES,
            result=shown,
        )
    )


@app.post("/bids/alerts/manage")
async def bid_alerts_save(request: Request):
    """The form on a subscriber's own page: new address, trades and words."""
    form = await _bid_alert_form(request)
    if isinstance(form, Response):
        return form
    token = _field(form, "t")
    sub = await run_in_threadpool(bid_alerts.by_token, token)
    if sub is None:
        raise HTTPException(404)
    choices = _choices(form)
    if choices is None:
        return _see_other(f"/bids/alerts/manage?t={token}&result=invalid")
    await run_in_threadpool(bid_alerts.update_choices, sub.id, *choices)
    return _see_other(f"/bids/alerts/manage?t={token}&result=saved")


@app.post("/bids/alerts/cancel")
async def bid_alerts_cancel(request: Request):
    """Cancel a subscription: PayPal stops the payments."""
    form = await _bid_alert_form(request)
    if isinstance(form, Response):
        return form
    token = _field(form, "t")
    sub = await run_in_threadpool(bid_alerts.by_token, token)
    if sub is None:
        raise HTTPException(404)
    try:
        await run_in_threadpool(bid_alerts.cancel, settings, sub.id)
    except paypal.PayPalError as exc:
        log.warning("bid alerts: could not cancel a subscription with PayPal: %s", exc)
        return _see_other(f"/bids/alerts/manage?t={token}&result=error")
    return _see_other(f"/bids/alerts/manage?t={token}&result=cancelled")


@app.post("/bids/alerts/link")
async def bid_alerts_link(request: Request):
    """Email the links of an address's bid alerts again. The answer is the
    same whether or not the address has any, so the form tells no one who
    subscribes."""
    form = await _bid_alert_form(request)
    if isinstance(form, Response):
        return form
    if _field(form, "website"):
        return _see_other("/bids/alerts?result=sent#lost")
    email = _field(form, "email")
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        return _see_other("/bids/alerts?result=invalid#lost")
    if not bid_alert_limit.allow(request.client.host if request.client else ""):
        return _see_other("/bids/alerts?result=busy#lost")
    try:
        await run_in_threadpool(bid_alerts.send_links, settings, email)
    except mail.MailError as exc:
        log.warning("bid alerts: could not email a page link: %s", exc)
        return _see_other("/bids/alerts?result=error#lost")
    return _see_other("/bids/alerts?result=sent#lost")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # For browsers and search engines that ask for /favicon.ico rather than
    # reading the page's icon link.
    return FileResponse(
        STATIC_DIR / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return render(request, "404.html", status_code=404, title="Not found", robots="noindex")
