"""Website smoke tests against a small seeded database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from guheadlines.db import session_scope
from guheadlines.models import Article, Source


@pytest.fixture()
def client(clean_db):
    from guheadlines.web.app import app, sidebar_cache

    sidebar_cache._data.clear()
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        post = Source(
            slug="postguam", name="The Guam Daily Post", homepage="https://www.postguam.com/"
        )
        kuam = Source(
            slug="kuam", name="KUAM News", homepage="https://www.kuam.com/", last_run_at=now
        )
        session.add_all([post, kuam])
        session.flush()
        session.add_all(
            [
                Article(
                    source_id=post.id,
                    url="https://www.postguam.com/news/local/a/article_1.html",
                    title="Andersen to host Cope North exercise",
                    intro="Hundreds of airmen will arrive at Andersen <script>x</script>.",
                    categories=["military"],
                    image_path="images/2026/09/example.webp",
                    image_width=720,
                    image_height=480,
                    published_at=now - timedelta(hours=2),
                ),
                Article(
                    source_id=kuam.id,
                    url="https://www.kuam.com/story/1/minimum-wage",
                    title="Minimum wage increase takes effect in Hagåtña",
                    intro="Workers across the island will see higher pay starting Monday.",
                    categories=["labor", "business"],
                    published_at=now - timedelta(days=400),
                ),
            ]
        )
    return TestClient(app)


def test_home_lists_latest(client):
    page = client.get("/")
    assert page.status_code == 200
    html = page.text
    assert "Andersen to host Cope North exercise" in html
    assert "/media/images/2026/09/example.webp" in html
    assert "Today" in html
    # Intros are escaped, never rendered as HTML.
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_category_and_source_pages(client):
    labor = client.get("/category/labor").text
    assert "Minimum wage increase" in labor and "Cope North" not in labor
    assert client.get("/source/kuam").status_code == 200
    assert client.get("/category/nope").status_code == 404
    assert client.get("/source/nope").status_code == 404


def test_search_finds_old_articles_without_accents(client):
    page = client.get("/search", params={"q": "hagatna wage"})
    assert page.status_code == 200
    # Matched words are highlighted, including the accented spelling.
    assert "Minimum <mark>wage</mark> increase takes effect in <mark>Hagåtña</mark>" in page.text


def test_search_filters_and_fuzzy_fallback(client):
    year_ago = (datetime.now(timezone.utc) - timedelta(days=400)).date()
    page = client.get(
        "/search",
        params={"from": str(year_ago - timedelta(days=1)), "to": str(year_ago + timedelta(days=1))},
    )
    assert "Minimum wage increase" in page.text and "Cope North" not in page.text
    fuzzy = client.get("/search", params={"q": "Andersn"})
    assert "Headlines that look similar" in fuzzy.text
    assert "Cope North" in fuzzy.text
    assert client.get("/search", params={"sort": "bogus"}).status_code == 422


def test_archive_pages(client):
    assert client.get("/archive").status_code == 200
    today = datetime.now(timezone(timedelta(hours=10))).date()
    month = client.get(f"/archive/{today.year}/{today.month}")
    assert month.status_code == 200
    day = client.get(f"/archive/{today.year}/{today.month}/{today.day}")
    assert "Cope North" in day.text
    assert client.get("/archive/2026/13").status_code == 404


def test_status_and_health(client):
    assert "KUAM News" in client.get("/status").text
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["last_scrape"]
    assert "Disallow: /search" in client.get("/robots.txt").text
