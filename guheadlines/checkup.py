"""The weekly health check in Discord (HEALTH_CHECK_DAY, at REPORT_TIME):
what on the site needs looking at.

  - Broken pages: the worker opens every page in the sitemap (and the search,
    the calendar feed, robots.txt and a few more) and lists each one that
    does not open: a server error, "not found", or no answer. It also lists
    the kinds of pages where visitors got a server error during the week.
  - Slow pages: the pages that took longer than SLOW_PAGE_SECONDS to make,
    twice in a row, and the kinds of pages that were often that slow for
    visitors during the week.
  - Photos: news sites whose new stories stopped coming with a photo, and
    those whose photos could not be saved on the server.
  - Sources: news sites, job boards and event calendars that do not work,
    and news sites that went quiet (alerts.py posts each one as it happens;
    this is the list of those still not fixed).
  - Uptime: how many of the worker's checks of the site, every 5 minutes,
    the site answered.

The worker reaches the website inside Docker's own network (WEBSITE_ADDRESS,
http://web:8000), so the times are how long the server took to make each
page, without the trip over the internet. Its visits are not counted as
page views. The counts of what visitors got come from the website itself
(web/stats.py: the "served:", "server-error:" and "slow:" rows of
page_views, by the page's pattern, such as /events/{event_id}).

reports.py posts it with the other reports (row "report:health" in the
alerts table), and the worker's "uptime" thread (cli._uptime_loop) makes
the checks every 5 minutes. `guheadlines health-check` runs it now.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from .alerts import Alert, _plain
from .config import ConfigError, load_config, load_events_config, load_jobs_config
from .db import session_scope
from .health import FAILING, QUIET, feed_health, news_health
from .models import (
    PAGE_ERRORS,
    PAGES_SERVED,
    SLOW_PAGES,
    UPTIME_DOWN,
    UPTIME_UP,
    Article,
    PageViews,
    Source,
)
from .scraper.text import GUAM_TZ
from .settings import Settings
from .web.stats import CHECKUP_AGENT

log = logging.getLogger(__name__)

# The worker checks that the site answers this often (seconds).
UPTIME_EVERY = 300
# Pages the check opens besides those in the sitemap.
EXTRA_PAGES = ("/search?q=guam", "/events.ics", "/robots.txt", "/llms.txt")
# Of the pages that differ only in numbers (archive days, event pages...),
# the check opens the newest this many.
PAGES_PER_KIND = 20
# The check stops after this long, and says how many pages were left.
CHECK_BUDGET = 600.0
PAGE_TIMEOUT = 30.0
# At most this many lines in each list; then "and N more".
MAX_LINES = 6
# Kinds of pages that were slow for visitors: at least this many times, and
# at least this share of their visits (a page made just after an update is
# often slow once).
SLOW_AT_LEAST = 3
SLOW_SHARE = 0.01
# A news site's photos have stopped when, of at least PHOTO_WEEK new stories
# this week, the share with a photo is less than half of what it was in the
# 4 weeks before (when at least half of at least PHOTO_BEFORE stories had one).
PHOTO_WEEK = 3
PHOTO_BEFORE = 5
BEFORE = timedelta(days=28)
# Below this share of answered checks, the uptime counts as a problem.
UPTIME_GOOD = 0.99


def is_on(settings: Settings) -> bool:
    return bool(settings.discord_webhook_url) and settings.health_check_day is not None


def _guam_midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=GUAM_TZ)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _seconds(value: float) -> str:
    return "1 second" if f"{value:.1f}" == "1.0" else f"{value:.1f} seconds"


def _took(seconds: float) -> str:
    """How long the whole check took: "less than a second", "42 seconds"."""
    if seconds < 1:
        return "less than a second"
    return "1 second" if round(seconds) == 1 else f"{round(seconds):,} seconds"


def _limited(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LINES:
        return lines
    return [*lines[: MAX_LINES - 1], f"• and {len(lines) - MAX_LINES + 1} more"]


def pattern_text(pattern: str) -> str:
    """/events/{event_id} -> /events/…"""
    return re.sub(r"\{[^}]*\}", "…", pattern)


# ------------------------------------------------------------- the pages


@dataclass
class PageResult:
    path: str
    # The answer's status (200, 404, 500...); None when there was no answer.
    status: int | None
    seconds: float
    error: str = ""

    @property
    def broken(self) -> bool:
        return self.status is None or self.status >= 400

    def problem(self) -> str:
        if self.status is None:
            return f"no answer ({self.error})"
        if self.status == 404:
            return "not found (404)"
        if self.status >= 500:
            return f"server error ({self.status})"
        return f"error {self.status}"


@dataclass
class PageCheck:
    checked: int = 0
    seconds: float = 0.0
    broken: list[PageResult] = field(default_factory=list)
    slow: list[PageResult] = field(default_factory=list)
    # Pages not opened because the check ran out of time.
    left: int = 0


def sitemap_paths(xml: str) -> list[str]:
    """The paths (with any query) of the pages in a sitemap. Of the pages that
    differ only in numbers (/archive/2026/11/26, /events/123), only the
    newest PAGES_PER_KIND."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    by_kind: dict[str, list[str]] = {}
    for loc in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
        parts = urlsplit((loc.text or "").strip())
        if not parts.path.startswith("/"):
            continue
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        by_kind.setdefault(re.sub(r"\d+", "#", path), []).append(path)
    found: list[str] = []
    for kind, paths in by_kind.items():
        if "#" in kind:
            paths = sorted(
                paths, key=lambda p: [int(n) for n in re.findall(r"\d+", p)], reverse=True
            )
        found.extend(dict.fromkeys(paths[:PAGES_PER_KIND] if "#" in kind else paths))
    return found


def extra_paths(settings: Settings, paths: list[str]) -> list[str]:
    extra = list(EXTRA_PAGES)
    if settings.newsletter_days:
        extra.append("/feed.xml")
    if "/advertise" in paths:
        extra.append("/advertise/media-kit.pdf")
    return [path for path in extra if path not in paths]


def _open(client: httpx.Client, path: str) -> PageResult:
    started = time.perf_counter()
    try:
        response = client.get(path)
    except httpx.HTTPError as exc:
        return PageResult(path, None, time.perf_counter() - started, type(exc).__name__)
    return PageResult(path, response.status_code, time.perf_counter() - started)


def check_pages(settings: Settings, transport: httpx.BaseTransport | None = None) -> PageCheck:
    """Open each page of the site, one after the other, and note those that
    are broken or slow. A slow page is opened once more, and counts as slow
    only when it is slow both times (the faster time is given)."""
    found = PageCheck()
    started = time.perf_counter()
    with httpx.Client(
        base_url=settings.website_address,
        timeout=PAGE_TIMEOUT,
        transport=transport,
        headers={"User-Agent": CHECKUP_AGENT},
        follow_redirects=False,
    ) as client:
        try:
            response = client.get("/sitemap.xml")
            response.raise_for_status()
            paths = sitemap_paths(response.text)
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            found.broken.append(PageResult("/sitemap.xml", status, 0.0, type(exc).__name__))
            paths = []
        found.checked = 1
        paths = (paths or ["/"]) + extra_paths(settings, paths)
        for number, path in enumerate(paths):
            if time.perf_counter() - started > CHECK_BUDGET:
                found.left = len(paths) - number
                break
            result = _open(client, path)
            if not result.broken and result.seconds > settings.slow_page_seconds:
                again = _open(client, path)
                if again.broken or again.seconds < result.seconds:
                    result = again
            found.checked += 1
            if result.broken:
                found.broken.append(result)
            elif result.seconds > settings.slow_page_seconds:
                found.slow.append(result)
    found.seconds = time.perf_counter() - started
    found.slow.sort(key=lambda r: -r.seconds)
    return found


# ------------------------------------------------------ what visitors got


@dataclass
class Visits:
    # {page pattern: count} of the week.
    served: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    slow: Counter = field(default_factory=Counter)
    up: int = 0
    down: int = 0

    def error_lines(self) -> list[str]:
        return [
            f"• {pattern_text(p)}: {_plural(n, 'time')}"
            + (f" (of {self.served[p]:,})" if self.served[p] >= n else "")
            for p, n in sorted(self.errors.items(), key=lambda item: (-item[1], item[0]))
        ]

    def slow_lines(self) -> list[str]:
        often = [
            (p, n)
            for p, n in self.slow.items()
            if n >= SLOW_AT_LEAST and n >= SLOW_SHARE * max(self.served[p], n)
        ]
        return [
            f"• {pattern_text(p)}: {n:,} of {max(self.served[p], n):,} times"
            for p, n in sorted(often, key=lambda item: (-item[1], item[0]))
        ]


def visits(session, first: date, end: date) -> Visits:
    """The counts of the Guam days from `first` up to (not with) `end`."""
    found = Visits()
    rows = session.execute(
        select(PageViews.path, func.sum(PageViews.views))
        .where(
            PageViews.day >= first,
            PageViews.day < end,
            or_(
                PageViews.path.startswith(PAGES_SERVED),
                PageViews.path.startswith(PAGE_ERRORS),
                PageViews.path.startswith(SLOW_PAGES),
                PageViews.path.in_([UPTIME_UP, UPTIME_DOWN]),
            ),
        )
        .group_by(PageViews.path)
    )
    for key, n in rows:
        n = int(n)
        if key == UPTIME_UP:
            found.up = n
        elif key == UPTIME_DOWN:
            found.down = n
        else:
            for prefix, counter in (
                (PAGES_SERVED, found.served),
                (PAGE_ERRORS, found.errors),
                (SLOW_PAGES, found.slow),
            ):
                if key.startswith(prefix):
                    counter[key.removeprefix(prefix)] += n
    return found


# ---------------------------------------------------------------- photos


def photo_lines(
    session, slugs: list[str], start: datetime, end: datetime, saved: bool
) -> list[str]:
    """News sites whose new stories (first found from `start` to `end`)
    stopped coming with a photo, compared with the 4 weeks before; and, when
    photos are saved on the server (`saved`), those whose photos were found
    but could not be saved."""
    if not slugs:
        return []
    week = Article.first_seen_at >= start
    before = Article.first_seen_at < start
    photo = Article.image_url.is_not(None)
    stored = Article.image_path.is_not(None)
    rows = session.execute(
        select(
            Source.name,
            func.count().filter(week),
            func.count().filter(week & photo),
            func.count().filter(week & photo & stored),
            func.count().filter(before),
            func.count().filter(before & photo),
        )
        .join(Article, Article.source_id == Source.id)
        .where(
            Source.slug.in_(slugs),
            Article.first_seen_at >= start - BEFORE,
            Article.first_seen_at < end,
        )
        .group_by(Source.id, Source.name)
        .order_by(Source.name)
    ).all()
    lines = []
    for name, n, with_photo, with_copy, n_before, photo_before in rows:
        was = photo_before / n_before if n_before else 0
        if n >= PHOTO_WEEK and n_before >= PHOTO_BEFORE and was >= 0.5 and with_photo / n < was / 2:
            had = f"({photo_before:,} of {n_before:,} had one in the 4 weeks before)"
            if with_photo:
                lines.append(f"• {name}: a photo on {with_photo:,} of its {n:,} new stories {had}")
            else:
                lines.append(f"• {name}: none of its {n:,} new stories had a photo {had}")
        elif saved and with_photo >= PHOTO_WEEK and with_copy < with_photo / 2:
            lines.append(
                f"• {name}: {with_photo - with_copy:,} of {with_photo:,} photos could not be saved "
                "on the server (the site shows the news site's own copy)"
            )
    return lines


# --------------------------------------------------------------- sources


def _since(moment: datetime, now: datetime) -> str:
    """Nov 23 (or Jan 10, 2025 in another year), in Guam time."""
    local = moment.astimezone(GUAM_TZ)
    year = "" if local.year == now.astimezone(GUAM_TZ).year else f", {local.year}"
    return f"{local:%b} {local.day}{year}"


def source_lines(
    session, news: list[str], boards: list[str], calendars: list[str], now: datetime
) -> list[str]:
    lines = []
    for h in news_health(session, news, now):
        if h.status == FAILING:
            since = f"since {_since(h.last_success_at, now)}" if h.last_success_at else "yet"
            lines.append(f"• The news site {h.name} has not worked {since}")
        elif h.status == QUIET and h.quiet_since:
            lines.append(f"• {h.name} has had no new story since {_since(h.quiet_since, now)}")
    for h in feed_health(session, boards, calendars):
        if h.status == FAILING:
            since = f"since {_since(h.last_success_at, now)}" if h.last_success_at else "yet"
            lines.append(f"• The {h.kind} {h.name} has not worked {since}")
    return lines


def _slugs(settings: Settings) -> tuple[list[str], list[str], list[str]]:
    """The news sites, job boards and event calendars that the worker reads."""
    try:
        news = [s.slug for s in load_config(settings.config_dir).sources if s.collected]
    except ConfigError:
        news = []
    try:
        boards = [b.slug for b in load_jobs_config(settings.config_dir) if b.collected]
    except ConfigError:
        boards = []
    try:
        calendars = [c.slug for c in load_events_config(settings.config_dir) if c.collected]
    except ConfigError:
        calendars = []
    return news, boards, calendars


# ---------------------------------------------------------------- uptime

_pending: Counter = Counter()
_pending_lock = threading.Lock()


def ping(settings: Settings, transport: httpx.BaseTransport | None = None) -> bool:
    """Ask the website whether it works (/healthz), and count the answer for
    the Guam day. A count that can't be saved (the database is down) is kept
    for the next time."""
    try:
        with httpx.Client(timeout=10, transport=transport) as client:
            response = client.get(
                f"{settings.website_address}/healthz", headers={"User-Agent": CHECKUP_AGENT}
            )
        up = response.status_code == 200 and response.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError, AttributeError):
        up = False
    with _pending_lock:
        _pending[(datetime.now(GUAM_TZ).date(), UPTIME_UP if up else UPTIME_DOWN)] += 1
        counts = dict(_pending)
        _pending.clear()
    try:
        with session_scope() as session:
            stmt = insert(PageViews).values(
                [{"day": day, "path": key, "views": n} for (day, key), n in counts.items()]
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[PageViews.day, PageViews.path],
                    set_={"views": PageViews.views + stmt.excluded.views},
                )
            )
    except Exception:
        log.warning("could not save the uptime check; trying again next time", exc_info=True)
        with _pending_lock:
            _pending.update(counts)
    return up


def uptime_line(found: Visits) -> tuple[str | None, bool]:
    """(the line, whether it is a problem)."""
    total = found.up + found.down
    if not total:
        return None, False
    share = found.up / total
    percent = "100" if found.up == total else f"{min(share * 100, 99.9):.1f}"
    line = (
        f"The site answered {found.up:,} of the worker's {total:,} checks "
        f"every 5 minutes ({percent}%)."
    )
    return line, share < UPTIME_GOOD


# ----------------------------------------------------------------- report


def health_alert(
    settings: Settings,
    due: datetime,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> Alert:
    """The health check of the 7 Guam days before the day of `due`; the
    pages are opened now."""
    end = due.astimezone(GUAM_TZ).date()
    first = end - timedelta(days=7)
    now = now or datetime.now(timezone.utc)
    news, boards, calendars = _slugs(settings)
    pages = check_pages(settings, transport)
    with session_scope() as session:
        seen = visits(session, first, end)
        photos = photo_lines(
            session, news, _guam_midnight(first), _guam_midnight(end), settings.store_images
        )
        sources = source_lines(session, news, boards, calendars, now)

    error_lines, often_slow_lines = seen.error_lines(), seen.slow_lines()
    uptime, uptime_bad = uptime_line(seen)
    # Each line is one thing to look at (sources that don't work had their
    # own alerts already, so they are listed, but not counted).
    problems = (
        len(pages.broken)
        + len(error_lines)
        + len(pages.slow)
        + len(often_slow_lines)
        + len(photos)
        + (1 if uptime_bad else 0)
    )
    broken = _limited([f"• {_plain(r.path)}: {r.problem()}" for r in pages.broken])
    errors = _limited(error_lines)
    slow = _limited([f"• {_plain(r.path)}: {_seconds(r.seconds)}" for r in pages.slow])
    often_slow = _limited(often_slow_lines)

    parts = []
    if broken or errors:
        part = ["**Broken pages**", *broken]
        if errors:
            part += ["Visitors got a server error on:", *errors]
            part.append("What went wrong is in the website's log: docker compose logs web")
        parts.append("\n".join(part))
    if slow or often_slow:
        part = [f"**Slow pages** (longer than {_seconds(settings.slow_page_seconds)} to make)"]
        part += slow
        if often_slow:
            part += ["Often slow for visitors:", *often_slow]
        parts.append("\n".join(part))
    if photos:
        parts.append("\n".join(["**Photos**", *_limited(photos)]))
    if sources:
        parts.append("\n".join(["**Not working or quiet**", *_limited(sources)]))
    end_part = []
    if uptime:
        end_part.append(uptime)
    checked = f"Opened {_plural(pages.checked, 'page')} in {_took(pages.seconds)}"
    if pages.left:
        checked += f"; stopped there, {_plural(pages.left, 'page')} not opened"
    elif not pages.broken and not pages.slow:
        checked += f": all opened, none slower than {_seconds(settings.slow_page_seconds)}"
    end_part.append(checked + ".")
    if not photos:
        end_part.append("Every news site's photos came in as usual.")
    parts.append("\n".join(end_part))

    if problems:
        title = f"Weekly health check: {_plural(problems, 'thing')} to look at"
    else:
        title = "Weekly health check: all good"
    # A phone notification only when something needs looking at.
    return Alert(title=title, message="\n\n".join(parts), emoji="🩺", quiet=not problems)
