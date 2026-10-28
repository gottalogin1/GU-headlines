"""The website: server-rendered pages (fast, no JavaScript needed, bookmarkable URLs)."""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import select
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import AppConfig, ConfigError, JobSourceConfig, load_config, load_jobs_config
from ..db import session_scope
from ..models import CATCH_UP_NOTE, Source
from ..scraper.text import GUAM_TZ
from ..settings import get_settings
from . import queries
from .queries import MARK_END, MARK_START, Filters, JobFilters

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

    def __init__(self, *args, cache_control: Callable[[Scope], str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cache_control = cache_control

    def file_response(self, full_path, stat_result, scope: Scope, status_code: int = 200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = self._cache_control(scope)
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

SECURITY_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class SecurityHeadersMiddleware:
    """Adds SECURITY_HEADERS to every response (pages, files and errors alike)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in SECURITY_HEADERS if n not in names]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = FastAPI(title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None)
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
    CachedStaticFiles(directory=settings.media_dir, cache_control=_media_cache),
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


class _JobsConfigCache:
    """jobs.yaml, re-read at most once a minute (a broken edit keeps the last good copy;
    a broken file at startup means no boards rather than a broken site)."""

    def __init__(self) -> None:
        self._boards: list[JobSourceConfig] = []
        self._loaded: float | None = None

    def get(self) -> list[JobSourceConfig]:
        if self._loaded is None or time.monotonic() - self._loaded > CONFIG_RELOAD_SECONDS:
            try:
                self._boards = load_jobs_config(settings.config_dir)
            except ConfigError as exc:
                log.error("jobs config error: %s", exc)
            self._loaded = time.monotonic()
        return self._boards


jobs_config_cache = _JobsConfigCache()


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


# --------------------------------------------------------- template helpers


def to_local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(GUAM_TZ) if dt else None


def fmt_datetime(dt: datetime | None) -> str:
    local = to_local(dt)
    if not local:
        return ""
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {local.year}, {hour}:{local:%M} {local:%p} ChST"


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
    prefix = "Today" if delta == 0 else "Yesterday" if delta == 1 else None
    long = fmt_date_long(day)
    return f"{prefix} · {long}" if prefix else long


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


def domain(url: str | None) -> str:
    """https://www.guampdn.com/news/... -> guampdn.com"""
    host = (urlsplit(url or "").hostname or "").lower()
    return host.removeprefix("www.")


templates.env.filters.update(
    fmt_datetime=fmt_datetime,
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
    months=MONTHS,
    static_url=static_url,
    theme_script=Markup(THEME_SCRIPT),
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
        # only_in_topic sources (r/guam) are reached through their topic.
        left_out = {s.slug for s in config.uncollectable_sources} | set(config.hidden_sources())
        facets["sources"] = [s for s in facets["sources"] if s["slug"] not in left_out]
        return {
            "facets_week": facets,
            "last_updated": last_updated,
            "uncollectable": config.uncollectable_sources,
        }

    context.setdefault("title", None)
    response = templates.TemplateResponse(
        request,
        template,
        {
            "categories": config.categories,
            "category_names": {c.slug: c.name for c in config.categories},
            "sidebar": sidebar_cache.get("sidebar", sidebar),
            "page_url": lambda **kw: page_url(request, **kw),
            **context,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "public, max-age=120"
    return response


# ------------------------------------------------------------------ routes


def _listing(request: Request, template: str, filters: Filters, page: int, **context):
    """A page of stories grouped by day (home, topic, source and archive day pages)."""
    with session_scope() as session:
        result = queries.list_articles(session, filters, page, settings.page_size)
    if page > 1 and not result.items:
        raise HTTPException(404)
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request, page: int = Query(1, ge=1, le=10000)):
    return _listing(
        request, "index.html", Filters(hide_sources=_hidden()), page, active_nav="latest"
    )


@app.get("/category/{slug}", response_class=HTMLResponse)
def category_page(request: Request, slug: str, page: int = Query(1, ge=1, le=10000)):
    category = config_cache.get().category(slug)
    if not category:
        raise HTTPException(404)
    return _listing(
        request,
        "index.html",
        Filters(category=slug, hide_sources=_hidden(slug)),
        page,
        active_nav=slug,
        heading=category.name,
        subheading=category.description,
        title=category.name,
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
        title=source.name,
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
    )


@app.get("/archive", response_class=HTMLResponse)
def archive(request: Request):
    with session_scope() as session:
        years = queries.archive_months(session, list(_hidden()))
    return render(request, "archive.html", years=years, title="Archive", active_nav="archive")


@app.get("/archive/{year}/{month}", response_class=HTMLResponse)
def archive_month(request: Request, year: int, month: int):
    if not (1990 <= year <= 2200 and 1 <= month <= 12):
        raise HTTPException(404)
    with session_scope() as session:
        days = queries.archive_days(session, year, month, list(_hidden()))
    return render(
        request,
        "archive_month.html",
        year=year,
        month=month,
        days=days,
        title=f"{MONTHS[month - 1]} {year}",
        active_nav="archive",
    )


@app.get("/archive/{year}/{month}/{day}", response_class=HTMLResponse)
def archive_day(request: Request, year: int, month: int, day: int, page: int = Query(1, ge=1)):
    try:
        the_day = date(year, month, day)
    except ValueError:
        raise HTTPException(404) from None
    filters = Filters(date_from=the_day, date_to=the_day, hide_sources=_hidden())
    return _listing(
        request,
        "index.html",
        filters,
        page,
        active_nav="archive",
        heading=fmt_date_long(the_day),
        title=fmt_date_long(the_day),
        archive_day=the_day,
        prev_day=the_day - timedelta(days=1),
        next_day=the_day + timedelta(days=1),
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
    return render(
        request,
        "jobs.html",
        result=result,
        job_filters=job_filters,
        boards=[b for b in boards if b["board"].slug in collected],
        uncollectable=[b for b in boards_config if b.enabled and b.cannot_scrape],
        title="Jobs on Guam",
        active_nav="jobs",
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
def robots():
    return "User-agent: *\nDisallow: /search\nDisallow: /status\n"


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return render(request, "404.html", status_code=404, title="Not found")
