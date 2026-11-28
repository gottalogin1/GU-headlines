"""Test setup.

Unit tests need nothing special. Database tests need an empty PostgreSQL
database (13+) and run only when TEST_DATABASE_URL is set, e.g.:

    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/guheadlines_test pytest
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent

TEST_DB = os.environ.get("TEST_DATABASE_URL")
# Settings are read once per process, so point them at the test database and a
# throwaway media folder before anything imports guheadlines.settings.
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
os.environ.setdefault("MEDIA_DIR", tempfile.mkdtemp(prefix="guheadlines-media-"))
os.environ["CONFIG_DIR"] = str(ROOT / "config")


@pytest.fixture(autouse=True)
def login_limit():
    """Each test starts with no wrong passwords counted (web/app.py
    LoginLimit), so tests that try wrong passwords never block later ones."""
    import sys

    web = sys.modules.get("guheadlines.web.app")
    if web is not None:
        web.login_limit = web.LoginLimit()
    yield


@pytest.fixture(autouse=True)
def visitor_counts(monkeypatch):
    """Each test counts visitors (web/stats.py) afresh, and what is not
    written by its end is dropped: else the counts' writer thread would
    write it into a later test's database, up to 30 seconds later."""
    from guheadlines.web import stats

    monkeypatch.setattr(stats, "counts", stats.Counts())
    yield
    with stats.counts._lock:
        for counter in (stats.counts._views, stats.counts._clicks, stats.counts._referrers):
            counter.clear()


def visitor_views(views: dict) -> dict:
    """Page views and clicks only: without the counts that the website keeps
    for the weekly health check (web/stats.py Counts.served), whose names
    start with "served:", "server-error:" or "slow:". `views` is {path: n}
    or {(day, path): n}."""
    from guheadlines.models import PAGE_ERRORS, PAGES_SERVED, SLOW_PAGES

    def path(key) -> str:
        return key[1] if isinstance(key, tuple) else key

    return {
        key: n
        for key, n in views.items()
        if not path(key).startswith((PAGES_SERVED, PAGE_ERRORS, SLOW_PAGES))
    }


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_jpeg(width: int = 1200, height: int = 800) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), (18, 48, 94))
    for x in range(0, width, 50):
        for y in range(height):
            image.putpixel((x, y), (200, 16, 46))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


@pytest.fixture(scope="session")
def app_config():
    from guheadlines.config import load_config

    return load_config(ROOT / "config")


@pytest.fixture(scope="session")
def database():
    if not TEST_DB:
        pytest.skip("set TEST_DATABASE_URL to run database tests")
    from sqlalchemy import text

    from guheadlines.cli import migrate
    from guheadlines.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("DROP TEXT SEARCH CONFIGURATION IF EXISTS guam_english"))
    migrate()
    return get_engine()


@pytest.fixture()
def clean_db(database):
    from sqlalchemy import text

    with database.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE articles, seen_urls, ignored_images, http_cache, scrape_runs, sources, "
                "jobs, job_sources, events, event_calendars, alerts, weather, page_views, "
                "story_clicks, referrers, newsletter_editions, keyword_alerts, bids, bid_sources, "
                "listings, ad_bookings, ad_slots, bid_alert_subscribers, bid_alert_plans, guides, "
                "guide_packs, guide_pack_guides, guide_passes "
                "RESTART IDENTITY CASCADE"
            )
        )
    return database
