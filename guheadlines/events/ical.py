"""Reading iCalendar (.ics) feeds, the format calendar apps share calendars in.

A public Google Calendar, an Outlook calendar and most website calendar
plugins can all be read through their iCal address. This reader covers what
those feeds use: folded lines, time zones, all-day events, repeating events
(RRULE, RDATE, EXDATE), changed or cancelled single dates of a repeating event
(RECURRENCE-ID) and DURATION. Events marked private or cancelled are left out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo

from dateutil import tz
from dateutil.rrule import rruleset, rrulestr

from ..scraper.text import GUAM_TZ, clean_text, strip_html, truncate

DESCRIPTION_LIMIT = 600
# A repeating event gives at most this many dates per check (a daily event
# over a long window would otherwise flood the page).
MAX_OCCURRENCES = 400


class CalendarError(ValueError):
    """The address answered, but not with a calendar we can read."""


@dataclass
class CalendarEvent:
    # Stays the same when the event is edited, so an edit updates the event
    # instead of adding a new one. A repeating event's dates each get their own.
    uid: str
    title: str
    starts_at: datetime  # always with a time zone
    ends_at: datetime | None = None
    # True for an event with a date but no time (all day, or a time not given).
    all_day: bool = False
    location: str | None = None
    # Who holds the event (a government office, a school...), when known.
    organizer: str | None = None
    description: str | None = None
    url: str | None = None
    # A document for the event, such as a session agenda (PDF).
    agenda_url: str | None = None


@dataclass
class _Component:
    name: str
    props: list[tuple[str, dict[str, str], str]] = field(default_factory=list)
    children: list[_Component] = field(default_factory=list)

    def first(self, name: str) -> tuple[dict[str, str], str] | None:
        for prop, params, value in self.props:
            if prop == name:
                return params, value
        return None

    def value(self, name: str) -> str | None:
        found = self.first(name)
        return found[1] if found else None

    def all(self, name: str) -> list[tuple[dict[str, str], str]]:
        return [(params, value) for prop, params, value in self.props if prop == name]


# ------------------------------------------------------------------ lines


def _unfold(text: str) -> list[str]:
    """Join folded lines (a line that starts with a space or tab continues the one before)."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


_PARAM_RE = re.compile(r';([A-Za-z0-9-]+)=("[^"]*"|[^";:]*)')


def _split_line(line: str) -> tuple[str, dict[str, str], str]:
    """'DTSTART;TZID=Pacific/Guam:20261201T090000' -> ('DTSTART', {'TZID': ...}, '2026...')"""
    in_quotes = False
    for i, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            head, value = line[:i], line[i + 1 :]
            break
    else:
        return line.upper(), {}, ""
    name, _, rest = head.partition(";")
    params = {k.upper(): v.strip('"') for k, v in _PARAM_RE.findall(";" + rest)} if rest else {}
    return name.strip().upper(), params, value


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _parse(text: str) -> _Component:
    root = _Component("ROOT")
    stack = [root]
    for line in _unfold(text):
        name, params, value = _split_line(line)
        if name == "BEGIN":
            child = _Component(value.strip().upper())
            stack[-1].children.append(child)
            stack.append(child)
        elif name == "END":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].props.append((name, params, value))
    return root


# ------------------------------------------------------------------ times


def _offset(value: str | None) -> tzinfo | None:
    """'+1000' -> UTC+10:00"""
    match = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?", (value or "").strip())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    return timezone(sign * delta)


class _Zones:
    """Time zone names used in a feed. Most feeds use names such as
    "Pacific/Guam"; Outlook uses its own ("West Pacific Standard Time"), which
    are read from the feed's VTIMEZONE (its standard offset). Anything else,
    and times without a zone, count as the calendar's own zone."""

    def __init__(self, calendar: _Component, default: tzinfo):
        self.default = default
        named = tz.gettz(calendar.value("X-WR-TIMEZONE") or "")
        if named is not None:
            self.default = named
        self._known: dict[str, tzinfo] = {}
        for zone in calendar.children:
            if zone.name != "VTIMEZONE" or not zone.value("TZID"):
                continue
            standard = next((c for c in zone.children if c.name == "STANDARD"), None)
            offset = _offset(standard.value("TZOFFSETTO")) if standard else None
            if offset is not None:
                self._known[zone.value("TZID")] = offset

    def get(self, name: str | None) -> tzinfo:
        if not name:
            return self.default
        return tz.gettz(name) or self._known.get(name) or self.default


def _parse_time(value: str, params: dict[str, str], zones: _Zones) -> tuple[datetime, bool]:
    """A DTSTART/DTEND/EXDATE value -> (time with zone, is a date only)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(day, time.min, tzinfo=zones.get(params.get("TZID"))), True
    utc = value.endswith("Z")
    stamp = value.rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            naive = datetime.strptime(stamp, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"not an iCal time: {value!r}")
    if utc:
        return naive.replace(tzinfo=timezone.utc), False
    return naive.replace(tzinfo=zones.get(params.get("TZID"))), False


_DURATION_RE = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _duration(value: str | None) -> timedelta | None:
    match = _DURATION_RE.match((value or "").strip().upper())
    if not match or not any(match.groups()[1:]):
        return None
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups()[1:])
    delta = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -delta if match.group(1) == "-" else delta


def _times(component: _Component, name: str, zones: _Zones) -> list[datetime]:
    """Every time in every EXDATE/RDATE line (values may be comma-separated)."""
    found = []
    for params, value in component.all(name):
        if params.get("VALUE", "").upper() == "PERIOD":
            continue
        for part in value.split(","):
            try:
                found.append(_parse_time(part, params, zones)[0])
            except ValueError:
                continue
    return found


# --------------------------------------------------------------- repeats


def _local_rule(rule: str, start: datetime) -> str:
    """Rewrite UNTIL in the start's own wall-clock time, so the rule can be
    worked out without time zones (and a weekly 9 AM meeting stays at 9 AM)."""

    def fix(match: re.Match) -> str:
        value = match.group(1)
        try:
            if len(value) == 8:  # a date: through the end of that day
                day = datetime.strptime(value, "%Y%m%d")
                until = day + timedelta(days=1, seconds=-1)
            elif value.endswith("Z"):
                utc = datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                until = utc.astimezone(start.tzinfo).replace(tzinfo=None)
            else:
                until = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        except ValueError:
            return ""
        return f"UNTIL={until:%Y%m%dT%H%M%S}"

    rule = re.sub(r"UNTIL=([0-9TZ]+)", fix, rule, flags=re.I)
    return ";".join(p for p in rule.split(";") if p)


def _occurrences(
    component: _Component,
    start: datetime,
    window: tuple[datetime, datetime],
    zones: _Zones,
) -> list[datetime]:
    """The dates of a repeating event that fall inside `window`."""
    zone = start.tzinfo
    naive_start = start.replace(tzinfo=None)
    rules = rruleset()
    for _, value in component.all("RRULE"):
        try:
            rules.rrule(rrulestr(_local_rule(value, start), dtstart=naive_start))
        except (ValueError, TypeError):
            continue
    rules.rdate(naive_start)
    for extra in _times(component, "RDATE", zones):
        rules.rdate(extra.astimezone(zone).replace(tzinfo=None))
    for skip in _times(component, "EXDATE", zones):
        rules.exdate(skip.astimezone(zone).replace(tzinfo=None))
    low = window[0].astimezone(zone).replace(tzinfo=None)
    high = window[1].astimezone(zone).replace(tzinfo=None)
    found = []
    for when in rules.xafter(low - timedelta(days=1), inc=True):
        if when > high or len(found) >= MAX_OCCURRENCES:
            break
        found.append(when.replace(tzinfo=zone))
    return found


# ---------------------------------------------------------------- events


def _text(component: _Component, name: str) -> str | None:
    value = component.value(name)
    return clean_text(_unescape(value)) or None if value else None


def _description(component: _Component) -> str | None:
    value = component.value("DESCRIPTION")
    return tidy_description(_unescape(value)) if value else None


_SPACE_BEFORE_RE = re.compile(r"\s+([.,;:!?)])")


def tidy_description(text: str | None) -> str | None:
    """Plain text with its line breaks kept, shortened for the Events page."""
    if not text:
        return None
    # Google Calendar descriptions may be HTML: keep its line breaks.
    if re.search(r"<(br|p|a|div|span|b|i|u|ul|li)\b", text, re.I):
        text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
        # strip_html puts a space where each tag was: "<b>ID</b>." -> "ID ."
        lines = [_SPACE_BEFORE_RE.sub(r"\1", strip_html(line)) for line in text.split("\n")]
    else:
        lines = [clean_text(line) for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return truncate(text, DESCRIPTION_LIMIT) or None


def _web_link(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if value.lower().startswith(("http://", "https://")) else None


def _skipped(component: _Component) -> bool:
    status = (component.value("STATUS") or "").strip().upper()
    privacy = (component.value("CLASS") or "").strip().upper()
    return status == "CANCELLED" or privacy in ("PRIVATE", "CONFIDENTIAL")


def _key(start: datetime) -> str:
    return start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_calendar(
    content: bytes | str,
    *,
    start: datetime,
    end: datetime,
    default_tz: tzinfo = GUAM_TZ,
) -> tuple[str | None, list[CalendarEvent]]:
    """(calendar name, the events that overlap start..end) from an iCal feed."""
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    text = text.lstrip("﻿")
    if "BEGIN:VCALENDAR" not in text[:2000].upper():
        raise CalendarError("not an iCal calendar (no BEGIN:VCALENDAR)")
    root = _parse(text)
    calendar = next((c for c in root.children if c.name == "VCALENDAR"), None)
    if calendar is None:
        raise CalendarError("not an iCal calendar")
    zones = _Zones(calendar, default_tz)
    name = _text(calendar, "X-WR-CALNAME")

    masters: list[_Component] = []
    # Single dates of a repeating event that were changed: (uid, original start) -> event.
    changed: dict[tuple[str, str], _Component] = {}
    for event in calendar.children:
        if event.name != "VEVENT" or not event.value("DTSTART"):
            continue
        recurrence = event.first("RECURRENCE-ID")
        uid = (event.value("UID") or "").strip()
        if recurrence and uid:
            try:
                original = _parse_time(recurrence[1], recurrence[0], zones)[0]
            except ValueError:
                continue
            changed[(uid, _key(original))] = event
        else:
            masters.append(event)

    events: list[CalendarEvent] = []

    def add(component: _Component, begins: datetime, uid: str, all_day: bool, length) -> None:
        if all_day and not length:
            length = timedelta(days=1)  # an all-day event without an end lasts its day
        ends = begins + length if length else None
        if begins > end or (ends or begins) < start:
            return
        title = _text(component, "SUMMARY")
        if not title or _skipped(component):
            return
        events.append(
            CalendarEvent(
                uid=uid[:500],
                title=truncate(title, 300),
                starts_at=begins,
                ends_at=ends,
                all_day=all_day,
                location=_text(component, "LOCATION"),
                description=_description(component),
                url=_web_link(component.value("URL")),
            )
        )

    def timing(component: _Component) -> tuple[datetime, bool, timedelta | None] | None:
        params, value = component.first("DTSTART")
        try:
            begins, all_day = _parse_time(value, params, zones)
        except ValueError:
            return None
        length = None
        dtend = component.first("DTEND")
        if dtend:
            try:
                length = _parse_time(dtend[1], dtend[0], zones)[0] - begins
            except ValueError:
                length = None
        else:
            length = _duration(component.value("DURATION"))
        if length is not None and length < timedelta(0):
            length = None
        return begins, all_day, length

    for master in masters:
        found = timing(master)
        if found is None:
            continue
        begins, all_day, length = found
        uid = (master.value("UID") or "").strip() or f"{_key(begins)}:{_text(master, 'SUMMARY')}"
        if not master.first("RRULE") and not master.first("RDATE"):
            add(master, begins, uid, all_day, length)
            continue
        for when in _occurrences(master, begins, (start, end), zones):
            override = changed.pop((uid, _key(when)), None)
            if override is not None:
                moved = timing(override)
                if moved is not None:
                    add(override, moved[0], f"{uid}/{_key(when)}", moved[1], moved[2])
                continue
            add(master, when, f"{uid}/{_key(when)}", all_day, length)

    # Changed dates whose repeating event isn't in the window (or the feed).
    for (uid, original), override in changed.items():
        moved = timing(override)
        if moved is not None:
            add(override, moved[0], f"{uid}/{original}", moved[1], moved[2])

    events.sort(key=lambda e: (e.starts_at, e.title))
    return name, events
