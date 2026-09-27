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
                "TRUNCATE articles, seen_urls, ignored_images, http_cache, scrape_runs, sources "
                "RESTART IDENTITY CASCADE"
            )
        )
    return database
