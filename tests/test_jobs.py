"""Job boards: the parsers on small copies of each board's pages, the job
check against a fake board, and the Jobs page."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import ROOT
from sqlalchemy import select

from guheadlines.config import ConfigError, JobSourceConfig, load_jobs_config, parse_jobs_config
from guheadlines.jobs.parsers import (
    PARSERS,
    BoardError,
    closing,
    find_date,
    find_pay,
    tidy_pay,
)
from guheadlines.jobs.pipeline import JobScraper
from guheadlines.scraper.http import FetchResult, HttpClient
from guheadlines.settings import get_settings


def page(url: str, html: str) -> FetchResult:
    return FetchResult(url=url, status=200, content=html.encode(), headers={})


def board(**kw) -> JobSourceConfig:
    return JobSourceConfig(
        **{"slug": "test", "name": "Test", "parser": "pdf_links", "pages": [], **kw}
    )


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Closes 10/30/2026", date(2026, 10, 30)),
        ("10/30/26", date(2026, 10, 30)),
        ("Open: October 7, 2026", date(2026, 10, 7)),
        ("Date posted: October 07, 2026", date(2026, 10, 7)),
        ("2026-08-26T00:00:00", date(2026, 8, 26)),
        ("7 Oct 2026", date(2026, 10, 7)),
        ("no date here", None),
        ("13/45/2026", None),
    ],
)
def test_find_date(text, expected):
    assert find_date(text) == expected


def test_closing_and_pay():
    assert closing("Open Until Filled") == (None, "Open until filled")
    assert closing("10/30/2026 or until filled") == (date(2026, 10, 30), "Open until filled")
    assert find_pay("Pay: $18.34 per hour Job description") == "$18.34 per hour"
    assert find_pay("Salary $45,000 - $60,000 per year") == "$45,000 - $60,000 per year"
    assert find_pay("Pay depends on experience") is None
    assert tidy_pay("2400.00 month") == "$2,400.00 per month"
    assert tidy_pay("COMMENSURATE WITH EXPERIENCE hour") == "Commensurate with experience"
    assert tidy_pay("$15.00 per hour") == "$15.00 per hour"
    assert tidy_pay("DOE") == "DOE"
    assert tidy_pay("") is None


# ------------------------------------------------------------------ parsers

HAFAJOBS = """<table id="example"><thead><tr><th>Title</th></tr></thead><tbody>
<tr><td><a href="https://apply.hafajobs.com/guam/GCC/job/job_detail/GJF-1790834718">Librarian</a>
  <a href="/uploads/jobs/application_1_JA030-26%20Librarian.pdf">Job Announcement</a></td>
  <td><p><strong>Guam Community College</strong></p></td>
  <td>10/01/2026</td><td>Open Until Filled</td><td>OPEN</td></tr>
<tr><td><a href="https://apply.hafajobs.com/guam/GCC/job/job_detail/GJF-1783987571">
  On-Call Substitute</a></td>
  <td><p><strong>Guam Community College</strong></p></td>
  <td>07/14/2026</td><td>10/30/2026</td><td>OPEN</td></tr>
<tr><td><a href="https://apply.hafajobs.com/guam/GCC/job/job_detail/GJF-1">Old job</a></td>
  <td></td><td>01/01/2026</td><td>02/01/2026</td><td>CLOSED</td></tr>
</tbody></table>"""


def test_hafajobs_listing_and_detail():
    parser = PARSERS["hafajobs"]
    listing = parser.listing(
        page("https://apply.hafajobs.com/guam/GCC/jobs/open", HAFAJOBS),
        board(location="Guam", employer="GCC"),
    )
    assert [p.title for p in listing.postings] == ["Librarian", "On-Call Substitute"]
    first, second = listing.postings
    assert first.url.endswith("/job_detail/GJF-1790834718")
    assert first.employer == "Guam Community College"
    assert first.open_date == date(2026, 10, 1)
    assert (first.close_date, first.close_note) == (None, "Open until filled")
    assert first.pdf_url == (
        "https://apply.hafajobs.com/uploads/jobs/application_1_JA030-26%20Librarian.pdf"
    )
    assert second.close_date == date(2026, 10, 30) and second.pdf_url is None

    detail = """<div class="item"><div class="name-coloumn">Salary</div>
      <div class="name-entity">$40,000 - $52,000</div></div>
      <div class="item"><div class="name-coloumn">Job Description</div>
      <div class="name-entity">JA030-26</div></div>"""
    parser.detail(page(first.url, detail), first)
    assert first.pay == "$40,000 - $52,000"
    assert first.description is None  # an announcement number is not a description


def test_pdf_links_skip_forms_and_take_section_employer():
    html = """<main><h2>Core Tech International Job Announcements</h2>
      <a href="/files/Welder.pdf">Welder</a>
      <a href="/files/app.pdf">Employment Application</a>
      <h2>Openings</h2><a href="https://x.ucarecd.net/abc/">Certified Teachers</a>
      <a href="/files/notes.docx">Notes</a></main>"""
    listing = PARSERS["pdf_links"].listing(
        page("https://gdoe.net/hr.html", html), board(employer="GDOE", location="Guam")
    )
    got = {p.title: (p.url, p.employer) for p in listing.postings}
    assert got == {
        "Welder": ("https://gdoe.net/files/Welder.pdf", "Core Tech International"),
        "Certified Teachers": ("https://x.ucarecd.net/abc/", "GDOE"),
    }
    assert all(p.pdf_url == p.url for p in listing.postings)


def test_federalgovernmentjobs_listing_and_detail():
    html = """<table><tr><th>Title</th></tr>
      <tr><td><a href="/jobs/Criminal-Investigator-882071300.html">Criminal Investigator</a></td>
      <td>Guam, GU</td><td>Public</td><td>U.S. Coast Guard</td>
      <td>08/26/2026</td><td>02/26/2027</td></tr>
      </table><a href="?page=2">Next</a>"""
    parser = PARSERS["federalgovernmentjobs"]
    listing = parser.listing(
        page("https://federalgovernmentjobs.us/job-location/guam-gu.html", html), board()
    )
    (job,) = listing.postings
    assert job.url == "https://federalgovernmentjobs.us/jobs/Criminal-Investigator-882071300.html"
    assert (job.employer, job.location) == ("U.S. Coast Guard", "Guam, GU")
    assert (job.open_date, job.close_date) == (date(2026, 8, 26), date(2027, 2, 26))
    assert listing.next_pages == [
        "https://federalgovernmentjobs.us/job-location/guam-gu.html?page=2"
    ]
    detail = """<table><tr><td class="job-label">Salary</td>
      <td>$106,437.00 to $138,370.00</td></tr></table>
      <h2>Summary</h2><p>This vacancy is for a GS-1811-13 Criminal Investigator.</p>
      <h2>Duties</h2><p>x</p>"""
    parser.detail(page(job.url, detail), job)
    assert job.pay == "$106,437.00 to $138,370.00"
    assert job.description == "This vacancy is for a GS-1811-13 Criminal Investigator."


def test_guamjobsonline_cards():
    html = """<div class="job-card">
      <h3><a href="job_view.php?jobid=10315&keyword=&x=1">Featured Warehouse Worker</a></h3>
      <span><i class="fa fa-calendar"></i> 10/12/2026</span>
      <span><i class="fa fa-briefcase"></i> Confidential</span>
      <span><i class="fa fa-location-arrow"></i> Tamuning</span>
      <span><i class="fa fa-usd"></i> 2400.00 month</span>
      <p>We are looking for a warehouse worker.</p></div>"""
    (job,) = (
        PARSERS["guamjobsonline"]
        .listing(page("https://guamjobsonline.com/open_search.php", html), board(location="Guam"))
        .postings
    )
    assert job.url == "https://guamjobsonline.com/job_view.php?jobid=10315"
    assert job.title == "Warehouse Worker"
    assert (job.employer, job.location, job.pay) == (
        "Confidential",
        "Tamuning",
        "$2,400.00 per month",
    )
    assert job.open_date == date(2026, 10, 12)
    assert job.description == "We are looking for a warehouse worker."


def test_sections_page():
    html = """<main><h4>Carpenter (Contract, Full-Time)</h4>
      <p>Date posted: October 07, 2026</p><p>Pay: $18.34 per hour</p>
      <p>Job description: Skilled carpenters for a project at Camp Blaz.</p>
      <p>SUBMIT APPLICATION</p>
      <h4>Pre-Delivery Operations</h4><p>Pay depends on experience.</p></main>"""
    first, second = (
        PARSERS["sections"]
        .listing(page("https://www.guamtemps.com/jobs", html), board(employer="GuamTemps"))
        .postings
    )
    assert first.url == "https://www.guamtemps.com/jobs#carpenter-contract-full-time"
    assert first.open_date == date(2026, 10, 7)
    assert first.pay == "$18.34 per hour"
    assert first.description == "Skilled carpenters for a project at Camp Blaz."
    assert (second.pay, second.open_date) == (None, None)


PEOPLEADMIN = """<div class="job-item-posting">
  <h3><a href="/postings/5573">Accountant II</a></h3>
  <div class="job-title-text-wrap">Accountant II</div>
  <div class="job-title-text-wrap">Business Office</div>
  <div class="job-title-text-wrap">005-27</div>
  <div class="job-title-text-wrap">10/01/2026</div>
  <div class="job-title-text-wrap">10/31/2026</div>
  <div class="job-description">Keeps the books.</div></div>
  <a rel="next" href="/postings/search?page=2">Next</a>"""


def test_university_of_guam_joins_pdfs_to_portal_postings():
    parser = PARSERS["uog"]
    uog = board(parser="uog", employer="University of Guam")
    portal = parser.listing(page("https://uog.peopleadmin.com/postings/search", PEOPLEADMIN), uog)
    hr_page = parser.listing(
        page(
            "https://www.uog.edu/hr/jobs",
            """<main><a href="/files/005-27-Accountant-II.pdf">005-27 Accountant II</a>
            <a href="/files/Dean.pdf">Dean of Students</a></main>""",
        ),
        uog,
    )
    assert portal.next_pages == ["https://uog.peopleadmin.com/postings/search?page=2"]
    postings = parser.finish(portal.postings + hr_page.postings)
    assert [p.title for p in postings] == ["Accountant II", "Dean of Students"]
    accountant = postings[0]
    assert accountant.employer == "University of Guam, Business Office"
    assert (accountant.open_date, accountant.close_date) == (date(2026, 10, 1), date(2026, 10, 31))
    assert accountant.pdf_url == "https://www.uog.edu/files/005-27-Accountant-II.pdf"

    parser.detail(
        page(accountant.url, "<table><tr><th>Salary Range</th><td>$50,000</td></tr></table>"),
        accountant,
    )
    assert accountant.pay == "$50,000"


def test_usajobs_needs_a_key():
    with pytest.raises(BoardError, match="USAJOBS_API_KEY"):
        PARSERS["usajobs"].collect(None, board(parser="usajobs"), get_settings())


def test_usajobs_posting():
    job = PARSERS["usajobs"]._posting(
        {
            "PositionURI": "https://www.usajobs.gov:443/job/1",
            "PositionTitle": "Nurse",
            "OrganizationName": "Veterans Health Administration",
            "PositionLocation": [{"LocationName": "Hagatna, Guam"}, {"LocationName": "Hilo"}],
            "PositionRemuneration": [
                {"MinimumRange": "80000", "MaximumRange": "104000", "RateIntervalCode": "PA"}
            ],
            "PublicationStartDate": "2026-10-01T00:00:00.0000",
            "ApplicationCloseDate": "2026-10-20T23:59:59.9970",
            "UserArea": {"Details": {"JobSummary": "Care for veterans."}},
        },
        board(location="Guam"),
    )
    assert job.pay == "$80,000.00 to $104,000.00 per year"
    assert job.location == "Hagatna, Guam"
    assert (job.open_date, job.close_date) == (date(2026, 10, 1), date(2026, 10, 20))
    assert job.description == "Care for veterans."


def test_govguam_rows():
    parser = PARSERS["govguam"]
    govguam = board(parser="govguam", employer="Government of Guam", location="Guam")
    job = parser._posting(
        {
            "ANNOUNCEMENT_ID": 812,
            "PositionTitle": "Clerk II",
            "DEPARTMENT_NAME": "Department of Revenue and Taxation",
            "DATE_OPENED": "2026-10-01T00:00:00",
            "DATE_CLOSED": None,
            "MIN_SALARY": 30000,
            "MAX_SALARY": 42000.5,
        },
        govguam,
    )
    assert job.url == "https://jobs.guam.gov/public/jobs#announcement-812"
    assert job.employer == "Department of Revenue and Taxation"
    assert job.open_date == date(2026, 10, 1)
    assert (job.close_date, job.close_note) == (None, "Open until filled")
    assert job.pay == "$30,000.00 to $42,000.50"
    assert parser._posting({"something": 1}, govguam) is None

    detail = {"result": {"DUTIES": "<p>Files <b>records</b>.</p>", "DATE_CLOSED": "2026-11-01"}}
    parser.detail(page(job.detail_url, json.dumps(detail)), job)
    assert job.description == "Files records ."
    assert job.close_date == date(2026, 11, 1)


# ------------------------------------------------------------------- config


def test_jobs_yaml_is_valid():
    boards = load_jobs_config(ROOT / "config")
    assert {b.slug for b in boards} >= {"govguam", "gdoe", "uog", "gcc", "guamtemps"}
    assert all(b.pages for b in boards if b.collected)
    assert {b.slug for b in boards if not b.collected} == {"govguam", "american-job-center"}


def test_jobs_config_errors():
    with pytest.raises(ConfigError, match="unknown parser"):
        parse_jobs_config({"sources": [{"slug": "a", "name": "A", "parser": "x"}]}, {"pdf_links"})
    with pytest.raises(ConfigError, match="needs at least one page"):
        parse_jobs_config({"sources": [{"slug": "a", "name": "A", "parser": "pdf_links"}]})
    with pytest.raises(ConfigError, match="duplicate"):
        parse_jobs_config(
            {
                "sources": [
                    {"slug": "a", "name": "A", "parser": "p", "cannot_scrape": "x"},
                    {"slug": "a", "name": "B", "parser": "p", "cannot_scrape": "x"},
                ]
            }
        )


# ------------------------------------------------------------- job checks

LISTING = "https://jobs.example.gu/openings"


class FakeBoard:
    def __init__(self) -> None:
        self.links = {"Welder": "/files/welder.pdf", "Cook": "/files/cook.pdf"}
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if self.down:
            return httpx.Response(503)
        if url == LISTING:
            links = "".join(f'<a href="{href}">{t}</a>' for t, href in self.links.items())
            return httpx.Response(200, html=f"<main>{links}</main>")
        return httpx.Response(404)


@pytest.fixture()
def fake_board():
    return FakeBoard()


@pytest.fixture()
def job_scraper(fake_board):
    client = HttpClient(
        "test-agent",
        per_host_delay=0,
        retries=0,
        transport=httpx.MockTransport(fake_board),
        sleep=lambda s: None,
    )
    boards = [
        board(slug="example", name="Example board", pages=[LISTING], location="Guam"),
        board(slug="hireguam", name="HireGuam", cannot_scrape="Behind a firewall."),
    ]
    yield JobScraper(get_settings(), boards, client=client)
    client.close()


def test_job_check_adds_updates_and_removes(clean_db, fake_board, job_scraper):
    from guheadlines.db import session_scope
    from guheadlines.models import Job, JobSource

    (result,) = job_scraper.run()
    assert (result.found, result.new, result.error) == (2, 2, None)

    fake_board.links = {"Welder": "/files/welder.pdf", "Nurse": "/files/nurse.pdf"}
    (result,) = job_scraper.run()
    assert (result.found, result.new, result.removed) == (2, 1, 1)
    with session_scope() as session:
        jobs = {j.title: j for j in session.scalars(select(Job))}
        assert jobs["Cook"].removed_at is not None
        assert jobs["Welder"].removed_at is None
        assert jobs["Welder"].last_seen_at > jobs["Welder"].first_seen_at
        assert jobs["Welder"].pdf_url == "https://jobs.example.gu/files/welder.pdf"
        assert jobs["Welder"].pay is None and jobs["Welder"].open_date is None
        boards = {s.slug: s for s in session.scalars(select(JobSource))}
        assert boards["example"].last_count == 2
        assert boards["hireguam"].last_run_at is None  # never read

    # A board that fails keeps its postings and says why.
    fake_board.down = True
    (result,) = job_scraper.run()
    assert "HTTP 503" in result.error
    with session_scope() as session:
        assert session.scalar(select(JobSource.last_error).where(JobSource.slug == "example"))
        assert {j.title for j in session.scalars(select(Job).where(Job.removed_at.is_(None)))} == {
            "Welder",
            "Nurse",
        }


def test_board_without_postings_is_an_error_not_ok(clean_db, fake_board, job_scraper):
    fake_board.links = {}
    (result,) = job_scraper.run()
    assert result.error and "no job postings found" in result.error


def test_javascript_board_is_reported():
    shell = page(
        LISTING, '<html><body><div id="root"></div><script src="/main.js"></script></body></html>'
    )

    class Client:
        def get(self, url, **kw):
            return shell

        def set_host_delay(self, *a):
            pass

    scraper = JobScraper(get_settings(), [], client=Client())
    with pytest.raises(BoardError, match="JavaScript"):
        scraper.collect(board(pages=[LISTING]))


# ----------------------------------------------------------------- the page


@pytest.fixture()
def web(clean_db):
    from fastapi.testclient import TestClient

    from guheadlines.db import session_scope
    from guheadlines.models import Job, JobSource
    from guheadlines.web.app import app, sidebar_cache

    sidebar_cache._data.clear()
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        gcc = JobSource(
            slug="gcc",
            name="Guam Community College",
            homepage="https://apply.hafajobs.com/guam/GCC/jobs",
            last_run_at=now,
        )
        temps = JobSource(
            slug="guamtemps",
            name="GuamTemps",
            last_run_at=now,
            last_error="no job postings found on https://www.guamtemps.com/jobs",
        )
        session.add_all([gcc, temps])
        session.flush()
        session.add_all(
            [
                Job(
                    source_id=gcc.id,
                    url="https://apply.hafajobs.com/guam/GCC/job/job_detail/GJF-1",
                    title="Licensed Practical Nurse I",
                    employer="Guam Community College",
                    pay="$35,000 - $45,000",
                    location="Mangilao",
                    description="Care for students <script>x</script>.",
                    open_date=date(2026, 8, 19),
                    close_note="Open until filled",
                    pdf_url="https://apply.hafajobs.com/uploads/jobs/nurse.pdf",
                    first_seen_at=now - timedelta(days=3),
                    last_seen_at=now,
                ),
                Job(
                    source_id=temps.id,
                    url="https://www.guamtemps.com/jobs#carpenter",
                    title="Carpenter",
                    first_seen_at=now,
                    last_seen_at=now,
                ),
                Job(
                    source_id=gcc.id,
                    url="https://apply.hafajobs.com/guam/GCC/job/job_detail/GJF-2",
                    title="Closed Job",
                    close_date=date(2020, 1, 1),
                    first_seen_at=now,
                    last_seen_at=now,
                ),
                Job(
                    source_id=gcc.id,
                    url="https://apply.hafajobs.com/guam/GCC/job/job_detail/GJF-3",
                    title="Delisted Job",
                    first_seen_at=now,
                    last_seen_at=now,
                    removed_at=now,
                ),
            ]
        )
    return TestClient(app)


def test_jobs_page(web):
    page = web.get("/jobs")
    assert page.status_code == 200
    html = page.text
    assert "Licensed Practical Nurse I" in html and "Carpenter" in html
    assert "Closed Job" not in html and "Delisted Job" not in html
    assert "$35,000 - $45,000" in html and "Mangilao" in html
    assert "Aug 19, 2026" in html and "Open until filled" in html
    assert 'href="https://apply.hafajobs.com/uploads/jobs/nurse.pdf"' in html
    assert "Scraped" in html
    assert "<script>x</script>" not in html
    assert 'aria-current="page">Jobs<' in html
    # Sidebar: boards with open postings and errors; the board that can't be collected.
    assert "no job postings found" in html
    assert "American Job Center" in html and "Imperva" in html


def test_jobs_page_filters(web):
    html = web.get("/jobs?q=nurse").text
    assert "Licensed Practical Nurse I" in html and "Carpenter" not in html
    html = web.get("/jobs?source=guamtemps").text
    assert "Carpenter" in html and "Licensed Practical Nurse I" not in html
    assert web.get("/jobs?page=5").status_code == 404
