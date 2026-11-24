"""Reading the event calendars: each calendar's inputs -> events -> the events table.

Each check reads every calendar's upcoming events (the next DAYS_AHEAD days).
Events seen before are updated, so a changed time or place shows up; events
the calendar no longer lists are marked removed and leave the Events page.
When one of a calendar's inputs can't be read, nothing of that calendar is
marked removed, and the reason is shown on the Events page.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from ..config import CalendarConfig
from ..db import EVENTS_LOCK_KEY, advisory_lock, session_scope
from ..models import Event, EventCalendar
from ..scraper.http import FetchError, HttpClient
from ..settings import Settings
from .ical import CalendarError, CalendarEvent
from .readers import (
    merge_agendas,
    read_agendas,
    read_google_calendar,
    read_ical,
    read_listing,
    read_notices,
)

log = logging.getLogger(__name__)

DAYS_AHEAD = 90  # how far ahead events are collected
DAYS_BEHIND = 1  # events that ended within this many days are still read
_CANCELLED_RE = re.compile(r"\b(cancell?ed|postponed)\b", re.IGNORECASE)


@dataclass
class CalendarResult:
    slug: str
    found: int = 0
    new: int = 0
    removed: int = 0
    error: str | None = None
    events: list[CalendarEvent] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def window(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or _now()
    return now - timedelta(days=DAYS_BEHIND), now + timedelta(days=DAYS_AHEAD)


def sync_calendars(session, calendars: list[CalendarConfig]) -> dict[str, int]:
    """Rows in event_calendars to match events.yaml (removed calendars are only disabled)."""
    existing = {c.slug: c for c in session.scalars(select(EventCalendar))}
    for calendar in calendars:
        row = existing.get(calendar.slug)
        if row is None:
            row = EventCalendar(slug=calendar.slug, name=calendar.name)
            session.add(row)
            existing[calendar.slug] = row
        row.name = calendar.name
        row.homepage = calendar.homepage
        row.enabled = calendar.enabled
    configured = {c.slug for c in calendars}
    for slug, row in existing.items():
        if slug not in configured:
            row.enabled = False
    session.flush()
    return {slug: row.id for slug, row in existing.items()}


class EventChecker:
    def __init__(
        self,
        settings: Settings,
        calendars: list[CalendarConfig],
        client: HttpClient | None = None,
    ):
        self.settings = settings
        self.calendars = calendars
        self._own_client = client is None
        self.client = client or HttpClient(
            settings.user_agent,
            timeout=settings.request_timeout,
            per_host_delay=settings.per_host_delay,
            respect_robots=settings.respect_robots,
        )

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    # ---------------------------------------------------------------- reading

    def collect(
        self, calendar: CalendarConfig, now: datetime | None = None
    ) -> tuple[list[CalendarEvent], list[str]]:
        """A calendar's upcoming events (no database needed), and the inputs
        that couldn't be read. Raises CalendarError when none could be."""
        start, end = window(now)
        for url in calendar.addresses:
            if calendar.request_delay:
                self.client.set_host_delay(url, calendar.request_delay)
            if calendar.ignore_robots:
                self.client.ignore_robots_for(url)
        readers = (
            [
                (cid, lambda cid: read_google_calendar(
                    self.client, cid, self.settings.google_calendar_api_key, start, end
                ))
                for cid in calendar.google_calendar
            ]
            + [(u, lambda u: read_ical(self.client, u, start, end)) for u in calendar.ical]
            + [(u, lambda u: read_agendas(self.client, u, start, end)) for u in calendar.agendas]
            + [(u, lambda u: read_notices(self.client, u, start, end)) for u in calendar.notices]
            + [
                (u, lambda u: read_listing(self.client, u, calendar, start, end))
                for u in calendar.pages
            ]
        )  # fmt: skip
        events: list[CalendarEvent] = []
        errors: list[str] = []
        for address, read in readers:
            try:
                events += read(address)
            except (FetchError, CalendarError) as exc:
                errors.append(str(exc) if address in str(exc) else f"{address}: {exc}")
        if errors and len(errors) == len(readers):
            raise CalendarError("; ".join(errors))
        return self._tidy(calendar, events), errors

    def _tidy(self, calendar: CalendarConfig, events: list[CalendarEvent]) -> list[CalendarEvent]:
        kept: dict[str, CalendarEvent] = {}
        for event in merge_agendas(events):
            event.location = event.location or calendar.location
            event.organizer = event.organizer or calendar.organizer
            if calendar.excluded(event.title, event.organizer) or _CANCELLED_RE.search(event.title):
                continue
            kept.setdefault(event.uid, event)
        return sorted(kept.values(), key=lambda e: (e.starts_at, e.title))

    # ------------------------------------------------------------------- runs

    def run(self, only: list[str] | None = None) -> list[CalendarResult] | None:
        """Check every collected calendar (or just `only`). None if another
        events check holds the lock."""
        with advisory_lock(EVENTS_LOCK_KEY) as acquired:
            if not acquired:
                log.warning("another events check is already running; skipping this one")
                return None
            with session_scope() as session:
                ids = sync_calendars(session, self.calendars)
            chosen = [c for c in self.calendars if c.collected and (not only or c.slug in only)]
            results = [self.run_calendar(c, ids[c.slug]) for c in chosen]
            log.info(
                "events check finished: %d upcoming events, %d new, from %d calendars",
                sum(r.found for r in results),
                sum(r.new for r in results),
                len(results),
            )
            return results

    def run_calendar(self, calendar: CalendarConfig, calendar_id: int) -> CalendarResult:
        result = CalendarResult(slug=calendar.slug)
        started = _now()
        try:
            events, errors = self.collect(calendar, started)
            result.found = len(events)
            # An input that failed leaves its events as they were.
            self._store(calendar_id, events, started, result, remove_missing=not errors)
            if errors:
                result.error = "; ".join(errors)
        except (FetchError, CalendarError) as exc:
            result.error = str(exc)
        except Exception as exc:  # never let one calendar break the others
            log.exception("[events:%s] crashed", calendar.slug)
            result.error = f"{type(exc).__name__}: {exc}"

        values = {"last_run_at": _now(), "last_error": result.error}
        if result.error is None or result.found:
            values |= {"last_success_at": values["last_run_at"], "last_count": result.found}
        with session_scope() as session:
            session.execute(
                update(EventCalendar).where(EventCalendar.id == calendar_id).values(**values)
            )
        log.info(
            "[events:%s] %d upcoming, %d new, %d removed%s",
            calendar.slug,
            result.found,
            result.new,
            result.removed,
            f" (error: {result.error})" if result.error else "",
        )
        return result

    def _store(
        self,
        calendar_id: int,
        events: list[CalendarEvent],
        now: datetime,
        result: CalendarResult,
        *,
        remove_missing: bool,
    ) -> None:
        start, _ = window(now)
        with session_scope() as session:
            # Events still to come (or just over): the ones this check covers.
            known = {
                row.uid: row
                for row in session.scalars(
                    select(Event).where(
                        Event.calendar_id == calendar_id,
                        or_(Event.ends_at >= start, Event.starts_at >= start),
                    )
                )
            }
            for found in events:
                row = known.get(found.uid)
                if row is None:
                    row = session.scalar(
                        select(Event).where(
                            Event.calendar_id == calendar_id, Event.uid == found.uid
                        )
                    )
                if row is None:
                    row = Event(calendar_id=calendar_id, uid=found.uid, first_seen_at=now)
                    session.add(row)
                    result.new += 1
                row.title = found.title[:500]
                row.starts_at = found.starts_at
                row.ends_at = found.ends_at
                row.all_day = found.all_day
                row.location = found.location
                row.organizer = found.organizer
                row.description = found.description
                row.url = found.url
                row.agenda_url = found.agenda_url
                row.last_seen_at = now
                row.removed_at = None
            if remove_missing:
                listed = {e.uid for e in events}
                for uid, row in known.items():
                    if uid not in listed and row.removed_at is None:
                        row.removed_at = now
                        result.removed += 1
