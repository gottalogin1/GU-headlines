"""Events: the calendar readers on small copies of each site's pages, the
events check against a fake site, and the Events page."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest
from conftest import ROOT
from sqlalchemy import select

from guheadlines.config import ConfigError, load_events_config, parse_events_config
from guheadlines.events.ical import CalendarError, CalendarEvent, read_calendar, tidy_description
from guheadlines.events.pipeline import EventChecker
from guheadlines.events.readers import (
    _next_page,
    google_event,
    merge_agendas,
    parse_agendas,
    parse_listing,
    parse_notices,
    parse_when,
    read_google_calendar,
    tidy_name,
)
from guheadlines.scraper.http import FetchResult, HttpClient
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import get_settings


def page(url: str, html: str) -> FetchResult:
    return FetchResult(url=url, status=200, content=html.encode(), headers={})


def guam(*args) -> datetime:
    return datetime(*args, tzinfo=GUAM_TZ)


def calendar(**raw):
    (found,) = parse_events_config({"calendars": [{"slug": "test", "name": "Test", **raw}]})
    return found


WINDOW = {"start": guam(2026, 11, 30), "end": guam(2027, 2, 28)}

# -------------------------------------------------------------------- iCal

ICAL = """BEGIN:VCALENDAR\r
VERSION:2.0\r
X-WR-CALNAME:Village calendar\r
X-WR-TIMEZONE:Pacific/Guam\r
BEGIN:VEVENT\r
UID:weekly@example.gu\r
DTSTART;TZID=Pacific/Guam:20261201T090000\r
DTEND;TZID=Pacific/Guam:20261201T100000\r
RRULE:FREQ=WEEKLY;COUNT=4\r
EXDATE;TZID=Pacific/Guam:20261208T090000\r
SUMMARY:Mayors' Council meeting\r
LOCATION:Hagåtña\\, Guam\r
DESCRIPTION:<p>Open to the public.</p><p>Bring <b>ID</b>.</p>\r
URL:https://example.gu/meetings\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:weekly@example.gu\r
RECURRENCE-ID;TZID=Pacific/Guam:20261215T090000\r
DTSTART;TZID=Pacific/Guam:20261215T140000\r
DTEND;TZID=Pacific/Guam:20261215T150000\r
SUMMARY:Mayors' Council meeting (afternoon)\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:fiesta@example.gu\r
DTSTART;VALUE=DATE:20261205\r
SUMMARY:Santa Barbara fiesta\r
 \\, Dededo\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:private@example.gu\r
CLASS:PRIVATE\r
DTSTART:20261203T000000Z\r
SUMMARY:Staff lunch\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancelled@example.gu\r
STATUS:CANCELLED\r
DTSTART:20261204T000000Z\r
SUMMARY:Cancelled thing\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:old@example.gu\r
DTSTART:20250101T000000Z\r
SUMMARY:Long ago\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_ical_repeats_moves_and_skips():
    name, events = read_calendar(ICAL.encode(), **WINDOW)
    assert name == "Village calendar"
    by_title = {}
    for e in events:
        by_title.setdefault(e.title, []).append(e)
    meetings = by_title["Mayors' Council meeting"]
    # Weekly, four times, one date left out (EXDATE) and one moved.
    assert [e.starts_at for e in meetings] == [guam(2026, 12, 1, 9), guam(2026, 12, 22, 9)]
    assert meetings[0].ends_at == guam(2026, 12, 1, 10)
    assert meetings[0].location == "Hagåtña, Guam"
    assert meetings[0].description == "Open to the public.\nBring ID."
    assert meetings[0].url == "https://example.gu/meetings"
    assert meetings[0].uid == "weekly@example.gu/20261130T230000Z"
    (moved,) = by_title["Mayors' Council meeting (afternoon)"]
    assert moved.starts_at == guam(2026, 12, 15, 14)
    assert moved.uid == "weekly@example.gu/20261214T230000Z"
    # A folded line, and an all-day event without an end lasts its day.
    (fiesta,) = by_title["Santa Barbara fiesta, Dededo"]
    assert fiesta.all_day and fiesta.starts_at == guam(2026, 12, 5)
    assert fiesta.ends_at == guam(2026, 12, 6)
    assert not {"Staff lunch", "Cancelled thing", "Long ago"} & by_title.keys()


def test_ical_rejects_other_files():
    with pytest.raises(CalendarError, match="not an iCal"):
        read_calendar(b"<html>Sign in</html>", **WINDOW)


def test_tidy_description():
    assert tidy_description("Line one<br>Line <a href='x'>two</a>") == "Line one\nLine two"
    assert tidy_description("  plain\n\n text  ") == "plain\ntext"
    assert len(tidy_description("word " * 400)) <= 601
    assert tidy_description("") is None


# ---------------------------------------------------------------- dates


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "12/21/2026 - 08:00 AM - 12/23/2026 - 04:00 PM",
            (guam(2026, 12, 21, 8), guam(2026, 12, 23, 16), False),
        ),
        (
            "Thursday, November 19, 2026, at 2:00 p.m.",
            (guam(2026, 11, 19, 14), None, False),
        ),
        (
            "11/24/2026 10:00 AM - 11:00 AM",
            (guam(2026, 11, 24, 10), guam(2026, 11, 24, 11), False),
        ),
        ("Dec. 5, 2026 at noon", (guam(2026, 12, 5, 12), None, False)),
        ("2026-12-05", (guam(2026, 12, 5), guam(2026, 12, 6), True)),
        ("12/21/2026 - 12/23/2026", (guam(2026, 12, 21), guam(2026, 12, 24), True)),
        ("Time: 9 am", None),
        ("no date here", None),
    ],
)
def test_parse_when(text, expected):
    assert parse_when(text) == expected


# ------------------------------------------------------- google calendar


def test_google_event():
    event = google_event(
        {
            "id": "abc123",
            "status": "confirmed",
            "summary": "  Regular Session ",
            "htmlLink": "https://www.google.com/calendar/event?eid=abc",
            "location": "Guam Congress Building",
            "description": "Session agenda<br>Day one",
            "start": {"dateTime": "2026-12-01T10:00:00+10:00"},
            "end": {"dateTime": "2026-12-01T17:00:00+10:00"},
        }
    )
    assert event.uid == "g:abc123" and event.title == "Regular Session"
    assert (event.starts_at, event.ends_at) == (guam(2026, 12, 1, 10), guam(2026, 12, 1, 17))
    assert event.description == "Session agenda\nDay one"
    assert event.url.startswith("https://www.google.com/calendar/")
    holiday = google_event(
        {"id": "h", "summary": "Holiday", "start": {"date": "2026-12-08"}, "end": {}}
    )
    assert holiday.all_day and holiday.ends_at == guam(2026, 12, 9)
    assert google_event({"id": "x", "summary": "S", "status": "cancelled"}) is None
    assert google_event({"id": "x", "summary": "S", "visibility": "private"}) is None


def test_google_calendar_needs_a_key():
    with pytest.raises(CalendarError, match="GOOGLE_CALENDAR_API_KEY"):
        read_google_calendar(None, "cal@example.gu", "", **WINDOW)


def test_google_calendar_reads_every_page_and_keeps_the_key_out_of_the_address():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        token = request.url.params.get("pageToken")
        item = {
            "id": token or "first",
            "summary": f"Hearing {token or 'one'}",
            "start": {"dateTime": "2026-12-02T09:00:00+10:00"},
        }
        body = {"timeZone": "Pacific/Guam", "items": [item]}
        if not token:
            body["nextPageToken"] = "two"
        return httpx.Response(200, json=body)

    client = HttpClient(
        "test-agent", per_host_delay=0, retries=0, transport=httpx.MockTransport(handler)
    )
    try:
        events = read_google_calendar(client, "cal@example.gu", "secret-key", **WINDOW)
    finally:
        client.close()
    assert [e.title for e in events] == ["Hearing one", "Hearing two"]
    # No robots.txt request (an API with a key isn't crawled), key in a header only.
    assert [r.url.path for r in seen] == ["/calendar/v3/calendars/cal@example.gu/events"] * 2
    assert all(r.headers["X-Goog-Api-Key"] == "secret-key" for r in seen)
    assert all("secret-key" not in str(r.url) for r in seen)
    assert seen[0].url.params["singleEvents"] == "true"


# ----------------------------------------------------------------- agendas

AGENDAS_URL = "https://guamlegislature.gov/agendas-and-votes/"
AGENDAS = """
<div class="blockspare-accordion-item">
  <div class="blockspare-accordion-panel-handler"><span
    class="blockspare-accordion-panel-handler-label">DECEMBER 1-3, 2026 – Regular Session</span>
  </div>
  <div class="blockspare-accordion-panel">
    <a href="/wp-content/uploads/2026/11/Session Agenda Dec 2026.pdf">Session Agenda</a>
    <a href="/wp-content/uploads/2026/12/Daily Agenda 12-2-26.pdf">Daily Agenda December 2, 2026</a>
    <a href="/wp-content/uploads/2026/12/Votes.pdf">Voting record</a>
  </div>
</div>
<div class="blockspare-accordion-item">
  <span class="blockspare-accordion-panel-handler-label">JANUARY 4, 2027 – Inaugural Session</span>
  <a href="/wp-content/uploads/2027/01/Inaugural agenda.pdf">Agenda</a>
</div>
<div class="blockspare-accordion-item">
  <span class="blockspare-accordion-panel-handler-label">Older agendas</span>
  <a href="/agenda-archive/">Agenda archive</a>
</div>
"""


def test_parse_agendas():
    events = parse_agendas(page(AGENDAS_URL, AGENDAS))
    assert [(e.title, e.starts_at.date()) for e in events] == [
        ("Regular Session", date(2026, 12, 1)),
        ("Regular Session (continued)", date(2026, 12, 2)),
        ("Inaugural Session", date(2027, 1, 4)),
    ]
    first = events[0]
    assert first.all_day and first.url == AGENDAS_URL
    # Spaces in the PDF names are escaped.
    assert first.agenda_url == (
        "https://guamlegislature.gov/wp-content/uploads/2026/11/Session%20Agenda%20Dec%202026.pdf"
    )
    assert events[1].agenda_url.endswith("Daily%20Agenda%2012-2-26.pdf")


def test_agenda_joins_the_calendars_own_session():
    session = CalendarEvent(uid="g:1", title="Regular Session", starts_at=guam(2026, 12, 1, 10))
    hearing = CalendarEvent(uid="g:2", title="Public hearing", starts_at=guam(2026, 12, 2, 9))
    agendas = parse_agendas(page(AGENDAS_URL, AGENDAS))
    merged = merge_agendas([session, hearing, *agendas])
    assert session.agenda_url.endswith("Session%20Agenda%20Dec%202026.pdf")
    assert hearing.agenda_url is None
    # The session isn't listed twice; days without a calendar entry stay.
    assert [e.title for e in merged] == [
        "Regular Session",
        "Public hearing",
        "Regular Session (continued)",
        "Inaugural Session",
    ]


# ----------------------------------------------------------------- notices

NOTICES_URL = "https://notices.guam.gov/notices?type_id=5&view=list"


def notice(number: int, title: str, department: str, meeting: str) -> str:
    # The page's own layout (trimmed): each fact is a list item with a label.
    return f"""
    <article class="post hentry tiles text-center">
      <h2 class="post-title entry-title"><span>
        <a href="/notice_detail/{number}">{title}</a></span></h2>
      <p><strong>MEETING</strong></p>
      <ul class="ec-directory-option">
        <li class="item-with-icon"><strong>Posted on:</strong> 11/20/2026 08:37 AM</li>
        <li class="item-with-icon"><strong>Posted by:</strong> Lisa Ibanez</li>
        <li class="item-with-icon"><strong>Meeting Date:</strong> {meeting}</li>
        <li class="item-with-icon department-value"><strong>Department(s): </strong>
          <a href="/notices?department_id=7">{department}</a></li>
      </ul>
    </article>"""


NOTICES = (
    "<main>"
    + notice(
        902,
        "SECOND NOTICE OF REGULAR BOARD MEETING",
        "GUAM POWER AUTHORITY (GPA)",
        "12/03/2026 05:30 PM",
    )
    + notice(
        901, "Notice of Regular Board Meeting", "GUAM POWER AUTHORITY (GPA)", "12/03/2026 05:30 PM"
    )
    + notice(
        903,
        "Notice - Public Hearing on Bill 12",
        "DEPARTMENT OF PUBLIC WORKS",
        "Thursday, December 10, 2026, at 10:00 a.m.",
    )
    + notice(904, "Notice of meeting", "GUAM VISITORS BUREAU", "To be announced")
    + '</main><ul class="pagination"><li><a href="?type_id=5&view=list&page=2">»</a></li></ul>'
)


def test_parse_notices():
    events = parse_notices(page(NOTICES_URL, NOTICES))
    # The second notice of a meeting wins; a notice without a date is skipped.
    assert [(e.title, e.organizer, e.starts_at) for e in events] == [
        ("Regular Board Meeting", "Guam Power Authority (GPA)", guam(2026, 12, 3, 17, 30)),
        ("Public Hearing on Bill 12", "Department of Public Works", guam(2026, 12, 10, 10)),
    ]
    assert events[0].uid == "notice:902"
    assert events[0].url == "https://notices.guam.gov/notice_detail/902"
    assert _next_page(page(NOTICES_URL, NOTICES)) == (
        "https://notices.guam.gov/notices?type_id=5&view=list&page=2"
    )


def test_notices_page_that_changed_is_an_error():
    with pytest.raises(CalendarError, match="no notices found"):
        parse_notices(page(NOTICES_URL, "<main>Maintenance</main>"))


@pytest.mark.parametrize(
    "name, expected",
    [
        ("DEPARTMENT OF PUBLIC WORKS (DPW)", "Department of Public Works (DPW)"),
        ("GUAM BOARD OF ALLIED HEALTH EXAMINERS", "Guam Board of Allied Health Examiners"),
        ("Guam Housing and Urban Renewal Authority", "Guam Housing and Urban Renewal Authority"),
        ("A.B. WON PAT INTERNATIONAL AIRPORT", "A.B. Won Pat International Airport"),
        ("", None),
    ],
)
def test_tidy_name(name, expected):
    assert tidy_name(name) == expected


# ---------------------------------------------------------- listing pages

GCC_URL = "https://guamcc.edu/events/list"
GCC = """
<article class="post">
  <h2 class="post-title"><a href="/events/fall-graduation">Fall Commencement</a></h2>
  <ul class="ec-directory-option"><li>12/18/2026 - 06:00 PM</li><li>12/18/2026 - 08:00 PM</li></ul>
  <div class="post-body"><p>Join us for the ceremony.</p><p>Doors open at 5 PM.</p></div>
</article>
<article class="post">
  <h2 class="post-title"><a href="/events/break">Winter Break</a></h2>
  <ul class="ec-directory-option"><li>12/21/2026 - 12/31/2026</li></ul>
</article>
<article class="post"><h2 class="post-title">No date yet</h2></article>
"""
GCC_CALENDAR = dict(
    pages=GCC_URL,
    item="article.post",
    title="h2.post-title a",
    when="ul.ec-directory-option li",
    description=".post-body",
)


def test_parse_listing():
    events = parse_listing(page(GCC_URL, GCC), calendar(**GCC_CALENDAR))
    assert [e.title for e in events] == ["Fall Commencement", "Winter Break"]
    graduation, winter_break = events
    assert (graduation.starts_at, graduation.ends_at) == (
        guam(2026, 12, 18, 18),
        guam(2026, 12, 18, 20),
    )
    assert graduation.url == "https://guamcc.edu/events/fall-graduation"
    assert graduation.description == "Join us for the ceremony.\nDoors open at 5 PM."
    assert winter_break.all_day and winter_break.ends_at == guam(2027, 1, 1)


def test_listing_that_changed_is_an_error():
    with pytest.raises(CalendarError, match="nothing matches"):
        parse_listing(page(GCC_URL, "<main></main>"), calendar(**GCC_CALENDAR))


# ------------------------------------------------------------------ config


def test_events_yaml_is_valid():
    calendars = {c.slug: c for c in load_events_config(ROOT / "config")}
    assert {"legislature", "govguam-meetings", "gcc"} <= {
        s for s, c in calendars.items() if c.collected
    }
    legislature = calendars["legislature"]
    assert legislature.google_calendar and legislature.agendas
    assert legislature.excluded("Presentation of Legislative Certificate to the Chamorro Club")
    assert not legislature.excluded("Regular Session")
    assert calendars["govguam-meetings"].excluded(None, "Guam Legislature")
    assert all(c.cannot_scrape for s, c in calendars.items() if not c.collected)


def test_events_config_errors():
    with pytest.raises(ConfigError, match="needs at least one of"):
        parse_events_config({"calendars": [{"slug": "a", "name": "A"}]})
    with pytest.raises(ConfigError, match="needs an item selector"):
        parse_events_config({"calendars": [{"slug": "a", "name": "A", "pages": "https://x.gu"}]})
    with pytest.raises(ConfigError, match="duplicate"):
        parse_events_config(
            {
                "calendars": [
                    {"slug": "a", "name": "A", "cannot_scrape": "x"},
                    {"slug": "a", "name": "B", "cannot_scrape": "x"},
                ]
            }
        )
    assert parse_events_config({}) == []


# ----------------------------------------------------------- event checks

SITE = "https://events.example.gu/list"


class FakeSite:
    def __init__(self) -> None:
        self.events = {"Fiesta": "12/05/2026", "Cancelled: Bingo night": "12/06/2026"}
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if self.down:
            return httpx.Response(503)
        if url == SITE:
            items = "".join(
                f'<div class="ev"><h3><a href="/e/{n}">{n}</a></h3><p>{d} 6:00 PM</p></div>'
                for n, d in self.events.items()
            )
            return httpx.Response(200, html=f"<main>{items}</main>")
        return httpx.Response(404)


@pytest.fixture()
def fake_site():
    return FakeSite()


@pytest.fixture()
def checker(fake_site):
    client = HttpClient(
        "test-agent",
        per_host_delay=0,
        retries=0,
        transport=httpx.MockTransport(fake_site),
        sleep=lambda s: None,
    )
    calendars = parse_events_config(
        {
            "calendars": [
                {
                    "slug": "example",
                    "name": "Example events",
                    "pages": SITE,
                    "item": "div.ev",
                    "location": "Hagåtña",
                },
                {"slug": "blocked", "name": "Blocked", "cannot_scrape": "Behind a firewall."},
            ]
        }
    )
    yield EventChecker(get_settings(), calendars, client=client)
    client.close()


NOW = guam(2026, 12, 1, 8)


def test_event_check_adds_updates_and_removes(clean_db, fake_site, checker, monkeypatch):
    from guheadlines.db import session_scope
    from guheadlines.events import pipeline
    from guheadlines.models import Event, EventCalendar

    monkeypatch.setattr(pipeline, "_now", lambda: NOW.astimezone(timezone.utc))
    (result,) = checker.run()
    # Cancelled events are left out.
    assert (result.found, result.new, result.error) == (1, 1, None)

    fake_site.events = {"Fiesta": "12/05/2026", "Craft fair": "12/12/2026"}
    (result,) = checker.run()
    assert (result.found, result.new, result.removed) == (2, 1, 0)
    fake_site.events = {"Craft fair": "12/12/2026"}
    (result,) = checker.run()
    assert result.removed == 1
    with session_scope() as session:
        events = {e.title: e for e in session.scalars(select(Event))}
        assert events["Fiesta"].removed_at is not None
        assert events["Craft fair"].removed_at is None
        assert events["Craft fair"].location == "Hagåtña"
        assert events["Craft fair"].starts_at == guam(2026, 12, 12, 18)
        assert events["Craft fair"].url == "https://events.example.gu/e/Craft%20fair"
        calendars = {c.slug: c for c in session.scalars(select(EventCalendar))}
        assert calendars["example"].last_count == 1
        assert calendars["blocked"].last_run_at is None  # never read

    # A calendar that fails keeps its events and says why.
    fake_site.down = True
    (result,) = checker.run()
    assert "HTTP 503" in result.error
    with session_scope() as session:
        assert session.scalar(
            select(EventCalendar.last_error).where(EventCalendar.slug == "example")
        )
        assert session.scalar(select(Event.removed_at).where(Event.title == "Craft fair")) is None


def test_one_failed_input_keeps_the_others(clean_db, fake_site, checker):
    calendar = checker.calendars[0]
    calendar.notices = ["https://events.example.gu/notices"]  # 404
    events, errors = checker.collect(calendar, now=NOW)
    assert [e.title for e in events] == ["Fiesta"]
    assert errors and "notices" in errors[0]


# ----------------------------------------------------------------- the page


@pytest.fixture()
def web(clean_db):
    from fastapi.testclient import TestClient

    from guheadlines.db import session_scope
    from guheadlines.models import Event, EventCalendar
    from guheadlines.web.app import app, sidebar_cache

    sidebar_cache._data.clear()
    now = datetime.now(timezone.utc)
    today = datetime.now(GUAM_TZ).date()

    def at(days: int, hour: int) -> datetime:
        return datetime.combine(today + timedelta(days=days), time(hour), tzinfo=GUAM_TZ)

    with session_scope() as session:
        legislature = EventCalendar(
            slug="legislature",
            name="Guam Legislature",
            homepage="https://guamlegislature.gov/calendar/",
            last_run_at=now,
        )
        gcc = EventCalendar(
            slug="gcc",
            name="Guam Community College",
            last_run_at=now,
            last_error="no events found on https://guamcc.edu/events/list",
        )
        session.add_all([legislature, gcc])
        session.flush()
        common = {"first_seen_at": now, "last_seen_at": now}
        session.add_all(
            [
                Event(
                    calendar_id=legislature.id,
                    uid="g:1",
                    title="Regular Session",
                    starts_at=at(1, 10),
                    ends_at=at(1, 17),
                    location="Guam Congress Building, Hagåtña",
                    organizer="Guam Legislature",
                    description="Bills on the agenda <script>x</script>.",
                    url="https://www.google.com/calendar/event?eid=abc",
                    agenda_url="https://guamlegislature.gov/wp-content/uploads/agenda.pdf",
                    **common,
                ),
                Event(
                    calendar_id=gcc.id,
                    uid="page:1",
                    title="Winter Break",
                    starts_at=at(-2, 0),
                    ends_at=at(5, 0),
                    all_day=True,
                    organizer="Guam Community College",
                    **common,
                ),
                Event(
                    calendar_id=gcc.id,
                    uid="page:2",
                    title="Last Week's Fair",
                    starts_at=at(-7, 9),
                    ends_at=at(-7, 12),
                    **common,
                ),
                Event(
                    calendar_id=gcc.id,
                    uid="page:3",
                    title="Delisted Event",
                    starts_at=at(3, 9),
                    removed_at=now,
                    **common,
                ),
            ]
        )
    return TestClient(app)


def test_events_page(web):
    page = web.get("/events")
    assert page.status_code == 200
    html = page.text
    assert "Regular Session" in html and "Winter Break" in html
    assert "Last Week&#39;s Fair" not in html and "Delisted Event" not in html
    assert "Guam Congress Building, Hagåtña" in html
    assert "10:00 AM – 5:00 PM" in html
    assert 'href="https://guamlegislature.gov/wp-content/uploads/agenda.pdf"' in html
    assert "Agenda (PDF)" in html and "Details at" in html
    assert "<script>x</script>" not in html
    assert 'aria-current="page">Events<' in html
    # An event that began before today is listed under today, as ongoing.
    assert "Today ·" in html and "Ongoing" in html and "Tomorrow ·" in html
    assert html.index("Winter Break") < html.index("Regular Session")
    # Sidebar: calendars with counts and errors; the calendars that can't be collected.
    assert "no events found" in html
    assert "Guam Visitors Bureau events" in html and "Akamai" in html


def test_events_page_filters(web):
    html = web.get("/events?q=session").text
    assert "Regular Session" in html and "Winter Break" not in html
    assert '<meta name="robots" content="noindex, follow">' in html
    html = web.get("/events?calendar=gcc").text
    assert "Winter Break" in html and "Regular Session" not in html
    assert web.get("/events?page=5").status_code == 404


def test_events_page_is_in_the_sitemap(web):
    assert "/events</loc>" in web.get("/sitemap.xml").text
