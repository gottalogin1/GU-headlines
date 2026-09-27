"""End-to-end scrape against a fake news site served by httpx.MockTransport."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml
from conftest import ROOT, fixture_bytes, make_jpeg
from sqlalchemy import select, text, update

from guheadlines.config import parse_config
from guheadlines.db import session_scope
from guheadlines.models import Article, HttpCache, IgnoredImage, ScrapeRun, SeenUrl, Source
from guheadlines.scraper.http import HttpClient
from guheadlines.scraper.pipeline import Scraper, sync_sources
from guheadlines.settings import get_settings

ARTICLE_1 = "https://www.pncguam.com/govguam-agencies-brace-for-h-2b-worker-shortage/"
ARTICLE_2 = "https://www.pncguam.com/mayors-council-plans-village-cleanup-ahead-of-typhoon-season/"
NOT_ARTICLE = "https://www.pncguam.com/advertise-with-pnc-news-first-today/"
IMAGE = "https://www.pncguam.com/wp-content/uploads/2026/09/h2b-workers.jpg"

HOME = f"""<html><body>
<a href="{ARTICLE_1}">GovGuam agencies brace for H-2B worker shortage</a>
<a href="{ARTICLE_2}?utm_source=home">Mayors' Council plans village cleanup</a>
<a href="{NOT_ARTICLE}">Advertise with us</a>
<a href="https://www.pncguam.com/category/news/">News</a>
</body></html>""".encode()

NOT_ARTICLE_HTML = b"<html><head><title>Advertise</title></head><body><p>Call us.</p></body></html>"

# A story page with no OpenGraph/JSON-LD metadata at all.
BARE = "https://www.pncguam.com/bare-page-without-metadata-story/"
BARE_HTML = b"""<html><head><title>Port board approves new crane purchase</title></head>
<body><h1>Port board approves new crane purchase</h1>
<time datetime="2026-09-20T10:00:00+10:00">Sept 20</time>
<div class="entry-content"><p>The Port Authority of Guam board on Friday approved the purchase
of two gantry cranes, a move officials say will cut container wait times for island
businesses.</p></div>
</body></html>"""


class FakeSite:
    def __init__(self) -> None:
        self.requests: Counter[str] = Counter()
        self.feed_etag = '"v1"'

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests[url] += 1
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /wp-admin/\n")
        if url == "https://www.pncguam.com/feed/":
            if request.headers.get("if-none-match") == self.feed_etag:
                return httpx.Response(304)
            return httpx.Response(
                200,
                content=fixture_bytes("wordpress_feed.xml"),
                headers={"content-type": "application/rss+xml", "etag": self.feed_etag},
            )
        if url == "https://www.pncguam.com/":
            return httpx.Response(200, content=HOME, headers={"content-type": "text/html"})
        if url == ARTICLE_1:
            return httpx.Response(
                200,
                content=fixture_bytes("wordpress_article.html"),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if url == ARTICLE_2:
            return httpx.Response(403, text="Forbidden")  # bot wall: fall back to feed data
        if url == BARE:
            return httpx.Response(200, content=BARE_HTML, headers={"content-type": "text/html"})
        if url == NOT_ARTICLE:
            return httpx.Response(
                200, content=NOT_ARTICLE_HTML, headers={"content-type": "text/html"}
            )
        if url == IMAGE:
            return httpx.Response(200, content=make_jpeg(), headers={"content-type": "image/jpeg"})
        return httpx.Response(404)


@pytest.fixture()
def config():
    sources = {
        "sources": [
            {
                "slug": "pnc",
                "name": "PNC News First",
                "homepage": "https://www.pncguam.com/",
                "feeds": ["https://www.pncguam.com/feed/"],
                "listing_pages": ["https://www.pncguam.com/"],
                "article_pattern": r"^https?://(www\.)?pncguam\.com/[a-z0-9][a-z0-9-]{8,}/?$",
                "exclude_url_patterns": [r"/category/"],
            }
        ]
    }
    categories = yaml.safe_load((ROOT / "config" / "categories.yaml").read_text())
    return parse_config(sources, categories)


@pytest.fixture()
def site():
    return FakeSite()


@pytest.fixture()
def scraper(config, site):
    client = HttpClient(
        "test-agent", per_host_delay=0, transport=httpx.MockTransport(site), sleep=lambda s: None
    )
    scraper = Scraper(get_settings(), config, client=client)
    yield scraper
    client.close()


def _articles():
    with session_scope() as session:
        return {a.url: a for a in session.scalars(select(Article))}


def test_first_run_stores_new_articles(clean_db, scraper, site):
    results = scraper.run()
    assert results is not None and len(results) == 1
    result = results[0]
    assert result.new_articles == 2
    assert result.rejected == 1  # the advertising page is not an article
    assert result.failed == 0

    articles = _articles()
    assert set(articles) == {ARTICLE_1, ARTICLE_2}

    full = articles[ARTICLE_1]
    assert full.title == "GovGuam agencies brace for H-2B worker shortage"
    assert full.intro.startswith("Government of Guam agencies are bracing")
    assert full.author == "Jolene Toves"
    assert "labor" in full.categories
    assert full.image_url == IMAGE
    assert full.image_path and full.image_path.endswith(".webp")
    assert (get_settings().media_dir / full.image_path).exists()
    assert full.image_width == 720

    # Article page was blocked (403): the feed's headline and summary are used,
    # with WordPress' "[…]" and "appeared first on" boilerplate removed.
    fallback = articles[ARTICLE_2]
    assert fallback.title == "Mayors’ Council plans village cleanup ahead of typhoon season"
    assert fallback.intro == (
        "Mayors across the island will coordinate a cleanup of village drainage ahead of "
        "typhoon season."
    )
    assert fallback.discovered_url is None
    assert "local" in fallback.categories

    with session_scope() as session:
        run = session.scalars(select(ScrapeRun)).one()
        assert run.new_articles == 2 and run.finished_at is not None
        source = session.scalars(select(Source)).one()
        assert source.last_success_at is not None and source.last_error is None
        seen = session.scalars(select(SeenUrl)).one()
        assert seen.url == NOT_ARTICLE and seen.status == "rejected"
        assert session.get(HttpCache, "https://www.pncguam.com/feed/").etag == '"v1"'


def test_second_run_only_fetches_new_pages(clean_db, scraper, site):
    scraper.run()
    first_counts = dict(site.requests)
    results = scraper.run()
    assert results[0].new_articles == 0
    # Article pages and the rejected page were not downloaded again...
    assert site.requests[ARTICLE_1] == first_counts[ARTICLE_1] == 1
    assert site.requests[NOT_ARTICLE] == 1
    assert site.requests[IMAGE] == first_counts.get(IMAGE, 0)  # 0 if already on disk
    # ...and the unchanged feed was answered with 304 Not Modified.
    assert site.requests["https://www.pncguam.com/feed/"] == 2


def test_failed_pages_are_retried_with_backoff(clean_db, scraper, site):
    calls = {"n": 0}
    original = site.__call__

    def flaky(request):
        if str(request.url) == ARTICLE_1:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503)
        return original(request)

    scraper.client._client._transport = httpx.MockTransport(flaky)
    scraper.client.retries = 0
    scraper.run()
    assert ARTICLE_1 not in _articles()
    with session_scope() as session:
        row = session.get(SeenUrl, ARTICLE_1)
        assert row.status == "failed" and row.attempts == 1
        # Pretend the backoff period has passed.
        session.execute(
            update(SeenUrl)
            .where(SeenUrl.url == ARTICLE_1)
            .values(last_attempt_at=datetime.now(timezone.utc) - timedelta(hours=2))
        )
    scraper.run()  # feed is 304 now, so the retry comes from seen_urls
    assert ARTICLE_1 in _articles()
    with session_scope() as session:
        assert session.get(SeenUrl, ARTICLE_1) is None


def test_generic_images_are_not_reused(clean_db, scraper, config):
    with session_scope() as session:
        source_id = sync_sources(session, config)["pnc"]
        for i in range(2):
            session.add(
                Article(
                    source_id=source_id,
                    url=f"https://www.pncguam.com/older-{i}/",
                    title=f"Older {i}",
                    image_url=IMAGE,
                    published_at=datetime.now(timezone.utc),
                )
            )
    scraper.run()
    articles = _articles()
    assert articles[ARTICLE_1].image_url is None
    # The reused picture is now ignored everywhere, including the older stories.
    assert all(a.image_url is None for a in articles.values())
    with session_scope() as session:
        assert session.get(IgnoredImage, IMAGE) is not None


def test_listing_pages_without_metadata(clean_db, scraper, config):
    from guheadlines.scraper.discover import Candidate
    from guheadlines.scraper.pipeline import Skip

    source = config.source("pnc")
    draft = scraper.build_draft(source, None, Candidate(url=BARE, key=BARE, via="listing"))
    assert draft.title == "Port board approves new crane purchase"
    assert draft.intro.startswith("The Port Authority of Guam board")
    assert draft.published_at == datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    assert "business" in draft.categories

    # Without an article_pattern vouching for the URL, such a page is rejected.
    source.article_patterns = []
    with pytest.raises(Skip):
        scraper.build_draft(source, None, Candidate(url=BARE, key=BARE, via="listing"))


def test_full_text_search_vector(clean_db, scraper):
    scraper.run()
    with session_scope() as session:
        hits = (
            session.execute(
                text(
                    "SELECT title FROM articles "
                    "WHERE search_vector @@ websearch_to_tsquery('guam_english', :q)"
                ),
                {"q": '"worker shortage" -typhoon'},
            )
            .scalars()
            .all()
        )
    assert hits == ["GovGuam agencies brace for H-2B worker shortage"]


def test_lock_prevents_parallel_runs(clean_db, scraper):
    from guheadlines.db import SCRAPE_LOCK_KEY, advisory_lock

    with advisory_lock(SCRAPE_LOCK_KEY) as acquired:
        assert acquired
        assert scraper.run() is None


def test_rate_limited_site_catches_up_after_a_pause(clean_db, scraper, site):
    original = site.__call__
    limited = {"on": True}

    def rate_limiter(request):
        if str(request.url) == ARTICLE_1 and limited["on"]:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return original(request)

    scraper.client._client._transport = httpx.MockTransport(rate_limiter)
    scraper.client.retries = 0
    result = scraper.run()[0]
    assert ARTICLE_1 not in _articles()
    assert any("rate limited" in e for e in result.errors)
    # Every new page is left for a catch-up, which will wait as the site asked.
    assert {c.key for c in result.pending} == {ARTICLE_1, ARTICLE_2, NOT_ARTICLE}
    assert result.retry_after == 120
    with session_scope() as session:
        # Not recorded as a failure (no backoff), and feed validators wait too.
        assert session.get(SeenUrl, ARTICLE_1) is None
        assert session.get(HttpCache, "https://www.pncguam.com/feed/") is None

    # The catch-up run continues with the pending pages without re-reading the feed.
    limited["on"] = False
    feed_reads = site.requests["https://www.pncguam.com/feed/"]
    caught_up = scraper.run(catch_up={"pnc": result})[0]
    assert not caught_up.incomplete
    assert caught_up.new_articles == 2 and caught_up.rejected == 1
    assert {ARTICLE_1, ARTICLE_2} <= set(_articles())
    assert site.requests["https://www.pncguam.com/feed/"] == feed_reads
    with session_scope() as session:
        assert session.get(HttpCache, "https://www.pncguam.com/feed/").etag == '"v1"'


def test_per_run_cap_leaves_pages_for_catch_up(clean_db, config, site):
    client = HttpClient(
        "test-agent", per_host_delay=0, transport=httpx.MockTransport(site), sleep=lambda s: None
    )
    scraper = Scraper(replace(get_settings(), max_new_per_source=1), config, client=client)
    try:
        first = scraper.run()[0]
        assert first.new_articles == 1 and len(first.pending) == 2
        rounds = 0
        pending = {"pnc": first}
        while pending:
            rounds += 1
            result = scraper.run(catch_up=pending)[0]
            pending = {"pnc": result} if result.incomplete else {}
        assert rounds == 2
        assert {ARTICLE_1, ARTICLE_2} <= set(_articles())
    finally:
        client.close()


def test_feed_items_filtered_before_fetching(clean_db, scraper, site, config):
    source = config.source("pnc")
    source.require_guam = True  # the "Mayors' Council" item never mentions Guam
    source.exclude_sections = ["Local News"]  # the other item's feed category
    scraper.run()
    assert _articles() == {}
    assert site.requests[ARTICLE_1] == 0 and site.requests[ARTICLE_2] == 0
    with session_scope() as session:
        reasons = {row.url: row.reason for row in session.scalars(select(SeenUrl))}
    assert reasons[ARTICLE_1] == "excluded section: Local News"
    assert reasons[ARTICLE_2] == "not about Guam"


def test_old_listing_links_are_skipped(clean_db, scraper, config):
    from guheadlines.scraper.discover import Candidate
    from guheadlines.scraper.pipeline import Skip

    source = config.source("pnc")
    source.max_age_days = 1
    with pytest.raises(Skip, match="evergreen"):
        scraper.build_draft(source, None, Candidate(url=BARE, key=BARE, via="listing"))
