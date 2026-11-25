"""Source health: which news sites, job boards and event calendars work, and
which news sites have gone quiet.

A source is failing when its latest check went wrong (the Sources page says
"failed"). A news site has gone quiet when its checks work but it has had no
new story for much longer than usual: its feed or page may have changed so
that the stories are no longer found, or the site may have stopped posting.

The private stats page (/stats) shows the health of every source, and
alerts.py posts the problems in Discord.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import DateTime, func, select

from .models import Article, EventCalendar, JobSource, Source

# How long a news site can go without a new story before it counts as quiet:
# twice the longest wait between two of its stories in the 60 days before its
# newest story, and at least 2 days. A site with fewer than 5 stories in
# those 60 days posts too rarely to tell, so it gets at least 30 days.
WINDOW = timedelta(days=60)
QUIET_TIMES = 2
QUIET_AT_LEAST = timedelta(days=2)
FEW_STORIES = 5
RARE_AT_LEAST = timedelta(days=30)

WORKING = "working"
FAILING = "failing"
QUIET = "quiet"
UNCHECKED = "unchecked"


def failed(row: Source | JobSource | EventCalendar) -> bool:
    """The latest check went wrong (the Sources page shows "failed")."""
    return row.last_run_at is not None and (
        row.last_success_at is None or row.last_success_at < row.last_run_at
    )


def quiet_after(longest_wait: timedelta | None, stories: int) -> timedelta:
    """How long a news site can go without a new story before it counts as
    quiet, from the longest wait between its recent stories."""
    limit = QUIET_AT_LEAST
    if longest_wait:
        limit = max(limit, QUIET_TIMES * longest_wait)
    if stories < FEW_STORIES:
        limit = max(limit, RARE_AT_LEAST)
    return limit


def duration(span: timedelta) -> str:
    """20 hours, 3 days, 1 day (rounded down to whole hours or days)."""
    hours = int(span.total_seconds() // 3600)
    if hours < 48:
        return "1 hour" if hours == 1 else f"{max(hours, 0)} hours"
    days = hours // 24
    return f"{days} days"


@dataclass
class NewsHealth:
    slug: str
    name: str
    status: str  # WORKING, FAILING, QUIET or UNCHECKED
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    # The time of its newest story (when it was published, or first found if
    # that was earlier), None if it has none.
    newest: datetime | None
    # The longest wait between two stories in the WINDOW before the newest.
    longest_wait: timedelta | None
    quiet_after: timedelta
    # New stories found in the last 7 days.
    week: int
    # QUIET: since when (the newest story, or when the site was added).
    quiet_since: datetime | None = None


def news_health(session, slugs: Iterable[str], now: datetime) -> list[NewsHealth]:
    """The health of these news sites (those the worker reads): problems
    first, then the rest, each in name order."""
    slugs = list(slugs)
    if not slugs:
        return []
    # A story's time: when it was published, or when it was first found if
    # that was earlier (a story dated in the future must not hide a quiet site).
    moment = func.least(Article.published_at, Article.first_seen_at, type_=DateTime(timezone=True))
    times = (
        select(Article.source_id, moment.label("moment"))
        .join(Source, Source.id == Article.source_id)
        .where(Source.slug.in_(slugs))
        .subquery()
    )
    newest = (
        select(times.c.source_id, func.max(times.c.moment).label("newest"))
        .group_by(times.c.source_id)
        .subquery()
    )
    previous = func.lag(times.c.moment, type_=DateTime(timezone=True)).over(
        partition_by=times.c.source_id, order_by=times.c.moment
    )
    recent = (
        select(times.c.source_id, (times.c.moment - previous).label("wait"))
        .join(newest, newest.c.source_id == times.c.source_id)
        .where(times.c.moment >= newest.c.newest - WINDOW)
        .subquery()
    )
    waits = {
        source_id: (longest, stories)
        for source_id, longest, stories in session.execute(
            select(recent.c.source_id, func.max(recent.c.wait), func.count()).group_by(
                recent.c.source_id
            )
        )
    }
    week_ago = now - timedelta(days=7)
    rows = session.execute(
        select(
            Source,
            func.max(moment).label("newest"),
            func.count(Article.id).filter(Article.first_seen_at >= week_ago).label("week"),
        )
        .outerjoin(Article, Article.source_id == Source.id)
        .where(Source.slug.in_(slugs))
        .group_by(Source.id)
    ).all()

    found: list[NewsHealth] = []
    for source, newest_story, week in rows:
        longest, stories = waits.get(source.id, (None, 0))
        limit = quiet_after(longest, stories)
        health = NewsHealth(
            slug=source.slug,
            name=source.name,
            status=WORKING,
            last_run_at=source.last_run_at,
            last_success_at=source.last_success_at,
            last_error=source.last_error,
            newest=newest_story,
            longest_wait=longest,
            quiet_after=limit,
            week=week,
        )
        if source.last_run_at is None:
            health.status = UNCHECKED
        elif failed(source):
            health.status = FAILING
        elif now - (newest_story or source.created_at) > limit:
            health.status = QUIET
            health.quiet_since = newest_story or source.created_at
        found.append(health)
    order = {FAILING: 0, QUIET: 1, UNCHECKED: 2, WORKING: 3}
    return sorted(found, key=lambda h: (order[h.status], h.name.lower()))


@dataclass
class FeedHealth:
    """A job board or an event calendar."""

    kind: str  # "job board" or "event calendar"
    name: str
    status: str  # WORKING, FAILING or UNCHECKED
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    # Open postings or upcoming events found on the last good check.
    found: int | None


def feed_health(
    session, board_slugs: Iterable[str], calendar_slugs: Iterable[str]
) -> list[FeedHealth]:
    """The health of these job boards and event calendars: problems first."""
    found: list[FeedHealth] = []
    for model, slugs, kind in (
        (JobSource, list(board_slugs), "job board"),
        (EventCalendar, list(calendar_slugs), "event calendar"),
    ):
        if not slugs:
            continue
        for row in session.scalars(select(model).where(model.slug.in_(slugs))):
            status = UNCHECKED if row.last_run_at is None else FAILING if failed(row) else WORKING
            found.append(
                FeedHealth(
                    kind=kind,
                    name=row.name,
                    status=status,
                    last_run_at=row.last_run_at,
                    last_success_at=row.last_success_at,
                    last_error=row.last_error,
                    found=row.last_count,
                )
            )
    order = {FAILING: 0, UNCHECKED: 1, WORKING: 2}
    return sorted(found, key=lambda h: (order[h.status], h.kind, h.name.lower()))
