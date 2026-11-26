"""Visitor guides (guides.py): the Markdown text, the owner's editor, the
pages readers see, the photos, the sponsor box and the starter guides."""

from __future__ import annotations

import base64
import dataclasses
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import make_jpeg
from fastapi.testclient import TestClient
from sqlalchemy import select

from guheadlines import guides
from guheadlines.db import session_scope
from guheadlines.models import Guide
from guheadlines.scraper.text import GUAM_TZ

FROM_SITE = {"origin": "http://testserver"}
PASSWORD = "guides-password"
ADMIN = {"authorization": "Basic " + base64.b64encode(f"me:{PASSWORD}".encode()).decode()}
OWNER = {**ADMIN, **FROM_SITE}

TEXT = """Guam is warm all year.

## Getting there

Fly in to GUM.

## Where to stay

Most hotels are in Tumon.

## What to eat

Red rice and kelaguen.
"""

FORM = {
    "title": "Getting around Guam",
    "slug": "",
    "section": "Start here",
    "position": "20",
    "summary": "Cars, shuttles and taxis.",
    "body": TEXT,
    "status": "draft",
}


# ------------------------------------------------------------- the text


def test_markdown_is_safe():
    text = (
        "<script>alert(1)</script> [bad](javascript:alert(1)) [data](data:text/html,x)"
        " [site](//evil.example) [weather](/weather) [call](tel:+16715550123)"
        " [out](https://example.com) ![theirs](https://example.com/photo.jpg)"
        "\n\n| a | b |\n|--:|:-:|\n| 1 | 2 |"
    )
    html = str(guides.render(text).parts[0])
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html.replace("[bad](javascript:alert(1))", "")
    assert 'href="data:' not in html and 'href="//evil' not in html
    assert '<a href="/weather">weather</a>' in html
    assert '<a href="tel:+16715550123">call</a>' in html
    assert '<a href="https://example.com" target="_blank" rel="nofollow noopener">out</a>' in html
    # Only photos uploaded for a guide are shown; another site's photo is its words.
    assert "<img" not in html and "theirs" in html
    # The page's security rules allow no style="" attributes.
    assert "style=" not in html and '<th class="num">a</th>' in html
    assert '<div class="table-wrap"><table>' in html


def test_an_uploaded_photo_gets_its_size_and_small_copy():
    path = "/media/guides/3-0123456789ab-1400x933.webp"
    html = str(guides.render(f'Look: ![Tumon Bay]({path} "At <dusk>") **after**').parts[0])
    assert f'src="{path}"' in html and 'width="1400" height="933"' in html
    assert f'srcset="{guides.small_photo(path)} 700w, {path} 1400w"' in html
    assert 'loading="lazy"' in html and 'alt="Tumon Bay"' in html
    assert '<span class="caption">At &lt;dusk&gt;</span>' in html
    assert "<strong>after</strong>" in html  # the text after a photo is not escaped
    assert guides.photo_srcset("/media/guides/3-0123456789ab-600x400.webp") is None
    assert guides.photo_size("/media/guides/../x-1x1.webp") is None


def test_the_text_is_cut_at_main_headings():
    text = guides.render("Intro.\n\n# One\n\nA.\n\n## One\n\n### Small\n\nB.\n\n## Two\n\nC.")
    assert len(text.parts) == 4 and str(text.parts[0]).strip() == "<p>Intro.</p>"
    assert text.contents == [("one", "One"), ("one-2", "One"), ("two", "Two")]
    assert '<h2 id="one-2">One</h2>' in text.parts[2] and "<h3>Small</h3>" in text.parts[2]
    # No opening text: the first part is the first heading's.
    assert str(guides.render("## Only\n\nText.").parts[0]).startswith('<h2 id="only">')


def test_slugify():
    assert guides.slugify("Håfa Adai: Two Lovers' Point!") == "hafa-adai-two-lovers-point"
    assert guides.slugify("  A morning in Hagåtña  ") == "a-morning-in-hagatna"
    assert len(guides.slugify("word " * 40)) <= guides.MAX_SLUG


def test_the_form_is_checked():
    values, errors = guides.check(FORM, has_photo=False)
    assert errors == {} and values["slug"] == "getting-around-guam"
    assert values["position"] == 20 and values["status"] == "draft"

    _, errors = guides.check({**FORM, "title": "", "summary": "", "body": " "}, has_photo=True)
    assert set(errors) == {"title", "summary", "body", "photo_alt"}

    _, errors = guides.check({**FORM, "slug": "Bad Address!", "position": "x"}, has_photo=False)
    assert set(errors) == {"slug", "position"}

    values, errors = guides.check(
        {**FORM, "sponsor_link": "islandtours.com", "sponsor_until": "2026-12-31"}, False
    )
    assert errors == {"sponsor_name": "Please give the sponsor's name too."}
    assert values["sponsor_link"] == "https://islandtours.com"
    assert values["sponsor_until"] == date(2026, 12, 31)
    _, errors = guides.check({**FORM, "sponsor_name": "X", "sponsor_link": "not a link"}, False)
    assert set(errors) == {"sponsor_link"}


def test_the_starter_guides():
    starters = guides.starters()
    assert len(starters) >= 6
    slugs = {s.slug for s in starters}
    assert len(slugs) == len(starters)
    pages = {"/weather", "/events", "/category/food"} | {f"/guides/{s}" for s in slugs}
    for s in starters:
        values, errors = guides.check(
            {"title": s.title, "slug": s.slug, "summary": s.summary, "body": s.body}, False
        )
        assert errors == {}, (s.slug, errors)
        text = s.body.lower()
        assert "github" not in text and "chamber of commerce" not in text
        # Every link inside the site goes to a page that exists.
        for link in re.findall(r"\]\((/[^)\s]*)\)", s.body):
            assert link in pages, (s.slug, link)
        assert len(guides.render(s.body).contents) >= 3, s.slug


def test_the_sitemap_lists_published_guides():
    from starlette.requests import Request

    from guheadlines.web import seo

    request = Request({"type": "http", "scheme": "https", "server": ("x.com", 443),
                       "path": "/sitemap.xml", "headers": []})  # fmt: skip
    changed = datetime(2026, 11, 20, tzinfo=timezone.utc)
    newer = datetime(2026, 11, 25, tzinfo=timezone.utc)
    data = {"latest": None, "categories": {}, "sources": {}, "days": []}
    urls = dict(seo.sitemap_entries(request, data, [], [], guides=[("a", changed), ("b", newer)]))
    assert urls["https://x.com/guides"] == newer
    assert urls["https://x.com/guides/a"] == changed
    assert "https://x.com/guides" not in dict(seo.sitemap_entries(request, data, [], []))


# ------------------------------------------------------------ the site


def _set(monkeypatch, **values):
    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, **values)
    monkeypatch.setattr(web, "settings", site)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)
    return site


@pytest.fixture()
def client(clean_db, monkeypatch):
    from guheadlines.web.app import app, guide_cache, sidebar_cache, sitemap_cache

    for cache in (guide_cache, sidebar_cache, sitemap_cache):
        cache._data.clear()
    _set(monkeypatch, stats_password=PASSWORD, adsense_client="", adsense_slot="")
    yield TestClient(app)
    guide_cache._data.clear()


def _save(client, guide_id=None, cover=None, picture=None, **fields):
    # Browsers send a file field empty when no file is chosen.
    empty = ("", b"", "application/octet-stream")
    files = {
        "cover": ("cover.jpg", cover, "image/jpeg") if cover else empty,
        "picture": ("food.jpg", picture, "image/jpeg") if picture else empty,
    }
    return client.post(
        f"/admin/guides/{guide_id or 'new'}",
        data={**FORM, **fields},
        files=files,
        headers=OWNER,
        follow_redirects=False,
    )


def _new(client, **fields) -> Guide:
    response = _save(client, **fields)
    assert response.status_code == 303, response.text
    found = re.fullmatch(r"/admin/guides/(\d+)\?done=saved", response.headers["location"])
    return _get(int(found[1]))


def _get(guide_id: int) -> Guide | None:
    with session_scope() as session:
        return session.get(Guide, guide_id)


def test_the_editor_needs_the_password_and_this_site(client, monkeypatch):
    assert client.get("/admin/guides").status_code == 401
    assert client.get("/admin/guides", headers=ADMIN).status_code == 200
    response = client.post(
        "/admin/guides/new",
        data=FORM,
        headers={**ADMIN, "origin": "https://evil.example"},
    )
    assert response.status_code == 403
    response = client.post("/admin/guides/new", data=FORM, headers=FROM_SITE)
    assert response.status_code == 401
    with session_scope() as session:
        assert session.scalar(select(Guide)) is None
    _set(monkeypatch, stats_password="")
    assert client.get("/admin/guides", headers=ADMIN).status_code == 404


def test_a_guide_from_draft_to_the_site(client):
    guide = _new(client)
    assert guide.slug == "getting-around-guam" and guide.status == "draft"
    assert guide.published_at is None

    # A draft is the owner's alone.
    assert client.get("/guides").status_code == 404
    assert client.get("/guides/getting-around-guam").status_code == 404
    assert 'href="/guides"' not in client.get("/").text
    preview = client.get(f"/admin/guides/{guide.id}/view", headers=ADMIN)
    assert preview.status_code == 200 and "Preview of a draft" in preview.text
    assert '<meta name="robots" content="noindex, nofollow">' in preview.text
    assert "no-store" in preview.headers["cache-control"]
    assert client.get(f"/admin/guides/{guide.id}/view").status_code == 401

    response = _save(client, guide.id, status="published", slug="getting-around")
    assert response.status_code == 303
    guide = _get(guide.id)
    assert guide.status == "published" and guide.published_at is not None

    index = client.get("/guides")
    assert index.status_code == 200
    assert '<h2 class="section-title">Start here</h2>' in index.text
    assert '<a href="/guides/getting-around">Getting around Guam</a>' in index.text
    assert 'href="/guides" aria-current="page">Guides</a>' in index.text

    page = client.get("/guides/getting-around")
    assert page.status_code == 200
    assert "<h1>Getting around Guam</h1>" in page.text
    assert '<a href="#where-to-stay">Where to stay</a>' in page.text  # On this page
    assert '"@type":"Article"' in page.text and '"headline":"Getting around Guam"' in page.text
    assert '<link rel="canonical" href="http://testserver/guides/getting-around">' in page.text
    assert "adsbygoogle" not in page.text  # AdSense is not set up
    # The menu links the guides on every page once one is published.
    assert '<a href="/guides" >Guides</a>' in client.get("/").text.replace("\n", "")

    sitemap = client.get("/sitemap.xml").text
    assert "<loc>http://testserver/guides/getting-around</loc>" in sitemap

    listing = client.get("/admin/guides", headers=ADMIN).text
    assert "Getting around Guam" in listing and "Published" in listing


def test_form_problems_come_back_with_the_text(client):
    _new(client, slug="taken")
    response = _save(client, title="", slug="taken", body="My **draft** text.")
    assert response.status_code == 400
    assert "Please give the guide a title." in response.text
    assert "My **draft** text." in response.text  # nothing typed is lost
    response = _save(client, title="Another", slug="taken")
    assert "Another guide has this address." in response.text
    with session_scope() as session:
        assert len(session.scalars(select(Guide)).all()) == 1


def test_photos_are_saved_shown_and_deleted(client, monkeypatch):
    from guheadlines.web import app as web

    media = web.settings.media_dir
    guide = _new(
        client,
        cover=make_jpeg(1800, 1000),
        photo_alt="Tumon Bay from the cliffs",
        picture=make_jpeg(900, 600),
        picture_alt="Red rice [and] kelaguen",
        status="published",
    )
    assert re.fullmatch(rf"/media/guides/{guide.id}-[0-9a-f]{{12}}-1400x778\.webp", guide.photo)
    assert (media / guide.photo.removeprefix("/media/")).exists()
    assert (media / guides.small_photo(guide.photo).removeprefix("/media/")).exists()
    added = r"!\[Red rice \(and\) kelaguen\]\((/media/guides/\S+-900x600\.webp)\)"
    line = re.search(added, guide.body)
    assert line, guide.body

    page = client.get(f"/guides/{guide.slug}").text
    assert f'class="guide-cover" src="{guide.photo}"' in page and 'fetchpriority="high"' in page
    assert 'alt="Tumon Bay from the cliffs"' in page
    assert f'src="{line[1]}"' in page and 'width="900" height="600"' in page

    # A new cover photo takes the old one's place, and the old file goes.
    old = guide.photo
    response = _save(client, guide.id, cover=make_jpeg(1200, 800), photo_alt="New", body=guide.body)
    assert response.status_code == 303
    guide = _get(guide.id)
    assert guide.photo != old and not (media / old.removeprefix("/media/")).exists()

    # Taking it off.
    _save(client, guide.id, remove_photo="yes", body=guide.body)
    assert _get(guide.id).photo is None

    # Deleting needs the box ticked, then takes the photos too.
    response = client.post(
        f"/admin/guides/{guide.id}/delete", headers=OWNER, follow_redirects=False
    )
    assert response.headers["location"] == f"/admin/guides/{guide.id}?done=confirm"
    assert _get(guide.id) is not None
    response = client.post(
        f"/admin/guides/{guide.id}/delete",
        data={"confirm": "yes"},
        headers=OWNER,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin/guides?done=deleted"
    assert _get(guide.id) is None
    assert not list((media / "guides").glob(f"{guide.id}-*"))
    assert client.get(f"/guides/{guide.slug}").status_code == 404


def test_a_bad_photo_is_refused(client):
    response = _save(client, cover=b"not a photo", photo_alt="Nothing")
    assert response.status_code == 400 and "The photo could not be used." in response.text
    with session_scope() as session:
        assert session.scalar(select(Guide)) is None


def test_the_starter_guides_are_added_as_drafts(client):
    listing = client.get("/admin/guides", headers=ADMIN).text
    assert "Add the starter guides as drafts" in listing
    response = client.post("/admin/guides/starters", headers=OWNER, follow_redirects=False)
    assert response.headers["location"] == "/admin/guides?done=starters"
    with session_scope() as session:
        added = session.scalars(select(Guide)).all()
    assert len(added) == len(guides.starters())
    assert all(g.status == "draft" and g.access == "free" for g in added)
    assert client.get("/guides").status_code == 404  # drafts until the owner publishes them
    listing = client.get("/admin/guides", headers=ADMIN).text
    assert "Add the starter" not in listing and "Your first day on Guam" in listing
    # Pressed twice: nothing more.
    client.post("/admin/guides/starters", headers=OWNER)
    with session_scope() as session:
        assert len(session.scalars(select(Guide)).all()) == len(added)


def test_the_guide_sponsor(client):
    today = datetime.now(GUAM_TZ).date()
    guide = _new(
        client,
        status="published",
        sponsor_name="Island Car Rentals",
        sponsor_link="islandcars.example.com",
        sponsor_text="Cars at the airport",
        sponsor_until=today.isoformat(),
    )
    page = client.get(f"/guides/{guide.slug}").text
    assert f'<a href="/go/guide-sponsor/{guide.id}" target="_blank"' in page
    assert 'rel="nofollow sponsored noopener">Island Car Rentals ↗</a>' in page
    assert "Cars at the airport" in page

    client.cookies.clear()
    click = client.get(
        f"/go/guide-sponsor/{guide.id}",
        headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0"},
        follow_redirects=False,
    )
    assert click.status_code == 302
    assert click.headers["location"] == "https://islandcars.example.com"
    from guheadlines.web import stats

    stats.counts.flush()
    listing = client.get("/admin/guides", headers=ADMIN).text
    assert "Island Car Rentals" in listing and "1 click" in listing

    # After its last day, the box and its link are gone.
    _save(
        client,
        guide.id,
        status="published",
        sponsor_name="Island Car Rentals",
        sponsor_link="islandcars.example.com",
        sponsor_until=(today - timedelta(days=1)).isoformat(),
    )
    assert "Sponsored by" not in client.get(f"/guides/{guide.slug}").text
    assert client.get(f"/go/guide-sponsor/{guide.id}").status_code == 404
    assert "ended" in client.get("/admin/guides", headers=ADMIN).text


def test_ads_in_a_guide(client, monkeypatch):
    _set(
        monkeypatch,
        stats_password=PASSWORD,
        adsense_client="ca-pub-1234567890123456",
        adsense_slot="9876543210",
        adsense_stories_slot="1112223334",
    )
    guide = _new(client, status="published")
    page = client.get(f"/guides/{guide.slug}")
    assert page.text.count('class="adsbygoogle"') == 2  # the column's and the one in the text
    # After the opening and the first part, before the second.
    assert page.text.index('id="getting-there"') < page.text.index("ad-in-guide")
    assert page.text.index("ad-in-guide") < page.text.index('id="where-to-stay"')
    assert "'nonce-" in page.headers["content-security-policy"]
    # Not in the owner's preview.
    preview = client.get(f"/admin/guides/{guide.id}/view", headers=ADMIN)
    assert "adsbygoogle" not in preview.text
