"""Website smoke tests against a small seeded database."""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from guheadlines.db import session_scope
from guheadlines.models import Article, Source
from guheadlines.scraper.text import GUAM_TZ


def _media(path: str):
    from guheadlines.settings import get_settings

    return get_settings().media_dir / path


@pytest.fixture()
def client(clean_db):
    from guheadlines.web.app import app, sidebar_cache, sitemap_cache

    sidebar_cache._data.clear()
    sitemap_cache._data.clear()
    # The saved copy of the first story's photo, and its small copy.
    for path in ("images/2026/09/example.webp", "images/2026/09/example-sm.webp"):
        _media(path).parent.mkdir(parents=True, exist_ok=True)
        _media(path).write_bytes(b"RIFF0000WEBP")
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
    assert 'src="/media/images/2026/09/example.webp"' in html
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
    # The Sources page is linked from every page's footer (the top bar is full).
    footer = client.get("/").text.split('<footer class="footer">', 1)[1]
    assert '<a href="/status">Sources</a>' in footer
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


def test_only_in_topic_source_stays_on_its_topic_page(client, monkeypatch):
    from guheadlines.web.app import config_cache

    governor = config_cache.get().source("governor")
    monkeypatch.setattr(governor, "only_in_topic", "sports")
    slug, name = governor.slug, governor.name
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        source = Source(slug=slug, name=name, homepage="https://www.example.com/")
        session.add(source)
        session.flush()
        session.add(
            Article(
                source_id=source.id,
                url="https://www.example.com/posts/best-kelaguen-on-island/",
                title="Best kelaguen on island?",
                intro="Looking for recommendations near Tumon.",
                categories=["sports", "local"],  # as if tagged before only_in_topic
                published_at=now - timedelta(hours=1),
            )
        )
    post = "Best kelaguen on island?"
    home = client.get("/").text
    assert post not in home
    assert f'href="/source/{slug}"' not in home  # nor in the sidebar's list of news sources
    assert post not in client.get("/category/local").text
    assert post in client.get("/category/sports").text
    assert post in client.get(f"/source/{slug}").text
    # Search results mark the matched word, so look for the post's link.
    link = "posts/best-kelaguen-on-island"
    assert link not in client.get("/search?q=kelaguen").text
    assert link in client.get("/search?q=kelaguen&category=sports").text
    assert post in client.get(f"/search?source={slug}").text
    day = (now - timedelta(hours=1)).astimezone(GUAM_TZ).date()
    assert post not in client.get(f"/archive/{day.year}/{day.month}/{day.day}").text
    assert "Andersen to host Cope North" in client.get("/").text  # news is unaffected


def test_obituaries_stay_on_the_obituaries_page(client):
    now = datetime.now(timezone.utc)
    published = now - timedelta(days=3)  # alone on its Guam day
    with session_scope() as session:
        post = session.query(Source).filter_by(slug="postguam").one()
        session.add(
            Article(
                source_id=post.id,
                url="https://www.postguam.com/obituaries/maria-cruz/article_9.html",
                title="Maria Taitano Cruz",
                intro="Maria Taitano Cruz, 85, of Dededo, passed away on Nov. 1.",
                section="Obituaries",
                categories=["obituaries"],
                published_at=published,
            )
        )
    name = "Maria Taitano Cruz"
    home = client.get("/")
    assert name not in home.text
    assert '<a href="/category/obituaries" >Obituaries</a>' in home.text  # in the top menu
    assert name not in client.get("/category/local").text
    page = client.get("/category/obituaries")
    assert page.status_code == 200 and name in page.text
    assert "<title>Guam obituaries" in page.text
    # Search still finds it (people look up a name).
    assert "maria-cruz/article_9" in client.get("/search?q=Taitano").text
    # The archive leaves it out: its day has no other story, so it is empty.
    day = published.astimezone(GUAM_TZ).date()
    assert name not in client.get(f"/archive/{day.year}/{day.month}/{day.day}").text
    month = client.get(f"/archive/{day.year}/{day.month}").text
    assert f'href="/archive/{day.year}/{day.month}/{day.day}"' not in month
    locs = client.get("/sitemap.xml").text
    assert f"/archive/{day.year}/{day.month}/{day.day}<" not in locs
    assert "/category/obituaries<" in locs
    assert "Andersen to host Cope North" in client.get("/").text  # news is unaffected


def test_muted_stories_are_shown_nowhere(client):
    now = datetime.now(timezone.utc)
    published = now - timedelta(days=4)  # alone on their Guam day
    with session_scope() as session:
        kuam = session.query(Source).filter_by(slug="kuam").one()
        session.add_all(
            [
                Article(
                    source_id=kuam.id,
                    url="https://www.kuam.com/story/2/mixer",
                    title="GUAM CHAMBER OF COMMERCE hosts holiday mixer",
                    categories=["business"],
                    published_at=published,
                ),
                Article(
                    source_id=kuam.id,
                    url="https://www.kuam.com/story/3/luncheon",
                    title="Business leaders meet in Tumon",
                    intro="The Guam Chamber's general membership luncheon drew 200 people.",
                    categories=["business"],
                    published_at=published + timedelta(minutes=5),
                ),
            ]
        )
    from guheadlines.web.app import config_cache

    assert config_cache.get().muted == ("chamber of commerce", "guam chamber")
    day = published.astimezone(GUAM_TZ).date()
    pages = [
        "/",
        "/category/business",
        "/source/kuam",
        "/search?q=mixer",
        "/search?q=luncheon",
        "/search?q=chamber",
        f"/archive/{day.year}/{day.month}/{day.day}",
        f"/archive/{day.year}/{day.month}",
    ]
    for path in pages:
        html = client.get(path).text.lower()
        assert "story/2/mixer" not in html and "story/3/luncheon" not in html, path
        if "q=chamber" not in path:  # that page repeats what was searched for
            assert "chamber" not in html, path
    assert f"/archive/{day.year}/{day.month}/{day.day}<" not in client.get("/sitemap.xml").text
    assert "story/1/minimum-wage" in client.get("/search?q=wage").text  # others unaffected


def test_sources_page_lists_sites_that_cannot_be_collected(client):
    with session_scope() as session:
        session.add(Source(slug="pnc", name="PNC News First", enabled=False))  # removed site
    page = client.get("/status").text
    assert "PNC News First" not in page
    blocked = page.split("Can't be collected", 1)[1].split("Recent runs", 1)[0]
    for name in (
        "Marianas Business Journal",
        "Guam Business Magazine",
        "Guam Visitors Bureau",
        "RNZ Pacific",
    ):
        assert name in blocked
    assert "Its firewall (Cloudflare) turns away automated visitors." in blocked
    assert "Andersen Air Force Base" not in blocked  # read from DVIDS since Nov 2026
    sidebar = client.get("/").text.split("Can't be collected", 1)[1]
    assert "Guam Business Magazine" in sidebar and "guambusinessmagazine.com ↗" in sidebar
    assert "RNZ Pacific" in sidebar


def test_r_guam_and_the_community_page_are_gone(client):
    # Removed when Reddit closed its feeds and API (Nov 2026).
    assert client.get("/category/community").status_code == 404
    for page in ("/", "/status", "/search?q=guam", "/sitemap.xml"):
        text = client.get(page).text
        assert "r/guam" not in text and "/category/community" not in text


def test_light_dark_switch(client):
    page = client.get("/").text
    # The switch is in the top bar; the script shows it and picks the theme:
    # the saved choice, else the device setting, else dark.
    assert 'id="theme-toggle"' in page and 'role="switch"' in page
    assert 'saved() || device() || "dark"' in page
    css = client.get("/static/style.css").text
    dark, light = css.split(':root[data-theme="light"] {', 1)
    assert "color-scheme: dark;" in dark.split(":root {", 1)[1]  # dark by default
    assert "--bg: #eeebe3;" in light.split("}", 1)[0]  # light mode is paper, not white
    assert "prefers-color-scheme" not in css  # the script decides, not the CSS
    # Matte: flat surfaces, with no glass blur, gloss or glow.
    assert "backdrop-filter" not in css and "gradient(" not in css


def _contrast(a: str, b: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_text_colors_are_easy_to_read(client):
    # WCAG AA (what PageSpeed's Accessibility score checks): 4.5:1 for text.
    import re

    css = client.get("/static/style.css").text
    themes = {
        "dark": css.split(":root {", 1)[1].split("}", 1)[0],
        "light": css.split(':root[data-theme="light"] {', 1)[1].split("}", 1)[0],
    }
    for theme, block in themes.items():
        color = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{6})", block))
        for text in ("text", "text-soft", "muted", "link"):
            for background in ("bg", "surface", "well", "field-bg"):
                ratio = _contrast(color[text], color[background])
                assert ratio >= 4.5, (theme, text, background, round(ratio, 2))
    # Faded text drops below that, whatever its colour.
    rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
    faded = [sel.strip() for sel, body in rules if "opacity:" in body and "thumb-fill" not in sel]
    assert faded == []


def test_headings_go_down_one_level_at_a_time(client):
    import re
    from itertools import pairwise

    for path in (
        "/",
        "/category/labor",
        "/jobs",
        "/events",
        "/bids",
        "/weather",
        "/status",
        "/search?q=wage",
        "/search",
        "/archive",
    ):
        levels = [int(n) for n in re.findall(r"<h([1-6])[\s>]", client.get(path).text)]
        assert levels[0] == 1, path
        for before, after in pairwise(levels):
            assert after <= before + 1, (path, levels)


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
    # HSTS only over https; browsers ignore it over plain http anyway.
    assert "strict-transport-security" not in page.headers


def test_https_visits_get_hsts(client):
    # Caddy sends X-Forwarded-Proto: https, which Uvicorn turns into the scheme.
    client.base_url = "https://testserver"
    for path in ("/", "/static/style.css", "/nope"):
        assert client.get(path).headers["strict-transport-security"] == "max-age=31536000"


def _photo_tags(html: str) -> list[str]:
    return [tag.split(">")[0] for tag in html.split('<img class="thumb-img" ')[1:]]


def test_larger_screens_get_the_small_photo(client):
    (tag,) = _photo_tags(client.get("/").text)
    assert (
        'srcset="/media/images/2026/09/example-sm.webp 360w, '
        '/media/images/2026/09/example.webp 720w"'
    ) in tag
    assert 'sizes="(max-width: 640px) calc(100vw - 46px), 228px"' in tag
    assert 'width="720" height="480"' in tag
    assert "referrerpolicy" not in tag


def test_photo_falls_back_when_a_copy_is_missing(client):
    # No small copy yet (made by the worker): the saved photo alone.
    _media("images/2026/09/example-sm.webp").unlink()
    (tag,) = _photo_tags(client.get("/").text)
    assert 'src="/media/images/2026/09/example.webp"' in tag and "srcset" not in tag
    # No saved copy at all: the news site's own photo, as for stories saved
    # without one.
    _media("images/2026/09/example.webp").unlink()
    html = client.get("/").text
    assert "/media/" not in html and _photo_tags(html) == []
    with session_scope() as session:
        session.execute(
            update(Article)
            .where(Article.image_path.isnot(None))
            .values(image_url="https://www.postguam.com/photo.jpg")
        )
    (tag,) = _photo_tags(client.get("/").text)
    assert 'src="https://www.postguam.com/photo.jpg"' in tag
    assert 'referrerpolicy="no-referrer"' in tag and "srcset" not in tag


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


def _ld_type(head: str, kind: str) -> dict:
    """The page's one structured-data block of this @type."""
    (found,) = [block for block in _json_ld(head) if block["@type"] == kind]
    return found


def test_pages_describe_themselves_to_search_engines(client):
    head = _head(client.get("/?utm_source=facebook&page=1").text)
    assert "<title>Guam Headlines: latest Guam news today from every newsroom</title>" in head
    # One address per page: tracking parameters and ?page=1 are dropped.
    assert '<link rel="canonical" href="http://testserver/">' in head
    assert '<meta property="og:url" content="http://testserver/">' in head
    assert '<meta property="og:image" content="http://testserver/static/social-card.jpg?v=' in head
    assert '<meta name="twitter:card" content="summary_large_image">' in head
    assert 'name="robots"' not in head
    # The home page names the newsrooms people search for.
    summary = '<meta name="description" content="Today&#39;s Guam news from The Guam Daily Post'
    assert summary in head
    site = _ld_type(head, "WebSite")
    assert site["name"] == "Guam Headlines" and site["url"] == "http://testserver/"
    assert site["about"]["name"] == "Guam"
    assert site["alternateName"] == "testserver"
    # Who runs the site, with its logo.
    org = _ld_type(head, "Organization")
    assert org["@id"] == "http://testserver/#organization" == site["publisher"]["@id"]
    assert org["logo"]["url"].startswith("http://testserver/static/apple-touch-icon.png?v=")
    assert org["areaServed"]["sameAs"][0] == "https://en.wikipedia.org/wiki/Guam"
    assert "sameAs" not in org  # no SOCIAL_PROFILES
    # The page itself: a list of Guam stories, each at its publisher.
    page = _ld_type(head, "CollectionPage")
    assert page["url"] == "http://testserver/" and page["isPartOf"]["@id"].endswith("/#website")
    stories = page["mainEntity"]["itemListElement"]
    assert stories[0]["position"] == 1 and stories[0]["url"].startswith("https://www.")
    assert page["dateModified"].endswith("+10:00")
    # The home page has a heading for search engines and screen readers, and
    # says what the site is in plain words.
    home = client.get("/").text
    heading = (
        '<h1 class="visually-hidden">Guam Headlines: Guam news today, '
        "the latest headlines from every newsroom</h1>"
    )
    assert heading in home
    assert '<h2 id="about-title">About Guam Headlines</h2>' in home
    # Every page links the Guam pages in the words people search for.
    assert (
        '<a href="/jobs">Guam jobs</a>' in home and '<a href="/bids">Guam bids and RFPs</a>' in home
    )

    labor = _head(client.get("/category/labor").text)
    assert "<title>Guam labor and workforce news · Guam Headlines</title>" in labor
    assert "<h1>Guam labor and workforce news</h1>" in client.get("/category/labor").text
    assert '<meta name="description" content="Jobs, wages, workforce, unions' in labor
    assert '<link rel="canonical" href="http://testserver/category/labor">' in labor
    crumbs = _ld_type(labor, "BreadcrumbList")["itemListElement"]
    assert [c["name"] for c in crumbs] == ["Home", "Labor"]
    assert crumbs[0]["item"] == "http://testserver/"
    assert "Organization" not in labor

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
    assert "WebSite" not in second and "Organization" not in second
    # Each page of a list has its own title.
    assert "<title>Latest Guam news, page 2 · Guam Headlines</title>" in second
    assert "About Guam Headlines" not in client.get("/?page=2").text

    kuam = _head(client.get("/source/kuam").text)
    assert "<title>KUAM News headlines · Guam Headlines</title>" in kuam
    assert "Every story links to kuam.com." in kuam


def test_search_and_error_pages_are_kept_out_of_search_results(client):
    for path in ("/search?q=wage", "/search", "/jobs?q=nurse", "/nope"):
        head = _head(client.get(path).text)
        assert '<meta name="robots" content="noindex' in head, path
        assert 'rel="canonical"' not in head, path
    # The Sources page has "Why this site exists" and the disclaimer: listed.
    sources = _head(client.get("/status").text)
    assert 'name="robots"' not in sources
    assert '<link rel="canonical" href="http://testserver/status">' in sources
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


def test_site_domain_gives_the_site_url_when_site_url_is_empty(monkeypatch):
    from guheadlines.settings import _domain_site_url, load_settings

    assert _domain_site_url("guamheadlines.com") == "https://guamheadlines.com"
    assert _domain_site_url(" GuamHeadlines.com, www.guamheadlines.com") == (
        "https://guamheadlines.com"
    )
    assert _domain_site_url("https://guamheadlines.com/") == "https://guamheadlines.com"
    for bad in ("", None, ":80", "localhost", "guam headlines"):
        assert _domain_site_url(bad) == "", bad
    monkeypatch.setenv("SITE_DOMAIN", "guamheadlines.com, www.guamheadlines.com")
    monkeypatch.delenv("SITE_URL", raising=False)
    assert load_settings().site_url == "https://guamheadlines.com"
    monkeypatch.setenv("SITE_URL", "https://www.guamheadlines.com")
    assert load_settings().site_url == "https://www.guamheadlines.com"


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
    assert "Disallow: /search" in robots and "Disallow: /status" not in robots
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
        "/events",
        "/bids",
        "/status",
        "/archive",
        f"/archive/{today.year}/{today.month}",
        f"/archive/{today.year}/{today.month}/{today.day}",
        f"/archive/{old.year}/{old.month}/{old.day}",
    ):
        assert f"http://testserver{path}" in locs, path
    # Topics without stories and search are left out.
    assert "http://testserver/category/sports" not in locs
    assert not [loc for loc in locs if "/search" in loc]
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


def _set(monkeypatch, **values):
    """Change website settings for one test."""
    import dataclasses

    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, **values)
    monkeypatch.setattr(web, "settings", site)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)


def test_sources_page_says_why_the_site_exists_and_disclaims(client):
    page = client.get("/status").text
    about = page.split('id="about"', 1)[1].split("</section>", 1)[0]
    assert "Why this site exists" in about
    about = " ".join(about.split())  # the template wraps its lines
    assert "Search engines such as Google collect and use more than that." in about
    assert "never stores the rest of the story" in about
    disclaimer = page.split('id="disclaimer"', 1)[1].split("</section>", 1)[0]
    disclaimer = " ".join(disclaimer.split())  # the template wraps its lines
    assert "does not write, edit or publish any of the articles" in disclaimer
    assert "Advertising" not in disclaimer  # only while ads are shown
    # Every page links to both from the footer.
    footer = client.get("/jobs").text.split('<footer class="footer">', 1)[1]
    assert 'href="/status#about"' in footer and 'href="/status#disclaimer"' in footer


def test_site_and_scraper_do_not_mention_github(client, monkeypatch):
    from guheadlines.settings import load_settings

    _set(monkeypatch, adsense_client="ca-pub-1234567890123456", adsense_slot="9876543210")
    pages = ("/", "/status", "/jobs", "/events", "/bids", "/weather", "/category/obituaries")
    for path in (*pages, "/robots.txt", "/sitemap.xml", "/events.ics", "/nope"):
        assert "github" not in client.get(path).text.lower(), path
    assert "github" not in load_settings().user_agent.lower()


def test_no_ads_until_adsense_is_set_up(client):
    for path in ("/", "/status", "/jobs", "/events", "/bids", "/category/obituaries"):
        page = client.get(path)
        assert "adsbygoogle" not in page.text and "google-adsense-account" not in page.text
        assert "'nonce-" not in page.headers["content-security-policy"]
    assert client.get("/ads.txt").status_code == 404
    assert "Mediapartners-Google" not in client.get("/robots.txt").text


def test_ad_block_on_the_home_sources_jobs_events_bids_and_obituaries_pages(client, monkeypatch):
    _set(monkeypatch, adsense_client="ca-pub-1234567890123456", adsense_slot="9876543210")
    nonces = set()
    for path in (
        "/",
        "/status",
        "/jobs",
        "/jobs?q=nurse",
        "/events",
        "/bids",
        "/category/obituaries",
    ):
        page = client.get(path)
        csp = page.headers["content-security-policy"]
        nonce = csp.split("'nonce-", 1)[1].split("'", 1)[0]
        nonces.add(nonce)
        assert "'strict-dynamic'" in csp and "frame-src https:" in csp
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
        sidebar = page.text.split('<aside class="sidebar">', 1)[1]
        assert 'data-ad-client="ca-pub-1234567890123456"' in sidebar
        assert 'data-ad-slot="9876543210"' in sidebar
        # One script, with the nonce, fills the slot and loads the AdSense code
        # on the first scroll, tap, key press or mouse move, not with the page.
        assert sidebar.count(f'nonce="{nonce}"') == 1
        assert "<script async src=" not in page.text
        assert '"scroll", "pointerdown", "pointermove", "keydown", "touchstart"' in sidebar
        assert (
            'script.src = "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js'
            '?client=ca-pub-1234567890123456";'
        ) in sidebar
        # An ad blocker stops the code: the empty blocks are then taken away.
        hide = (
            'document.querySelectorAll(".ad-slot")'
            ".forEach(function (slot) { slot.hidden = true; });"
        )
        assert hide in sidebar
        # Without ADSENSE_STORIES_SLOT: no ad between the stories.
        assert "ad-between" not in page.text
    assert len(nonces) == 7  # a new nonce for every page

    # Other pages keep the strict policy and show no ad.
    for path in ("/category/military", "/search?q=wage", "/archive"):
        page = client.get(path)
        assert "adsbygoogle" not in page.text
        assert "'nonce-" not in page.headers["content-security-policy"]
        # Google reads this tag to confirm the site is the AdSense account's.
        assert '<meta name="google-adsense-account" content="ca-pub-1234567890123456">' in page.text

    ads_txt = client.get("/ads.txt")
    assert ads_txt.text == "google.com, pub-1234567890123456, DIRECT, f08c47fec0942fa0\n"
    robots = client.get("/robots.txt").text
    assert "\nUser-agent: Mediapartners-Google\nDisallow:\n\nUser-agent: *\n" in robots
    assert "Advertising and cookies" in client.get("/status").text

    # A publisher ID without an ad unit: the site can be verified, but no ad block.
    _set(monkeypatch, adsense_client="ca-pub-1234567890123456", adsense_slot="")
    page = client.get("/")
    assert "adsbygoogle" not in page.text and "google-adsense-account" in page.text
    assert "'nonce-" not in page.headers["content-security-policy"]


def test_second_ad_between_the_stories(client, monkeypatch):
    _set(
        monkeypatch,
        adsense_client="ca-pub-1234567890123456",
        adsense_slot="9876543210",
        adsense_stories_slot="1122334455",
    )
    # Two stories: the ad goes after the fifth story, so not yet.
    assert "ad-between" not in client.get("/").text
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        kuam = session.query(Source).filter_by(slug="kuam").one()
        session.add_all(
            Article(
                source_id=kuam.id,
                url=f"https://www.kuam.com/story/{n}/news",
                title=f"Story number {n}",
                published_at=now - timedelta(minutes=n),
            )
            for n in range(10, 16)
        )
    page = client.get("/")
    main = page.text.split('<main id="main">', 1)[1].split("</main>", 1)[0]
    assert main.count('<div class="panel ad-slot ad-between">') == 1
    before, after = main.split("ad-between", 1)
    assert before.count('<article class="story') == 5 and after.count('<article class="story') == 3
    unit = after.split("</div>", 1)[0]
    assert 'data-ad-client="ca-pub-1234567890123456" data-ad-slot="1122334455"' in unit
    # Its script only puts the unit in the queue, with the page's nonce. The
    # block in the right-hand column loads the AdSense code for both units.
    nonce = page.headers["content-security-policy"].split("'nonce-", 1)[1].split("'", 1)[0]
    assert f'<script nonce="{nonce}">(adsbygoogle = window.adsbygoogle || []).push({{}});' in unit
    assert "adsbygoogle.js" not in main
    sidebar = page.text.split('<aside class="sidebar">', 1)[1]
    assert 'data-ad-slot="9876543210"' in sidebar and "adsbygoogle.js" in sidebar

    # Pages without the ad block have no ad between the stories either.
    assert "adsbygoogle" not in client.get("/source/kuam").text
    # It needs the first ad unit too.
    _set(monkeypatch, adsense_slot="")
    assert "adsbygoogle" not in client.get("/").text


def test_adsense_settings_must_be_ids():
    from guheadlines.settings import _adsense_client, _adsense_slot

    assert _adsense_client(" ca-pub-1234567890123456 ") == "ca-pub-1234567890123456"
    assert _adsense_client("pub-1234567890123456") == "ca-pub-1234567890123456"
    # The other forms that AdSense shows the ID in: the ads.txt line, the
    # verification meta tag and the AdSense code.
    for pasted in (
        '"ca-pub-1234567890123456"',
        "google.com, pub-1234567890123456, DIRECT, f08c47fec0942fa0",
        '<meta name="google-adsense-account" content="ca-pub-1234567890123456">',
        '<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js'
        '?client=ca-pub-1234567890123456" crossorigin="anonymous"></script>',
    ):
        assert _adsense_client(pasted) == "ca-pub-1234567890123456", pasted
    for bad in ("", None, "ca-pub-12", "ca-pub-1234567890123456x", "xpub-1234567890123456"):
        assert _adsense_client(bad) == ""
    assert _adsense_slot(" 9876543210 ") == "9876543210"
    for bad in ("", None, "slot", "98765 43210", '9876543210"'):
        assert _adsense_slot(bad) == ""


# A person in a browser. Over https, browsers also send Sec-Fetch-Mode.
BROWSER = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "sec-fetch-mode": "navigate",
}


@pytest.fixture()
def counting(client, monkeypatch):
    """Visitor stats on, with nothing counted yet."""
    from guheadlines.web import stats

    _set(monkeypatch, stats_password="a long stats password")
    monkeypatch.setattr(stats, "counts", stats.Counts())
    client.base_url = "https://testserver"
    return client


def _article_id(url_part: str) -> int:
    with session_scope() as session:
        return session.query(Article.id).filter(Article.url.contains(url_part)).scalar()


def _counted() -> tuple[dict, dict]:
    from guheadlines.models import PageViews, StoryClicks
    from guheadlines.web import stats

    stats.counts.flush()
    with session_scope() as session:
        views = {row.path: row.views for row in session.query(PageViews)}
        clicks = {row.article_id: row.clicks for row in session.query(StoryClicks)}
    return views, clicks


def test_story_links_go_through_the_counting_stop(counting):
    story = "https://www.postguam.com/news/local/a/article_1.html"
    story_id = _article_id("article_1")
    html = counting.get("/").text
    # Headline, photo and "read the full story" open /go/<id>, which search
    # engines are asked not to follow; the outlet's name still opens its site.
    assert html.count(f'href="/go/{story_id}" target="_blank" rel="nofollow noopener"') == 3
    assert f'href="{story}"' not in html
    assert '<a class="source" href="https://www.postguam.com/"' in html
    assert f'href="/go/{story_id}"' in counting.get("/search?q=Andersen").text
    go = counting.get(f"/go/{story_id}", follow_redirects=False)
    assert go.status_code == 302 and go.headers["location"] == story
    assert go.headers["cache-control"] == "no-store"
    # Only stored stories: no address can be passed in, so no open redirect.
    assert counting.get("/go/999999", follow_redirects=False).status_code == 404
    assert counting.get(f"/go/{2**63}", follow_redirects=False).status_code == 404
    assert counting.get("/go/https://example.com", follow_redirects=False).status_code == 404
    assert counting.get("/go/abc", follow_redirects=False).status_code == 422
    assert "Disallow: /go/\n" in counting.get("/robots.txt").text


def test_readers_are_counted_and_robots_are_not(counting):
    story_id = _article_id("article_1")
    for path in ("/", "/?page=1", "/category/labor", "/search?q=secret+words", "/nope"):
        counting.get(path, headers=BROWSER)
    counting.get(f"/go/{story_id}", headers=BROWSER, follow_redirects=False)
    counting.get(f"/go/{story_id}", headers=BROWSER, follow_redirects=False)
    # Not counted: robots, link previews, PageSpeed, pages the browser loads
    # ahead, and programs that leave out what browsers send over https.
    for headers in (
        {"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
        {"user-agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"},
        {"user-agent": BROWSER["user-agent"] + " Chrome-Lighthouse"},
        {**BROWSER, "sec-purpose": "prefetch;prerender"},
        {**BROWSER, "sec-fetch-mode": "no-cors"},
        {"user-agent": BROWSER["user-agent"]},
        {"user-agent": "python-httpx/0.28.1"},
    ):
        counting.get("/", headers=headers)
        counting.get(f"/go/{story_id}", headers=headers, follow_redirects=False)
    views, clicks = _counted()
    # Search words are never stored; 404 pages are not counted.
    assert views == {"/": 2, "/category/labor": 1, "/search": 1}
    assert clicks == {story_id: 2}
    # Totals add up across writes.
    counting.get("/", headers=BROWSER)
    assert _counted()[0]["/"] == 3


def test_over_plain_http_browsers_are_counted_without_sec_fetch(counting):
    counting.base_url = "http://testserver"
    counting.get("/", headers={"user-agent": BROWSER["user-agent"]})
    assert _counted()[0] == {"/": 1}


def test_nothing_is_counted_while_stats_are_off(client):
    from guheadlines.web import stats

    story_id = _article_id("article_1")
    client.get("/", headers=BROWSER)
    client.get(f"/go/{story_id}", headers=BROWSER, follow_redirects=False)
    assert stats.counts._views == {} and stats.counts._clicks == {}
    # Old links still lead to the story.
    go = client.get(f"/go/{story_id}", follow_redirects=False)
    assert go.headers["location"] == "https://www.postguam.com/news/local/a/article_1.html"
    assert client.get("/stats").status_code == 404
    assert "Visitor counts" not in client.get("/status").text


def test_counts_are_kept_when_the_database_cannot_be_reached(counting, monkeypatch):
    from guheadlines.web import stats

    story_id = _article_id("article_1")
    counting.get("/", headers=BROWSER)
    counting.get(f"/go/{story_id}", headers=BROWSER, follow_redirects=False)

    def broken():
        raise RuntimeError("database down")

    with monkeypatch.context() as patched:
        patched.setattr(stats, "session_scope", broken)
        stats.counts.flush()
    # A click on a story that was deleted before the write is left out.
    with session_scope() as session:
        session.query(Article).filter(Article.id == story_id).delete()
    assert _counted() == ({"/": 1}, {})


def test_counts_are_written_when_the_website_stops(counting):
    from guheadlines.web.app import app

    with TestClient(app, base_url="https://testserver") as running:
        running.get("/", headers=BROWSER)
    from guheadlines.models import PageViews

    with session_scope() as session:
        assert session.query(PageViews.views).filter(PageViews.path == "/").scalar() == 1


def _basic(password: str) -> dict:
    return {"authorization": "Basic " + base64.b64encode(f"me:{password}".encode()).decode()}


def test_guessing_the_password_gets_an_address_blocked(counting, monkeypatch):
    from guheadlines.web import app as web

    sent = []
    monkeypatch.setattr(web.alerts, "send", lambda settings, alert: sent.append(alert))
    _set(
        monkeypatch,
        stats_password="a long stats password",
        discord_webhook_url="https://discord.com/api/webhooks/1/x",
    )
    monkeypatch.setattr(web, "login_limit", web.LoginLimit(tries=3, window=900, block=3600))
    monkeypatch.setitem(web._blocked_alert, "sent", float("-inf"))
    # The browser's first visit, without a password, is not a wrong guess.
    for _ in range(5):
        assert counting.get("/stats").status_code == 401
    assert counting.get("/stats", headers=_basic("wrong 1")).status_code == 401
    assert counting.get("/admin/guides", headers=_basic("wrong 2")).status_code == 401
    third = counting.get("/advertise/review", headers=_basic("wrong 3"))
    assert third.status_code == 429
    assert third.headers["retry-after"] == "3600"
    assert "Too many wrong passwords" in third.text
    # Blocked: even the right password is refused now, on every private page.
    right = _basic("a long stats password")
    for path in ("/stats", "/admin/guides", "/admin/listings", "/stats/backups/x.tar"):
        assert counting.get(path, headers=right).status_code == 429, path
    assert counting.post("/admin/guides/1/delete", headers=right).status_code == 429
    # The owner hears about it in Discord, once.
    for _ in range(100):
        if sent:
            break
        time.sleep(0.01)
    assert len(sent) == 1
    assert "testclient" in sent[0].message and "STATS_PASSWORD" in sent[0].message
    # Another address is not blocked.
    other = TestClient(web.app, base_url="https://testserver", client=("203.0.113.9", 4000))
    assert other.get("/stats", headers=right).status_code == 200


def test_login_limit_forgets_old_guesses_and_ends_the_block(monkeypatch):
    from guheadlines.web import app as web

    now = [1000.0]
    monkeypatch.setattr(web.time, "monotonic", lambda: now[0])
    limit = web.LoginLimit(tries=3, window=900, block=3600)
    assert not limit.wrong("a") and not limit.wrong("a")
    now[0] += 901  # the first two are older than the window now
    assert not limit.wrong("a") and not limit.wrong("a")
    assert limit.wrong("a")  # the third within 15 minutes blocks
    assert limit.blocked("a") and not limit.blocked("b")
    now[0] += 3599
    assert limit.blocked("a")
    now[0] += 2
    assert not limit.blocked("a")
    limit.wrong("b")
    assert "a" not in limit._blocked  # forgotten once its block ended


def test_a_short_password_is_pointed_out_on_the_stats_page(counting, monkeypatch):
    _set(monkeypatch, stats_password="short1")
    page = counting.get("/stats", headers=_basic("short1"))
    assert page.status_code == 200
    assert "Your password (STATS_PASSWORD) is short." in page.text
    _set(monkeypatch, stats_password="a long stats password")
    page = counting.get("/stats", headers=_basic("a long stats password"))
    assert "is short." not in page.text


def test_huge_requests_are_refused_before_they_are_read():
    from guheadlines import guides
    from guheadlines.web import app as web

    client = TestClient(web.app)
    origin = {"origin": "http://testserver"}
    # Says it is larger than any form: refused at once, whatever the page.
    for path in ("/newsletter", "/admin/guides/new", "/advertise/booking/abc/again"):
        big = client.post(
            path, content=b"", headers={**origin, "content-length": str(web.MAX_REQUEST_BYTES + 1)}
        )
        assert big.status_code == 413, path
    # Sent in pieces, without a size: the small forms stop reading at their limit.
    pieces = (b"day=2026-12-07&" for _ in range(1000))
    answer = client.post("/advertise/booking/abc/again", content=pieces, headers=origin)
    assert answer.status_code == 413
    # Caddy refuses them too, and the largest form still fits.
    caddyfile = (Path(__file__).parent.parent / "caddy" / "Caddyfile").read_text()
    assert "max_size 20MB" in caddyfile
    assert web.MAX_REQUEST_BYTES == 20 * 1024 * 1024 > guides.MAX_FORM


def test_stats_page_needs_the_password(counting):
    import base64

    def login(password: str) -> dict:
        return {"authorization": "Basic " + base64.b64encode(password.encode()).decode()}

    page = counting.get("/stats")
    assert page.status_code == 401
    assert page.headers["www-authenticate"].startswith("Basic ")
    assert counting.get("/stats", headers=login("anyone:wrong")).status_code == 401
    assert counting.get("/stats", headers={"authorization": "Basic %%%"}).status_code == 401
    assert counting.get("/stats", headers={"authorization": "Bearer x"}).status_code == 401

    story_id = _article_id("article_1")
    counting.get("/", headers=BROWSER)
    counting.get("/category/military", headers=BROWSER)
    counting.get(f"/go/{story_id}", headers=BROWSER, follow_redirects=False)
    page = counting.get("/stats", headers={**BROWSER, **login("stats:a long stats password")})
    assert page.status_code == 200
    # Private: never stored by a browser or a proxy, and kept out of search.
    assert page.headers["cache-control"] == "private, no-store"
    assert page.headers["x-robots-tag"] == "noindex, nofollow"
    assert '<meta name="robots" content="noindex, nofollow">' in _head(page.text)
    html = page.text
    assert "<dt>Page views</dt><dd>2</dd>" in html and "<dt>Story clicks</dt><dd>1</dd>" in html
    top = html.split("Top stories", 1)[1].split("Topics", 1)[0]
    assert "Andersen to host Cope North exercise" in top and "The Guam Daily Post" in top
    topics = html.split("<th>Topic</th>", 1)[1].split("</table>", 1)[0]
    assert '<tr><td>Military</td><td class="num">1</td><td class="num">1</td></tr>' in topics
    assert "Home (Latest)" in html and "github" not in html.lower()
    # The stats page does not count itself, and the other periods work.
    assert "/stats" not in _counted()[0]
    for days in (1, 30, 90, 365, 12):
        page = counting.get(f"/stats?days={days}", headers=login(":a long stats password"))
        assert page.status_code == 200, days
    assert counting.get("/stats?days=0", headers=login(":a long stats password")).status_code == 422


def test_sources_page_says_that_visits_are_counted(counting):
    disclaimer = counting.get("/status").text.split('id="disclaimer"', 1)[1]
    assert "Visitor counts." in disclaimer and "no IP address" in disclaimer


def test_reader_check():
    from starlette.requests import Request

    from guheadlines.web.stats import is_reader

    def request(headers: dict, method: str = "GET", scheme: str = "https") -> Request:
        raw = [(k.encode(), v.encode()) for k, v in headers.items()]
        return Request(
            {
                "type": "http",
                "method": method,
                "scheme": scheme,
                "path": "/",
                "query_string": b"",
                "headers": raw,
                "server": ("x", 443),
            }
        )

    assert is_reader(request(BROWSER))
    assert not is_reader(request(BROWSER, method="HEAD"))
    iphone = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBAV/490.0.0.38.109]"
    )
    assert is_reader(request({"user-agent": iphone, "sec-fetch-mode": "navigate"}))
    for robot in (
        "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2)",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/131.0.0.0",
        "Mozilla/5.0 (compatible; Google-InspectionTool/1.0;)",
        "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
        "Mozilla/5.0 (compatible; GUHeadlinesBot/1.0)",
        "curl/8.5.0",
        "",
    ):
        assert not is_reader(request({"user-agent": robot, "sec-fetch-mode": "navigate"})), robot


def _arrivals() -> dict:
    from guheadlines.models import Referrers
    from guheadlines.web import stats

    stats.counts.flush()
    with session_scope() as session:
        return {row.site: row.visits for row in session.query(Referrers)}


def test_the_websites_that_visits_come_from_are_counted(counting):
    import base64

    for referrer in (
        "https://www.google.com/",
        "https://www.google.com/",
        "https://lm.facebook.com/l.php?u=https%3A%2F%2Fguamheadlines.com%2F&h=AT0",
        "https://m.facebook.com/",
        "https://news.google.com/stories/abc?hl=en-US",
        "android-app://com.google.android.gm/",
        "javascript:alert(1)",
        "https://" + "x" * 120 + ".com/",
        "not an address",
    ):
        counting.get("/", headers={**BROWSER, "referer": referrer})
    counting.get("/", headers=BROWSER)  # typed, or a bookmark
    # Moving between the site's own pages is not a new visit.
    counting.get("/category/labor", headers={**BROWSER, "referer": "https://testserver/"})
    # Robots and missing pages are not counted.
    robot = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    counting.get("/", headers={"user-agent": robot, "referer": "https://spam.example/"})
    counting.get("/nope", headers={**BROWSER, "referer": "https://spam.example/"})
    # Only the site's name is kept: no address, page or search words.
    assert _arrivals() == {"google.com": 2, "facebook.com": 2, "news.google.com": 1, "": 5}

    login = {"authorization": "Basic " + base64.b64encode(b":a long stats password").decode()}
    page = counting.get("/stats", headers=login).text
    table = page.split("Where readers come from", 1)[1].split("</table>", 1)[0]
    rows = [" ".join(row.split()) for row in table.split("<tr>")[2:]]
    assert rows[0].startswith('<td><span class="muted">No website</span></td><td class="num">5')
    # Most visits first; a tie in name order.
    assert rows[1].startswith('<td>facebook.com</td><td class="num">2</td><td class="num">20%')
    assert rows[2].startswith('<td>google.com</td><td class="num">2</td><td class="num">20%')


def test_stats_page_shows_source_health(counting):
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        kuam = session.scalar(select(Source).where(Source.slug == "kuam"))
        kuam.last_success_at = now - timedelta(days=2)
        kuam.last_error = "HTTP 403 Forbidden"
        post = session.scalar(select(Source).where(Source.slug == "postguam"))
        post.last_run_at = post.last_success_at = now
    login = {"authorization": "Basic " + base64.b64encode(b":a long stats password").decode()}
    page = counting.get("/stats", headers=login).text
    assert '<h2 class="section-title" id="health">Source health</h2>' in page
    top, health = page.split('id="health"', 1)
    assert "KUAM News" in health and "HTTP 403 Forbidden" in health
    assert '<span class="error">Failing</span>' in health
    # The Post's only story is 2 hours old: working, quiet after 30 days
    # (too few stories to tell).
    post_row = health.split("The Guam Daily Post", 1)[1].split("</tr>", 1)[0]
    assert "Working" in post_row and "30 days" in post_row
    # The problems are named at the top of the page.
    assert "1 source needs a look:" in top and "KUAM News (failing)" in top
    assert "Discord alerts are off (DISCORD_WEBHOOK_URL is empty)." in health
    assert "Keyword alerts: off (ALERT_KEYWORDS is empty)." in health


def test_stats_page_names_the_alert_words(counting, monkeypatch):
    _set(
        monkeypatch,
        stats_password="a long stats password",
        discord_webhook_url="https://discord.com/api/webhooks/1/x",
        alert_keywords=("typhoon", "power outage"),
    )
    login = {"authorization": "Basic " + base64.b64encode(b":a long stats password").decode()}
    health = counting.get("/stats", headers=login).text.split('id="health"', 1)[1]
    assert "Keyword alerts: a new story that mentions typhoon, power outage is posted" in health
    assert "A site that fails for 24 hours, or goes quiet, is posted in Discord." in health
    assert "needs a look" not in health


def test_ai_assistants_get_a_plain_summary_and_are_welcome(client):
    robots = client.get("/robots.txt").text
    assert "OAI-SearchBot" in robots and "PerplexityBot" in robots and "Claude-SearchBot" in robots
    assert (
        "# A plain-text summary of the site for AI assistants: http://testserver/llms.txt" in robots
    )
    # No crawler is turned away from the pages.
    assert "Disallow: /\n" not in robots and "Disallow: /llms" not in robots

    page = client.get("/llms.txt")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/markdown")
    text = page.text
    assert text.startswith("# Guam Headlines\n\n> Guam Headlines (testserver) collects Guam news")
    assert "- [Guam weather and tides](http://testserver/weather):" in text
    assert "- [Guam military news](http://testserver/category/military):" in text
    assert "- [KUAM News](http://testserver/source/kuam): kuam.com" in text
    # The newest headlines, each linked to the publisher's story.
    assert "## Latest Guam headlines" in text
    assert "- [Andersen to host Cope North exercise](https://www.postguam.com/news/" in text
    assert "github" not in text.lower() and "@" not in text


def test_indexnow_key_file(client, monkeypatch):
    from guheadlines import indexnow
    from guheadlines.web import app as web

    key = indexnow.key(web.settings)
    page = client.get(f"/{key}.txt")
    assert page.status_code == 200 and page.text == key
    assert client.get("/0123456789abcdef0123456789abcdef.txt").status_code == 404
    # The other text files still answer as before.
    assert client.get("/robots.txt").text.startswith("# Search engines")
    _set(monkeypatch, indexnow=False)
    assert client.get(f"/{key}.txt").status_code == 404


def test_social_profiles_tell_search_engines_where_else_the_site_is(client, monkeypatch):
    from guheadlines.settings import _web_addresses

    assert _web_addresses(
        " https://www.facebook.com/guamheadlines , mailto:x@y.z, facebook.com/x,"
        " https://www.facebook.com/guamheadlines, https://www.instagram.com/guamheadlines"
    ) == ("https://www.facebook.com/guamheadlines", "https://www.instagram.com/guamheadlines")

    import dataclasses

    from guheadlines.web import seo

    monkeypatch.setattr(
        seo, "settings", dataclasses.replace(seo.settings, social_profiles=("https://x.com/gu",))
    )
    org = _ld_type(_head(client.get("/").text), "Organization")
    assert org["sameAs"] == ["https://x.com/gu"]
    about = _head(client.get("/status").text)
    assert _ld_type(about, "AboutPage")["about"]["name"] == "Guam"
    assert _ld_type(about, "Organization")["sameAs"] == ["https://x.com/gu"]


def test_guam_pages_have_guam_titles(client):
    for path, title in (
        ("/jobs", "<title>Guam jobs: the latest job openings on Guam · Guam Headlines</title>"),
        ("/events", "<title>Guam events calendar: things to do on Guam · Guam Headlines</title>"),
        ("/bids", "<title>Guam bids and RFPs: Government of Guam procurement · "),
        ("/archive", "<title>Guam news archive · Guam Headlines</title>"),
        ("/status", "<title>About Guam Headlines and its news sources · Guam Headlines</title>"),
    ):
        head = _head(client.get(path).text)
        assert title in head, path
        assert _json_ld(head), path
