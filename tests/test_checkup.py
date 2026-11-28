"""The weekly health check in Discord (checkup.py), and the counts the website
keeps for it (web/stats.py, web/app.py PageWatchMiddleware)."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import count

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from guheadlines import checkup, reports
from guheadlines.db import session_scope
from guheadlines.models import (
    PAGE_ERRORS,
    PAGES_SERVED,
    SLOW_PAGES,
    UPTIME_DOWN,
    UPTIME_UP,
    AlertState,
    Article,
    PageViews,
    Source,
)
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import get_settings, load_settings
from guheadlines.web import stats

WEBHOOK = "https://discord.com/api/webhooks/1234567890/abc-DEF_123"
# Mon Nov 30, 2026, 8:00 AM in Guam: the health check of Nov 23 to 29 is due.
MONDAY = datetime(2026, 11, 30, 8, 0, tzinfo=GUAM_TZ)
WEEK = datetime(2026, 11, 26, 12, 0, tzinfo=GUAM_TZ)  # in that week
FIRST, END = date(2026, 11, 23), date(2026, 11, 30)


def _settings(**changes):
    base = replace(
        get_settings(),
        discord_webhook_url=WEBHOOK,
        paypal_client_id="",
        paypal_client_secret="",
        weekly_report_day=None,
        health_check_day=0,
        slow_page_seconds=1.0,
        website_address="http://web:8000",
        newsletter_days=(1, 4),
        store_images=True,
    )
    return replace(base, **changes)


def _sitemap(*paths: str) -> str:
    urls = "".join(f"<url><loc>https://guamheadlines.com{p}</loc></url>" for p in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    )


class Site:
    """The website (as the worker reaches it) and Discord."""

    def __init__(self, sitemap=None, pages=None, slow=(), down=False, healthz=None):
        self.sitemap = sitemap if sitemap is not None else _sitemap("/", "/events", "/jobs")
        # {path: status}; every other page answers 200.
        self.pages = pages or {}
        self.slow = set(slow)
        self.down = down
        self.healthz = healthz or {"status": "ok"}
        self.opened: list[str] = []
        self.agents: set[str] = set()
        self.discord: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "discord.com":
            self.discord.append(json.loads(request.content))
            return httpx.Response(204)
        assert request.url.host == "web" and request.url.port == 8000
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.raw_path.decode()
        self.opened.append(path)
        self.agents.add(request.headers["user-agent"])
        if path == "/healthz":
            return httpx.Response(200, json=self.healthz)
        if path == "/sitemap.xml":
            if isinstance(self.sitemap, int):
                return httpx.Response(self.sitemap)
            return httpx.Response(200, text=self.sitemap)
        if path in self.slow:
            time.sleep(0.06)
        if path == "/hangs":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(self.pages.get(path, 200), text="<html></html>")


# ------------------------------------------------------------------ settings


def test_health_check_settings(monkeypatch):
    for name in ("HEALTH_CHECK_DAY", "SLOW_PAGE_SECONDS", "WEBSITE_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.health_check_day == 0  # Monday
    assert settings.slow_page_seconds == 1.0
    assert settings.website_address == "http://web:8000"
    monkeypatch.setenv("HEALTH_CHECK_DAY", "sun")
    monkeypatch.setenv("SLOW_PAGE_SECONDS", "2.5")
    monkeypatch.setenv("WEBSITE_ADDRESS", " http://127.0.0.1:8000/ ")
    settings = load_settings()
    assert settings.health_check_day == 6
    assert settings.slow_page_seconds == 2.5
    assert settings.website_address == "http://127.0.0.1:8000"
    monkeypatch.setenv("HEALTH_CHECK_DAY", "off")
    assert load_settings().health_check_day is None
    for value, expected in (("0", 0.1), ("500", 60.0), ("soon", 1.0), ("nan", 1.0)):
        monkeypatch.setenv("SLOW_PAGE_SECONDS", value)
        assert load_settings().slow_page_seconds == expected


def test_health_check_needs_discord_and_a_day():
    assert checkup.is_on(_settings())
    assert not checkup.is_on(_settings(discord_webhook_url=""))
    assert not checkup.is_on(_settings(health_check_day=None))
    assert reports._jobs(_settings()) == [(reports.HEALTH_KEY, (0,))]
    assert reports.describe(_settings()) == "a health check on Mondays, at 8:00 AM (Guam time)"


# ------------------------------------------------------------------- pages


def test_sitemap_paths_keep_the_newest_of_each_kind(monkeypatch):
    monkeypatch.setattr(checkup, "PAGES_PER_KIND", 2)
    xml = _sitemap(
        "/",
        "/source/kuam",
        "/source/postguam",
        "/archive/2026/11/9",
        "/archive/2026/11/26",
        "/archive/2026/10/31",
        "/events/12",
        "/events/140",
        "/events/99",
        "/guides/packs/pcs",
        "/search?q=x",
    )
    assert checkup.sitemap_paths(xml) == [
        "/",
        "/source/kuam",
        "/source/postguam",
        "/archive/2026/11/26",
        "/archive/2026/11/9",
        "/events/140",
        "/events/99",
        "/guides/packs/pcs",
        "/search?q=x",
    ]
    assert checkup.sitemap_paths("not xml") == []


def test_extra_pages():
    assert checkup.extra_paths(_settings(), ["/", "/advertise"]) == [
        "/search?q=guam",
        "/events.ics",
        "/robots.txt",
        "/llms.txt",
        "/feed.xml",
        "/advertise/media-kit.pdf",
    ]
    assert "/feed.xml" not in checkup.extra_paths(_settings(newsletter_days=()), ["/"])
    assert checkup.pattern_text("/archive/{year}/{month}/{day}") == "/archive/…/…/…"


def test_check_pages_finds_broken_and_slow_pages():
    site = Site(
        sitemap=_sitemap("/", "/events", "/bids/awards", "/guides/gone", "/weather", "/hangs"),
        pages={"/bids/awards": 500, "/guides/gone": 404},
        slow={"/weather"},
    )
    found = checkup.check_pages(_settings(slow_page_seconds=0.05), site.transport())
    assert [(r.path, r.problem()) for r in found.broken] == [
        ("/bids/awards", "server error (500)"),
        ("/guides/gone", "not found (404)"),
        ("/hangs", "no answer (ReadTimeout)"),
    ]
    assert [r.path for r in found.slow] == ["/weather"]
    assert found.slow[0].seconds >= 0.05
    # A slow page is opened twice; the others once, then the extra pages.
    assert site.opened.count("/weather") == 2
    assert site.opened.count("/") == 1
    assert "/search?q=guam" in site.opened and "/feed.xml" in site.opened
    assert found.checked == 1 + 6 + 5  # the sitemap, its pages, the extra pages
    assert site.agents == {stats.CHECKUP_AGENT}


def test_a_page_slow_only_once_is_not_slow():
    class OnceSlow(Site):
        def handle(self, request):
            if request.url.path == "/weather" and "/weather" not in self.opened:
                time.sleep(0.06)
            return super().handle(request)

    site = OnceSlow(sitemap=_sitemap("/", "/weather"))
    found = checkup.check_pages(_settings(slow_page_seconds=0.05), site.transport())
    assert found.slow == [] and found.broken == []


def test_check_pages_without_a_sitemap_opens_the_home_page():
    site = Site(sitemap=500)
    found = checkup.check_pages(_settings(), site.transport())
    assert [(r.path, r.problem()) for r in found.broken] == [("/sitemap.xml", "server error (500)")]
    assert site.opened[:2] == ["/sitemap.xml", "/"]


def test_check_pages_stops_after_its_time(monkeypatch):
    monkeypatch.setattr(checkup, "CHECK_BUDGET", 0.05)
    site = Site(sitemap=_sitemap("/", "/a", "/b", "/c"), slow={"/"})
    found = checkup.check_pages(_settings(slow_page_seconds=10), site.transport())
    assert found.left > 0
    assert found.checked + found.left == 1 + 4 + 5


def test_site_down():
    found = checkup.check_pages(_settings(), Site(down=True).transport())
    assert [(r.path, r.problem()) for r in found.broken[:2]] == [
        ("/sitemap.xml", "no answer (ConnectError)"),
        ("/", "no answer (ConnectError)"),
    ]


# -------------------------------------------------------- counts on the site


@pytest.fixture()
def web(monkeypatch):
    from guheadlines.web import app as web_app

    monkeypatch.setattr(web_app, "settings", replace(web_app.settings, slow_page_seconds=60.0))
    return web_app


def _counted() -> dict[str, int]:
    with stats.counts._lock:
        return {key: n for (_, key), n in stats.counts._views.items()}


def test_the_website_counts_pages_errors_and_slow_pages(web, monkeypatch):
    client = TestClient(web.app, raise_server_exceptions=False)
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/robots.txt").status_code == 200
    assert _counted() == {f"{PAGES_SERVED}/robots.txt": 2}

    def broken(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(web.llms_cache, "get", broken)
    assert client.get("/llms.txt").status_code == 500
    assert _counted()[f"{PAGE_ERRORS}/llms.txt"] == 1
    assert _counted()[f"{PAGES_SERVED}/llms.txt"] == 1

    # Slower than SLOW_PAGE_SECONDS.
    monkeypatch.setattr(web, "settings", replace(web.settings, slow_page_seconds=0.0))
    client.get("/robots.txt")
    assert _counted()[f"{SLOW_PAGES}/robots.txt"] == 1
    # Not counted: files, /healthz, the health check's own visits, POSTs as slow.
    client.get("/static/style.css")
    client.get("/robots.txt", headers={"User-Agent": stats.CHECKUP_AGENT})
    assert _counted()[f"{PAGES_SERVED}/robots.txt"] == 3
    assert not any("static" in key or "healthz" in key for key in _counted())


def test_pages_that_wait_for_paypal_are_never_slow(web, monkeypatch):
    monkeypatch.setattr(web, "settings", replace(web.settings, slow_page_seconds=0.0))
    route = next(r for r in web.app.routes if getattr(r, "path", "") == "/bids/alerts/pay")
    assert route.endpoint.waits_elsewhere is True
    client = TestClient(web.app, raise_server_exceptions=False)
    client.get("/bids/alerts/pay", follow_redirects=False)
    counted = _counted()
    assert counted[f"{PAGES_SERVED}/bids/alerts/pay"] == 1
    assert f"{SLOW_PAGES}/bids/alerts/pay" not in counted


# ---------------------------------------------------------------- database

_numbers = count()


def _source(slug, name, **fields):
    with session_scope() as session:
        source = Source(slug=slug, name=name, **fields)
        session.add(source)
        session.flush()
        return source.id


def _stories(source_id, n, when, photo=True, saved=True):
    with session_scope() as session:
        for _ in range(n):
            number = next(_numbers)
            session.add(
                Article(
                    source_id=source_id,
                    url=f"https://example.com/{source_id}/{number}",
                    title=f"Story {number}",
                    image_url=f"https://example.com/{number}.jpg" if photo else None,
                    image_path=f"images/{number}.webp" if photo and saved else None,
                    published_at=when,
                    first_seen_at=when,
                )
            )


def _views(day, **counts):
    with session_scope() as session:
        for key, n in counts.items():
            session.add(PageViews(day=day, path=key, views=n))


def test_photo_lines(clean_db):
    ok = _source("postguam", "The Guam Daily Post")
    _stories(ok, 10, WEEK - timedelta(days=14))
    _stories(ok, 5, WEEK)
    stopped = _source("kuam", "KUAM News")
    _stories(stopped, 10, WEEK - timedelta(days=10))
    _stories(stopped, 6, WEEK, photo=False)
    fewer = _source("kandit", "Kandit")
    _stories(fewer, 8, WEEK - timedelta(days=20))
    _stories(fewer, 2, WEEK - timedelta(days=20), photo=False)
    _stories(fewer, 1, WEEK)
    _stories(fewer, 5, WEEK, photo=False)
    not_saved = _source("mvariety", "Marianas Variety")
    _stories(not_saved, 4, WEEK, saved=False)
    _stories(not_saved, 1, WEEK)
    # Never had photos: nothing to say. Too few stories: neither.
    never = _source("gdol", "Guam Department of Labor")
    _stories(never, 10, WEEK - timedelta(days=10), photo=False)
    _stories(never, 5, WEEK, photo=False)
    few = _source("guampdn", "Pacific Daily News")
    _stories(few, 10, WEEK - timedelta(days=10))
    _stories(few, 2, WEEK, photo=False)
    slugs = ["postguam", "kuam", "kandit", "mvariety", "gdol", "guampdn"]
    start, end = datetime(2026, 11, 23, tzinfo=GUAM_TZ), datetime(2026, 11, 30, tzinfo=GUAM_TZ)
    with session_scope() as session:
        lines = checkup.photo_lines(session, slugs, start, end, saved=True)
        unsaved_off = checkup.photo_lines(session, slugs, start, end, saved=False)
    assert lines == [
        "• KUAM News: none of its 6 new stories had a photo (10 of 10 had one in the 4 weeks"
        " before)",
        "• Kandit: a photo on 1 of its 6 new stories (8 of 10 had one in the 4 weeks before)",
        "• Marianas Variety: 4 of 5 photos could not be saved on the server (the site shows the"
        " news site's own copy)",
    ]
    assert len(unsaved_off) == 2


def test_visits_and_uptime(clean_db):
    _views(
        date(2026, 11, 24),
        **{
            f"{PAGES_SERVED}/search": 900,
            f"{SLOW_PAGES}/search": 30,
            f"{PAGES_SERVED}/events/{{event_id}}": 400,
            f"{PAGE_ERRORS}/events/{{event_id}}": 3,
            f"{PAGES_SERVED}/": 5000,
            f"{SLOW_PAGES}/": 20,  # under 1% of its visits
            f"{SLOW_PAGES}/archive": 2,  # fewer than 3
            UPTIME_UP: 280,
            UPTIME_DOWN: 8,
            "/search": 77,  # a page view: not counted here
        },
    )
    _views(date(2026, 11, 25), **{f"{PAGES_SERVED}/search": 300, f"{SLOW_PAGES}/search": 11})
    _views(date(2026, 11, 22), **{f"{PAGE_ERRORS}/bids": 9, UPTIME_DOWN: 50})  # the week before
    with session_scope() as session:
        seen = checkup.visits(session, FIRST, END)
    assert seen.error_lines() == ["• /events/…: 3 times (of 400)"]
    assert seen.slow_lines() == ["• /search: 41 of 1,200 times"]
    assert checkup.uptime_line(seen) == (
        "The site answered 280 of the worker's 288 checks every 5 minutes (97.2%).",
        True,
    )
    seen.down = 1
    assert checkup.uptime_line(seen)[1] is False  # 99.6%
    seen.up = seen.down = 0
    assert checkup.uptime_line(seen) == (None, False)


def test_ping_counts_the_answers(clean_db, monkeypatch):
    settings = _settings()
    assert checkup.ping(settings, Site().transport()) is True
    assert checkup.ping(settings, Site(healthz={"status": "error"}).transport()) is False
    assert checkup.ping(settings, Site(down=True).transport()) is False
    # The database is down: the count is kept for the next time.
    monkeypatch.setattr(checkup, "session_scope", _broken_session)
    assert checkup.ping(settings, Site().transport()) is True
    monkeypatch.undo()
    checkup.ping(settings, Site().transport())
    with session_scope() as session:
        rows = dict(session.execute(select(PageViews.path, PageViews.views)).all())
    assert rows == {UPTIME_UP: 3, UPTIME_DOWN: 2}


def _broken_session():
    raise RuntimeError("no database")


def _seed_week():
    worked = WEEK - timedelta(hours=1)
    post = _source("postguam", "The Guam Daily Post", last_run_at=worked, last_success_at=worked)
    _stories(post, 10, WEEK - timedelta(days=10))
    _stories(post, 5, WEEK)
    kuam = _source("kuam", "KUAM News", last_run_at=worked, last_success_at=worked)
    _stories(kuam, 10, WEEK - timedelta(days=10))
    _stories(kuam, 6, WEEK, photo=False)
    _source(
        "kandit",
        "Kandit",
        last_run_at=worked,
        last_success_at=WEEK - timedelta(days=3),
        last_error="HTTP 403",
    )
    _views(
        date(2026, 11, 24),
        **{
            f"{PAGES_SERVED}/events/{{event_id}}": 400,
            f"{PAGE_ERRORS}/events/{{event_id}}": 3,
            UPTIME_UP: 2016,
        },
    )


def test_health_alert_lists_what_needs_looking_at(clean_db):
    _seed_week()
    site = Site(
        sitemap=_sitemap("/", "/events", "/bids/awards", "/weather"),
        pages={"/bids/awards": 500},
        slow={"/weather"},
    )
    alert = checkup.health_alert(
        _settings(slow_page_seconds=0.05), MONDAY, site.transport(), now=MONDAY
    )
    assert alert.title == "Weekly health check: 4 things to look at"
    assert not alert.quiet
    assert alert.emoji == "🩺"
    parts = alert.message.split("\n\n")
    assert parts[0] == (
        "**Broken pages**\n"
        "• /bids/awards: server error (500)\n"
        "Visitors got a server error on:\n"
        "• /events/…: 3 times (of 400)\n"
        "What went wrong is in the website's log: docker compose logs web"
    )
    assert parts[1].startswith(
        "**Slow pages** (longer than 0.1 seconds to make)\n• /weather: 0.1 seconds"
    )
    assert parts[2] == (
        "**Photos**\n"
        "• KUAM News: none of its 6 new stories had a photo (10 of 10 had one in the 4 weeks"
        " before)"
    )
    assert parts[3] == (
        "**Not working or quiet**\n• The news site Kandit has not worked since Nov 23"
    )
    assert parts[4].startswith(
        "The site answered 2,016 of the worker's 2,016 checks every 5 minutes (100%).\n"
        "Opened 10 pages in less than a second."
    )
    assert len(alert.message) < 2000


def test_health_alert_all_good(clean_db):
    alert = checkup.health_alert(_settings(), MONDAY, Site().transport(), now=MONDAY)
    assert alert.title == "Weekly health check: all good"
    assert alert.quiet
    assert alert.message == (
        "Opened 9 pages in less than a second: all opened, none slower than 1 second.\n"
        "Every news site's photos came in as usual."
    )


def test_send_due_posts_the_health_check_once(clean_db):
    _seed_week()
    site = Site()
    settings = _settings()
    assert reports.send_due(settings, MONDAY + timedelta(minutes=1), site.transport()) == [
        reports.HEALTH_KEY
    ]
    assert len(site.discord) == 1
    assert site.discord[0]["content"].startswith("🩺 **Weekly health check: ")
    assert reports.send_due(settings, MONDAY + timedelta(minutes=20), site.transport()) == []
    with session_scope() as session:
        assert session.get(AlertState, reports.HEALTH_KEY).failing_since == MONDAY
    # Off: nothing.
    later = MONDAY + timedelta(days=7, minutes=1)
    assert reports.send_due(_settings(health_check_day=None), later, site.transport()) == []


def test_env_example_has_the_health_check_settings():
    from conftest import ROOT

    lines = (ROOT / ".env.example").read_text().splitlines()
    assert "HEALTH_CHECK_DAY=monday" in lines
    assert "SLOW_PAGE_SECONDS=1" in lines
    # Only for running without Docker: named in the last note, never set.
    assert not any(line.startswith("WEBSITE_ADDRESS") for line in lines)
    assert any(line.startswith("#   WEBSITE_ADDRESS ") for line in lines)
