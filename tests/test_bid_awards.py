"""The award history: reading awarded bids from each source's list, storing
them, and the /bids/awards page (the last BID_AWARDS_FREE_DAYS days for
everyone, all of it for bid alert subscribers)."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import fixture_bytes
from fastapi.testclient import TestClient
from sqlalchemy import select

from guheadlines.bids import alerts
from guheadlines.bids.pipeline import AWARDS_EVERY, BidChecker
from guheadlines.bids.readers import BidError, parse_awards, read_awards, split_award
from guheadlines.config import parse_bids_config
from guheadlines.db import session_scope
from guheadlines.models import BidAlertSubscriber, BidAward, BidSource
from guheadlines.scraper.http import FetchResult, HttpClient
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import get_settings, load_settings

GPA_AWARDS = (
    "https://go.opengovguam.com/tenders/tenders_list-standalone/gpa?tender_status=Awarded&fy=All"
)
PORT = "https://portofguam.com/bids-and-rfps"
GMHA = "https://www.gmha.org/procurement/"


def page(url: str, fixture: str) -> FetchResult:
    return FetchResult(url=url, status=200, content=fixture_bytes(fixture), headers={})


# ------------------------------------------------------------------ readers


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "$39,600.00 Awarded to: Pacific Revolution Corp.",
            ("Pacific Revolution Corp.", 3960000, None),
        ),
        (
            "$69,000.00 and $15,600.00 Awarded to: JMI Edison and Example Wire Co.",
            ("JMI Edison and Example Wire Co.", 8460000, "$69,000.00 and $15,600.00"),
        ),
        ("N/A Awarded to: N/A", (None, None, None)),
        (
            "HEMA Corporation dba Deesoniis. - $8,990.00",
            ("HEMA Corporation dba Deesoniis.", 899000, None),
        ),
        # Several winners, each with its amount: the names between the amounts.
        (
            "Triple J Tire - $36,422.00. Napa Auto Parts - $21,912.48. Depot, LLC. - $1,957.68",
            (
                "Triple J Tire; Napa Auto Parts; Depot, LLC.",
                6029216,
                "$36,422.00 and $21,912.48 and $1,957.68",
            ),
        ),
        # A space typed after the comma.
        ("J&B Modern Tech - $16, 078.65", ("J&B Modern Tech", 1607865, None)),
        ("Plus Marketing", ("Plus Marketing", None, None)),
        # No winner: a state, a link to a PDF, nothing.
        ("Award Information", (None, None, None)),
        ("CANCELLED", (None, None, None)),
        ("Under evaluation", (None, None, None)),
        ("", (None, None, None)),
        ("$1,234 Awarded to: TBD", (None, 123400, None)),
    ],
)
def test_winners_and_amounts(text, expected):
    assert split_award(text) == expected


def test_power_authority_award_list():
    awards = parse_awards(page(GPA_AWARDS, "bids_opengov_gpa_awarded.html"), "Guam Power Authority")
    first = awards[0]
    assert first.number == "IFB GPA-082-26" and first.title == "Photo Electric Control"
    assert first.awarded_to == "Pacific Revolution Corp." and first.amount_cents == 3960000
    assert first.kind == "Invitation for bid" and first.agency == "Guam Power Authority"
    assert first.closed_on == date(2026, 9, 9) and first.posted_on == date(2026, 8, 18)
    assert first.url.endswith("/tender_details-standalone/gpa/IFB%20GPA-082-26")
    # The bid abstract (every bidder's price), not the bid's other files.
    assert first.document_url.endswith("GPA-082-26%20Bid%20Abstract.pdf")
    two = awards[1]
    assert two.amount_cents == 8460000 and two.amounts == "$69,000.00 and $15,600.00"
    assert two.document_url is None
    # Listed as awarded without a winner or amount: still an award.
    assert (awards[3].number, awards[3].awarded_to, awards[3].amount_cents) == (
        "RFP GPA-050-26",
        None,
        None,
    )
    assert len(awards) == 5  # sole-source purchases are left out later, by the check


def test_lists_of_open_bids_give_no_awards():
    gpa = "https://go.opengovguam.com/tenders/tenders_list-standalone/gpa?tender_status=Available"
    assert parse_awards(page(gpa, "bids_opengov_gpa.html")) == []
    ghura = "https://go.opengovguam.com/bids/bids_list_ghura/ghura?bid_status=Awarded"
    assert parse_awards(page(ghura, "bids_opengov_ghura.html")) == []


def test_port_and_hospital_lists():
    (port,) = parse_awards(page(PORT, "bids_port.html"), "Port Authority of Guam")
    assert port.number == "IFB-PAG-006-26" and port.amount_cents == 899000
    assert port.awarded_to == "HEMA Corporation dba Deesoniis."
    assert port.document_url.endswith("contract_cover_line_items_-_deesoniis.pdf")
    assert port.posted_on == date(2026, 6, 4) and port.closed_on is None
    # The hospital's "Status/Awarded" column: the closed and open bids are not awards.
    (gmha,) = parse_awards(page(GMHA, "bids_gmha.html"))
    assert gmha.awarded_to == "Plus Marketing" and gmha.number == "SOLE SOURCE"


def test_hospital_award_information_link():
    html = fixture_bytes("bids_gmha.html").replace(
        b"Plus Marketing",
        b'<a href="https://www.gmha.org/wp-content/uploads/award_information.pdf">'
        b"Award Information</a>",
    )
    (award,) = parse_awards(FetchResult(url=GMHA, status=200, content=html))
    assert award.awarded_to is None
    assert award.document_url == "https://www.gmha.org/wp-content/uploads/award_information.pdf"


def test_a_table_without_an_awarded_column_is_an_error():
    with pytest.raises(BidError, match="no awarded column"):
        parse_awards(page("https://guamwaterworks.org/bids/", "bids_gwa.html"))


def test_award_list_pages_are_followed():
    """The Port's list is split into pages by a Drupal pager."""
    first = fixture_bytes("bids_port.html").replace(
        b"</table>",
        b'</table><ul class="pager"><li class="pager-next">'
        b'<a title="Go to next page" href="/bids-and-rfps?page=1">next</a></li></ul>',
    )
    second = fixture_bytes("bids_port.html").replace(b"IFB-PAG-006-26", b"IFB-PAG-099-25")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.params.get("page") == "1":
            return httpx.Response(200, content=second)
        return httpx.Response(200, content=first)

    client = HttpClient("test", per_host_delay=0, retries=0, transport=httpx.MockTransport(handler))
    try:
        awards = read_awards(client, PORT, "Port Authority of Guam")
    finally:
        client.close()
    assert [a.number for a in awards] == ["IFB-PAG-006-26", "IFB-PAG-099-25"]
    assert seen[-1] == f"{PORT}?page=1"


def test_awards_in_the_config():
    sources = parse_bids_config(
        {
            "sources": [
                {"slug": "gpa", "name": "GPA", "table": "https://x.gu/", "awards": GPA_AWARDS}
            ]
        }
    )
    assert sources[0].awards == [GPA_AWARDS]
    assert (
        parse_bids_config({"sources": [{"slug": "a", "name": "A", "table": "u"}]})[0].awards == []
    )


# -------------------------------------------------------------------- check


class FakeAwardSite:
    def __init__(self):
        self.content = fixture_bytes("bids_opengov_gpa_awarded.html")
        self.reads = 0
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        self.reads += 1
        if self.down:
            return httpx.Response(503)
        return httpx.Response(200, content=self.content)


@pytest.fixture()
def award_site():
    return FakeAwardSite()


@pytest.fixture()
def award_checker(award_site):
    client = HttpClient(
        "test", per_host_delay=0, retries=0, transport=httpx.MockTransport(award_site)
    )
    sources = parse_bids_config(
        {
            "exclude": ["chamber of commerce"],
            "sources": [
                {
                    "slug": "gpa",
                    "name": "Guam Power Authority",
                    "table": "https://go.opengovguam.com/tenders/x",
                    "awards": GPA_AWARDS,
                    "agency": "Guam Power Authority",
                    "exclude": ["consulting"],
                },
                {"slug": "gwa", "name": "Waterworks", "table": "https://guamwaterworks.org/bids/"},
            ],
        }
    )
    yield BidChecker(get_settings(), sources, client=client)
    client.close()


def test_collect_awards_leaves_out_sole_source_and_excluded(award_checker):
    awards, errors = award_checker.collect_awards(award_checker.sources[0])
    assert errors == []
    # Newest first; the sole-source purchase and the excluded consulting job are left out.
    assert [a.number for a in awards] == ["IFB GPA-082-26", "IFB GPA-073-26", "IFB GPA-067-26"]


def test_awards_are_stored_kept_and_read_once_a_day(clean_db, award_site, award_checker):
    now = datetime(2026, 11, 27, 8, tzinfo=timezone.utc)
    (result,) = award_checker.run_awards(now=now)
    assert (result.slug, result.found, result.new, result.error) == ("gpa", 3, 3, None)
    with session_scope() as session:
        rows = {r.number: r for r in session.scalars(select(BidAward))}
        gpa = session.scalar(select(BidSource).where(BidSource.slug == "gpa"))
        assert gpa.awards_run_at == now and gpa.awards_count == 3 and gpa.awards_error is None
    assert rows["IFB GPA-067-26"].awarded_to == "JMI Edison"
    assert rows["IFB GPA-067-26"].amount_cents == 27974400
    # Read again within a day: nothing is read.
    reads = award_site.reads
    assert award_checker.run_awards(now=now + timedelta(hours=2)) == []
    assert award_site.reads == reads
    # A day later the agency lists only one award, without its amount: the
    # others stay in the history, and the amount found before is kept.
    head, rest = award_site.content.split(b"<tbody>", 1)
    one = rest.split(b"</tr>")[2].replace(b"$279,744.00", b"N/A")  # GPA-067-26
    award_site.content = head + b"<tbody>" + one + b"</tr></tbody></table></body></html>"
    (result,) = award_checker.run_awards(now=now + AWARDS_EVERY)
    assert (result.found, result.new) == (1, 0)
    with session_scope() as session:
        rows = {r.number: r for r in session.scalars(select(BidAward))}
    assert len(rows) == 3
    assert rows["IFB GPA-067-26"].amount_cents == 27974400
    assert rows["IFB GPA-067-26"].last_seen_at == now + AWARDS_EVERY
    assert rows["IFB GPA-082-26"].last_seen_at == now


def test_an_award_list_that_fails_keeps_the_history(clean_db, award_site, award_checker):
    now = datetime(2026, 11, 27, 8, tzinfo=timezone.utc)
    award_checker.run_awards(now=now)
    award_site.down = True
    (result,) = award_checker.run_awards(now=now, force=True)
    assert result.error and result.found == 0
    with session_scope() as session:
        assert session.query(BidAward).count() == 3
        gpa = session.scalar(select(BidSource).where(BidSource.slug == "gpa"))
        assert gpa.awards_error and gpa.awards_count == 3  # the last good count


# --------------------------------------------------------------------- page

ON = {
    "bid_alerts_price_cents": 1500,
    "paypal_client_id": "client-id",
    "paypal_client_secret": "secret",
    "smtp_host": "smtp.example.com",
    "mail_from": "GU Headlines <alerts@example.com>",
    "site_url": "https://news.example.com",
}
TODAY = datetime.now(GUAM_TZ).date()


def _web(monkeypatch, **values):
    from guheadlines.web import app as web_app

    site = dataclasses.replace(load_settings(), **values)
    monkeypatch.setattr(web_app, "settings", site)
    monkeypatch.setitem(web_app.templates.env.globals, "settings", site)
    web_app.sidebar_cache._data.clear()
    web_app.sitemap_cache._data.clear()
    return TestClient(web_app.app, follow_redirects=False)


@pytest.fixture()
def awards(clean_db):
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        gpa = BidSource(
            slug="gpa",
            name="Guam Power Authority",
            homepage="https://go.opengovguam.com/tenders/x",
            awards_run_at=now,
            awards_count=3,
        )
        port = BidSource(
            slug="port",
            name="Port Authority of Guam",
            homepage=PORT,
            awards_run_at=now,
            awards_count=1,
            awards_error="portofguam.com: 503",
        )
        session.add_all([gpa, port])
        session.flush()
        common = {"first_seen_at": now, "last_seen_at": now}
        session.add_all(
            [
                BidAward(
                    source_id=gpa.id,
                    uid="award:IFB GPA-082-26",
                    title="Photo Electric Control",
                    number="IFB GPA-082-26",
                    kind="Invitation for bid",
                    agency="Guam Power Authority",
                    awarded_to="Pacific Revolution Corp.",
                    amount_cents=3960000,
                    url="https://go.opengovguam.com/tenders/x/IFB%20GPA-082-26",
                    document_url="https://go.opengovguam.com/uploads/GPA-082-26%20Bid%20Abstract.pdf",
                    closed_on=TODAY - timedelta(days=20),
                    **common,
                ),
                BidAward(
                    source_id=gpa.id,
                    uid="award:IFB GPA-073-26",
                    title="Wire(s)",
                    number="IFB GPA-073-26",
                    agency="Guam Power Authority",
                    awarded_to="JMI Edison and Example Wire Co.",
                    amount_cents=8460000,
                    amounts="$69,000.00 and $15,600.00",
                    url="https://go.opengovguam.com/tenders/x/IFB%20GPA-073-26",
                    closed_on=TODAY - timedelta(days=60),
                    **common,
                ),
                # Older than 180 days: subscribers only.
                BidAward(
                    source_id=gpa.id,
                    uid="award:IFB GPA-030-24",
                    title="Old Transformer Purchase",
                    number="IFB GPA-030-24",
                    agency="Guam Power Authority",
                    awarded_to="JMI Edison",
                    amount_cents=50000000,
                    url="https://go.opengovguam.com/tenders/x/IFB%20GPA-030-24",
                    closed_on=date(2024, 3, 1),
                    **common,
                ),
                BidAward(
                    source_id=port.id,
                    uid="award:IFB-PAG-002-26",
                    title="Tires and Batteries Bid",
                    number="IFB-PAG-002-26",
                    agency="Port Authority of Guam",
                    awarded_to="Triple J Tire; Napa Auto Parts",
                    amount_cents=5833448,
                    amounts="$36,422.00 and $21,912.48",
                    url="https://portofguam.com/tires",
                    posted_on=TODAY - timedelta(days=5),
                    **common,
                ),
            ]
        )


def _subscribe(token: str, status: str = "active", paid_days: int = 20) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(
            BidAlertSubscriber(
                token=token,
                email=f"{token}@example.com",
                trades=["electrical"],
                keywords=[],
                status=status,
                paid_until=now + timedelta(days=paid_days),
            )
        )


def _listing(html: str) -> str:
    return html.split('<h2 class="result-count">', 1)[1].split('<aside class="sidebar">')[0]


def test_award_page_shows_the_last_days_to_everyone(awards, monkeypatch):
    web = _web(monkeypatch, **ON)
    page = web.get("/bids/awards")
    assert page.status_code == 200 and "Cookie" in page.headers["vary"]
    html = page.text
    listing = _listing(html)
    assert "3 awards" in listing and "in the last 180 days" in listing
    # Newest first.
    assert (
        listing.index("Tires and Batteries")
        < listing.index("Photo Electric")
        < listing.index("Wire(s)")
    )
    assert "Pacific Revolution Corp." in listing and "$39,600" in listing
    assert "$84,600" in listing and "($69,000.00 and $15,600.00)" in listing
    assert "Bid abstract (PDF)" in listing
    # Each of several winners links to its own awards.
    assert 'href="/bids/awards?company=Napa%20Auto%20Parts"' in listing
    assert "Old Transformer Purchase" not in html
    # The older awards are counted, with the way to see them.
    assert "1 older award, $500,000 in all" in listing
    assert 'href="/bids/alerts"' in listing and "$15 a month" in html
    sidebar = html.split('<aside class="sidebar">', 1)[1]
    assert "Top winners" in sidebar and "JMI Edison and Example Wire Co." in sidebar
    assert "portofguam.com: 503" in sidebar
    assert "<title>Bid award history" in html and '<meta name="robots"' not in html
    assert "chamber" not in html.lower() and "github" not in html.lower()


def test_award_page_filters(awards, monkeypatch):
    web = _web(monkeypatch, **ON)
    html = _listing(web.get("/bids/awards?q=wire").text)
    assert "Wire(s)" in html and "Photo Electric" not in html
    html = _listing(web.get("/bids/awards?company=jmi+edison").text)
    # The award it shared counts; the old one is for subscribers.
    assert "Wire(s)" in html and "won by jmi edison" in html and "Photo Electric" not in html
    assert "1 older award" in html
    page = web.get("/bids/awards?agency=Port+Authority+of+Guam")
    assert "Tires and Batteries" in page.text and "Photo Electric" not in _listing(page.text)
    assert '<meta name="robots" content="noindex, follow">' in page.text
    assert web.get("/bids/awards?page=9").status_code == 404


def test_subscribers_see_everything(awards, monkeypatch):
    web = _web(monkeypatch, **ON)
    _subscribe("tok-active")
    # The link from the emails remembers the browser, then drops the code from the address.
    response = web.get("/bids/awards?t=tok-active")
    assert response.status_code == 303 and response.headers["location"] == "/bids/awards"
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"bid_awards={alerts.awards_key('tok-active')};")
    assert "HttpOnly" in cookie and "Path=/bids/awards" in cookie and "tok-active" not in cookie
    web.cookies.set("bid_awards", alerts.awards_key("tok-active"))
    page = web.get("/bids/awards")
    html = page.text
    assert "Old Transformer Purchase" in html and "4 awards" in _listing(html)
    assert "included with your bid alerts" in html and "older award" not in html
    assert page.headers["cache-control"] == "private, no-store"
    assert "noindex" in page.headers["x-robots-tag"]
    # The cookie can't open the subscriber's own page.
    assert alerts.by_token(alerts.awards_key("tok-active")) is None


def test_ended_subscriptions_see_the_last_days(awards, monkeypatch):
    web = _web(monkeypatch, **ON)
    _subscribe("tok-old", status="cancelled", paid_days=-1)
    response = web.get("/bids/awards?t=tok-old")
    assert response.headers["location"] == "/bids/awards?access=ended"
    assert "set-cookie" not in response.headers
    assert "no longer opens the full history" in web.get("/bids/awards?access=ended").text
    # A browser that still has the cookie: the short history, and the cookie goes.
    web.cookies.set("bid_awards", alerts.awards_key("tok-old"))
    page = web.get("/bids/awards")
    assert "Old Transformer Purchase" not in page.text
    assert 'bid_awards=""' in page.headers["set-cookie"]
    assert "unknown" not in web.get("/bids/awards?t=unknown").headers["location"]


def test_everyone_sees_everything_while_bid_alerts_are_off(awards, monkeypatch):
    web = _web(monkeypatch, bid_alerts_price_cents=0)
    html = web.get("/bids/awards").text
    assert "Old Transformer Purchase" in html and "older award" not in html
    assert "4 awards" in _listing(html) and "in the last" not in _listing(html)
    assert "/bids/alerts" not in _listing(html)


def test_award_history_links(awards, monkeypatch):
    web = _web(monkeypatch, **ON)
    assert 'href="/bids/awards"' in web.get("/bids").text
    assert "/bids/awards</loc>" in web.get("/sitemap.xml").text
    signup = web.get("/bids/alerts").text
    assert 'href="/bids/awards"' in signup and "last 180 days" in signup
    _subscribe("tok-page")
    manage = web.get("/bids/alerts/manage?t=tok-page").text
    assert 'href="/bids/awards?t=tok-page"' in manage
    sub = alerts.by_token("tok-page")
    site = dataclasses.replace(load_settings(), **ON)
    _, text, html = alerts.alert_email(site, sub, [])
    assert "https://news.example.com/bids/awards?t=tok-page" in text
    assert "https://news.example.com/bids/awards?t=tok-page" in html
    _, text, html = alerts.welcome_email(site, sub, [])
    assert "full award history" in text and "/bids/awards?t=tok-page" in html


def test_free_days_setting(monkeypatch):
    monkeypatch.delenv("BID_AWARDS_FREE_DAYS", raising=False)
    assert load_settings().bid_awards_free_days == 180
    for typed, days in (("30", 30), ("1", 7), ("99999", 3650), ("soon", 180)):
        monkeypatch.setenv("BID_AWARDS_FREE_DAYS", typed)
        assert load_settings().bid_awards_free_days == days
