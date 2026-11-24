from datetime import datetime, timezone

from conftest import fixture_bytes

from guheadlines.scraper.discover import (
    looks_like_article_url,
    parse_feed,
    parse_listing,
    parse_sitemap,
    sitemaps_from_robots,
)


def test_parse_wordpress_feed():
    items = parse_feed(fixture_bytes("wordpress_feed.xml"), "https://www.pncguam.com/feed/")
    assert len(items) == 3
    first = items[0]
    assert first.url == "https://www.pncguam.com/govguam-agencies-brace-for-h-2b-worker-shortage/"
    assert first.title == "GovGuam agencies brace for H-2B worker shortage"
    assert first.published_at == datetime(2026, 9, 26, 21, 15, tzinfo=timezone.utc)
    assert first.image_url.endswith("h2b-workers.jpg")
    assert first.author == "Jolene Toves"
    assert first.tags == ["Local News"]
    assert items[1].title == "Mayors’ Council plans village cleanup ahead of typhoon season"


def test_parse_listing_page(app_config):
    source = app_config.source("postguam")
    source.autodiscover_feeds = True
    try:
        links, feeds = parse_listing(
            fixture_bytes("listing_page.html"), "https://www.postguam.com/news/local/", source
        )
    finally:
        source.autodiscover_feeds = False
    urls = [c.url for c in links]
    assert urls == [
        "https://www.postguam.com/news/local/andersen-to-host-cope-north-exercise-with-japan-australia/"
        "article_0f2c3a9e-1111-2222-3333-444455556666.html",
        "https://www.postguam.com/news/local/second-story/article_bbbbbbbb-1111-2222-3333-444455556666.html",
    ]
    # The longest anchor text for a URL becomes its title hint.
    assert links[0].title == "Andersen to host Cope North exercise with Japan, Australia"
    assert feeds == ["https://www.postguam.com/search/?c=news%2Flocal&f=rss&t=article"]


def test_parse_sitemaps():
    urls, children = parse_sitemap(fixture_bytes("sitemap_index.xml"))
    assert urls == [] and len(children) == 2
    urls, children = parse_sitemap(fixture_bytes("sitemap_posts.xml"))
    assert children == [] and len(urls) == 3
    assert urls[0][1] == datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_sitemaps_from_robots():
    robots = "User-agent: *\nDisallow: /wp-admin/\nSitemap: https://x.com/sitemap_index.xml\n"
    assert sitemaps_from_robots(robots) == ["https://x.com/sitemap_index.xml"]


def test_article_url_heuristic():
    assert looks_like_article_url("https://x.com/2026/09/27/some-story/")
    assert looks_like_article_url("https://x.com/news/governor-signs-new-minimum-wage-bill")
    assert not looks_like_article_url("https://x.com/news/")
    assert not looks_like_article_url("https://x.com/about-us")
