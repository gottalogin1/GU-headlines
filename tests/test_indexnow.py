"""IndexNow (indexnow.py): which pages are sent, and how."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

import httpx

from guheadlines import indexnow
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import get_settings


def _settings(**changes):
    values = {"site_url": "https://guamheadlines.com", "indexnow": True, **changes}
    return replace(get_settings(), **values)


def test_the_key_is_secret_steady_and_per_site():
    site = _settings(database_url="postgresql://guheadlines:one@db/guheadlines")
    key = indexnow.key(site)
    assert len(key) == 32 and key.isalnum()
    assert indexnow.key(site) == key
    other = replace(site, database_url="postgresql://guheadlines:two@db/guheadlines")
    assert indexnow.key(other) != key
    assert "one" not in key


def test_only_a_public_address_is_sent():
    assert indexnow.site(_settings()) == "https://guamheadlines.com"
    assert indexnow.is_on(_settings())
    assert not indexnow.is_on(_settings(indexnow=False))
    for local in (
        "",
        "http://localhost:8000",
        "http://192.168.1.20",
        "http://news.local",
        "http://web",
    ):
        assert indexnow.site(_settings(site_url=local)) is None, local


def _recording(status: int = 200):
    sent: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), **json.loads(request.content)})
        return httpx.Response(status)

    return sent, httpx.MockTransport(handle)


def test_submit_sends_full_addresses_with_the_key():
    site = _settings()
    sent, transport = _recording(202)
    assert indexnow.submit(site, ["/", "/jobs", "/"], transport=transport) == 2
    (request,) = sent
    assert request["url"] == indexnow.ENDPOINT
    assert request["host"] == "guamheadlines.com"
    assert request["key"] == indexnow.key(site)
    assert request["keyLocation"] == f"https://guamheadlines.com/{indexnow.key(site)}.txt"
    assert request["urlList"] == ["https://guamheadlines.com/", "https://guamheadlines.com/jobs"]


def test_submit_does_nothing_when_off_and_reports_a_refusal(caplog):
    sent, transport = _recording()
    assert indexnow.submit(_settings(indexnow=False), ["/"], transport=transport) == 0
    assert indexnow.submit(_settings(site_url=""), ["/"], transport=transport) == 0
    assert indexnow.submit(_settings(), [], transport=transport) == 0
    assert sent == []
    _, refused = _recording(403)
    assert indexnow.submit(_settings(), ["/"], transport=refused) == 0
    assert "the key was not accepted" in caplog.text


def test_main_pages():
    paths = indexnow.main_paths(_settings(), ["local", "obituaries"], guides=True)
    assert paths[0] == "/" and "/category/obituaries" in paths and "/guides" in paths
    off = _settings(weather_interval_minutes=0, outages_interval_minutes=0)
    assert not {"/weather", "/outages", "/guides"} & set(indexnow.main_paths(off, [], False))


def test_changed_pages(clean_db):
    from guheadlines.db import session_scope
    from guheadlines.models import (
        Article,
        Bid,
        BidSource,
        Event,
        EventCalendar,
        Job,
        JobSource,
        Source,
    )

    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    tomorrow = datetime.combine(
        datetime.now(GUAM_TZ).date() + timedelta(days=1), time(10), tzinfo=GUAM_TZ
    )
    with session_scope() as session:
        kuam = Source(slug="kuam", name="KUAM News")
        post = Source(slug="postguam", name="The Guam Daily Post")
        calendar = EventCalendar(slug="legislature", name="Guam Legislature")
        board = JobSource(slug="doa", name="Department of Administration")
        bids = BidSource(slug="notices", name="GovGuam public notices")
        session.add_all([kuam, post, calendar, board, bids])
        session.flush()
        session.add_all(
            [
                Article(
                    source_id=kuam.id,
                    url="https://www.kuam.com/story/new",
                    title="New story",
                    categories=["military"],
                    published_at=now,
                    first_seen_at=now,
                ),
                Article(
                    source_id=post.id,
                    url="https://www.postguam.com/old",
                    title="Old story",
                    categories=["labor"],
                    published_at=day_ago,
                    first_seen_at=day_ago,
                ),
                Event(
                    calendar_id=calendar.id,
                    uid="new",
                    title="Regular Session",
                    starts_at=tomorrow,
                    first_seen_at=now,
                    last_seen_at=now,
                ),
                Event(
                    calendar_id=calendar.id,
                    uid="old",
                    title="Public hearing",
                    starts_at=tomorrow,
                    first_seen_at=day_ago,
                    last_seen_at=now,
                ),
                Job(
                    source_id=board.id,
                    url="https://doa.guam.gov/1",
                    title="Nurse",
                    first_seen_at=day_ago,
                    last_seen_at=now,
                ),
                Bid(
                    source_id=bids.id,
                    uid="1",
                    title="Crane",
                    url="https://notices.guam.gov/1",
                    first_seen_at=now,
                    last_seen_at=now,
                ),
            ]
        )
    with session_scope() as session:
        event_ids = dict(session.query(Event.uid, Event.id).all())
        paths = indexnow.changed_paths(session, hour_ago, upcoming_events=event_ids.values())
    day = now.astimezone(GUAM_TZ).date()
    assert paths[0] == "/"
    assert {"/category/military", "/source/kuam", "/bids", "/events"} <= set(paths)
    assert f"/archive/{day.year}/{day.month}/{day.day}" in paths
    assert f"/events/{event_ids['new']}" in paths
    # Nothing new on these.
    assert not {"/category/labor", "/source/postguam", "/jobs"} & set(paths)
    assert f"/events/{event_ids['old']}" not in paths
    with session_scope() as session:
        assert indexnow.changed_paths(session, now + timedelta(minutes=1)) == []
