"""Bids: the readers on small copies of each source's pages, the bids check
against a fake site, and the Bids page."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import ROOT, fixture_bytes
from sqlalchemy import select

from guheadlines.bids.pipeline import BidChecker
from guheadlines.bids.readers import (
    due_from_lines,
    due_from_text,
    find_number,
    is_closed,
    kind_of,
    number_key,
    parse_due,
    parse_notices,
    parse_table,
    read_notices,
    read_table,
    tidy_caps,
    tidy_title,
)
from guheadlines.config import ConfigError, load_bids_config, parse_bids_config
from guheadlines.scraper.http import FetchResult, HttpClient
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import get_settings

NOTICES = "https://notices.guam.gov/notices?view=list&topic_id%5B%5D=59&topic_id%5B%5D=62"
GPA = "https://go.opengovguam.com/tenders/tenders_list-standalone/gpa?tender_status=Available"
GHURA = "https://go.opengovguam.com/bids/bids_list_ghura/ghura?bid_status=Open"
GMHA = "https://www.gmha.org/procurement/"
GWA = "https://guamwaterworks.org/bids/"
PORT = "https://portofguam.com/bids-and-rfps"


def guam(*args) -> datetime:
    return datetime(*args, tzinfo=GUAM_TZ)


def page(url: str, fixture: str) -> FetchResult:
    return FetchResult(url=url, status=200, content=fixture_bytes(fixture), headers={})


def source(**raw):
    (found,) = parse_bids_config({"sources": [{"slug": "test", "name": "Test", **raw}]})
    return found


def fake_client(handler) -> HttpClient:
    return HttpClient(
        "test-agent",
        per_host_delay=0,
        retries=0,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )


# ------------------------------------------------------------- due dates


def test_due_dates_in_the_ways_agencies_write_them():
    assert parse_due("10:00 a.m., Tuesday, November 10, 2026") == (guam(2026, 11, 10, 10), True)
    assert parse_due("January 06, 2027 10:00 AM ChST (Guam time)") == (guam(2027, 1, 6, 10), True)
    assert parse_due("10:00 AM, 11/11/26") == (guam(2026, 11, 11, 10), True)
    assert parse_due("12/18/2026 10:00 AM") == (guam(2026, 12, 18, 10), True)
    # Only the day: due at its last minute, and said to have no time.
    assert parse_due("Friday, October 2, 2026") == (guam(2026, 10, 2, 23, 59), False)
    assert parse_due("no date here") is None


def test_due_date_after_its_label_not_the_meeting_before_it():
    body = (
        "Bid No: GCC-FB-27-002 Pre-Bid Conference 10:00 a.m., (CHST), Wednesday, November 4, "
        "2026 Date (Mandatory) Bid Submission: 10:00 a.m., (CHST) Wednesday, November 18, 2026 "
        "Bid Opening Date: 10:15 a.m., (CHST) Wednesday, November 18, 2026"
    )
    assert due_from_text(body) == (guam(2026, 11, 18, 10), True)
    assert due_from_text(
        "Date Issued: November 9, 2026 Submission Deadline: December 1, 2026, 10:00 AM "
        "Opening Date: December 1, 2026, 10:05 AM"
    ) == (guam(2026, 12, 1, 10), True)
    assert due_from_text(
        "Questions & Concerns (Q&C) Deadline: Monday, September 21, 2026, at 4:00pm "
        "Close Date: Friday, October 2, 2026, at 10:00 a.m."
    ) == (guam(2026, 10, 2, 10), True)
    # Packets on sale "through" a day is not a due date.
    assert due_from_text("available from Tuesday, November 3, 2026 through November 27") is None


def test_due_date_among_a_cells_lines():
    lines = [
        "Intent-to-Propose Due: 5:00 p.m. CHST, Friday, December 4, 2026",
        "RFI Due: 5:00 p.m. CHST, Friday, December 11, 2026",
        "Proposal Submission Deadline: 4:00 p.m. CHST, Friday, January 15, 2027",
    ]
    assert due_from_lines(lines) == (guam(2027, 1, 15, 16), True)
    assert due_from_lines(["January 06, 2027", "10:00 AM ChST"]) == (guam(2027, 1, 6, 10), True)
    assert due_from_lines(["CANCELLED"]) is None


# ------------------------------------------------- numbers, kinds and titles


def test_bid_numbers_and_the_key_two_listings_share():
    assert find_number("Invitation for Bid No., GIAA-004-FY27 Security Screening") == (
        "GIAA-004-FY27"
    )
    assert find_number("GSA-003-27 Cargo Van (Latest Model)") == "GSA-003-27"
    assert find_number("COVID-19 Supplies") is None
    assert number_key("ITB GPA-001-27") == number_key("GPA-001-27") == "GPA00127"
    # Numbers of digits only get the agency's initials, so two agencies' "001-2027" differ.
    gmha = "Guam Memorial Hospital Authority"
    assert number_key("RFP 001-2027", gmha) == number_key("RFP-001-2027", gmha) == "GMHA0012027"
    assert number_key("IFB 2026-27", "Guam Waterworks Authority") == "GWA202627"
    assert number_key("IFB 001-2027", "Port Authority of Guam") != number_key("RFP 001-2027", gmha)
    assert number_key(None) is None and number_key("IFB 12") is None


def test_kinds():
    assert kind_of("RFP: GCC-RFP-26-003 Emergency Medical services") == "Request for proposals"
    assert kind_of("Multi-Step GPA-078-26") == "Multi-step bid"
    assert kind_of("Emergency Procurement") == "Emergency procurement"
    assert kind_of("IFB") == "Invitation for bid"
    assert kind_of("SOI") == "Statement of qualifications"
    assert kind_of("Annual report") is None


@pytest.mark.parametrize(
    ("title", "number", "expected"),
    [
        ("GSA-003-27 Cargo Van (Latest Model)", "GSA-003-27", "Cargo Van (Latest Model)"),
        (
            "REQUEST FOR PROPOSAL No. OPA-RFP-26-014 Guam Department of Education Independent "
            "Financial Audit Services",
            "OPA-RFP-26-014",
            "Guam Department of Education Independent Financial Audit Services",
        ),
        (
            "Invitation for Bid: Security Screening Checkpoint Area Expansion",
            "GIAA-004-FY27",
            "Security Screening Checkpoint Area Expansion",
        ),
        (
            "2nd Notice of DRT RFP for CFE & Actuarial Review Services",
            "RFP-BIB-2027-001",
            "CFE & Actuarial Review Services",
        ),
        (
            "RFP-001-2027 For an Enterprise Electronic Health Record (EHR) System",
            "RFP-001-2027",
            "Enterprise Electronic Health Record (EHR) System",
        ),
        (
            "Bid Invitation for Facilities Maintenance Supplies",
            None,
            "Facilities Maintenance Supplies",
        ),
        ("CABLE RACK BID IDIQ", "IFB-PAG-010-26", "Cable Rack Bid IDIQ"),
    ],
)
def test_titles_without_the_notice_wording(title, number, expected):
    assert tidy_title(title, number) == expected


def test_titles_in_capitals_are_tidied():
    assert tidy_caps("DESIGN – BUILD RETROFIT OF WAREHOUSE “C” FOR EDUCATIONAL USE") == (
        "Design – Build Retrofit of Warehouse “C” for Educational Use"
    )
    assert tidy_caps("Already Fine") == "Already Fine"


# ------------------------------------------------------- notices.guam.gov


def test_notices_list():
    bids = {b.uid: b for b in parse_notices(page(NOTICES, "bids_notices.html"))}
    airport = bids["notice:10062"]
    assert airport.number == "GIAA-004-FY27"
    assert airport.title.startswith("Security Screening Checkpoint Area Expansion")
    assert airport.kind == "Invitation for bid"
    assert airport.agency == "A.B. Won Pat International Airport Authority"
    assert airport.posted_on == date(2026, 11, 18)
    # From the "Deadline:" box, as the text has no due date.
    assert (airport.due_at, airport.due_time) == (guam(2026, 12, 18, 10), True)
    assert airport.url == "https://notices.guam.gov/notice_detail/10062"
    gsa = bids["notice:9978"]
    assert (gsa.number, gsa.agency) == ("GSA-004-27", "General Services Agency")
    assert gsa.due_at == guam(2026, 12, 1, 10)
    # A notice titled only with its kind takes its title from the text.
    assert (
        bids["notice:10082"].title == "Design – Build Retrofit of Warehouse “C” for Educational Use"
    )
    # The kind comes from the title or the notice's topic, not "Emergency Medical".
    assert bids["notice:9727"].kind == "Request for proposals"
    # The share dialog's copy of the title is not read as the notice's text.
    assert bids["notice:10090"].description.startswith("Certified Financial Examiner")


def test_cancelled_notice_is_marked_closed():
    html = fixture_bytes("bids_notices.html").replace(
        b"Notice of DRT RFP for CFE", b"Cancellation of DRT RFP for CFE", 1
    )
    bids = parse_notices(FetchResult(NOTICES, 200, html, {}))
    assert is_closed(next(b for b in bids if b.uid == "notice:10090").status)


def test_reading_notices_follows_pages_and_keeps_the_newest_notice():
    old = (
        '<article class="post"><h2 class="post-title"><a href="/notice_detail/5">IFB GSA-099-26 '
        "Old Thing</a></h2><ul><li><strong>Posted on:</strong> 01/05/2026 08:00 AM</li></ul>"
        '<div class="contxt">Old Thing</div></article>'
    )
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if "page:2" in str(request.url):
            return httpx.Response(200, html=f"<main>{old}</main>")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=fixture_bytes("bids_notices.html"))

    client = fake_client(handler)
    bids, oldest = read_notices(client, NOTICES, date(2026, 11, 25))
    client.close()
    assert oldest == date(2026, 1, 5)
    assert any("page:2" in url for url in requested)
    uids = [b.uid for b in bids]
    # The tax office's RFP was posted four times: only its latest notice is kept.
    assert [u for u in uids if u in {"notice:10090", "notice:10087"}] == ["notice:10090"]
    # Two notices the same day: the later one (the corrected title) wins.
    assert "notice:9727" in uids and "notice:9726" not in uids
    assert "notice:5" in uids


# ------------------------------------------------------------------ tables


def test_opengov_lists():
    gpa = parse_table(page(GPA, "bids_opengov_gpa.html"), "Guam Power Authority")
    first = gpa[0]
    assert (first.number, first.kind) == ("ITB GPA-001-27", "Invitation for bid")
    assert first.title == "New Guam Power Authority T&D Administration Building"
    assert first.posted_on == date(2026, 11, 12)
    assert (first.due_at, first.due_time) == (guam(2027, 1, 6, 10), True)
    assert first.url == (
        "https://go.opengovguam.com/tenders/tender_details-standalone/gpa/ITB%20GPA-001-27"
    )
    assert gpa[1].kind == "Multi-step bid"
    ghura = parse_table(page(GHURA, "bids_opengov_ghura.html"), "GHURA")
    assert [is_closed(b.status) for b in ghura] == [False, False, True]
    assert ghura[0].document_url.endswith(".pdf")


def test_hospital_list():
    bids = parse_table(page(GMHA, "bids_gmha.html"), "Guam Memorial Hospital Authority")
    ehr = bids[0]
    assert (ehr.number, ehr.title) == (
        "RFP 001-2027",
        "Enterprise Electronic Health Record (EHR) System",
    )
    # The proposal deadline, not the earlier intent-to-propose or RFI dates.
    assert (ehr.due_at, ehr.due_time) == (guam(2027, 1, 15, 16), True)
    assert ehr.url.startswith("https://www.gmha.org/procurement/rfp-001-2027/")
    sole = bids[-1]
    assert sole.number == "SOLE SOURCE" and is_closed(sole.status)  # names the company


def test_waterworks_list_without_row_tags():
    bids = {b.number: b for b in parse_table(page(GWA, "bids_gwa.html"), "GWA")}
    assert list(bids) == [
        "IFB 2026-27",
        "IFB 2026-28",
        "IFB 2026-01",
        "IFB 2026-14",
        "IFB-2026-07",
        "IFB-2026-02",
    ]
    pumps = bids["IFB 2026-01"]
    assert pumps.title == "Water Booster Pumps, Impeller Kits, and Repair Seal Kits"
    assert (pumps.posted_on, pumps.due_at) == (date(2026, 9, 21), guam(2026, 12, 15, 13))
    assert pumps.document_url.endswith(".pdf") and pumps.url == GWA
    # "CANCELLED" in place of the due date, and rows with an extra "CLOSED" cell.
    assert bids["IFB 2026-14"].status == "CANCELLED"
    assert bids["IFB-2026-07"].status == "CLOSED"
    assert bids["IFB-2026-07"].title == "12 Yard Mounted Combination Sewer Truck"


def test_port_list_and_its_bid_pages():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/bids-and-rfps":
            return httpx.Response(200, content=fixture_bytes("bids_port.html"))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=fixture_bytes("bids_port_detail.html"))

    client = fake_client(handler)
    bids = {b.number: b for b in read_table(client, PORT, "Port Authority of Guam", details=True)}
    # Only the open bid's page is read (closed and awarded ones aren't).
    assert requested.count("/real-estate-appraisal-and-consulting-services") == 1
    assert "/cable-rack-bid-idiq" not in requested
    appraisal = bids["RFP-PAG-026-001"]
    assert (appraisal.due_at, appraisal.due_time) == (guam(2026, 10, 2, 10), True)
    assert appraisal.document_url.endswith("ad_-_rfp-026-001.pdf")
    assert is_closed(bids["IFB-PAG-006-26"].status) and is_closed(bids["IFB-PAG-001-27"].status)
    # A due date found before is kept, and the page isn't read again.
    requested.clear()
    known = {"table:RFP-PAG-026-001": (guam(2026, 12, 4, 10), True)}
    bids = read_table(client, PORT, "Port Authority of Guam", details=True, known=known)
    client.close()
    assert requested == ["/bids-and-rfps"]
    assert next(b for b in bids if b.number == "RFP-PAG-026-001").due_at == guam(2026, 12, 4, 10)


# ------------------------------------------------------------------ config


def test_bids_config_is_valid():
    sources = load_bids_config(ROOT / "config")
    slugs = [s.slug for s in sources]
    assert slugs == ["notices", "gpa", "ghura", "gmha", "gwa", "port"]
    assert all(s.collected and s.addresses for s in sources)
    notices = sources[0]
    # The Power Authority's standing notice and hearing notices are left out.
    assert notices.excluded("Continuous Formal Tenders")
    assert notices.excluded("Notice of Public Hearing")
    assert notices.excluded_everywhere("Guam Chamber of Commerce luncheon")
    assert all(s.agency for s in sources[1:])
    assert next(s for s in sources if s.slug == "port").details


def test_bids_config_errors():
    with pytest.raises(ConfigError, match="needs notices or table"):
        source()
    with pytest.raises(ConfigError, match="undated_days"):
        source(table=GWA, undated_days=0)
    with pytest.raises(ConfigError, match="invalid regex"):
        source(table=GWA, exclude=["("])
    assert source(cannot_scrape=True).cannot_scrape  # no address needed
    assert source(table=GWA).undated_days == 30


# ------------------------------------------------------------- the check


class FakeSite:
    """A notices list and a table of bids that the tests change."""

    NOTICES = "https://notices.example.gu/list"
    TABLE = "https://agency.example.gu/bids"

    def __init__(self):
        self.down = False
        self.notices = [
            # (id, title, posted, body)
            (
                2,
                "IFB GSA-010-27 Office Chairs",
                "11/20/2026",
                "Submission Deadline: 12/20/2026 10:00 AM",
            ),
            (1, "RFP GSA-011-27 Audit Services", "11/10/2026", "Proposals due in the RFP packet."),
        ]
        self.rows = [
            # (number, title, issued, due, status)
            ("IFB 2026-30", "Water Meters", "11/01/26", "10:00 AM, 12/20/26", ""),
            ("IFB 2026-31", "Sole Source: Pump Parts", "11/02/26", "", ""),
            ("IFB 2026-20", "Old Pipes", "08/01/26", "10:00 AM, 09/01/26", ""),
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if self.down:
            return httpx.Response(503)
        url = str(request.url)
        if url == self.NOTICES:
            articles = "".join(
                f'<article class="post"><h2 class="post-title"><a href="/notice_detail/{n}">'
                f"{title}</a></h2><ul><li><strong>Posted on:</strong> {posted} 08:00 AM</li>"
                f'<li><strong>Department(s):</strong> <a href="/notices?department_id=1">'
                f'GENERAL SERVICES AGENCY</a></li></ul><div class="contxt">{body}</div></article>'
                for n, title, posted, body in self.notices
            )
            return httpx.Response(200, html=f"<main>{articles}</main>")
        if url == self.TABLE:
            rows = "".join(
                f"<tr><td>{n}</td><td>{t}</td><td>{i}</td><td>{d}</td><td>{s}</td></tr>"
                for n, t, i, d, s in self.rows
            )
            return httpx.Response(
                200,
                html=(
                    "<table><thead><tr><th>Number</th><th>Description</th><th>Issuance Date</th>"
                    f"<th>Due Date</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>"
                ),
            )
        return httpx.Response(404)


@pytest.fixture()
def fake_site():
    return FakeSite()


@pytest.fixture()
def checker(fake_site):
    client = fake_client(fake_site)
    sources = parse_bids_config(
        {
            "exclude": ["chamber of commerce"],
            "sources": [
                {"slug": "notices", "name": "Notices", "notices": FakeSite.NOTICES},
                {
                    "slug": "agency",
                    "name": "Waterworks",
                    "table": FakeSite.TABLE,
                    "agency": "Guam Waterworks Authority",
                },
                {"slug": "blocked", "name": "Blocked", "cannot_scrape": "Behind a firewall."},
            ],
        }
    )
    yield BidChecker(get_settings(), sources, client=client)
    client.close()


NOW = guam(2026, 11, 25, 8)


def test_collect_keeps_open_bids_only(fake_site, checker):
    bids, errors, covered_since = checker.collect(checker.sources[1], NOW)
    assert errors == [] and covered_since is None
    # Sole-source and past-due rows are left out.
    assert [(b.number, b.agency) for b in bids] == [("IFB 2026-30", "Guam Waterworks Authority")]
    bids, _, covered_since = checker.collect(checker.sources[0], NOW)
    assert covered_since == date(2026, 11, 10)
    # Soonest due first; the one whose due date is only in its packet comes last.
    assert [b.number for b in bids] == ["GSA-010-27", "GSA-011-27"]
    fake_site.notices.append(
        (3, "Guam Chamber of Commerce mixer", "11/21/2026", "Bid due 12/01/2026")
    )
    bids, _, _ = checker.collect(checker.sources[0], NOW)
    assert len(bids) == 2
    # Without a due date, a bid stays for undated_days (30) after it was posted.
    later = NOW + timedelta(days=20)
    bids, _, _ = checker.collect(checker.sources[0], later)
    assert [b.number for b in bids] == ["GSA-010-27"]


def test_bids_check_adds_updates_and_removes(clean_db, fake_site, checker, monkeypatch):
    from guheadlines.bids import pipeline
    from guheadlines.db import session_scope
    from guheadlines.models import Bid, BidSource

    monkeypatch.setattr(pipeline, "_now", lambda: NOW.astimezone(timezone.utc))
    notices, agency = checker.run()
    assert (notices.found, notices.new, notices.error) == (2, 2, None)
    assert (agency.found, agency.new) == (1, 1)

    # A moved due date is updated; a bid now listed as closed leaves.
    fake_site.rows[0] = ("IFB 2026-30", "Water Meters", "11/01/26", "10:00 AM, 12/22/26", "")
    fake_site.notices[0] = (2, "Cancellation of IFB GSA-010-27 Office Chairs", "11/20/2026", "")
    notices, agency = checker.run()
    assert (notices.found, notices.removed, agency.found, agency.removed) == (1, 1, 1, 0)
    with session_scope() as session:
        bids = {b.number: b for b in session.scalars(select(Bid))}
        assert bids["GSA-010-27"].removed_at is not None
        assert bids["IFB 2026-30"].due_at == guam(2026, 12, 22, 10)
        assert bids["IFB 2026-30"].removed_at is None
        assert bids["GSA-011-27"].kind == "Request for proposals"
        sources = {s.slug: s for s in session.scalars(select(BidSource))}
        assert sources["agency"].last_count == 1
        assert sources["blocked"].last_run_at is None and not sources["blocked"].enabled

    # A source that fails keeps its bids and says why.
    fake_site.down = True
    notices, agency = checker.run()
    assert "HTTP 503" in agency.error
    with session_scope() as session:
        assert session.scalar(select(Bid.removed_at).where(Bid.number == "IFB 2026-30")) is None
        assert session.scalar(select(BidSource.last_error).where(BidSource.slug == "agency"))


def test_older_notices_than_were_read_stay(clean_db, fake_site, checker, monkeypatch):
    """A notice still to come that has moved past the pages read isn't removed."""
    from guheadlines.bids import pipeline
    from guheadlines.db import session_scope
    from guheadlines.models import Bid

    monkeypatch.setattr(pipeline, "_now", lambda: NOW.astimezone(timezone.utc))
    checker.run(only=["notices"])
    fake_site.notices = [
        (4, "IFB GSA-012-27 Laptops", "11/24/2026", "Submission Deadline: 12/15/2026")
    ]
    (result,) = checker.run(only=["notices"])
    with session_scope() as session:
        removed = {b.number: b.removed_at is not None for b in session.scalars(select(Bid))}
    # GSA-010-27 is due Dec 20 and was posted before the newest list began: kept.
    # GSA-011-27 has no due date, so it's taken as gone.
    assert removed == {"GSA-010-27": False, "GSA-011-27": True, "GSA-012-27": False}
    assert result.removed == 1


# ----------------------------------------------------------------- the page


@pytest.fixture()
def web(clean_db):
    from fastapi.testclient import TestClient

    from guheadlines.db import session_scope
    from guheadlines.models import Bid, BidSource
    from guheadlines.web.app import app, sidebar_cache

    sidebar_cache._data.clear()
    now = datetime.now(timezone.utc)
    today = datetime.now(GUAM_TZ).date()
    soon = now + timedelta(days=1)
    later = now + timedelta(days=20)
    with session_scope() as session:
        notices = BidSource(
            slug="notices",
            name="GovGuam public notices",
            homepage="https://notices.guam.gov/",
            last_run_at=now,
        )
        gpa = BidSource(
            slug="gpa",
            name="Guam Power Authority",
            homepage=GPA,
            last_run_at=now,
            last_error="no table of bids found on the page",
        )
        gone = BidSource(slug="gone", name="Removed source", enabled=False)
        session.add_all([notices, gpa, gone])
        session.flush()
        common = {"first_seen_at": now, "last_seen_at": now, "posted_on": today}
        session.add_all(
            [
                # Posted on the portal and on GPA's own list: shown once, the
                # GPA listing (it has the time), crediting the portal too.
                Bid(
                    source_id=notices.id,
                    uid="notice:1",
                    title="T&D Administration Building",
                    number="GPA-001-27",
                    kind="Invitation for bid",
                    agency="Guam Power Authority",
                    url="https://notices.guam.gov/notice_detail/1",
                    document_url="https://notices.guam.gov/files/ad.pdf",
                    due_at=later.replace(hour=0),
                    due_time=False,
                    **common,
                ),
                Bid(
                    source_id=gpa.id,
                    uid="table:ITB GPA-001-27",
                    title="New Guam Power Authority T&D Administration Building",
                    number="ITB GPA-001-27",
                    kind="Invitation for bid",
                    agency="Guam Power Authority",
                    url="https://go.opengovguam.com/tenders/x",
                    due_at=later,
                    due_time=True,
                    **common,
                ),
                Bid(
                    source_id=notices.id,
                    uid="notice:2",
                    title="Cargo Van (Latest Model)",
                    number="GSA-003-27",
                    kind="Invitation for bid",
                    agency="General Services Agency",
                    description="Invitation for Bid for a cargo van for the Guam Public Library.",
                    url="https://notices.guam.gov/notice_detail/2",
                    due_at=soon,
                    due_time=True,
                    **common,
                ),
                Bid(
                    source_id=notices.id,
                    uid="notice:3",
                    title="Financial Audit Services",
                    number="OPA-RFP-26-014",
                    kind="Request for proposals",
                    agency="Office of Public Accountability",
                    url="https://notices.guam.gov/notice_detail/3",
                    **common,
                ),
                # Past due, removed, or from a removed source: not shown.
                Bid(
                    source_id=notices.id,
                    uid="notice:4",
                    title="Past Due Thing",
                    url="https://notices.guam.gov/notice_detail/4",
                    due_at=now - timedelta(hours=1),
                    **common,
                ),
                Bid(
                    source_id=notices.id,
                    uid="notice:5",
                    title="Withdrawn Thing",
                    url="https://notices.guam.gov/notice_detail/5",
                    removed_at=now,
                    **common,
                ),
                Bid(
                    source_id=gone.id,
                    uid="table:x",
                    title="Orphan Thing",
                    url="https://example.gu/x",
                    **common,
                ),
            ]
        )
    with TestClient(app) as client:
        yield client


def test_bids_page(web):
    page = web.get("/bids")
    assert page.status_code == 200
    html = page.text
    listing = html.split('<h2 class="result-count">', 1)[1].split('<aside class="sidebar">')[0]
    assert "3 open bids" in listing
    # Soonest due first, those without a due date last.
    van = listing.index("Cargo Van")
    building = listing.index("T&amp;D Administration Building")
    audit = listing.index("Financial Audit Services")
    assert van < building < audit
    # One card for the bid posted twice, with the agency's own listing and both credited.
    assert listing.count("T&amp;D Administration Building") == 1
    assert "New Guam Power Authority T&amp;D Administration Building" in listing
    assert 'also on <a href="https://notices.guam.gov/notice_detail/1"' in listing
    assert (
        'href="https://notices.guam.gov/files/ad.pdf"' in listing
    )  # its document, from the portal
    assert "ChST" in listing and "(tomorrow)" in listing and "due-soon" in listing
    assert "See the notice" in listing  # the audit has no due date here
    for gone in ("Past Due Thing", "Withdrawn Thing", "Orphan Thing", "Removed source"):
        assert gone not in html
    # The source list, with a source's error.
    sidebar = html.split('<aside class="sidebar">', 1)[1]
    assert 'href="/bids?source=gpa"' in sidebar and "no table of bids found" in sidebar
    assert 'href="/bids" aria-current="page"' in html
    assert "<title>Bids and RFPs" in html
    assert "chamber" not in html.lower() and "github" not in html.lower()
    assert '<meta name="robots"' not in html


def test_bids_page_filters(web):
    html = web.get("/bids?q=van").text
    assert "Cargo Van" in html and "Financial Audit" not in html
    assert '<meta name="robots" content="noindex, follow">' in html
    html = web.get("/bids?source=notices").text
    # A source's own list: the portal's copy of the GPA bid, with nothing to merge.
    assert "Cargo Van" in html and "New Guam Power Authority" not in html
    assert "T&amp;D Administration Building" in html and "also on" not in html
    html = web.get("/bids?agency=Office+of+Public+Accountability").text
    assert "Financial Audit Services" in html and "Cargo Van" not in html
    assert '<option value="General Services Agency" >General Services Agency (1)</option>' in html
    assert web.get("/bids?source=nope").status_code == 200


def test_bids_page_is_in_the_sitemap(web):
    assert "/bids</loc>" in web.get("/sitemap.xml").text
