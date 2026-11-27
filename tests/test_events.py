"""Events: the calendar readers on small copies of each site's pages, the
events check against a fake site, and the Events page."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest
from conftest import ROOT, fixture_bytes
from sqlalchemy import select

from guheadlines.config import ConfigError, load_events_config, parse_events_config
from guheadlines.events.ical import (
    CalendarError,
    CalendarEvent,
    read_calendar,
    tidy_description,
    write_calendar,
)
from guheadlines.events.pipeline import EventChecker
from guheadlines.events.readers import (
    _next_page,
    _notice_title,
    google_event,
    merge_agendas,
    parse_agendas,
    parse_listing,
    parse_moderncampus,
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


DECOLONIZATION = (
    "Commission on Decolonization - Regular Monthly Board Meeting - December 1, 2026 @ 3:00 p.m."
)


@pytest.mark.parametrize(
    "title, expected",
    [
        ("SECOND NOTICE OF PUC REGULAR BUSINESS MEETING", "PUC Regular Business Meeting"),
        (
            "CCU Regular Meeting | November 24, 2026, 5:30 PM | Second Notice & Amended Agenda",
            "CCU Regular Meeting",
        ),
        (
            "Second Notice of the November 25, 2026 Judicial Council Regular Meeting",
            "Judicial Council Regular Meeting",
        ),
        (
            "SECOND NOTICE- Cannabis Control Board (CCB) Meeting- Wednesday, November 25, 2026",
            "Cannabis Control Board (CCB) Meeting",
        ),
        (
            DECOLONIZATION,
            "Commission on Decolonization – Regular Monthly Board Meeting",
        ),
        (
            "Notice of Meeting of the Guam Criminal Law and Procedure Review Commission.",
            "Meeting of the Guam Criminal Law and Procedure Review Commission",
        ),
        (
            "Huntan Manggé'helo'/Kumisión i Fino CHamoru Board Meeting",
            "Huntan Manggé'helo'/Kumisión i Fino CHamoru Board Meeting",
        ),
    ],
)
def test_notice_titles(title, expected):
    assert _notice_title(title) == expected


def test_notice_without_a_meeting_date_takes_the_date_in_its_title():
    html = notice(
        905,
        DECOLONIZATION,
        "COMMISSION ON DECOLONIZATION",
        "",
    ).replace("<strong>Meeting Date:</strong> </li>", "</li>")
    (event,) = parse_notices(page(NOTICES_URL, f"<main>{html}</main>"))
    assert event.starts_at == guam(2026, 12, 1, 15)
    assert event.organizer == "Commission on Decolonization"


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


# ------------------------------------------------------- Modern Campus RSS

UOG_FEED = "https://api.calendar.moderncampus.net/pubcalendar/abc/rss?category=def"


def uog_feed(content: bytes | None = None) -> FetchResult:
    content = content if content is not None else fixture_bytes("uog_calendar.rss")
    return FetchResult(url=UOG_FEED, status=200, content=content, headers={})


def test_moderncampus_feed():
    events = {e.title: e for e in parse_moderncampus(uog_feed())}
    assert len(events) == 8  # the two dates of the workshop are one title here
    # A date is an all-day event; its end date is its last day.
    holiday = events["Thanksgiving Holiday (Observed)"]
    assert holiday.all_day
    assert (holiday.starts_at, holiday.ends_at) == (guam(2026, 11, 26), guam(2026, 11, 27))
    break_ = events["Thanksgiving Break (No Classes)"]
    assert break_.ends_at == guam(2026, 11, 29)
    # A time is in UTC: 04:00Z is 2 PM in Guam.
    chess = events["Thanksgiving Chess Tournament"]
    assert not chess.all_day
    assert (chess.starts_at, chess.ends_at) == (guam(2026, 11, 25, 14), guam(2026, 11, 25, 18))
    assert chess.location == "Dr. Lucio C. Tan Student Success Center, room 237"
    assert chess.organizer == "College of Natural and Applied Sciences"
    assert chess.uid == "mc:8ce97288-489d-40c3-9dd2-e0a7875fe4e4"
    assert chess.url == (
        "https://www.uog.edu/calendar/#event-details/8ce97288-489d-40c3-9dd2-e0a7875fe4e4"
    )
    assert chess.description.startswith("Come join the UOG Chess Club")
    assert "<br" not in chess.description and "&nbsp;" not in chess.description
    assert events["Commencement"].location is None


def test_moderncampus_repeating_event_dates_are_separate():
    workshops = [e for e in parse_moderncampus(uog_feed()) if e.title == "Resume Writing Workshops"]
    assert [e.starts_at for e in workshops] == [guam(2026, 12, 7, 13), guam(2026, 12, 14, 13)]
    assert len({e.uid for e in workshops}) == 2
    assert workshops[0].location == "Dr. Lucio C. Tan Student Success Center, Room 231"


def test_moderncampus_cancelled_and_broken():
    feed = fixture_bytes("uog_calendar.rss").replace(
        b"<cal:status>CONFIRMED</cal:status>", b"<cal:status>CANCELLED</cal:status>", 1
    )
    assert "Thinking of Place IV" not in {e.title for e in parse_moderncampus(uog_feed(feed))}
    with pytest.raises(CalendarError, match="not an RSS feed"):
        parse_moderncampus(uog_feed(b"<html><body>Maintenance</body></html>"))
    with pytest.raises(CalendarError, match="not an RSS feed"):
        parse_moderncampus(uog_feed(b""))


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
    uog = calendars["uog"]
    assert uog.collected and uog.moderncampus[0].startswith("https://api.calendar.moderncampus")
    assert uog.moderncampus[0] in uog.addresses
    # Colours on the month calendar, and which events are main events.
    assert (uog.color, legislature.color, calendars["gcc"].color) == ("green", "red", "blue")
    assert calendars["govguam-meetings"].color is None
    assert not calendars["govguam-meetings"].is_main("CCU Regular Meeting")
    assert uog.is_main("Commencement") and legislature.is_main("Regular Session")
    assert not uog.is_main("Thesis Defense by Ami Vice, M.S. Biology Program")
    assert not uog.is_main("Evaluations for Part B Classes")
    # No Chamber of Commerce events from any calendar.
    for c in calendars.values():
        assert c.excluded_everywhere(None, None, "Guam Chamber of Commerce office")
        assert c.excluded_everywhere("Business mixer", None, None, "Hosted by the Guam Chamber.")
        assert not c.excluded_everywhere("Regular Session", "Guam Legislature")
    assert "chamber" not in {s for s in calendars}


def test_main_events_and_colors():
    assert calendar(ical="https://x.gu/a.ics").is_main("Anything")
    assert not calendar(ical="https://x.gu/a.ics", main_events="none").is_main("Anything")
    picky = calendar(ical="https://x.gu/a.ics", main_events=["session", "public hearing"])
    assert picky.is_main("Regular Session") and picky.is_main("PUBLIC HEARING on Bill 12")
    assert not picky.is_main("Committee meeting")
    most = calendar(ical="https://x.gu/a.ics", main_events={"except": ["^evaluations"]})
    assert most.is_main("Final Exams") and not most.is_main("Evaluations for Part B")
    with pytest.raises(ConfigError, match="only have an except list"):
        calendar(ical="https://x.gu/a.ics", main_events={"only": ["x"]})
    assert calendar(ical="https://x.gu/a.ics", color="Green").color == "green"
    with pytest.raises(ConfigError, match="color must be one of"):
        calendar(ical="https://x.gu/a.ics", color="purple")
    with pytest.raises(ConfigError, match="main_events must be"):
        calendar(ical="https://x.gu/a.ics", main_events=[])


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
    from guheadlines.web.app import app, sidebar_cache, sitemap_cache

    sidebar_cache._data.clear()
    sitemap_cache._data.clear()
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
    # Sidebar: calendars with counts and errors. Calendars that can't be
    # collected are not mentioned, and neither is the Chamber of Commerce.
    assert "no events found" in html
    assert "collected" not in html and "Akamai" not in html
    assert "chamber" not in html.lower()


def test_events_page_month_calendar(web):
    today = datetime.now(GUAM_TZ).date()
    html = web.get("/events").text
    grid = html[html.index('<section class="panel cal"') : html.index('<div id="events-list">')]
    assert f"{today:%B} {today.year}</h2>" in grid
    assert 'aria-current="date"' in grid
    # Each calendar's colour, and a legend for it.
    assert "cal-dot cal-blue" in grid  # Guam Community College
    assert "Guam Legislature</span>" in grid and "Guam Community College</span>" in grid
    # Upcoming days link to everything on that day; nothing before today.
    assert f'href="/events?day={today.isoformat()}#events-list"' in grid
    assert "Last Week&#39;s Fair" not in grid
    # The list below shows its calendar's colour too.
    listing = html[html.index('<div id="events-list">') :]
    assert "cal-dot cal-red" in listing


def test_events_page_picked_day_and_month(web):
    today = datetime.now(GUAM_TZ).date()
    tomorrow = today + timedelta(days=1)
    page = web.get(f"/events?day={tomorrow.isoformat()}")
    assert page.status_code == 200
    html = page.text
    listing = html[html.index('<div id="events-list">') :]
    # Tomorrow: the session, and the break that is still on.
    assert "Regular Session" in listing and "Winter Break" in listing
    assert f"2 events on {tomorrow:%a}, {tomorrow:%b} {tomorrow.day}" in listing
    assert "All upcoming events" in listing and "cal-selected" in html
    assert '<meta name="robots" content="noindex, follow">' in html
    in_a_week = today + timedelta(days=6)
    listing = web.get(f"/events?day={in_a_week.isoformat()}").text.split('id="events-list"')[1]
    assert "Regular Session" not in listing and "Winter Break" not in listing
    assert "Nothing listed for this day" in listing
    # Another month: noindex, and out-of-range or broken values fall back.
    later = (today.replace(day=1) + timedelta(days=40)).replace(day=1)
    html = web.get(f"/events?month={later:%Y-%m}").text
    assert f"{later:%B} {later.year}</h2>" in html
    assert '<meta name="robots" content="noindex, follow">' in html
    for value in ("1999-01", "2999-12", "2026-1x", "2026-13"):
        assert web.get(f"/events?month={value}").status_code == 200
    assert web.get("/events?day=not-a-day").status_code == 200


def test_events_page_filters(web):
    html = web.get("/events?q=session").text
    assert "Regular Session" in html and "Winter Break" not in html
    assert '<meta name="robots" content="noindex, follow">' in html
    html = web.get("/events?calendar=gcc").text
    assert "Winter Break" in html and "Regular Session" not in html
    assert web.get("/events?page=5").status_code == 404


def test_events_page_is_in_the_sitemap(web):
    sitemap = web.get("/sitemap.xml").text
    assert "/events</loc>" in sitemap
    # Each upcoming event's own page too, but not the ones that are over or delisted.
    listed = {title: f"/events/{n}</loc>" in sitemap for title, n in _event_ids().items()}
    assert listed == {
        "Regular Session": True,
        "Winter Break": True,
        "Last Week's Fair": False,
        "Delisted Event": False,
    }


def _event_ids() -> dict[str, int]:
    from guheadlines.db import session_scope
    from guheadlines.models import Event

    with session_scope() as session:
        return {e.title: e.id for e in session.scalars(select(Event))}


def _structured(html: str) -> list[dict]:
    import json

    blocks = html.split('<script type="application/ld+json">')[1:]
    return [json.loads(block.split("</script>", 1)[0]) for block in blocks]


def test_event_titles_lead_to_each_events_own_page(web):
    session_id = _event_ids()["Regular Session"]
    html = web.get("/events").text
    assert f'<a href="/events/{session_id}">Regular Session</a>' in html
    # The calendar's own page is still one click away.
    assert 'href="https://www.google.com/calendar/event?eid=abc"' in html


def test_event_page(web):
    session_id = _event_ids()["Regular Session"]
    page = web.get(f"/events/{session_id}")
    assert page.status_code == 200
    html = page.text
    tomorrow = datetime.now(GUAM_TZ).date() + timedelta(days=1)
    assert "<h1>Regular Session</h1>" in html
    assert f"<title>Regular Session, {tomorrow:%a}, {tomorrow:%b} {tomorrow.day} · " in html
    assert "10:00 AM – 5:00 PM" in html and "Guam Congress Building, Hagåtña" in html
    assert "Agenda (PDF)" in html and "<script>x</script>" not in html
    assert 'aria-current="page">Events<' in html
    url = f"http://testserver/events/{session_id}"
    assert f'<link rel="canonical" href="{url}">' in html
    # What Google reads to list the event in its event results.
    (event,) = [d for d in _structured(html) if d["@type"] == "Event"]
    assert event["name"] == "Regular Session" and event["url"] == url
    assert event["startDate"] == f"{tomorrow.isoformat()}T10:00:00+10:00"
    assert event["endDate"] == f"{tomorrow.isoformat()}T17:00:00+10:00"
    assert event["location"]["name"] == "Guam Congress Building, Hagåtña"
    assert event["location"]["address"]["addressCountry"] == "GU"
    assert event["organizer"] == {
        "@type": "Organization",
        "name": "Guam Legislature",
        "url": "https://guamlegislature.gov/calendar/",
    }
    (crumbs,) = [d for d in _structured(html) if d["@type"] == "BreadcrumbList"]
    assert [i["name"] for i in crumbs["itemListElement"]] == ["Home", "Events", "Regular Session"]
    # Add to a calendar app: Google Calendar, or a file for the others.
    day = f"{tomorrow:%Y%m%d}"
    assert (
        "https://calendar.google.com/calendar/render?action=TEMPLATE&amp;text=Regular+Session"
        in html
    )
    assert f"dates={day}T000000Z%2F{day}T070000Z" in html
    assert f'href="/events/{session_id}.ics"' in html


def test_event_page_without_a_place_or_that_is_over(web):
    ids = _event_ids()
    html = web.get(f"/events/{ids['Winter Break']}").text
    # No place given: Google lists events at a known place only.
    assert "Not given by the calendar" in html
    assert [d["@type"] for d in _structured(html)] == ["BreadcrumbList"]
    # An all-day event's Google Calendar link has dates only, the end being the day after.
    today = datetime.now(GUAM_TZ).date()
    assert f"dates={today - timedelta(days=2):%Y%m%d}%2F{today + timedelta(days=5):%Y%m%d}" in html
    for title in ("Last Week's Fair", "Delisted Event"):
        assert web.get(f"/events/{ids[title]}").status_code == 404
        assert web.get(f"/events/{ids[title]}.ics").status_code == 404
    for path in ("/events/0", "/events/abc", "/events/99999999999999999999", "/events/1x"):
        assert web.get(path).status_code == 404, path


def test_event_file(web):
    response = web.get(f"/events/{_event_ids()['Regular Session']}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-robots-tag"] == "noindex"
    text = response.text
    assert "SUMMARY:Regular Session" in text and text.count("BEGIN:VEVENT") == 1
    # A file to add once, not a calendar of its own.
    assert "X-WR-CALNAME" not in text and "REFRESH-INTERVAL" not in text


def test_events_feed(web):
    from guheadlines.db import session_scope
    from guheadlines.models import Event, EventCalendar

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        meetings = EventCalendar(slug="govguam-meetings", name="GovGuam public meetings")
        session.add(meetings)
        session.flush()
        session.add(
            Event(
                calendar_id=meetings.id,
                uid="n:1",
                title="Board Meeting",
                starts_at=now + timedelta(days=2),
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    response = web.get("/events.ics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert response.headers["x-robots-tag"] == "noindex"
    text = response.text
    assert "X-WR-CALNAME:Guam events · Guam Headlines" in text
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT3H" in text
    # The main events, as on the month calendar (GovGuam meetings are not one),
    # and the past month's, so subscribed calendars keep them.
    assert "SUMMARY:Regular Session" in text and "SUMMARY:Winter Break" in text
    assert "SUMMARY:Last Week's Fair" in text
    assert "Board Meeting" not in text and "Delisted Event" not in text
    assert "github" not in text.lower() and "chamber" not in text.lower()
    # One calendar: every event on it.
    text = web.get("/events.ics?calendar=govguam-meetings").text
    assert "SUMMARY:Board Meeting" in text and "Regular Session" not in text
    assert "X-WR-CALNAME:GovGuam public meetings · Guam Headlines" in text
    assert web.get("/events.ics?calendar=nope").status_code == 404


def test_events_page_offers_the_feed(web):
    html = web.get("/events").text
    assert 'href="webcal://testserver/events.ics"' in html
    assert (
        'href="https://calendar.google.com/calendar/render?cid=webcal%3A%2F%2Ftestserver%2Fevents.ics"'
        in html
    )
    assert "http://testserver/events.ics</span>" in html
    html = web.get("/events?calendar=gcc").text
    assert 'href="webcal://testserver/events.ics?calendar=gcc"' in html
    assert "Get every Guam Community College event in your calendar" in html
    # A calendar that isn't in events.yaml: the feed of every calendar.
    assert 'href="webcal://testserver/events.ics"' in web.get("/events?calendar=nope").text


def _stored_event(**fields):
    from guheadlines.models import Event

    now = datetime.now(timezone.utc)
    return Event(**{"id": 7, "uid": "x", "last_seen_at": now, **fields})


def test_write_calendar_reads_back():
    events = [
        _stored_event(
            id=1,
            title="Budget hearing; FY2027, part 2",
            starts_at=guam(2026, 12, 1, 9),
            ends_at=guam(2026, 12, 1, 12, 30),
            location="Guam Congress Building, Hagåtña",
            organizer="Committee on Appropriations",
            description="Line one\nLine two with a long text " + "ä" * 60,
            url="https://example.gu/hearing?a=1,2",
            agenda_url="javascript:alert(1)",
        ),
        _stored_event(
            id=2,
            title="Liberation Day",
            starts_at=guam(2026, 12, 21),
            ends_at=guam(2026, 12, 23),
            all_day=True,
        ),
        _stored_event(id=3, title="Open house", starts_at=guam(2026, 12, 5, 10)),
    ]
    items = [{"event": e, "calendar_name": "Guam Legislature"} for e in events]
    text = write_calendar(
        items,
        product="GU Headlines",
        host="news.example",
        name="Guam events · GU Headlines",
        refresh=timedelta(hours=3),
    )
    lines = text.split("\r\n")
    assert lines[-1] == "" and all(len(line.encode()) <= 75 for line in lines)
    assert "UID:1@news.example" in lines and "DTSTART;VALUE=DATE:20261221" in lines
    assert "DTSTART:20261130T230000Z" in lines
    assert "javascript" not in text
    name, found = read_calendar(text, start=guam(2026, 11, 1), end=guam(2027, 1, 1))
    assert name == "Guam events · GU Headlines"
    hearing, open_house, holiday = found
    assert hearing.title == "Budget hearing; FY2027, part 2"
    assert hearing.starts_at == guam(2026, 12, 1, 9) and hearing.ends_at == guam(
        2026, 12, 1, 12, 30
    )
    assert hearing.location == "Guam Congress Building, Hagåtña"
    assert hearing.url == "https://example.gu/hearing?a=1,2"
    assert hearing.description.startswith("Committee on Appropriations\nLine one\nLine two")
    assert "Details: https://example.gu/hearing?a=1,2\nCalendar: Guam Legislature" in (
        hearing.description
    )
    assert holiday.all_day and holiday.starts_at == guam(2026, 12, 21)
    assert holiday.ends_at == guam(2026, 12, 23)
    # No end given: the event has none.
    assert open_house.ends_at is None


def test_events_matching_the_file_wide_exclude_are_left_out():
    (calendar_,) = parse_events_config(
        {
            "exclude": ["chamber of commerce"],
            "calendars": [{"slug": "a", "name": "A", "ical": "https://x.gu/a.ics"}],
        }
    )
    checker = EventChecker(get_settings(), [calendar_], client=object())
    found = [
        CalendarEvent(uid="1", title="Regular Session", starts_at=guam(2026, 12, 1, 10)),
        CalendarEvent(uid="2", title="Chamber of Commerce mixer", starts_at=guam(2026, 12, 2)),
        CalendarEvent(
            uid="3", title="Luncheon", starts_at=guam(2026, 12, 3), organizer="Chamber of Commerce"
        ),
        CalendarEvent(
            uid="4",
            title="Talk",
            starts_at=guam(2026, 12, 4),
            description="With the chamber of commerce.",
        ),
    ]
    assert [e.title for e in checker._tidy(calendar_, found)] == ["Regular Session"]


# ------------------------------------------------------- the month calendar


def stored(title: str, start: datetime, end: datetime | None = None, all_day=False, slug="a"):
    from guheadlines.models import Event

    event = Event(title=title, starts_at=start, ends_at=end, all_day=all_day)
    return {"event": event, "calendar_slug": slug, "calendar_name": slug.upper()}


def test_event_days():
    from guheadlines.web.event_calendar import event_days

    first, last = date(2026, 11, 29), date(2027, 1, 2)
    days = lambda item: event_days(item["event"], first, last)  # noqa: E731
    # An all-day event ends at the next midnight; a late event stays on its day.
    assert days(stored("Break", guam(2026, 12, 24), guam(2026, 12, 26), True)) == [
        date(2026, 12, 24),
        date(2026, 12, 25),
    ]
    assert days(stored("Gala", guam(2026, 12, 5, 18), guam(2026, 12, 6, 0))) == [date(2026, 12, 5)]
    assert days(stored("Overnight", guam(2026, 12, 5, 22), guam(2026, 12, 6, 2))) == [
        date(2026, 12, 5),
        date(2026, 12, 6),
    ]
    # Longer than a week: its first day only (and not at all if that is earlier).
    assert days(stored("Exhibit", guam(2026, 12, 3), guam(2027, 1, 20), True)) == [
        date(2026, 12, 3)
    ]
    assert days(stored("Semester", guam(2026, 8, 18), guam(2026, 12, 19), True)) == []
    assert days(stored("Fair", guam(2026, 11, 27), guam(2026, 12, 2), True)) == [
        date(2026, 11, 29),
        date(2026, 11, 30),
        date(2026, 12, 1),
    ]


def test_build_month():
    from guheadlines.web.event_calendar import build_month

    calendars = parse_events_config(
        {
            "calendars": [
                {"slug": "a", "name": "A", "ical": "https://x.gu/a.ics", "color": "green"},
                {"slug": "b", "name": "B", "ical": "https://x.gu/b.ics", "main_events": "none"},
            ]
        }
    )
    today, last_day = date(2026, 12, 10), date(2027, 3, 10)
    items = [
        stored("Before today", guam(2026, 12, 3, 9)),
        stored("Meeting", guam(2026, 12, 15, 9), slug="b"),
        stored("Morning talk", guam(2026, 12, 15, 9)),
        stored("Evening show", guam(2026, 12, 15, 19)),
        stored("Holiday", guam(2026, 12, 15), guam(2026, 12, 16), True),
        stored("Lunch", guam(2026, 12, 15, 12)),
    ]
    month = build_month(date(2026, 12, 1), today, last_day, items, calendars)
    days = {d.day: d for week in month.weeks for d in week if d.day}
    # This month starts with this week (Sunday, Dec 6 to Saturday, Dec 12).
    assert [d.day.day for d in month.weeks[0]] == [6, 7, 8, 9, 10, 11, 12]
    assert date(2026, 12, 3) not in days and not days[date(2026, 12, 9)].upcoming
    january = build_month(date(2027, 1, 1), today, last_day, items, calendars)
    assert january.weeks[0][0].day is None  # Sunday, Dec 27
    assert [d.day.day for d in january.weeks[0] if d.day] == [1, 2]
    assert days[today].today and days[today].upcoming
    fifteenth = days[date(2026, 12, 15)]
    # All-day first, then by time; at most 3, and calendar b's are not main events.
    assert [e["event"].title for e in fifteenth.events] == ["Holiday", "Morning talk", "Lunch"]
    assert fifteenth.more == 1 and fifteenth.events[0]["color"] == "green"
    everything = build_month(date(2026, 12, 1), today, last_day, items, calendars, everything=True)
    assert [d for w in everything.weeks for d in w if d.day == date(2026, 12, 15)][0].more == 2
    # Stepping: from this month to the month of the last collected day.
    assert (month.previous, month.next) == (None, date(2027, 1, 1))
    march = build_month(date(2027, 3, 1), today, last_day, [], calendars)
    assert (march.previous, march.next) == (date(2027, 2, 1), None)
    assert [d.upcoming for w in march.weeks for d in w if d.day][9:11] == [True, False]


def test_plan_a_trip_box_on_the_weather_and_events_pages(web, monkeypatch):
    import dataclasses

    from guheadlines.web import app as site

    def configure(**links):
        changed = dataclasses.replace(site.settings, **links)
        monkeypatch.setattr(site, "settings", changed)
        monkeypatch.setitem(site.templates.env.globals, "settings", changed)

    event = f"/events/{_event_ids()['Regular Session']}"
    for path in ("/weather", "/events", event, "/status"):
        assert "panel travel" not in web.get(path).text, path
    assert "Affiliate links." not in web.get("/status").text

    configure(
        affiliate_hotels_link="https://hotels.example.com/guam?aid=12345",
        affiliate_tours_link="https://tours.example.com/guam?partner=abc",
    )
    for path in ("/weather", "/events", event):
        page = web.get(path)
        sidebar = page.text.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
        box = sidebar.split('<section class="panel travel" aria-labelledby="travel-title">', 1)[1]
        box = box.split("</section>", 1)[0]
        link = r'<a href="([^"]+)" target="_blank" rel="sponsored noopener">([^<]+)</a>'
        links = re.findall(link, box)
        assert links == [
            ("https://hotels.example.com/guam?aid=12345", "Hotels ↗"),
            ("https://tours.example.com/guam?partner=abc", "Tours and activities ↗"),
        ], path
        assert "Affiliate links: if you book through one" in " ".join(box.split())
    # Only on those pages, and the disclaimer says what the links are.
    for path in ("/", "/jobs", "/bids"):
        assert "panel travel" not in web.get(path).text, path
    assert "Affiliate links." in web.get("/status").text
