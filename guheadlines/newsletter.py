"""The newsletter feed (/feed.xml): the top stories since the last edition, as
an RSS feed that an email service such as SendFox turns into an email.

The worker makes a new edition on NEWSLETTER_DAYS at NEWSLETTER_TIME (Guam
time) and keeps it: the feed shows the same stories until the next edition,
so the email service sees new stories only just after an edition is made.
Each edition holds the stories first found since the edition before it, so
no story is in two editions.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert

from .config import AppConfig
from .db import session_scope
from .models import NewsletterEdition
from .scraper.text import GUAM_TZ
from .settings import DAY_NAMES, Settings
from .web import queries

# The most stories from one news source in an edition, so that one busy
# newsroom cannot fill it.
PER_SOURCE = 3


def edition_time(now: datetime, days: tuple[int, ...], at: time) -> datetime | None:
    """The newest scheduled edition time at or before `now`, in Guam time
    (None when there are no days)."""
    local = now.astimezone(GUAM_TZ)
    for back in range(8):
        day = local.date() - timedelta(days=back)
        when = datetime.combine(day, at, tzinfo=GUAM_TZ)
        if day.weekday() in days and when <= local:
            return when
    return None


def next_edition_time(now: datetime, days: tuple[int, ...], at: time) -> datetime | None:
    """The first scheduled edition time after `now`, in Guam time (None when
    there are no days)."""
    local = now.astimezone(GUAM_TZ)
    for ahead in range(8):
        day = local.date() + timedelta(days=ahead)
        when = datetime.combine(day, at, tzinfo=GUAM_TZ)
        if day.weekday() in days and when > local:
            return when
    return None


def schedule_text(days: tuple[int, ...], at: time) -> str:
    """(1, 4), 10:00 -> "Tuesday and Friday at 10:00 AM"."""
    names = [DAY_NAMES[d].capitalize() for d in days]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    return f"{joined} at {at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def form_edition(
    settings: Settings, config: AppConfig, now: datetime | None = None
) -> tuple[datetime, int] | None:
    """Make the newest edition that is due, if it is not made yet. Returns
    (its time, its number of stories), or None when there was nothing to make."""
    now = now or datetime.now(timezone.utc)
    days, at = settings.newsletter_days, settings.newsletter_time
    formed_at = edition_time(now, days, at)
    if formed_at is None:
        return None
    since = edition_time(formed_at - timedelta(seconds=1), days, at)
    with session_scope() as session:
        if session.get(NewsletterEdition, formed_at) is not None:
            return None
        ids = queries.top_stories(
            session,
            since,
            formed_at,
            settings.newsletter_stories,
            PER_SOURCE,
            # Like the home page: no Obituaries, and no only_in_topic sources.
            config.hidden_sources(),
            config.separate_topics(),
        )
        # Two workers can make the same edition at the same time; the first one wins.
        made = session.execute(
            insert(NewsletterEdition)
            .values(formed_at=formed_at, article_ids=ids)
            .on_conflict_do_nothing()
            .returning(NewsletterEdition.formed_at)
        ).first()
    return (formed_at, len(ids)) if made else None
