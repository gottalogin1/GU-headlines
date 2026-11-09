"""Website smoke tests against a small seeded database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from guheadlines.db import session_scope
from guheadlines.models import Article, Source
from guheadlines.scraper.text import GUAM_TZ


@pytest.fixture()
def client(clean_db):
    from guheadlines.web.app import app, sidebar_cache, sitemap_cache

    sidebar_cache._data.clear()
    sitemap_cache._data.clear()
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


def test_links_to_articles_and_source_websites(client):
    html = client.get("/").text
    article = "https://www.postguam.com/news/local/a/article_1.html"
    # Headline, photo and the "read the full story" link all open the article.
    assert html.count(f'href="{article}"') >= 2
    assert 'Read the full story at <span class="read-domain">postguam.com</span>' in html
    # The outlet's name opens the outlet's website; the sidebar shows its address.
    assert '<a class="source" href="https://www.postguam.com/"' in html
    assert 'class="site-link" href="https://www.kuam.com/"' in html
    source_page = client.get("/source/kuam").text
    link = 'href="https://www.kuam.com/" target="_blank" rel="noopener">kuam.com ↗</a>'
    assert link in source_page


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


def test_odd_addresses_are_refused_without_a_server_error(client):
    # Days the calendar cannot hold, and page numbers past the limit.
    for path in ("/archive/1/1/1", "/archive/9999/12/31", "/archive/2026/99999999999999999999/1"):
        assert client.get(path).status_code == 404
    assert client.get("/archive/2026/10/1", params={"page": 10**19}).status_code == 422
    # A NUL character, which the database cannot store or search for.
    for path in ("/search?q=a%00b", "/search?source=%00", "/jobs?q=%00", "/source/kuam%00"):
        assert client.get(path).status_code == 400


def test_status_and_health(client):
    assert "KUAM News" in client.get("/status").text
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["last_scrape"]
    assert "Disallow: /search" in client.get("/robots.txt").text


def test_status_shows_catch_up_pauses_as_notes(client):
    from sqlalchemy import update

    with session_scope() as session:
        session.execute(
            update(Source)
            .where(Source.slug == "kuam")
            .values(last_error="catching up: the site asked us to slow down, 12 pages left")
        )
        session.execute(
            update(Source)
            .where(Source.slug == "postguam")
            .values(last_error="feed https://www.postguam.com/rss: HTTP 404")
        )
    page = client.get("/status").text
    assert '<div class="muted small" title="catching up: the site asked us' in page
    assert '<div class="error small" title="feed https://www.postguam.com/rss: HTTP 404">' in page


def test_community_only_source_stays_on_the_community_page(client):
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        reddit = Source(
            slug="reddit-guam", name="r/guam (Reddit)", homepage="https://www.reddit.com/r/guam/"
        )
        session.add(reddit)
        session.flush()
        session.add(
            Article(
                source_id=reddit.id,
                url="https://www.reddit.com/r/guam/comments/1abc/best_kelaguen_on_island/",
                title="Best kelaguen on island?",
                intro="Looking for recommendations near Tumon.",
                categories=["community", "local"],  # as if tagged before only_in_topic
                published_at=now - timedelta(hours=1),
            )
        )
    post = "Best kelaguen on island?"
    home = client.get("/").text
    assert post not in home
    assert "r/guam (Reddit)" not in home  # nor in the sidebar's list of news sources
    assert post not in client.get("/category/local").text
    assert post in client.get("/category/community").text
    assert post in client.get("/source/reddit-guam").text
    # Search results mark the matched word, so look for the post's link.
    link = "comments/1abc/best_kelaguen_on_island"
    assert link not in client.get("/search?q=kelaguen").text
    assert link in client.get("/search?q=kelaguen&category=community").text
    assert post in client.get("/search?source=reddit-guam").text
    day = (now - timedelta(hours=1)).astimezone(GUAM_TZ).date()
    assert post not in client.get(f"/archive/{day.year}/{day.month}/{day.day}").text
    assert "Andersen to host Cope North" in client.get("/").text  # news is unaffected


def test_sources_page_lists_sites_that_cannot_be_collected(client):
    with session_scope() as session:
        session.add(Source(slug="pnc", name="PNC News First", enabled=False))  # removed site
    page = client.get("/status").text
    assert "PNC News First" not in page
    blocked = page.split("Can't be collected", 1)[1].split("Recent runs", 1)[0]
    for name in ("Marianas Business Journal", "Andersen Air Force Base", "Guam Business Magazine"):
        assert name in blocked
    assert "Its firewall (Akamai) turns away automated visitors." in blocked
    sidebar = client.get("/").text.split("Can't be collected", 1)[1]
    assert "Guam Business Magazine" in sidebar and "guambusinessmagazine.com ↗" in sidebar


def test_light_dark_switch(client):
    page = client.get("/").text
    # The switch is in the top bar; the script shows it and picks the theme:
    # the saved choice, else the device setting, else dark.
    assert 'id="theme-toggle"' in page and 'role="switch"' in page
    assert 'saved() || device() || "dark"' in page
    css = client.get("/static/style.css").text
    dark, light = css.split(':root[data-theme="light"] {', 1)
    assert "color-scheme: dark;" in dark.split(":root {", 1)[1]  # dark by default
    assert "--bg: #ffffff;" in light.split("}", 1)[0]  # light mode is plain white
    assert "prefers-color-scheme" not in css  # the script decides, not the CSS


def test_scraped_links_must_be_web_addresses():
    from guheadlines.web.app import safe_url

    assert safe_url("https://www.kuam.com/story") == "https://www.kuam.com/story"
    assert safe_url(" http://example.com/a ") == "http://example.com/a"
    for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "", None):
        assert safe_url(bad) == ""


def test_security_and_caching_headers(client):
    page = client.get("/")
    csp = page.headers["content-security-policy"]
    assert "script-src 'sha256-" in csp and "'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert page.headers["cross-origin-opener-policy"] == "same-origin"
    # The light/dark script is inlined and matches the hash the policy allows.
    from guheadlines.web.app import THEME_SCRIPT

    assert f"<script>{THEME_SCRIPT}</script>" in page.text
    # Pages are compressed; the stylesheet's address changes with its contents.
    assert page.headers.get("content-encoding") == "gzip"
    css_link = page.text.split('rel="stylesheet" href="', 1)[1].split('"', 1)[0]
    assert css_link.startswith("/static/style.css?v=")
    css = client.get(css_link)
    assert "immutable" in css.headers["cache-control"]
    assert css.headers["x-content-type-options"] == "nosniff"


def test_top_photo_loads_first(client):
    html = client.get("/").text
    # The top story's photo (and its blurred backdrop) is fetched right away.
    top = html.split('class="story', 2)[1]
    assert top.count('fetchpriority="high"') == 2 and 'loading="lazy"' not in top
    assert html.count('fetchpriority="high"') == 2


def _head(html: str) -> str:
    return html.split("</head>", 1)[0]


def _json_ld(html: str) -> list[dict]:
    import json

    blocks = html.split('<script type="application/ld+json">')[1:]
    return [json.loads(block.split("</script>", 1)[0]) for block in blocks]


def test_pages_describe_themselves_to_search_engines(client):
    head = _head(client.get("/?utm_source=facebook&page=1").text)
    assert "<title>GU Headlines · Guam news, updated every hour</title>" in head
    # One address per page: tracking parameters and ?page=1 are dropped.
    assert '<link rel="canonical" href="http://testserver/">' in head
    assert '<meta property="og:url" content="http://testserver/">' in head
    assert '<meta property="og:image" content="http://testserver/static/social-card.jpg?v=' in head
    assert '<meta name="twitter:card" content="summary_large_image">' in head
    assert 'name="robots"' not in head
    site = _json_ld(head)
    assert site[0]["@type"] == "WebSite" and site[0]["name"] == "GU Headlines"
    assert site[0]["url"] == "http://testserver/"
    # The home page has a heading for search engines and screen readers.
    heading = '<h1 class="visually-hidden">GU Headlines: the latest Guam news</h1>'
    assert heading in client.get("/").text

    labor = _head(client.get("/category/labor").text)
    assert "<title>Guam labor and workforce news · GU Headlines</title>" in labor
    assert '<meta name="description" content="Jobs, wages, workforce, unions' in labor
    assert '<link rel="canonical" href="http://testserver/category/labor">' in labor
    crumbs = _json_ld(labor)[0]["itemListElement"]
    assert [c["name"] for c in crumbs] == ["Home", "Labor"]
    assert crumbs[0]["item"] == "http://testserver/"

    # Older pages say where they sit in the series.
    from guheadlines.web.app import settings as app_settings

    with session_scope() as session:
        kuam_id = session.query(Source.id).filter_by(slug="kuam").scalar()
        for n in range(app_settings.page_size * 2):
            session.add(
                Article(
                    source_id=kuam_id,
                    url=f"https://www.kuam.com/story/filler-{n}",
                    title=f"Filler story {n}",
                    published_at=datetime.now(timezone.utc) - timedelta(days=3, minutes=n),
                )
            )
    second = _head(client.get("/?page=2&fbclid=abc").text)
    assert '<link rel="canonical" href="http://testserver/?page=2">' in second
    assert '<link rel="prev" href="http://testserver/">' in second
    assert '<link rel="next" href="http://testserver/?page=3">' in second
    assert "WebSite" not in second

    kuam = _head(client.get("/source/kuam").text)
    assert "<title>KUAM News headlines · GU Headlines</title>" in kuam
    assert "Every story links to kuam.com." in kuam


def test_search_and_error_pages_are_kept_out_of_search_results(client):
    for path in ("/search?q=wage", "/search", "/status", "/jobs?q=nurse", "/nope"):
        head = _head(client.get(path).text)
        assert '<meta name="robots" content="noindex' in head, path
        assert 'rel="canonical"' not in head, path
    jobs = _head(client.get("/jobs?source=gdoe&utm_medium=x").text)
    assert '<link rel="canonical" href="http://testserver/jobs?source=gdoe">' in jobs


def test_site_url_setting_is_used_for_canonical_links(client, monkeypatch):
    import dataclasses

    from guheadlines.web import seo

    site = dataclasses.replace(seo.settings, site_url="https://news.example.com")
    monkeypatch.setattr(seo, "settings", site)
    head = _head(client.get("/category/military", params={"page": 1}).text)
    assert '<link rel="canonical" href="https://news.example.com/category/military">' in head
    assert "Sitemap: https://news.example.com/sitemap.xml" in client.get("/robots.txt").text
    assert "<loc>https://news.example.com/</loc>" in client.get("/sitemap.xml").text


def test_site_url_must_be_a_web_address():
    from guheadlines.settings import _site_url

    assert _site_url(" https://news.example.com/ ") == "https://news.example.com"
    for bad in ("", None, "news.example.com", "javascript:alert(1)", "https://"):
        assert _site_url(bad) == ""


def test_structured_data_cannot_end_its_script_block():
    from guheadlines.web.seo import json_ld

    text = str(json_ld({"name": "</script><script>alert(1)</script> & more"}))
    assert "<" not in text and ">" not in text and "&" not in text
    import json

    assert json.loads(text)["name"] == "</script><script>alert(1)</script> & more"


def _recent_day():
    """The Guam day of the fixture's newest story (two hours ago)."""
    return (datetime.now(timezone.utc) - timedelta(hours=2)).astimezone(GUAM_TZ).date()


def test_sitemap_and_robots(client):
    robots = client.get("/robots.txt").text
    assert "Disallow: /search" in robots and "Disallow: /status" in robots
    assert "Sitemap: http://testserver/sitemap.xml" in robots

    page = client.get("/sitemap.xml")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("application/xml")
    xml = page.text
    import xml.etree.ElementTree as ET

    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in ET.fromstring(xml).findall("s:url/s:loc", ns)]
    today = _recent_day()
    old = (datetime.now(timezone.utc) - timedelta(days=400)).astimezone(GUAM_TZ).date()
    for path in (
        "/",
        "/category/military",
        "/category/labor",
        "/source/postguam",
        "/source/kuam",
        "/jobs",
        "/archive",
        f"/archive/{today.year}/{today.month}",
        f"/archive/{today.year}/{today.month}/{today.day}",
        f"/archive/{old.year}/{old.month}/{old.day}",
    ):
        assert f"http://testserver{path}" in locs, path
    # Topics without stories, search and the Sources page are left out.
    assert "http://testserver/category/sports" not in locs
    assert not [loc for loc in locs if "/search" in loc or "/status" in loc]
    assert "<lastmod>" in xml


def test_archive_links_skip_empty_days_and_months(client):
    today = _recent_day()
    old = (datetime.now(timezone.utc) - timedelta(days=400)).astimezone(GUAM_TZ).date()
    day = client.get(f"/archive/{today.year}/{today.month}/{today.day}").text
    nav = day.split('<p class="month-nav">', 1)[1].split("</p>", 1)[0]
    # The previous day with stories is 400 days back; nothing after the newest.
    assert f'href="/archive/{old.year}/{old.month}/{old.day}"' in nav
    assert nav.count("<a ") == 1
    assert 'name="robots"' not in _head(day)

    empty = old + timedelta(days=1)
    page = client.get(f"/archive/{empty.year}/{empty.month}/{empty.day}")
    assert page.status_code == 200
    assert '<meta name="robots" content="noindex">' in _head(page.text)
    nav = page.text.split('<p class="month-nav">', 1)[1].split("</p>", 1)[0]
    assert f'href="/archive/{old.year}/{old.month}/{old.day}"' in nav
    assert f'href="/archive/{today.year}/{today.month}/{today.day}"' in nav

    month = client.get(f"/archive/{today.year}/{today.month}").text
    nav = month.split('<p class="month-nav">', 1)[1].split("</p>", 1)[0]
    assert f'href="/archive/{old.year}/{old.month}"' in nav and nav.count("<a ") == 1
    assert "<title>Guam news archive: " in month
    assert "noindex" in _head(client.get("/archive/1995/1").text)
    # Dates far outside any archive are not pages at all.
    assert client.get("/archive/1/1/1").status_code == 404
    assert client.get("/archive/9999/12/31").status_code == 404


def test_icons_and_stored_photos(client):
    from guheadlines.settings import get_settings

    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/x-icon"
    page = client.get("/").text
    assert 'rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=' in page
    # Copies of the news sites' photos stay out of image search.
    photo = get_settings().media_dir / "images" / "test.webp"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"RIFF0000WEBP")
    response = client.get("/media/images/test.webp")
    assert response.headers["x-robots-tag"] == "noindex"
