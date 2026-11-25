"""The newsletter feed (/feed.xml) and its editions."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time, timedelta, timezone
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from guheadlines.db import session_scope
from guheadlines.models import Article, NewsletterEdition, Source, StoryClicks
from guheadlines.newsletter import edition_time, form_edition, next_edition_time, schedule_text
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import get_settings, load_settings

TUE_FRI = (1, 4)
TEN = time(10, 0)


def guam(*args) -> datetime:
    return datetime(*args, tzinfo=GUAM_TZ)


def test_edition_times():
    # 2026-11-24 is a Tuesday.
    assert edition_time(guam(2026, 11, 25, 13, 52), TUE_FRI, TEN) == guam(2026, 11, 24, 10)
    assert edition_time(guam(2026, 11, 24, 10, 0), TUE_FRI, TEN) == guam(2026, 11, 24, 10)
    assert edition_time(guam(2026, 11, 24, 9, 59), TUE_FRI, TEN) == guam(2026, 11, 20, 10)
    # A time in UTC is read in Guam time: Monday 23:30 UTC is Tuesday 9:30 in Guam.
    utc = datetime(2026, 11, 23, 23, 30, tzinfo=timezone.utc)
    assert edition_time(utc, TUE_FRI, TEN) == guam(2026, 11, 20, 10)
    assert next_edition_time(utc, TUE_FRI, TEN) == guam(2026, 11, 24, 10)
    assert next_edition_time(guam(2026, 11, 24, 10, 0), TUE_FRI, TEN) == guam(2026, 11, 27, 10)
    assert next_edition_time(guam(2026, 11, 27, 11), TUE_FRI, TEN) == guam(2026, 12, 1, 10)
    # One day a week.
    assert edition_time(guam(2026, 11, 26, 8), (4,), TEN) == guam(2026, 11, 20, 10)
    assert edition_time(guam(2026, 11, 26, 8), (), TEN) is None
    assert schedule_text(TUE_FRI, TEN) == "Tuesday and Friday at 10:00 AM"
    assert schedule_text((0, 2, 4), time(14, 5)) == "Monday, Wednesday and Friday at 2:05 PM"
    assert schedule_text((6,), time(0, 30)) == "Sunday at 12:30 AM"


def test_newsletter_settings(monkeypatch):
    for name in ("NEWSLETTER_DAYS", "NEWSLETTER_TIME", "NEWSLETTER_STORIES"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert (settings.newsletter_days, settings.newsletter_time) == (TUE_FRI, TEN)
    assert settings.newsletter_stories == 12
    for value, days in (
        ("tue,fri", TUE_FRI),
        ("Friday, Tuesday", TUE_FRI),
        ("mon thu", (0, 3)),
        ("tues, thurs", (1, 3)),
        ("", ()),  # no feed
        ("  ", ()),
        ("every day", TUE_FRI),  # no day name: the default
    ):
        monkeypatch.setenv("NEWSLETTER_DAYS", value)
        assert load_settings().newsletter_days == days, value
    for value, at in (
        ("10:00", TEN),
        ("9:30", time(9, 30)),
        ("14", time(14, 0)),
        ("2:30 pm", time(14, 30)),
        ("10 AM", TEN),
        ("12 a.m.", time(0, 0)),
        ("12pm", time(12, 0)),
        ("25:00", TEN),
        ("13 pm", TEN),
        ("noon", TEN),
        ("", TEN),
    ):
        monkeypatch.setenv("NEWSLETTER_TIME", value)
        assert load_settings().newsletter_time == at, value
    for value, stories in (("20", 20), ("0", 1), ("500", 50), ("many", 12)):
        monkeypatch.setenv("NEWSLETTER_STORIES", value)
        assert load_settings().newsletter_stories == stories, value


# Tuesday 2026-11-24, 10:00 in Guam, and the edition before it.
TUESDAY = guam(2026, 11, 24, 10)
FRIDAY = guam(2026, 11, 20, 10)


def _add_sources(session, *slugs):
    sources = [Source(slug=s, name=s.upper(), homepage=f"https://www.{s}.com/") for s in slugs]
    session.add_all(sources)
    session.flush()
    return sources


def _story(source, n, found, published=None, **values):
    fields = {
        "source_id": source.id,
        "url": f"https://www.{source.slug}.com/story/{n}",
        "title": f"{source.slug} story {n}",
        "intro": f"What happened in story {n}.",
        "categories": ["local"],
        "published_at": published or found,
        "first_seen_at": found,
    }
    return Article(**{**fields, **values})


def test_edition_holds_the_top_stories_since_the_last_one(clean_db, app_config):
    settings = dataclasses.replace(get_settings(), newsletter_stories=5)
    with session_scope() as session:
        kuam, post, pnc = _add_sources(session, "kuam", "post", "pnc")
        stories = {
            # Found between the two editions:
            "clicked": _story(post, 1, FRIDAY + timedelta(days=1)),
            "photo": _story(pnc, 2, FRIDAY + timedelta(days=2), image_path="images/a.webp"),
            "newest": _story(pnc, 3, TUESDAY - timedelta(hours=1)),
            "older": _story(pnc, 4, FRIDAY + timedelta(hours=1)),
            "kuam1": _story(kuam, 5, FRIDAY + timedelta(days=3)),
            "kuam2": _story(kuam, 6, FRIDAY + timedelta(days=3, hours=1)),
            "kuam3": _story(kuam, 7, FRIDAY + timedelta(days=3, hours=2)),
            "kuam4": _story(kuam, 8, FRIDAY + timedelta(days=3, hours=3)),
            # Left out:
            "before": _story(post, 9, FRIDAY - timedelta(minutes=1)),
            "after": _story(post, 10, TUESDAY),  # in the next edition
            "archive": _story(post, 11, TUESDAY - timedelta(hours=2), FRIDAY - timedelta(days=9)),
            "obituary": _story(post, 12, TUESDAY - timedelta(hours=3), categories=["obituaries"]),
            # Muted (muted: in sources.yaml), in the headline or the intro:
            "muted": _story(pnc, 13, TUESDAY - timedelta(minutes=30), title="Guam Chamber mixer"),
            "muted2": _story(
                post, 14, TUESDAY - timedelta(minutes=20), intro="The Chamber of Commerce said."
            ),
        }
        session.add_all(stories.values())
        session.flush()
        session.add(StoryClicks(day=date(2026, 11, 22), article_id=stories["clicked"].id, clicks=3))
        ids = {name: story.id for name, story in stories.items()}

    assert form_edition(settings, app_config, TUESDAY + timedelta(minutes=5)) == (TUESDAY, 5)
    with session_scope() as session:
        edition = session.get(NewsletterEdition, TUESDAY)
        assert edition.article_ids == [
            ids["clicked"],  # readers clicked it
            ids["photo"],  # it has a photo
            ids["newest"],  # then the newest first
            ids["kuam4"],
            ids["kuam3"],
        ]
    settings = dataclasses.replace(settings, newsletter_stories=12)
    with session_scope() as session:
        session.query(NewsletterEdition).delete()
    form_edition(settings, app_config, TUESDAY)
    with session_scope() as session:
        picked = session.get(NewsletterEdition, TUESDAY).article_ids
    assert ids["kuam2"] in picked and ids["older"] in picked
    assert ids["kuam1"] not in picked  # at most 3 from one news source
    for name in ("before", "after", "archive", "obituary", "muted", "muted2"):
        assert ids[name] not in picked, name


def test_editions_are_kept_as_they_were_made(clean_db, app_config):
    settings = get_settings()
    with session_scope() as session:
        (kuam,) = _add_sources(session, "kuam")
        session.add(_story(kuam, 1, TUESDAY - timedelta(hours=5)))
    assert form_edition(settings, app_config, TUESDAY + timedelta(hours=1)) == (TUESDAY, 1)
    # A story found later, or a click, does not change the edition.
    with session_scope() as session:
        (kuam,) = session.query(Source).all()
        session.add(_story(kuam, 2, TUESDAY - timedelta(hours=4)))
    assert form_edition(settings, app_config, TUESDAY + timedelta(hours=2)) is None
    with session_scope() as session:
        assert len(session.get(NewsletterEdition, TUESDAY).article_ids) == 1
    # Nothing new is made before the next edition is due.
    assert form_edition(settings, app_config, TUESDAY + timedelta(days=2)) is None
    # A day with no new stories still gets its (empty) edition.
    friday = TUESDAY + timedelta(days=3)
    assert form_edition(settings, app_config, friday) == (friday, 0)


@pytest.fixture()
def client(clean_db):
    from guheadlines.web.app import app

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        kuam, post = _add_sources(session, "kuam", "postguam")
        stories = [
            _story(
                post,
                1,
                now,
                title="Andersen to host Cope North & more",
                intro="Hundreds of airmen <script>x</script> will arrive. " + "More words. " * 40,
                image_path="images/2026/09/example.webp",
                image_width=720,
                image_height=480,
            ),
            _story(kuam, 2, now, title="Wage\x08 rises", image_url="https://www.kuam.com/a.jpg"),
            _story(kuam, 3, now, title="Not in the edition"),
            # Muted after the edition was made: left out of the feed.
            _story(kuam, 4, now, title="Guam Chamber of Commerce luncheon"),
        ]
        session.add_all(stories)
        session.flush()
        session.add(
            NewsletterEdition(
                formed_at=TUESDAY, article_ids=[stories[0].id, stories[1].id, 999, stories[3].id]
            )
        )
    return TestClient(app)


def _set(monkeypatch, **values):
    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, **values)
    monkeypatch.setattr(web, "settings", site)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)


MEDIA = "{http://search.yahoo.com/mrss/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


def test_feed_lists_the_newest_edition(client, monkeypatch):
    _set(monkeypatch, stats_password="")
    response = client.get("/feed.xml")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/rss+xml; charset=utf-8"
    assert response.headers["x-robots-tag"] == "noindex"
    channel = ElementTree.fromstring(response.content).find("channel")
    assert channel.findtext("link") == "http://testserver/"
    assert "every Tuesday and Friday at 10:00 AM" in channel.findtext("description")
    assert channel.findtext("pubDate") == "Tue, 24 Nov 2026 10:00:00 +1000"
    items = channel.findall("item")
    assert [i.findtext("title") for i in items] == [
        "Andersen to host Cope North & more",
        "Wage rises",  # a character XML does not allow is left out
    ]  # a deleted story (999) and a muted one are skipped

    first, second = items
    assert first.findtext("link") == "https://www.postguam.com/story/1"  # stats off: straight there
    assert first.findtext("guid") == "https://www.postguam.com/story/1"
    assert first.findtext("pubDate") == "Tue, 24 Nov 2026 10:00:00 +1000"
    assert first.find("source").get("url") == "https://www.postguam.com/"
    description = first.findtext("description")
    assert description.startswith("POSTGUAM: Hundreds of airmen <script>x</script> will arrive.")
    assert len(description) < 300 and description.count("More words.") < 40  # a part only
    photo = "http://testserver/media/images/2026/09/example.webp"
    media = first.find(f"{MEDIA}content")
    assert (media.get("url"), media.get("type"), media.get("width")) == (photo, "image/webp", "720")
    assert first.find("enclosure").get("url") == photo
    html = first.findtext(f"{CONTENT}encoded")
    assert f'<img src="{photo}"' in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "Read the full story at postguam.com" in html
    # A photo that is not stored here comes from the news site.
    assert second.find(f"{MEDIA}content").get("url") == "https://www.kuam.com/a.jpg"
    assert second.find("enclosure") is None


def test_feed_links_count_clicks_while_stats_are_on(client, monkeypatch):
    _set(monkeypatch, stats_password="secret")
    items = ElementTree.fromstring(client.get("/feed.xml").content).iter("item")
    links = [i.findtext("link") for i in items]
    assert all(link.startswith("http://testserver/go/") for link in links)
    story = client.get(links[0].removeprefix("http://testserver"), follow_redirects=False)
    assert story.headers["location"] == "https://www.postguam.com/story/1"


def test_feed_stays_unlisted(client, monkeypatch):
    for path in ("/", "/status", "/jobs", "/events", "/bids", "/robots.txt", "/sitemap.xml"):
        assert "feed.xml" not in client.get(path).text, path
    assert "github" not in client.get("/feed.xml").text.lower()
    _set(monkeypatch, newsletter_days=())
    assert client.get("/feed.xml").status_code == 404


def test_feed_before_the_first_edition(client):
    with session_scope() as session:
        session.query(NewsletterEdition).delete()
    channel = ElementTree.fromstring(client.get("/feed.xml").content).find("channel")
    assert channel.findall("item") == [] and channel.find("pubDate") is None
