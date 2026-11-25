"""The website: server-rendered pages (fast, no JavaScript needed, bookmarkable URLs)."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import select
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import (
    AppConfig,
    CalendarConfig,
    ConfigError,
    load_config,
    load_events_config,
    load_jobs_config,
)
from ..db import session_scope
from ..models import CATCH_UP_NOTE, Article, Source
from ..scraper.text import GUAM_TZ
from ..settings import get_settings
from ..weather import (
    GUAM_RADAR_URL,
    JTWC_URL,
    MARINE_PAGE,
    NWS_GUAM_URL,
    SURF_PAGE,
    for_page,
    weather_page,
)
from . import queries, seo, stats
from .queries import MARK_END, MARK_START, EventFilters, Filters, JobFilters

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Counts that this worker has not written to the database yet.
    stats.counts.flush()


app = FastAPI(
    title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)
app.add_middleware(RejectNulMiddleware)
# Pages are mostly text and shrink to a fraction of their size; photos are
# already compressed and left alone.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
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
    """jobs.yaml or events.yaml, re-read at most once a minute (a broken edit
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
# updates it every few minutes.
weather_cache = _TTLCache(ttl=60)


def weather_reports() -> dict[str, tuple]:
    """The saved weather (see weather.py)."""

    def load():
        with session_scope() as session:
            return queries.weather_reports(session)

    return weather_cache.get("weather", load)


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


def domain(url: str | None) -> str:
    """https://www.guampdn.com/news/... -> guampdn.com"""
    host = (urlsplit(url or "").hostname or "").lower()
    return host.removeprefix("www.")


templates.env.filters.update(
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
templates.env.globals.update(
    settings=settings,
    event_time=event_time,
    months=MONTHS,
    static_url=static_url,
    story_link=story_link,
    theme_script=Markup(THEME_SCRIPT),
    nws_guam_url=NWS_GUAM_URL,
    jtwc_url=JTWC_URL,
    guam_radar_url=GUAM_RADAR_URL,
    surf_page_url=SURF_PAGE,
    marine_page_url=MARINE_PAGE,
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


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    """Render a page with what every page needs: the sections, the sidebar and
    the pagination helper."""
    config = config_cache.get()

    def sidebar():
        with session_scope() as session:
            week_ago = datetime.now(timezone.utc) - timedelta(days=7)
            facets = queries.facet_counts(session, week_ago)
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
        }

    context.setdefault("title", None)
    count_view = context.pop("count_view", True)
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
        years = queries.archive_months(session, list(_hidden()), list(_separate()))
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
        days = queries.archive_days(session, year, month, list(_hidden()), list(_separate()))
        years = queries.archive_months(session, list(_hidden()), list(_separate()))
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
        prev_day, next_day = queries.neighbor_days(session, the_day, list(hidden), list(separate))
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
    if page > 1 and not result.items:
        raise HTTPException(404)
    # Boards that can't be collected are listed apart, with the reason.
    collected = {b.slug for b in boards_config if b.collected}
    boards = [b for b in boards if b["board"].slug in collected]
    return render(
        request,
        "jobs.html",
        result=result,
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
    page: int = Query(1, ge=1, le=10000),
):
    calendars_config: list[CalendarConfig] = events_config_cache.get()
    event_filters = EventFilters(q=q.strip(), calendar=calendar.strip() or None)
    today = datetime.now(GUAM_TZ).date()
    since = datetime.combine(today, datetime.min.time(), tzinfo=GUAM_TZ)
    with session_scope() as session:
        result = queries.list_events(session, event_filters, page, settings.page_size, since)
        calendars = queries.event_calendars(session, since)
    if page > 1 and not result.items:
        raise HTTPException(404)
    # Calendars that can't be collected are listed apart, with the reason.
    collected = {c.slug for c in calendars_config if c.collected}
    calendars = [c for c in calendars if c["calendar"].slug in collected]
    return render(
        request,
        "events.html",
        result=result,
        groups=group_events(result.items, today),
        event_filters=event_filters,
        calendars=calendars,
        uncollectable=[c for c in calendars_config if c.enabled and c.cannot_scrape],
        title="Events on Guam",
        description=_events_description([c["calendar"].name for c in calendars]),
        active_nav="events",
        canonical={"calendar": event_filters.calendar, "page": page},
        # A word search is a search page; a calendar's own list is worth finding.
        robots="noindex, follow" if event_filters.q else None,
        breadcrumbs=[("Events", None)],
        show_ad=True,
    )


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


@app.get("/go/{article_id}")
def go(request: Request, article_id: int):
    """Count a click on a story's link, then send the reader on to the story.
    Only a stored story's own address is used, so nobody can make a link here
    that leads to another site."""
    if not 1 <= article_id < 2**63:
        raise HTTPException(404)
    with session_scope() as session:
        link = safe_url(session.scalar(select(Article.url).where(Article.id == article_id)))
    if not link:
        raise HTTPException(404)
    if settings.stats_password and stats.is_reader(request):
        stats.counts.click(article_id)
    return RedirectResponse(
        link, status_code=302, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
    )


STATS_PERIODS = {1: "Today", 7: "7 days", 30: "30 days", 90: "90 days", 365: "1 year"}


def _stats_password_ok(request: Request) -> bool:
    """The browser's own login box (HTTP Basic): any user name, and the
    password in STATS_PASSWORD."""
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        login = base64.b64decode(encoded, validate=True).decode("utf-8")
    except ValueError:  # not base64, or not UTF-8
        return False
    password = login.partition(":")[2]
    return secrets.compare_digest(password.encode(), settings.stats_password.encode())


@app.get("/stats", response_class=HTMLResponse)
def visitor_stats(request: Request, days: int = Query(7, ge=1, le=365)):
    if not settings.stats_password:
        raise HTTPException(404)
    if not _stats_password_ok(request):
        return PlainTextResponse(
            "This page needs a password.",
            status_code=401,
            headers={
                "WWW-Authenticate": 'Basic realm="Visitor stats", charset="UTF-8"',
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex",
            },
        )
    if days not in STATS_PERIODS:
        days = 7
    # This worker's newest counts. The other worker writes its own within 30 seconds.
    stats.counts.flush()
    config = config_cache.get()
    since = datetime.now(GUAM_TZ).date() - timedelta(days=days - 1)
    with session_scope() as session:
        data = queries.visitor_stats(session, since)
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
        "/status": "Sources",
        "/search": "Search",
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
        topics=sorted(topics, key=lambda t: (-t["clicks"], -t["views"])),
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


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
        with session_scope() as session:
            return queries.sitemap_data(session, hidden, separate)

    news_sources = [s.slug for s in config.sources if not s.cannot_scrape and s.slug not in hidden]
    entries = seo.sitemap_entries(
        request,
        sitemap_cache.get("sitemap", data),
        config.categories,
        news_sources,
        weather=bool(settings.weather_interval_minutes),
    )
    return Response(
        seo.sitemap_xml(entries),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=600"},
    )


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
