"""Source health: failing and quiet sources, on the stats page and in Discord."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count

import httpx
from sqlalchemy import select

from guheadlines.alerts import check_sources
from guheadlines.db import session_scope
from guheadlines.health import (
    FAILING,
    QUIET,
    UNCHECKED,
    WORKING,
    duration,
    feed_health,
    news_health,
    quiet_after,
)
from guheadlines.models import AlertState, Article, EventCalendar, JobSource, Source
from guheadlines.settings import get_settings

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)  # 4:00 PM in Guam
WEBHOOK = "https://discord.com/api/webhooks/1234567890/abc-DEF_123"


class Outbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.sent.append(json.loads(request.content)["content"])
            return httpx.Response(204)

        return httpx.MockTransport(handle)


def _settings(**changes):
    return replace(get_settings(), discord_webhook_url=WEBHOOK, alert_after_hours=24, **changes)


def test_quiet_after():
    # A busy site: at least 2 days.
    assert quiet_after(timedelta(hours=6), 300) == timedelta(days=2)
    # Twice the longest wait between two stories.
    assert quiet_after(timedelta(days=3), 40) == timedelta(days=6)
    # Too few stories to tell: at least 30 days.
    assert quiet_after(timedelta(days=3), 4) == timedelta(days=30)
    assert quiet_after(None, 0) == timedelta(days=30)


def test_duration():
    assert duration(timedelta(minutes=50)) == "0 hours"
    assert duration(timedelta(hours=1, minutes=59)) == "1 hour"
    assert duration(timedelta(hours=47)) == "47 hours"
    assert duration(timedelta(hours=48)) == "2 days"
    assert duration(timedelta(days=30, hours=5)) == "30 days"


# ---------------------------------------------------------------- database


def _source(slug, name, *, checked=NOW, worked=NOW, error=None, created=NOW - timedelta(days=90)):
    with session_scope() as session:
        source = Source(
            slug=slug,
            name=name,
            last_run_at=checked,
            last_success_at=worked,
            last_error=error,
            created_at=created,
        )
        session.add(source)
        session.flush()
        return source.id


_numbers = count()


def _stories(source_id, times, published=None):
    """Stories first found at these times (published then too, unless given)."""
    with session_scope() as session:
        for n, moment in enumerate(times):
            session.add(
                Article(
                    source_id=source_id,
                    url=f"https://example.com/{source_id}/{next(_numbers)}",
                    title=f"Story {n}",
                    published_at=published or moment,
                    first_seen_at=moment,
                )
            )


def _seed():
    """A busy site, a weekly site that went quiet, a failing site, a site never
    checked yet, and a site with a story dated in the future."""
    busy = _source("postguam", "The Guam Daily Post")
    _stories(busy, [NOW - timedelta(hours=1 + 6 * n) for n in range(40)])
    weekly = _source("gdol", "Guam Department of Labor")
    _stories(weekly, [NOW - timedelta(days=40 + 7 * n) for n in range(8)])
    # A story long before the 60 days before its newest story does not count.
    _stories(weekly, [NOW - timedelta(days=400)])
    failing = _source("kuam", "KUAM News", worked=NOW - timedelta(hours=30), error="HTTP 403")
    _stories(failing, [NOW - timedelta(days=20 + n) for n in range(10)])
    _source("isla", "Isla 63", checked=None, worked=None)
    dated = _source("pactimes", "Pacific Island Times")
    # First found 10 days ago, but dated next week by the site.
    _stories(dated, [NOW - timedelta(days=10 + n / 4) for n in range(20)])
    _stories(dated, [NOW - timedelta(days=10)], published=NOW + timedelta(days=7))


def test_news_health(clean_db):
    _seed()
    with session_scope() as session:
        found = news_health(
            session, ["postguam", "gdol", "kuam", "isla", "pactimes", "stripes"], NOW
        )
    by_slug = {h.slug: h for h in found}
    # Problems first, then the rest, each in name order.
    assert [h.slug for h in found] == ["kuam", "gdol", "pactimes", "isla", "postguam"]

    busy = by_slug["postguam"]
    assert busy.status == WORKING
    assert busy.newest == NOW - timedelta(hours=1)
    assert busy.longest_wait == timedelta(hours=6)
    assert busy.quiet_after == timedelta(days=2)
    assert busy.week == 28

    weekly = by_slug["gdol"]
    assert weekly.status == QUIET
    assert weekly.quiet_since == NOW - timedelta(days=40)
    assert weekly.longest_wait == timedelta(days=7)
    assert weekly.quiet_after == timedelta(days=14)
    assert weekly.week == 0

    assert by_slug["kuam"].status == FAILING  # failing comes before quiet
    assert by_slug["isla"].status == UNCHECKED
    # The story dated next week counts from when it was found.
    assert by_slug["pactimes"].status == QUIET
    assert by_slug["pactimes"].newest == NOW - timedelta(days=10)


def test_a_site_that_never_had_a_story(clean_db):
    _source("gft", "Guam Fishermen's Co-op", created=NOW - timedelta(days=31))
    _source("gspn", "GSPN", created=NOW - timedelta(days=3))
    with session_scope() as session:
        found = {h.slug: h for h in news_health(session, ["gft", "gspn"], NOW)}
    assert found["gft"].status == QUIET
    assert found["gft"].quiet_since == NOW - timedelta(days=31)
    assert found["gspn"].status == WORKING


def test_feed_health(clean_db):
    with session_scope() as session:
        session.add_all(
            [
                JobSource(
                    slug="hireguam",
                    name="HireGuam",
                    last_run_at=NOW,
                    last_success_at=NOW,
                    last_count=12,
                ),
                JobSource(slug="doa", name="DOA", last_run_at=NOW, last_error="HTTP 500"),
                EventCalendar(slug="uog", name="University of Guam"),
            ]
        )
    with session_scope() as session:
        found = feed_health(session, ["hireguam", "doa"], ["uog"])
    assert [(h.name, h.kind, h.status, h.found) for h in found] == [
        ("DOA", "job board", FAILING, None),
        ("University of Guam", "event calendar", UNCHECKED, None),
        ("HireGuam", "job board", WORKING, 12),
    ]


def _states():
    with session_scope() as session:
        return {s.key: s.notified_at for s in session.scalars(select(AlertState))}


def test_quiet_alert_once_and_again_with_new_stories(clean_db):
    settings = _settings(site_url="https://guamheadlines.com")
    outbox = Outbox()
    _seed()
    news = ["postguam", "gdol", "pactimes"]
    check_sources(settings, news, [], now=NOW, transport=outbox.transport())
    assert len(outbox.sent) == 1
    alert = outbox.sent[0]
    assert alert.startswith("⚠️ **2 news sites have gone quiet**\n")
    assert (
        "Guam Department of Labor still answers, but has had no new story since "
        "Thu Oct 15, 4:00 PM (Guam time). In the two months before, its longest wait "
        "between two stories was 7 days." in alert
    )
    assert "Pacific Island Times still answers" in alert
    assert "Their feeds or pages may have changed" in alert
    assert "More on the Sources page: https://guamheadlines.com/status" in alert
    assert set(_states()) == {"quiet:gdol", "quiet:pactimes"}

    # Still quiet an hour later: no second alert.
    check_sources(settings, news, [], now=NOW + timedelta(hours=1), transport=outbox.transport())
    assert len(outbox.sent) == 1

    # A new story on one: one more alert, for that one.
    with session_scope() as session:
        gdol = session.scalar(select(Source.id).where(Source.slug == "gdol"))
    _stories(gdol, [NOW + timedelta(hours=2)])
    later = NOW + timedelta(hours=3)
    check_sources(settings, news, [], now=later, transport=outbox.transport())
    assert len(outbox.sent) == 2
    assert outbox.sent[1].startswith("✅ **Guam Department of Labor has new stories again**\n")
    assert "Newest story: Tue Nov 24, 6:00 PM (Guam time)." in outbox.sent[1]
    assert set(_states()) == {"quiet:pactimes"}

    # A quiet site that starts failing keeps its quiet alert (the failure gets
    # its own); one that is no longer read is forgotten without an alert.
    with session_scope() as session:
        row = session.scalar(select(Source).where(Source.slug == "pactimes"))
        row.last_run_at = later
    check_sources(settings, news, [], now=later, transport=outbox.transport())
    assert len(outbox.sent) == 2
    assert set(_states()) == {"quiet:pactimes", "news:pactimes"}
    check_sources(settings, ["postguam", "gdol"], [], now=later, transport=outbox.transport())
    assert len(outbox.sent) == 2
    assert _states() == {}


def test_quiet_alert_links_to_the_stats_page_while_it_is_on(clean_db):
    settings = _settings(site_url="https://guamheadlines.com", stats_password="secret")
    outbox = Outbox()
    weekly = _source("gdol", "Guam Department of Labor")
    _stories(weekly, [NOW - timedelta(days=40 + 7 * n) for n in range(8)])
    check_sources(settings, ["gdol"], [], now=NOW, transport=outbox.transport())
    assert outbox.sent[0].startswith("⚠️ **Guam Department of Labor has gone quiet**\n")
    assert "Its feed or pages may have changed" in outbox.sent[0]
    assert "More on the stats page: https://guamheadlines.com/stats#health" in outbox.sent[0]


def test_a_failing_event_calendar_is_reported(clean_db):
    settings = _settings(site_url="https://guamheadlines.com")
    outbox = Outbox()
    with session_scope() as session:
        session.add(
            EventCalendar(
                slug="uog",
                name="University of Guam",
                last_run_at=NOW,
                last_success_at=NOW - timedelta(days=2),
                last_error="HTTP 404",
            )
        )
    check_sources(settings, [], [], ["uog"], now=NOW, transport=outbox.transport())
    assert len(outbox.sent) == 1
    assert outbox.sent[0].startswith("⚠️ **University of Guam has stopped working**\n")
    assert "The event calendar University of Guam has not worked since" in outbox.sent[0]
    assert "More on the Events page: https://guamheadlines.com/events" in outbox.sent[0]
    assert _states() == {"events:uog": NOW}
    with session_scope() as session:
        row = session.scalar(select(EventCalendar))
        row.last_success_at = row.last_run_at = NOW + timedelta(hours=1)
    check_sources(
        settings, [], [], ["uog"], now=NOW + timedelta(hours=1), transport=outbox.transport()
    )
    assert outbox.sent[1].startswith("✅ **University of Guam works again**")
    assert _states() == {}
