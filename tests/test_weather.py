"""The weather: NWS alerts, the airport reading, the forecast, the JTWC storm
list, the warning banner and the weather box. The fixtures are real answers
from api.weather.gov and the JTWC feed, saved on 2026-11-24."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import fixture_bytes

from guheadlines import weather
from guheadlines.scraper.http import HttpClient

# The fixtures' alerts end on 2026-11-25 and 2026-11-26, so tests that show
# them use a fixed "now".
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def fixture_json(name: str) -> dict:
    return json.loads(fixture_bytes(name))


# A western Pacific list with storms, in the layout of the real feed's
# Southern Hemisphere entry (the real western Pacific list had none).
ACTIVE_STORMS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>JTWC</title>
<item>
<title>Current Northwest Pacific/North Indian Ocean* Tropical Systems</title>
<description><![CDATA[<p><b>Typhoon 25W (Yagi) Warning #12 </b><br>
<b>Issued at 06/0300Z<b>
<ul>
<li><a
href='https://www.metoc.navy.mil/jtwc/products/wp2524web.txt'
target='newwin'>TC Warning Text </a></li>
<li><a
href='https://www.metoc.navy.mil/jtwc/products/wp2524.gif'
target='newwin'>TC Warning Graphic</a></li></ul>
<p><b>Tropical Cyclone 03B (Three) Warning #05 </b><br>
<b>Issued at 06/0300Z<b>
<ul>
<li><a
href='https://www.metoc.navy.mil/jtwc/products/io0324web.txt'
target='newwin'>TC Warning Text </a></li></ul>
<b>Tropical Cyclone Formation Alert WTPN21 <b><br>
<b>Issued at 06/0400Z<b>
<ul>
<li><a
href='https://www.metoc.navy.mil/jtwc/products/wp9624web.txt'
target='newwin'>TCFA Text </a></li></ul>
]]></description>
<guid>NWPAC-NIO-WARNINGS</guid>
</item>
</channel></rss>
"""


def test_alerts_from_nws():
    alerts = weather.parse_alerts(fixture_json("nws_alerts.json"))
    assert [a["event"] for a in alerts] == ["Rip Current Statement", "Small Craft Advisory"]
    rip = alerts[0]
    assert rip["area"] == "Guam, Rota, Tinian, Saipan"
    assert not rip["warning"]  # a statement: the weather box only, no banner
    # "ends" (when the danger ends) wins over "expires" (when NWS sends an update).
    assert rip["ends"] == "2026-11-26T15:00:00+10:00"
    # The text is joined into paragraphs, and "* WHAT..." made readable.
    assert rip["text"][0] == "What: Dangerous rip currents."
    assert rip["text"][4].startswith("Additional details: A trade-wind swell will continue")
    assert rip["text"][-1].startswith("Rip currents will be life threatening. If caught")


def test_watches_and_warnings_go_in_the_banner():
    assert weather.is_warning("Typhoon Warning", "Extreme")
    assert weather.is_warning("Tropical Storm Watch", "Moderate")
    assert weather.is_warning("Tsunami Warning", "Unknown")
    assert weather.is_warning("Special Weather Statement", "Severe")
    assert not weather.is_warning("Small Craft Advisory", "Minor")
    assert not weather.is_warning("Rip Current Statement", "Moderate")

    data = fixture_json("nws_alerts.json")
    data["features"][1]["properties"].update(event="Typhoon Warning", severity="Extreme")
    data["features"][0]["properties"]["messageType"] = "Cancel"
    alerts = weather.parse_alerts(data)
    assert [(a["event"], a["warning"]) for a in alerts] == [("Typhoon Warning", True)]


def test_airport_reading():
    # NWS gives the decoded values as null for Guam; the report itself has them.
    observation = fixture_json("nws_observation.json")
    assert observation["properties"]["temperature"]["value"] is None
    now = weather.parse_observation(observation)
    assert now == {
        "time": "2026-11-24T09:54:00+00:00",
        "sky": "Mostly Clear",
        "temp_f": 82,  # 27.8 °C from the remarks (T02780244)
        "heat_index_f": 89,
        "humidity": 82,
        "wind_dir": "E",
        "wind_mph": 9,
        "gust_mph": None,
    }
    with pytest.raises(weather.WeatherError):
        weather.parse_observation({"properties": {"timestamp": None}})


def test_airport_report_variants():
    gusty = weather.parse_metar("PGUM 240354Z 06018G23KT 10SM SCT027 32/24 A2988")
    assert (gusty["wind_dir"], gusty["wind_mph"], gusty["gust_mph"]) == ("ENE", 21, 26)
    assert gusty["temp_f"] == 90 and gusty["heat_index_f"] > 90
    calm = weather.parse_metar("PGUM 240354Z 00000KT 10SM CLR 24/22 A2988")
    assert (calm["wind_dir"], calm["wind_mph"]) == (None, 0)
    assert calm["temp_f"] == 75 and calm["heat_index_f"] is None  # below 80 °F
    variable = weather.parse_metar("XXXX 240354Z VRB03KT 10SM CLR M02/M05 A2988")
    assert (variable["wind_dir"], variable["temp_f"]) == (None, 28)
    assert weather.parse_metar("PGUM 240354Z AUTO")["temp_f"] is None


def test_forecast():
    assert weather.forecast_url(fixture_json("nws_points.json")) == (
        "https://api.weather.gov/gridpoints/GUM/47,48/forecast"
    )
    periods = weather.parse_forecast(fixture_json("nws_forecast.json"))
    assert [p["name"] for p in periods] == [
        "Tonight",
        "Wednesday",
        "Wednesday Night",
        "Thanksgiving Day",
    ]
    assert periods[0] | {"detail": ""} == {
        "name": "Tonight",
        "temp_f": 77,
        "daytime": False,
        "short": "Scattered Rain Showers",
        "rain": 26,
        "detail": "",
    }
    with pytest.raises(weather.WeatherError):
        weather.forecast_url({"properties": {"forecast": "https://example.com/forecast"}})
    with pytest.raises(weather.WeatherError):
        weather.parse_forecast({"properties": {"periods": []}})


def test_storm_list():
    # The real feed: no western Pacific storms (the Southern Hemisphere alert
    # in it is left out).
    assert weather.parse_storms(fixture_bytes("jtwc.rss")) == []
    assert weather.parse_storms(ACTIVE_STORMS.encode()) == [
        {
            "name": "Typhoon 25W (Yagi) Warning #12",
            "url": "https://www.metoc.navy.mil/jtwc/products/wp2524web.txt",
        },
        # 03B is in the Bay of Bengal: left out.
        {
            "name": "Tropical Cyclone Formation Alert",
            "url": "https://www.metoc.navy.mil/jtwc/products/wp9624web.txt",
        },
    ]
    changed = fixture_bytes("jtwc.rss").replace(b"No Current Tropical Cyclone Warnings", b"?")
    with pytest.raises(weather.WeatherError):
        weather.parse_storms(changed)
    with pytest.raises(weather.WeatherError):
        weather.parse_storms(b"<rss><channel></channel></rss>")


def test_what_the_pages_show():
    alerts = weather.parse_alerts(fixture_json("nws_alerts.json"))
    alerts.append({**alerts[1], "event": "Typhoon Warning", "warning": True, "ends": None})
    reports = {
        "alerts": (alerts, NOW - timedelta(minutes=5)),
        "conditions": (weather.parse_observation(fixture_json("nws_observation.json")), NOW),
        "forecast": ([{"name": "Tonight"}], NOW - timedelta(hours=7)),
    }
    shown = weather.for_page(reports, NOW)
    assert [a["event"] for a in shown["warnings"]] == ["Typhoon Warning"]
    assert [a["event"] for a in shown["advisories"]] == [
        "Rip Current Statement",
        "Small Craft Advisory",
    ]
    assert shown["advisories"][1]["ends"] == datetime(2026, 11, 24, 19, 0, tzinfo=timezone.utc)
    assert shown["conditions"]["time"] == datetime(2026, 11, 24, 9, 54, tzinfo=timezone.utc)
    assert shown["forecast"] is None  # 7 hours old
    assert shown["storms"] is None  # never read
    assert shown["checked"] == NOW - timedelta(minutes=5)

    # Alerts leave at their end time, also when the worker can't reach NWS.
    later = weather.for_page(reports, datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc))
    assert [a["event"] for a in later["advisories"]] == ["Rip Current Statement"]
    # One with no end time stays only while the copy is new.
    assert later["warnings"] == []
    assert weather.for_page({}, NOW)["warnings"] == []


def test_weather_setting(monkeypatch):
    from guheadlines.settings import load_settings

    monkeypatch.delenv("WEATHER_INTERVAL_MINUTES", raising=False)
    assert load_settings().weather_interval_minutes == 10
    for value, minutes in (("0", 0), ("2", 5), ("15", 15), ("-1", 0), ("x", 10)):
        monkeypatch.setenv("WEATHER_INTERVAL_MINUTES", value)
        assert load_settings().weather_interval_minutes == minutes, value


# ------------------------------------------------------------ with a database


def _fake_sources(fail: tuple[str, ...] = ()):
    files = {
        "/alerts/active": "nws_alerts.json",
        "/stations/PGUM/observations/latest": "nws_observation.json",
        "/points/13.4757,144.7489": "nws_points.json",
        "/gridpoints/GUM/47,48/forecast": "nws_forecast.json",
        "/jtwc/rss/jtwc.rss": "jtwc.rss",
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = files.get(request.url.path)
        if name is None or any(part in request.url.path for part in fail):
            return httpx.Response(404)
        return httpx.Response(200, content=fixture_bytes(name))

    client = HttpClient(
        "test-agent",
        per_host_delay=0,
        respect_robots=False,  # as in check_weather
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    return client, seen


def test_check_saves_each_source_and_keeps_the_last_good_copy(clean_db):
    from guheadlines.db import session_scope
    from guheadlines.settings import get_settings
    from guheadlines.web import queries

    client, seen = _fake_sources()
    results = weather.check_weather(get_settings(), client)
    assert [(kind, error) for kind, _, error in results] == [
        ("alerts", None),
        ("conditions", None),
        ("forecast", None),
        ("storms", None),
    ]
    assert seen[0].url.params["zone"] == "GUZ001,PMZ151"
    assert seen[0].headers["accept"] == "application/geo+json"
    with session_scope() as session:
        first = queries.weather_reports(session)
    assert set(first) == {"alerts", "conditions", "forecast", "storms"}
    assert first["conditions"][0]["temp_f"] == 82

    # NWS is down: the alerts and the forecast keep their last copy.
    client, _ = _fake_sources(fail=("/alerts", "/points"))
    results = weather.check_weather(get_settings(), client)
    errors = {kind: error for kind, _, error in results}
    assert errors["alerts"].startswith("FetchError: HTTP 404")
    assert errors["forecast"] and errors["conditions"] is None
    with session_scope() as session:
        second = queries.weather_reports(session)
    assert second["alerts"] == first["alerts"]
    assert second["conditions"][1] > first["conditions"][1]


@pytest.fixture()
def client(clean_db):
    from fastapi.testclient import TestClient

    from guheadlines.web.app import app, sidebar_cache, weather_cache

    sidebar_cache._data.clear()
    weather_cache._data.clear()
    yield TestClient(app)
    weather_cache._data.clear()


def _save_weather(warning: bool = True):
    data = fixture_json("nws_alerts.json")
    if warning:  # the Rip Current Statement becomes a Typhoon Warning
        props = data["features"][1]["properties"]
        props.update(event="Typhoon Warning", severity="Extreme", areaDesc="Guam")
        props["description"] = "* WHAT...Damaging winds <script>alert(1)</script>."
    alerts = weather.parse_alerts(data)
    for alert in alerts:  # the fixtures' end times are in November 2026
        alert["ends"] = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    weather.save("alerts", alerts)
    weather.save("conditions", weather.parse_observation(fixture_json("nws_observation.json")))
    weather.save("forecast", weather.parse_forecast(fixture_json("nws_forecast.json")))
    weather.save("storms", weather.parse_storms(ACTIVE_STORMS.encode()))


def test_banner_on_every_page_and_weather_box_on_the_home_page(client):
    _save_weather()
    home = client.get("/").text
    for path in ("/", "/category/military", "/jobs", "/status", "/archive", "/nope"):
        page = client.get(path).text
        assert 'class="weather-alert is-warning"' in page, path
        assert "<strong>Typhoon Warning</strong> for Guam, until " in page, path
    # The NWS text is shown as text, never as HTML.
    assert "<script>alert(1)</script>" not in home
    assert "What: Damaging winds &lt;script&gt;" in home

    # The weather box: only on the first page of the home page.
    assert 'class="weather"' in home
    assert ">82°F<" in home and "Feels like 89°F" in home and "Wind E 9 mph" in home
    assert "Low 77°" in home and "High 88°" in home and "Thanksgiving Day" in home
    assert 'href="https://www.metoc.navy.mil/jtwc/products/wp2524web.txt"' in home
    assert "Typhoon 25W (Yagi) Warning #12" in home
    # Advisories are in the box; the warning is in the banner only.
    box = home.split('class="weather"', 1)[1].split("</section>", 1)[0]
    assert "<summary><strong>Small Craft Advisory</strong></summary>" in box
    assert "<p><strong>Until " in box  # the end time is inside, when it is opened
    assert "<strong>Typhoon Warning" not in box
    for path in ("/category/military", "/jobs", "/status"):
        assert 'class="weather"' not in client.get(path).text, path


def test_quiet_weather_has_no_banner(client):
    _save_weather(warning=False)
    home = client.get("/").text
    assert "weather-alert" not in home
    assert 'class="weather"' in home


def test_no_weather_box_before_the_first_check_or_when_turned_off(client, monkeypatch):
    from test_web import _set

    assert 'class="weather' not in client.get("/").text
    _save_weather()
    _set(monkeypatch, weather_interval_minutes=0)
    assert 'class="weather' not in client.get("/").text


def test_weather_needs_no_change_to_the_content_security_policy(client):
    from guheadlines.web.app import CONTENT_SECURITY_POLICY

    _save_weather()
    page = client.get("/")
    # Everything is drawn on the server: no new script, style or address.
    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert " style=" not in page.text and "<script src" not in page.text
