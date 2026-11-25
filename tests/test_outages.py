"""Power and water outages: GPA's notices on GuamALERTS, GWA's outage blog,
Guam EPA's boil-water notices and the Outages page.

The fixtures are in the layout of the real pages, read from GitHub's network
on 2026-11-25:
- gwa_outages.rss: six real items of GWA's Blogger feed, plus one boil-water
  notice added by hand (post-1000000000000000001), since none was current.
- guamalerts_gpa.html: GPA's GuamALERTS page, cut down to three notices. The
  P-204 and P-203 Tamuning notices are real; the P-311 Barrigada one (Nov 28)
  was made up to have one still to come.
- epa_feed.xml: two real items of Guam EPA's news feed, plus a boil-water
  notice added by hand.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import fixture_bytes

from guheadlines import outages
from guheadlines.scraper.http import HttpClient

# Wednesday, Nov 25, 2026, 2:00 PM in Guam.
NOW = datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc)


def _parsed():
    return (
        outages.parse_gpa(fixture_bytes("guamalerts_gpa.html")),
        outages.parse_gwa(fixture_bytes("gwa_outages.rss")),
        outages.parse_epa(fixture_bytes("epa_feed.xml")),
    )


def test_gpa_notices():
    gpa = outages.parse_gpa(fixture_bytes("guamalerts_gpa.html"))
    assert [n["id"] for n in gpa] == ["GPA-GA-900001", "GPA-GA-841362", "GPA-GA-978829"]
    tamuning = gpa[1]
    assert tamuning["title"] == "Preventative maintenance outage announcement P-204 Tamuning"
    assert tamuning["category"] == "Scheduled Maintenance"
    assert tamuning["day"] == "2026-11-24"
    assert tamuning["hours"] == "8:30 a.m. – 2:30 p.m. (6 hours)"
    assert tamuning["villages"] == "Tamuning-Tumon-Harmon"
    assert tamuning["posted"] == "2026-11-19T16:30:00+10:00"
    # The notice's own page (from its share box) and its PDF, spaces escaped.
    assert tamuning["url"] == (
        "https://web.guamalerts.com/notification_details/gpa/"
        "preventative-maintenance-outage-announcement-p-204-tamuning"
    )
    assert tamuning["pdf"] == (
        "https://web.guamalerts.com/uploads/guam_alert/alert_1795071351_SCHEDULED%20PREVENTATIVE"
        "%20MAINTENANCE%20OUTAGE%20ANNOUNCEMENT%20-%20P-204%20Tamuning.pdf"
    )


def test_gpa_page_that_changed():
    with pytest.raises(outages.OutageError, match="no notices found"):
        outages.parse_gpa(b"<html><body><p>Down for maintenance</p></body></html>")


def test_gwa_notices():
    gwa = outages.parse_gwa(fixture_bytes("gwa_outages.rss"))
    assert len(gwa) == 7
    assert [n["kind"] for n in gwa] == [
        "boil",
        "emergency",
        "emergency",
        "emergency",
        "scheduled",
        "emergency",
        "extended",
    ]
    inalahan = gwa[1]
    assert inalahan["day"] == "2026-11-25"
    assert (inalahan["start"], inalahan["end"]) == ("Immediately", "8PM")
    assert inalahan["villages"] == "Inalahan and Malesso"
    # "&nbsp;" in the feed's text becomes a space.
    assert inalahan["areas"] == (
        "Chalan Canton Tasi from Inalahan Village to Toguan Malesso and surrounding areas."
    )
    assert inalahan["notes"] == [
        "We appreciate your patience as crew work on repairing a main water line."
    ]
    assert inalahan["url"] == (
        "https://bloggwapio.blogspot.com/2026_11_01_archive.html#7963080157478856600"
    )
    # "Estimated Start Time: 9AM - End Time: 5PM" on one line.
    barrigada = gwa[4]
    assert (barrigada["start"], barrigada["end"]) == ("9AM", "5PM")
    assert barrigada["day"] == "2026-11-19"
    # "Estimated - End Time: 8AM"; the title's capitals are softened.
    extended = gwa[6]
    assert extended["end"] == "8AM" and extended["start"] is None
    assert extended["title"].startswith("Extended Water Outage - Both Agana Heights")
    assert extended["villages"] == "Agana Heights / Sinajana"


def test_gwa_feed_that_is_not_a_feed():
    with pytest.raises(outages.OutageError):
        outages.parse_gwa(b"<html><body>Rate limit exceeded</body></html>")
    with pytest.raises(outages.OutageError):
        outages.parse_gwa(b"not xml at all <")


def test_epa_keeps_only_boil_water_notices():
    epa = outages.parse_epa(fixture_bytes("epa_feed.xml"))
    assert [n["title"] for n in epa] == ["Guam EPA Concurs: Boil Water Notice for Malesso Lifted"]
    assert epa[0]["summary"].endswith("the boil water notice is lifted.")
    assert "first appeared on" not in epa[0]["summary"]


def test_dates_in_notices():
    assert outages.find_date("Emergency Outage - Peca Inalahan, Wednesday November 25, 2026.") == (
        date(2026, 11, 25)
    )
    assert outages.find_date("Outage on Nov. 3, 2026") == date(2026, 11, 3)
    assert outages.find_date("Tuesday 11/24/2026") == date(2026, 11, 24)
    assert outages.find_date("13/45/2026") is None
    assert outages.find_date("No date here") is None


def test_boil_water_words():
    assert outages.is_boil_water("Precautionary Boil Water Notice")
    assert outages.is_boil_water("customers should boil-water before use")
    assert outages.is_boil_water("Please boil your water")
    assert not outages.is_boil_water("Water boiler repair at the plant")


def test_soften_caps():
    assert outages.soften_caps("PREVENTATIVE MAINTENANCE OUTAGE ANNOUNCEMENT P-204 Tamuning") == (
        "Preventative maintenance outage announcement P-204 Tamuning"
    )
    assert outages.soften_caps("GPA AND GWA ADVISORY") == "GPA and GWA advisory"
    assert outages.soften_caps("Emergency Outage - Dededo") == "Emergency Outage - Dededo"


def test_page_sorts_outages_by_day():
    gpa, gwa, epa = _parsed()
    checked = NOW - timedelta(minutes=5)
    reports = {
        "outages_gpa": (gpa, checked),
        "outages_gwa": (gwa, checked),
        "outages_epa": (epa, checked),
    }
    page = outages.outages_page(reports, NOW)
    # Power: Barrigada on Saturday is still to come; Tamuning on Tuesday is
    # earlier; Tamuning on Friday the 20th is too old (more than 3 days).
    assert [n["villages"] for n in page["power"]["coming"]] == ["Barrigada"]
    assert page["power"]["today"] == []
    assert [n["day"] for n in page["power"]["earlier"]] == [date(2026, 11, 24)]
    # Water: today's two emergencies; Sunday's is earlier; the boil-water
    # notice is not an outage.
    assert [n["villages"] for n in page["water"]["today"]] == [
        "Inalahan and Malesso",
        "Peca Inalahan",
    ]
    assert [n["day"] for n in page["water"]["earlier"]] == [date(2026, 11, 22)]
    assert all(n["kind"] != "boil" for g in page["water"].values() for n in g)
    # Boil-water notices from GWA and Guam EPA, newest first.
    assert [(n["source"], n["lifted"]) for n in page["boil"]] == [
        ("GWA", False),
        ("Guam EPA", True),
    ]
    assert page["gpa"] == {"checked": checked, "stale": False}


def test_page_before_the_first_check_and_when_a_source_is_stale():
    page = outages.outages_page({}, NOW)
    assert page["power"] == {"today": [], "coming": [], "earlier": []}
    assert page["boil"] == []
    assert page["gpa"]["stale"] and page["gpa"]["checked"] is None
    old = NOW - timedelta(hours=7)
    page = outages.outages_page({"outages_gwa": ([], old)}, NOW)
    assert page["gwa"] == {"checked": old, "stale": True}


def test_boil_water_notices_leave_after_30_days():
    _, gwa, epa = _parsed()
    later = NOW + timedelta(days=31)
    page = outages.outages_page({"outages_gwa": (gwa, later), "outages_epa": (epa, later)}, later)
    assert page["boil"] == []


def test_outages_setting(monkeypatch):
    from guheadlines.settings import load_settings

    monkeypatch.delenv("OUTAGES_INTERVAL_MINUTES", raising=False)
    assert load_settings().outages_interval_minutes == 15
    for value, minutes in (("0", 0), ("2", 5), ("30", 30), ("-1", 0), ("x", 15)):
        monkeypatch.setenv("OUTAGES_INTERVAL_MINUTES", value)
        assert load_settings().outages_interval_minutes == minutes, value


# ------------------------------------------------------------ with a database


def _fake_sources(fail: tuple[str, ...] = ()):
    files = {
        "web.guamalerts.com": "guamalerts_gpa.html",
        "bloggwapio.blogspot.com": "gwa_outages.rss",
        "epa.guam.gov": "epa_feed.xml",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path == "/robots.txt" or host in fail:
            return httpx.Response(404)
        return httpx.Response(200, content=fixture_bytes(files[host]))

    return HttpClient(
        "test-agent",
        per_host_delay=0,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )


def test_check_saves_each_source_and_keeps_the_last_good_copy(clean_db):
    from guheadlines.db import session_scope
    from guheadlines.settings import get_settings
    from guheadlines.web import queries

    results = outages.check_outages(get_settings(), _fake_sources())
    assert [(name, len(notices), error) for name, notices, error in results] == [
        ("gpa", 3, None),
        ("gwa", 7, None),
        ("epa", 1, None),
    ]
    with session_scope() as session:
        first = queries.weather_reports(session)
    assert {"outages_gpa", "outages_gwa", "outages_epa"} <= set(first)

    # GuamALERTS is down: GPA keeps its last copy, the others are read again.
    results = outages.check_outages(get_settings(), _fake_sources(fail=("web.guamalerts.com",)))
    errors = {name: error for name, _, error in results}
    assert errors["gpa"].startswith("FetchError: HTTP 404") and errors["gwa"] is None
    with session_scope() as session:
        second = queries.weather_reports(session)
    assert second["outages_gpa"] == first["outages_gpa"]
    assert second["outages_gwa"][1] > first["outages_gwa"][1]


@pytest.fixture()
def client(clean_db):
    from fastapi.testclient import TestClient

    from guheadlines.web.app import app, sidebar_cache, weather_cache

    sidebar_cache._data.clear()
    weather_cache._data.clear()
    yield TestClient(app)
    weather_cache._data.clear()


def _save_outages():
    """The fixtures' notices, moved so that their days fall around today."""
    from guheadlines.weather import save

    gpa, gwa, epa = _parsed()
    shift = datetime.now(outages.GUAM_TZ).date() - date(2026, 11, 25)
    moved = timedelta(days=shift.days)
    for notice in gpa + gwa:
        if notice.get("day"):
            notice["day"] = (date.fromisoformat(notice["day"]) + moved).isoformat()
    for notice in gpa + gwa + epa:
        notice["posted"] = (datetime.fromisoformat(notice["posted"]) + moved).isoformat()
    gwa[1]["areas"] = "Route 4 <script>alert(1)</script>"
    save("outages_gpa", gpa)
    save("outages_gwa", gwa)
    save("outages_epa", epa)


def test_outages_page(client):
    from guheadlines.web.app import CONTENT_SECURITY_POLICY

    _save_outages()
    page = client.get("/outages")
    assert page.status_code == 200
    html = page.text
    assert '<a href="/outages" aria-current="page">Outages</a>' in html
    for title in ("Boil-water notices", "Power (GPA)", "Water (GWA)"):
        assert f">{title}</h2>" in html, title
    assert "<h3>Today</h3>" in html and "<h3>Coming up</h3>" in html
    assert "<summary>Earlier (1)</summary>" in html
    assert "Inalahan and Malesso" in html and "Starts immediately · Ends about 8PM" in html
    assert ">Barrigada</strong>" in html and "8:00 a.m. – 12:00 p.m. (4 hours)" in html
    assert 'class="outage is-boil"' in html and ">Lifted</span>" in html
    # Scraped text is shown as text, never as HTML.
    assert "<script>alert(1)</script>" not in html
    assert "Route 4 &lt;script&gt;" in html
    # Each source is credited with a link, and readers can call to report.
    for url in (
        "https://web.guamalerts.com/notification/gpa",
        "https://bloggwapio.blogspot.com/",
        "https://epa.guam.gov/",
        "https://web.guamalerts.com/notification_details/gpa/"
        "preventative-maintenance-outage-announcement-p-311-barrigada",
        "tel:671-475-1472",
        "tel:671-646-4211",
    ):
        assert f'href="{url}"' in html, url
    # Drawn on the server: the same policy as every page, no inline style.
    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert " style=" not in html and "<script src" not in html
    assert html.count("Checked just now.") == 3
    # A separate page: no outage in the banner or on the home page.
    home = client.get("/").text
    assert "Inalahan and Malesso" not in home and "weather-alert" not in home


def test_outages_page_before_the_first_check(client):
    html = client.get("/outages").text
    assert "No boil-water notices in the last 30 days." in html
    assert "No power outages posted for today or the days ahead." in html
    assert "No water outages posted for today or the days ahead." in html
    assert html.count("Not checked yet.") == 3


def test_no_outages_page_when_turned_off(client, monkeypatch):
    from test_web import _set

    assert "/outages</loc>" in client.get("/sitemap.xml").text
    _set(monkeypatch, outages_interval_minutes=0)
    assert client.get("/outages").status_code == 404
    assert 'href="/outages"' not in client.get("/").text
    assert "/outages</loc>" not in client.get("/sitemap.xml").text
