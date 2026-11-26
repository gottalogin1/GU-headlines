"""The month calendar at the top of the Events page.

A grid of weeks (Sunday first) for one month, with each day's main events.
Which events are main events is set per calendar in config/events.yaml
(`main_events`); when a visitor narrows the page to one calendar or searches
for a word, every matching event is shown. A day shows at most MAX_PER_DAY
events (paid featured events first, then all-day ones) and how many more
there are. An event that lasts
longer than LONG_EVENT_DAYS is shown on its first day only, so a semester or
an exhibition doesn't fill every square. Past days are left empty, and this
month starts with this week: the page is about what's coming up.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..config import CalendarConfig
from ..models import FEATURED_CALENDAR
from ..scraper.text import GUAM_TZ

MAX_PER_DAY = 3
# The colour of paid featured events (listings.py) on the calendar.
FEATURED_COLOR = "gold"
LONG_EVENT_DAYS = 7
_WEEKS = calendar.Calendar(firstweekday=calendar.SUNDAY)


@dataclass
class Day:
    day: date | None  # None: a square of the month before or after
    upcoming: bool = False  # today or later, within the collected days
    today: bool = False
    selected: bool = False
    events: list[dict] = field(default_factory=list)
    more: int = 0


@dataclass
class Month:
    first: date  # the month's first day
    weeks: list[list[Day]]
    previous: date | None  # the first day of the months to step to
    next: date | None


def first_of(day: date) -> date:
    return day.replace(day=1)


def add_months(first: date, count: int) -> date:
    months = first.year * 12 + first.month - 1 + count
    return date(months // 12, months % 12 + 1, 1)


def parse_month(value: str) -> date | None:
    """'2026-12' -> 2026-12-01, else None."""
    try:
        year, month = (int(part) for part in value.split("-"))
        return date(year, month, 1)
    except ValueError:
        return None


def grid_bounds(first: date) -> tuple[date, date]:
    """The first and last day shown for a month (whole weeks)."""
    weeks = _WEEKS.monthdatescalendar(first.year, first.month)
    return weeks[0][0], weeks[-1][-1]


def event_days(event, first: date, last: date) -> list[date]:
    """The days from `first` to `last` an event is shown on."""
    begins = event.starts_at.astimezone(GUAM_TZ)
    ends = event.ends_at.astimezone(GUAM_TZ) if event.ends_at else None
    start = begins.date()
    final = start
    if ends is not None and ends > begins:
        # An end at midnight belongs to the day before (all-day events end
        # at the next day's midnight).
        final = max(start, (ends - timedelta(microseconds=1)).date())
    if (final - start).days + 1 > LONG_EVENT_DAYS:
        final = start
    days = []
    day = max(start, first)
    while day <= min(final, last):
        days.append(day)
        day += timedelta(days=1)
    return days


def _order(entry: dict) -> tuple:
    event = entry["event"]
    featured = entry["calendar_slug"] == FEATURED_CALENDAR
    return (not featured, not event.all_day, event.starts_at, event.title.lower(), event.id or 0)


def build_month(
    first: date,
    today: date,
    last_day: date,
    items: list[dict],
    calendars: list[CalendarConfig],
    *,
    everything: bool = False,
    selected: date | None = None,
) -> Month:
    """The month starting `first`. `items` are {"event", "calendar_slug",
    "calendar_name"} rows; `last_day` is the last day events are collected
    for. With `everything`, every event is shown, else only main events."""
    config = {c.slug: c for c in calendars}
    # This month starts with this week: the weeks before it would be empty.
    weeks = [w for w in _WEEKS.monthdatescalendar(first.year, first.month) if w[-1] >= today]
    start, end = max(weeks[0][0], today), weeks[-1][-1]
    by_day: dict[date, list[dict]] = {}
    for item in items:
        settings = config.get(item["calendar_slug"])
        if not everything and settings is not None and not settings.is_main(item["event"].title):
            continue
        if settings:
            color = settings.color
        else:
            color = FEATURED_COLOR if item["calendar_slug"] == FEATURED_CALENDAR else None
        entry = {**item, "color": color}
        for day in event_days(item["event"], start, end):
            by_day.setdefault(day, []).append(entry)
    rows = []
    for week in weeks:
        row = []
        for day in week:
            if day.month != first.month:
                row.append(Day(day=None))
                continue
            found = sorted(by_day.get(day, []), key=_order)
            row.append(
                Day(
                    day=day,
                    upcoming=today <= day <= last_day,
                    today=day == today,
                    selected=day == selected,
                    events=found[:MAX_PER_DAY],
                    more=max(0, len(found) - MAX_PER_DAY),
                )
            )
        rows.append(row)
    lowest, highest = first_of(today), first_of(last_day)
    return Month(
        first=first,
        weeks=rows,
        previous=add_months(first, -1) if first > lowest else None,
        next=add_months(first, 1) if first < highest else None,
    )


def month_span(first: date) -> tuple[datetime, datetime]:
    """Guam midnight at the start of the first and after the last day shown."""
    low, high = grid_bounds(first)
    return (
        datetime.combine(low, datetime.min.time(), tzinfo=GUAM_TZ),
        datetime.combine(high + timedelta(days=1), datetime.min.time(), tzinfo=GUAM_TZ),
    )
