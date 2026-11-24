"""Reading each kind of calendar input in config/events.yaml.

Every reader returns CalendarEvents. An event's uid starts with its input
kind ("g:", "ical:", "agenda:", "notice:", "page:"), so two inputs of one
calendar never mix up their events.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, tzinfo
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import tz

from ..scraper.http import FetchResult, HttpClient
from ..scraper.text import GUAM_TZ, clean_text, truncate
from .ical import CalendarError, CalendarEvent, read_calendar, tidy_description

DAY = timedelta(days=1)
GOOGLE_API = "https://www.googleapis.com/calendar/v3/calendars/{id}/events"
GOOGLE_MAX_PAGES = 8  # 250 events a page


# ------------------------------------------------------------ dates in text

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|"
    "june|july|august|september|october|november|december"
)
_DATE = (
    r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}|\d{2})(?!\d)"
    rf"|(?P<mon>{_MONTHS})\.?\s+(?P<md>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<my>\d{{4}})"
    r"|(?P<iy>\d{4})-(?P<im>\d{2})-(?P<id>\d{2})"
)
_TIME = r"(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ap>[ap])\.?\s?m\b\.?|(?P<noon>\bnoon\b)"
_TOKEN_RE = re.compile(rf"\b(?P<date>{_DATE})|(?P<time>{_TIME})", re.IGNORECASE)


def _token_date(match: re.Match) -> date | None:
    try:
        if match.group("m"):
            year = int(match.group("y"))
            year += 2000 if year < 100 else 0
            return date(year, int(match.group("m")), int(match.group("d")))
        if match.group("mon"):
            month = datetime.strptime(match.group("mon")[:3].title(), "%b").month
            return date(int(match.group("my")), month, int(match.group("md")))
        return date(int(match.group("iy")), int(match.group("im")), int(match.group("id")))
    except ValueError:
        return None


def _token_time(match: re.Match) -> time | None:
    if match.group("noon"):
        return time(12, 0)
    hour, minute = int(match.group("h")), int(match.group("min") or 0)
    if not (1 <= hour <= 12 and minute < 60):
        return None
    if match.group("ap").lower() == "p" and hour != 12:
        hour += 12
    elif match.group("ap").lower() == "a" and hour == 12:
        hour = 0
    return time(hour, minute)


def parse_when(
    text: str | None, zone: tzinfo = GUAM_TZ
) -> tuple[datetime, datetime | None, bool] | None:
    """(start, end, all_day) from text such as "12/21/2026 - 08:00 AM -
    12/23/2026 - 04:00 PM", "Thursday, November 19, 2026, at 2:00 p.m." or
    "11/24/2026 10:00 AM - 11:00 AM". Times are Guam time. None without a date."""
    tokens: list[tuple[str, date | time]] = []
    for match in _TOKEN_RE.finditer(text or ""):
        value = _token_date(match) if match.group("date") else _token_time(match)
        if value is not None:
            tokens.append(("date" if match.group("date") else "time", value))
    while tokens and tokens[0][0] != "date":
        tokens.pop(0)
    if not tokens:
        return None
    start_day = tokens[0][1]
    rest = tokens[1:]
    start_time = rest.pop(0)[1] if rest and rest[0][0] == "time" else None
    end_day = rest.pop(0)[1] if rest and rest[0][0] == "date" else None
    end_time = rest.pop(0)[1] if rest and rest[0][0] == "time" else None
    if end_day is not None and end_day < start_day:
        end_day = None
    if start_time is None:
        begins = datetime.combine(start_day, time.min, tzinfo=zone)
        last = end_day or start_day
        return begins, datetime.combine(last + DAY, time.min, tzinfo=zone), True
    begins = datetime.combine(start_day, start_time, tzinfo=zone)
    ends = None
    if end_time is not None:
        ends = datetime.combine(end_day or start_day, end_time, tzinfo=zone)
    elif end_day is not None:
        ends = datetime.combine(end_day + DAY, time.min, tzinfo=zone)
    if ends is not None and ends <= begins:
        ends = None
    return begins, ends, False


def _in_window(event: CalendarEvent, start: datetime, end: datetime) -> bool:
    return event.starts_at <= end and (event.ends_at or event.starts_at) >= start


def _web_link(href: str | None, base: str) -> str | None:
    """An absolute http(s) address, with spaces and other unsafe characters
    escaped (the Legislature's PDF names contain spaces)."""
    if not href or href.strip().lower().startswith(("javascript:", "mailto:", "#")):
        return None
    url = urljoin(base, href.strip())
    if not url.lower().startswith(("http://", "https://")):
        return None
    return quote(url, safe=":/?&=#%@+,;~!$'()*[]")


# --------------------------------------------------------- google calendar


def _google_time(value: dict | None, zone: tzinfo) -> tuple[datetime, bool] | None:
    value = value or {}
    if value.get("dateTime"):
        moment = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz.gettz(value.get("timeZone") or "") or zone)
        return moment, False
    if value.get("date"):
        day = date.fromisoformat(value["date"])
        return datetime.combine(day, time.min, tzinfo=zone), True
    return None


def google_event(item: dict, zone: tzinfo = GUAM_TZ) -> CalendarEvent | None:
    """One event from the Calendar API's events list (None: skip it)."""
    if item.get("status") == "cancelled" or item.get("visibility") in ("private", "confidential"):
        return None
    title = clean_text(item.get("summary"))
    started = _google_time(item.get("start"), zone)
    if not title or not item.get("id") or started is None:
        return None
    begins, all_day = started
    finished = _google_time(item.get("end"), zone)
    ends = finished[0] if finished and finished[0] > begins else None
    if all_day and ends is None:
        ends = begins + DAY
    link = item.get("htmlLink") or ""
    return CalendarEvent(
        uid=f"g:{item['id']}"[:500],
        title=truncate(title, 300),
        starts_at=begins,
        ends_at=ends,
        all_day=all_day,
        location=clean_text(item.get("location")) or None,
        description=tidy_description(item.get("description")),
        url=link if link.startswith("https://") else None,
    )


def read_google_calendar(
    client: HttpClient, calendar_id: str, api_key: str, start: datetime, end: datetime
) -> list[CalendarEvent]:
    """A public Google Calendar, through Google's Calendar API. Google expands
    repeating events itself (singleEvents), so each date comes as its own event."""
    if not api_key:
        raise CalendarError(
            "needs a free Google API key: set GOOGLE_CALENDAR_API_KEY (see docs/events.md)"
        )
    base = GOOGLE_API.format(id=quote(calendar_id, safe=""))
    params = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "250",
    }
    # The key goes in a header, so it never appears in an address or a log.
    headers = {"X-Goog-Api-Key": api_key}
    events: list[CalendarEvent] = []
    token = None
    for _ in range(GOOGLE_MAX_PAGES):
        query = params | ({"pageToken": token} if token else {})
        # An API that is called with a key is not crawled: robots.txt (which
        # tells search engines to stay off www.googleapis.com) doesn't apply.
        result = client.get(
            f"{base}?{urlencode(query)}",
            accept="application/json",
            headers=headers,
            check_robots=False,
        )
        try:
            data = json.loads(result.content)
        except ValueError as exc:
            raise CalendarError(f"Google Calendar API gave no JSON: {exc}") from None
        zone = tz.gettz(data.get("timeZone") or "") or GUAM_TZ
        events += [e for item in data.get("items") or [] if (e := google_event(item, zone))]
        token = data.get("nextPageToken")
        if not token:
            break
    return events


# --------------------------------------------------------------------- iCal


def read_ical(client: HttpClient, url: str, start: datetime, end: datetime) -> list[CalendarEvent]:
    result = client.get(url, accept="text/calendar, */*;q=0.5")
    _, events = read_calendar(result.content, start=start, end=end)
    for event in events:
        event.uid = f"ical:{event.uid}"[:500]
    return events


# ------------------------------------------------------------------ agendas


_DASH_RE = re.compile(r"\s+[–—-]\s+|\s*[–—]\s*")
# "OCTOBER 15-16, 2026" -> "OCTOBER 15, 2026" (a session over several days)
_RANGE_RE = re.compile(rf"\b({_MONTHS})\.?\s+(\d{{1,2}})\s*[-–—]\s*\d{{1,2}}(,?\s+\d{{4}})", re.I)


def parse_agendas(result: FetchResult) -> list[CalendarEvent]:
    """Sessions from a page that lists agendas (PDFs) under a dated heading,
    such as "DECEMBER 1, 2026 – Regular Session". A daily agenda dated after
    the session's first day adds that day too."""
    soup = BeautifulSoup(result.content, "lxml")
    events: list[CalendarEvent] = []
    for item in soup.select(".blockspare-accordion-item, details, .accordion-item"):
        label_node = item.select_one(
            ".blockspare-accordion-panel-handler-label, summary, .accordion-button, "
            "h2, h3, h4, button"
        )
        label = clean_text(label_node.get_text(" ")) if label_node else ""
        label = _RANGE_RE.sub(r"\1 \2\3", label)
        when = parse_when(label)
        if when is None:
            continue
        day = when[0].date()
        parts = _DASH_RE.split(label, maxsplit=1)
        title = clean_text(parts[1]) if len(parts) == 2 else "Legislative session"
        agendas = []
        for link in item.find_all("a", href=True):
            text = clean_text(link.get_text(" "))
            href = link["href"]
            if "agenda" not in f"{text} {href}".lower():
                continue
            url = _web_link(href, result.url)
            if url:
                dated = parse_when(text)
                agendas.append((url, text, dated[0].date() if dated else None))
        if not agendas:
            continue
        # The whole session's agenda, else the first day's daily agenda.
        main = next((a for a in agendas if a[2] is None or a[2] == day), agendas[0])
        days = {day: main[0]}
        for url, _, dated in agendas:
            if dated and dated > day:
                days.setdefault(dated, url)
        for number, (when_day, agenda) in enumerate(sorted(days.items())):
            begins = datetime.combine(when_day, time.min, tzinfo=GUAM_TZ)
            events.append(
                CalendarEvent(
                    uid=f"agenda:{when_day.isoformat()}:{title.lower()}"[:500],
                    title=title if number == 0 else f"{title} (continued)",
                    starts_at=begins,
                    ends_at=begins + DAY,
                    all_day=True,
                    url=result.url,
                    agenda_url=agenda,
                )
            )
    return events


def read_agendas(
    client: HttpClient, url: str, start: datetime, end: datetime
) -> list[CalendarEvent]:
    result = client.get(url)
    return [e for e in parse_agendas(result) if _in_window(e, start, end)]


def merge_agendas(events: list[CalendarEvent]) -> list[CalendarEvent]:
    """Put a session's agenda on the calendar's own entry for that session
    (same day, "session" in its title), instead of listing the session twice."""
    merged = []
    entries = [e for e in events if not e.uid.startswith("agenda:")]
    for event in events:
        if not event.uid.startswith("agenda:"):
            merged.append(event)
            continue
        day = event.starts_at.astimezone(GUAM_TZ).date()
        twin = next(
            (
                e
                for e in entries
                if "session" in e.title.lower() and e.starts_at.astimezone(GUAM_TZ).date() == day
            ),
            None,
        )
        if twin is None:
            merged.append(event)
        elif not twin.agenda_url:
            twin.agenda_url = event.agenda_url
    return merged


# ------------------------------------------------------- notices.guam.gov


_NOTICE_PREFIX_RE = re.compile(
    r"^\W*(?:(?:first|second|third|final|1st|2nd|3rd|amended|revised|updated)\s+)?"
    r"notice\b\s*(?:of\s+(?:the\s+)?|[-–—:]\s*)?",
    re.IGNORECASE,
)
_MEETING_DATE_RE = re.compile(r"Meeting Date:\s*(.{0,40})", re.IGNORECASE)
# Words kept in capitals when a name written in capitals is tidied.
_ACRONYMS = {
    "BBMR", "BSP", "CCU", "CLTC", "CNMI", "DOA", "DOE", "DOL", "DPHSS", "DPR", "DPW", "DRT",
    "DYA", "GALC", "GCC", "GDOE", "GEDA", "GEPA", "GHURA", "GIAA", "GMHA", "GPA", "GPD",
    "GSA", "GVB", "GWA", "OPA", "PUC", "UOG", "US", "USA",
}  # fmt: skip
_SMALL_WORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to"}


def tidy_name(name: str | None) -> str | None:
    """'DEPARTMENT OF PUBLIC WORKS (DPW)' -> 'Department of Public Works (DPW)'.
    Names not written all in capitals are kept as they are."""
    name = clean_text(name)
    if not name or name != name.upper():
        return name or None
    words = []
    for number, word in enumerate(name.split(" ")):
        bare = word.strip("().,-")
        if bare in _ACRONYMS or re.fullmatch(r"[A-Z](?:\.[A-Z])+\.?", word.strip("(),-")):
            words.append(word)
        elif number and bare.lower() in _SMALL_WORDS:
            words.append(word.lower())
        else:
            words.append(word.capitalize() if "-" not in word else word.title())
    return " ".join(words)


def _notice_title(title: str) -> str:
    """'SECOND NOTICE OF REGULAR BOARD MEETING' -> 'Regular Board Meeting'."""
    title = _NOTICE_PREFIX_RE.sub("", clean_text(title)).strip(" -–—:")
    title = tidy_name(title) or ""
    return title[:1].upper() + title[1:]


def _meeting_date(article: Tag) -> str | None:
    """The text after "Meeting Date:" (its own list item on the notice)."""
    for item in article.find_all("li"):
        text = clean_text(item.get_text(" "))
        if text.lower().startswith("meeting date"):
            return text.split(":", 1)[-1]
    found = _MEETING_DATE_RE.search(clean_text(article.get_text(" ")))
    return found.group(1) if found else None


def parse_notices(result: FetchResult) -> list[CalendarEvent]:
    """Meetings from a notices.guam.gov listing page: each notice that gives a
    meeting date. A meeting's second notice (posted later, listed first) wins
    over its first."""
    soup = BeautifulSoup(result.content, "lxml")
    articles = soup.select("article.post")
    if not articles:
        raise CalendarError(f"no notices found on {result.url}; the page may have changed")
    events: list[CalendarEvent] = []
    seen: set[tuple[str | None, datetime]] = set()
    for article in articles:
        heading = article.select_one(".post-title a[href]")
        if heading is None:
            continue
        when = parse_when(_meeting_date(article))
        if when is None:
            continue
        department = article.select_one('a[href*="department_id="]')
        organizer = tidy_name(department.get_text(" ")) if department else None
        if (organizer, when[0]) in seen:
            continue
        seen.add((organizer, when[0]))
        url = _web_link(heading["href"], result.url)
        notice = re.search(r"/notice_detail/(\d+)", url or "")
        title = _notice_title(heading.get_text(" "))
        events.append(
            CalendarEvent(
                uid=f"notice:{notice.group(1) if notice else url}"[:500],
                title=truncate(title, 300),
                starts_at=when[0],
                ends_at=when[1],
                all_day=when[2],
                organizer=organizer,
                url=url,
            )
        )
    return events


NOTICE_PAGES = 4  # listing pages read per check (newest notices first)


def _next_page(result: FetchResult) -> str | None:
    """The listing's "next page" link, if any."""
    soup = BeautifulSoup(result.content, "lxml")
    link = soup.select_one('a[rel~="next"], li.next a[href], .pagination a.next[href]')
    if link is None:
        labels = {"»", "›", "next", "next »", "next ›", "next page"}
        link = next(
            (
                a
                for a in soup.select(".pagination a[href], .paging a[href]")
                if clean_text(a.get_text(" ")).lower() in labels
            ),
            None,
        )
    return _web_link(link.get("href"), result.url) if link else None


def read_notices(
    client: HttpClient, url: str, start: datetime, end: datetime
) -> list[CalendarEvent]:
    """Meetings from a notices.guam.gov listing and its next few pages. Pages
    list the newest notices first, so a few pages cover the coming weeks."""
    events: list[CalendarEvent] = []
    seen_pages = set()
    while url and url not in seen_pages and len(seen_pages) < NOTICE_PAGES:
        seen_pages.add(url)
        result = client.get(url)
        found = parse_notices(result)
        known = {(e.organizer, e.starts_at) for e in events}
        events += [e for e in found if (e.organizer, e.starts_at) not in known]
        url = _next_page(result)
    return [e for e in events if _in_window(e, start, end)]


# --------------------------------------------------------- listing pages


def _link_of(node: Tag | None) -> Tag | None:
    if node is None:
        return None
    if node.name == "a" and node.get("href"):
        return node
    return node.find("a", href=True) or node.find_parent("a", href=True)


def parse_listing(result: FetchResult, calendar) -> list[CalendarEvent]:
    """Events from a web page that lists them, using the calendar's CSS
    selectors: `item` (each event), `title`, `when` and `description`."""
    soup = BeautifulSoup(result.content, "lxml")
    items = soup.select(calendar.item)
    if not items:
        raise CalendarError(
            f"no events found on {result.url} (nothing matches {calendar.item!r}); "
            "the page may have changed"
        )
    events = []
    for item in items:
        heading = item.select_one(calendar.title) if calendar.title else None
        heading = heading or item.find(["h1", "h2", "h3", "h4"])
        if heading is None:
            continue
        title = clean_text(heading.get_text(" "))
        link = _link_of(heading)
        if calendar.when:
            when_text = " ".join(n.get_text(" ") for n in item.select(calendar.when))
        else:
            when_text = item.get_text(" ")
        when = parse_when(clean_text(when_text))
        if not title or when is None:
            continue
        url = _web_link(link["href"], result.url) if link else None
        description = None
        if calendar.description:
            node = item.select_one(calendar.description)
            description = tidy_description(node.get_text("\n")) if node else None
        events.append(
            CalendarEvent(
                uid=f"page:{url or title}:{when[0].isoformat()}"[:500],
                title=truncate(title, 300),
                starts_at=when[0],
                ends_at=when[1],
                all_day=when[2],
                description=description,
                url=url,
            )
        )
    return events


def read_listing(
    client: HttpClient, url: str, calendar, start: datetime, end: datetime
) -> list[CalendarEvent]:
    result = client.get(url)
    return [e for e in parse_listing(result, calendar) if _in_window(e, start, end)]
