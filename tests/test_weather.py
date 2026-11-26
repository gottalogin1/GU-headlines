"""The weather: NWS alerts, the airport reading, the forecasts, the surf and
coastal waters forecasts, the JTWC storm list, NOAA's tides, the warning
banner, the weather box and the Weather page. The NWS and JTWC fixtures are
real answers from api.weather.gov and the JTWC feed, saved on 2026-11-24 and
2026-11-25. NOAA's tide API can't be reached from where the tests were
written: noaa_tides.json is in the layout of its answer, with the Apra Harbor
times and heights of 25 and 26 Nov 2026 that the NWS surf forecast
(nws_surf.json) gives, and the tides before and after them filled in by
hand."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import fixture_bytes

from guheadlines import weather
from guheadlines.scraper.http import HttpClient
from guheadlines.scraper.text import GUAM_TZ

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
    # Every period is kept: the box shows four, the Weather page all of them.
    assert [p["name"] for p in periods] == [
        "Tonight",
        "Wednesday",
        "Wednesday Night",
        "Thanksgiving Day",
        "Thursday Night",
        "Friday",
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
        "/gridpoints/GUM/47,48/forecast/hourly": "nws_hourly.json",
        "/products/types/SRF/locations/GUM/latest": "nws_surf.json",
        "/products/types/CWF/locations/GUM/latest": "nws_marine.json",
        "/jtwc/rss/jtwc.rss": "jtwc.rss",
        "/api/prod/datagetter": "noaa_tides.json",
        "/mdapi/prod/webapi/stations/1630000.json": "noaa_station.json",
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
        ("hourly", None),
        ("surf", None),
        ("marine", None),
        ("storms", None),
        ("tides", None),
    ]
    assert seen[0].url.params["zone"] == "GUZ001,PMZ151"
    assert seen[0].headers["accept"] == "application/geo+json"
    with session_scope() as session:
        first = queries.weather_reports(session)
    assert set(first) == {
        "alerts",
        "conditions",
        "forecast",
        "hourly",
        "surf",
        "marine",
        "storms",
        "tides",
    }
    assert first["conditions"][0]["temp_f"] == 82
    assert first["tides"][0]["name"] == "Apra Harbor"
    tides_read = [r for r in seen if r.url.path == "/api/prod/datagetter"]
    assert tides_read[0].url.params["station"] == "1630000"

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
    now = datetime.now(timezone.utc)
    # The forecast for each hour and the tides, moved to start now.
    hours = weather.parse_hourly(fixture_json("nws_hourly.json"))
    for i, hour in enumerate(hours):
        hour["time"] = (now + timedelta(hours=i)).isoformat()
    weather.save("hourly", hours)
    weather.save("surf", weather.parse_surf(fixture_json("nws_surf.json")))
    weather.save("marine", weather.parse_marine(fixture_json("nws_marine.json")))
    weather.save(
        "tides", {"station": "1630000", "name": "Apra Harbor", "tides": _tides_around(now)}
    )


def _tides_around(now: datetime) -> list[dict]:
    """Highs and lows every 6 h 12 min from two days ago to 32 days ahead."""
    start = now - timedelta(days=2)
    return [
        {
            "time": (start + i * timedelta(hours=6, minutes=12)).isoformat(),
            "high": i % 2 == 0,
            "ft": 2.4 if i % 2 == 0 else (-1.1 if i % 4 == 1 else 1.4),
        }
        for i in range(34 * 4)
    ]


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


# ------------------------------------------------------------------- tides

TIDES_NOW = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)  # 12:00 PM ChST, Nov 25


def _saved_tides(station: str = "1630000") -> dict:
    return {
        "station": station,
        "name": "Apra Harbor",
        "tides": weather.parse_tides(fixture_json("noaa_tides.json")),
    }


def test_tides_from_noaa():
    tides = weather.parse_tides(fixture_json("noaa_tides.json"))
    assert len(tides) == 11
    assert tides[2] == {"time": "2026-11-24T22:45:00+00:00", "high": True, "ft": 2.4}
    assert tides[1]["ft"] == -1.0 and not tides[1]["high"]
    # -0.04 ft is 0.0 ft, not "-0.0 ft".
    almost = weather.parse_tides(
        {"predictions": [{"t": "2026-11-25 01:00", "v": "-0.04", "type": "L"}]}
    )
    assert str(almost[0]["ft"]) == "0.0"
    # Bad rows are skipped; an answer with no tides, or NOAA's error, raises.
    rows = [{"t": "soon", "v": "1", "type": "H"}, {"t": "2026-11-25 01:00", "v": "x", "type": "L"}]
    with pytest.raises(weather.WeatherError):
        weather.parse_tides({"predictions": rows})
    with pytest.raises(weather.WeatherError, match="No Predictions data was found"):
        weather.parse_tides({"error": {"message": "No Predictions data was found."}})

    url = httpx.URL(weather.tides_url("1630000", date(2026, 11, 24), date(2026, 12, 26)))
    assert url.host == "api.tidesandcurrents.noaa.gov"
    assert dict(url.params) == {
        "product": "predictions",
        "interval": "hilo",
        "station": "1630000",
        "begin_date": "20261124",
        "end_date": "20261226",
        "datum": "MLLW",
        "units": "english",
        "time_zone": "gmt",
        "format": "json",
    }
    assert weather.station_name(fixture_json("noaa_station.json")) == "Apra Harbor"
    assert weather.station_name({"stations": [{"name": "Guam"}]}) == ""
    assert weather.station_name({}) == ""
    assert weather.tide_page_url("1630000").endswith("noaatidepredictions.html?id=1630000")


def test_todays_tides_in_guam_time():
    saved = _saved_tides()
    today = weather.tides_today(saved, "1630000", TIDES_NOW)
    # Guam's 25 Nov starts at 14:00 UTC on the 24th: its first tide is the
    # low of 1:30 AM ChST, and the last the high of 6:52 PM.
    assert [(t["time"].hour, t["time"].minute, t["high"]) for t in today["today"]] == [
        (15, 30, False),
        (22, 45, True),
        (3, 53, False),
        (8, 52, True),
    ]
    assert [t["past"] for t in today["today"]] == [True, True, False, False]
    assert today["name"] == "Apra Harbor"
    # Off, another station, or no tides for today: nothing.
    assert weather.tides_today(saved, "", TIDES_NOW) is None
    assert weather.tides_today(saved, "1631428", TIDES_NOW) is None
    assert weather.tides_today(saved, "1630000", TIDES_NOW + timedelta(days=5)) is None
    assert weather.tides_today(None, "1630000", TIDES_NOW) is None

    days = weather.tide_days(saved, "1630000", TIDES_NOW, 7)
    # The saved tides reach into the 27th (ChST) only: the rest is left out.
    assert [d["day"].day for d in days] == [25, 26, 27]
    assert len(days[1]["tides"]) == 4


def test_tide_chart():
    chart = weather.tide_chart(_saved_tides(), date(2026, 11, 25), TIDES_NOW)
    assert chart["line"].startswith("M0.0,") and chart["line"].count(" L") == 96
    assert [d["high"] for d in chart["dots"]] == [False, True, False, True]
    assert chart["now"] == 360.0  # noon, half way across
    # Every point is inside the chart, with room above and below for the times.
    ys = [float(xy.split(",")[1]) for xy in chart["line"][1:].split(" L")]
    assert 42 <= min(ys) and max(ys) <= weather.CHART_HEIGHT - 42
    # A day that the saved tides don't cover from start to end has no chart.
    assert weather.tide_chart(_saved_tides(), date(2026, 11, 27), TIDES_NOW) is None
    assert weather.tide_chart(None, date(2026, 11, 25), TIDES_NOW) is None


def test_tide_station_setting(monkeypatch):
    from guheadlines.settings import load_settings

    monkeypatch.delenv("TIDE_STATION", raising=False)
    assert load_settings().tide_station == "1630000"
    for value, station in (
        ("", ""),
        (" 1631428 ", "1631428"),
        ("TPT2707", "TPT2707"),
        ("16 30", ""),
    ):
        monkeypatch.setenv("TIDE_STATION", value)
        assert load_settings().tide_station == station, value


def test_tides_are_read_every_few_hours(clean_db):
    import dataclasses

    from guheadlines.settings import get_settings

    settings = get_settings()
    kinds = lambda results: [kind for kind, _, _ in results]  # noqa: E731
    client, seen = _fake_sources()
    assert "tides" in kinds(weather.check_weather(settings, client))
    # Saved a moment ago: not read again, unless asked (guheadlines weather).
    assert "tides" not in kinds(weather.check_weather(settings, client))
    assert "tides" in kinds(weather.check_weather(settings, client, force_tides=True))
    later = datetime.now(timezone.utc) + weather.TIDES_EVERY
    assert weather.tides_due("1630000", later)
    # Another station: read now. No station: never read.
    assert weather.tides_due("1631428")
    off = dataclasses.replace(settings, tide_station="")
    assert "tides" not in kinds(weather.check_weather(off, client, force_tides=True))
    # The station's name is a nice-to-have: without it, the tides still work.
    client, _ = _fake_sources(fail=("/mdapi",))
    data = weather.read_tides(client, "1630000", TIDES_NOW)
    assert data["name"] == "" and len(data["tides"]) == 11


# ----------------------------------------------- the Weather page's parts


def test_sunrise_sunset_and_moon():
    from guheadlines import sky
    from guheadlines.scraper.text import GUAM_TZ

    def clock(dt: datetime) -> str:
        return f"{dt.astimezone(GUAM_TZ):%H:%M}"

    sunrise, sunset = sky.sun_times(date(2026, 11, 25))
    assert (clock(sunrise), clock(sunset)) == ("06:25", "17:50")
    sunrise, sunset = sky.sun_times(date(2026, 6, 21))
    assert (clock(sunrise), clock(sunset)) == ("05:55", "18:50")
    assert sky.for_day(date(2026, 11, 24))["moon"] == "Full moon"
    assert sky.for_day(date(2026, 12, 9))["moon"] == "New moon"
    assert sky.for_day(date(2026, 12, 1))["moon"] == "Last quarter"


def test_hourly_forecast():
    hours = weather.parse_hourly(fixture_json("nws_hourly.json"))
    assert len(hours) == 30
    assert hours[0] == {
        "time": "2026-11-25T11:00:00+10:00",
        "temp_f": 86,
        "rain": 13,
        "short": "Isolated Rain Showers",
        "wind": "ENE 15 mph",
    }
    assert weather.forecast_url(fixture_json("nws_points.json"), "forecastHourly").endswith(
        "/forecast/hourly"
    )
    with pytest.raises(weather.WeatherError):
        weather.parse_hourly({"properties": {"periods": []}})


def test_surf_zone_forecast():
    surf = weather.parse_surf(fixture_json("nws_surf.json"))
    assert surf["issued"] == "2026-11-24T19:24:00+00:00"
    assert surf["headlines"] == [
        "High risk of rip currents through Thursday afternoon along east facing reefs."
    ]
    assert [p["name"] for p in surf["periods"]] == ["Today", "Tonight", "Thursday"]
    today = surf["periods"][0]
    assert today["rip"] == "High"
    assert today["surf"] == [
        ["North", "4 to 6 ft"],
        ["East", "7 to 9 ft"],
        ["South", "2 to 4 ft"],
        ["West", "2 to 4 ft"],
    ]
    assert today["water_f"] == 84
    assert surf["discussion"].startswith("A trade-wind swell will continue")
    with pytest.raises(weather.WeatherError):
        weather.parse_surf({"productText": "nothing", "issuanceTime": "2026-11-25T00:00:00Z"})


def test_coastal_waters_forecast():
    marine = weather.parse_marine(fixture_json("nws_marine.json"))
    assert marine["headlines"] == [
        "Small craft advisory will expire at 5 AM ChST early this morning."
    ]
    assert [p["name"] for p in marine["periods"]][:4] == [
        "Today",
        "Tonight",
        "Thursday",
        "Thursday Night",
    ]
    assert marine["periods"][0]["text"].startswith("East wind 15 kt. Seas 6 to 8 ft.")
    assert marine["synopsis"].startswith("Moderate to fresh trade winds")
    with pytest.raises(weather.WeatherError):
        weather.parse_marine({"productText": "", "issuanceTime": ""})


def test_forecast_by_day():
    periods = weather.parse_forecast(fixture_json("nws_forecast.json"))
    days = weather.forecast_days(periods)
    assert [(d["name"], d["high"], d["low"]) for d in days] == [
        ("Tonight", None, 77),
        ("Wednesday", 88, 77),
        ("Thanksgiving Day", 88, 77),
        ("Friday", 86, None),
    ]
    assert days[1]["rain"] == 18


def test_weather_page_parts():
    now = TIDES_NOW
    reports = {
        "hourly": (weather.parse_hourly(fixture_json("nws_hourly.json")), now),
        "surf": (weather.parse_surf(fixture_json("nws_surf.json")), now),
        "marine": (weather.parse_marine(fixture_json("nws_marine.json")), now - timedelta(days=1)),
        "tides": (_saved_tides(), now),
    }
    page = weather.weather_page(reports, TIDES_NOW, "1630000")
    # From the hour that starts now (12 PM ChST): 11 AM is over.
    assert page["hourly"][0]["time"] == datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)
    assert len(page["hourly"]) == 24
    assert page["surf"]["issued"] == datetime(2026, 11, 24, 19, 24, tzinfo=timezone.utc)
    assert page["marine"] is None  # a day old
    assert [(t["time"].hour, t["high"]) for t in page["next_tides"]] == [(3, False), (8, True)]
    assert [len(page["tide_views"][v]) for v in ("day", "week", "month")] == [1, 3, 3]
    assert page["tide_station"]["name"] == "Apra Harbor"
    assert page["tide_chart"]["now"] == 360.0
    assert page["sky"]["moon"] == "Full moon"
    # Dive and snorkel: the south and west reefs have the smallest surf
    # (2 to 4 ft), today's two low tides, a range of 2.4 - -1.0 ft, and spring
    # tides near the full moon.
    dive = page["dive"]
    assert dive["calm"] == [("South", "Merizo, Cocos Lagoon"), ("West", "Tumon, Piti, Agat")]
    assert dive["calm_surf"] == "2 to 4 ft"
    lows = [(t["time"].astimezone(GUAM_TZ).hour, t["ft"]) for t in dive["lows"]]
    assert lows == [(1, -1.0), (13, 1.4)]
    assert dive["range"] == 3.4
    assert dive["cycle"] == "spring"
    # Guam's climate, this month (November) marked.
    climate = page["climate"]
    assert [m["month"] for m in climate["months"] if m["now"]] == [11]
    assert climate["wettest"]["month"] == 8 and climate["rain_year"] == 98
    assert climate["highs"] == (86, 89) and climate["lows"] == (75, 78)
    # No tides for the station: no tide parts.
    no_tides = weather.weather_page(reports, TIDES_NOW, "")
    assert no_tides["tide_station"] is None
    assert no_tides["dive"]["lows"] == [] and no_tides["dive"]["range"] is None


def test_calmest_reefs():
    assert weather.calm_reefs([["East", "7 to 9 ft"], ["West", "2 to 4 ft"]]) == ["West"]
    assert weather.calm_reefs([["North", "3 ft or less"], ["South", "2 to 4 ft"]]) == ["North"]
    # The same surf all round: no side is calmer.
    assert weather.calm_reefs([["East", "2 to 4 ft"], ["West", "2 to 4 ft"]]) == []
    assert weather.calm_reefs([]) == []


def test_spring_and_neap_tides():
    assert weather.dive_conditions(None, [], "New moon")["cycle"] == "spring"
    assert weather.dive_conditions(None, [], "Last quarter")["cycle"] == "neap"
    assert weather.dive_conditions(None, [], "Waxing gibbous")["cycle"] is None


def test_weather_page(client):
    _save_weather()
    page = client.get("/weather")
    assert page.status_code == 200
    html = page.text
    assert '<a href="/weather" aria-current="page">Weather</a>' in html
    for title in (
        "Right now",
        "Watches, warnings and advisories",
        "Next 24 hours",
        "Tides at Apra Harbor",
        "This week",
        "On the water",
        "Dive and snorkel",
        "Typhoons and tropical storms",
        "Guam's climate through the year",
        "Be ready",
    ):
        assert f">{title}</h2>" in html, title
    # Dive and snorkel, and the climate table with this month in bold.
    assert "<dt>Calmest reefs</dt><dd>South and west, 2 to 4 ft</dd>" in html
    assert "surf is smallest on the south and west reefs (Merizo, Cocos Lagoon;" in html
    month = datetime.now(timezone.utc).astimezone(GUAM_TZ).strftime("%B")
    assert f'<tr class="is-now">\n        <th scope="row">{month}<span' in html
    assert html.count('<tr class="is-now">') == 1
    assert "<td>17.7 in</td>" in html
    # The tide views switch with radio buttons: Day is the one shown first.
    assert 'id="tide-day" checked' in html and 'id="tide-month">' in html
    assert html.count('<tr class="is-today">') == 2  # the week and the month tables
    assert '<svg viewBox="0 0 720 190"' in html and 'class="tide-now"' in html
    assert "High risk of rip currents through Thursday afternoon" in html
    assert "Small craft advisory will expire at 5 AM ChST" in html
    assert "Typhoon 25W (Yagi) Warning #12" in html
    # Each source is credited with a link.
    for url in (
        "https://www.weather.gov/gum/",
        "https://www.metoc.navy.mil/jtwc/jtwc.html",
        "https://tidesandcurrents.noaa.gov/noaatidepredictions.html?id=1630000",
        "https://forecast.weather.gov/product.php?site=GUM&amp;issuedby=GUM&amp;product=SRF",
        "https://forecast.weather.gov/product.php?site=GUM&amp;issuedby=GUM&amp;product=CWF",
        "https://epa.guam.gov/current-beach-report/",
        "https://www.ncei.noaa.gov/access/us-climate-normals/"
        "#dataset=normals-monthly&amp;timeframe=30&amp;station=GQW00041415",
        # Flights: only a link, the airport's site turns away automated readers.
        "https://www.guamairport.com/passenger/flights/flight-schedule",
    ):
        assert f'href="{url}"' in html, url
    # Drawn on the server: the same policy as every page, no inline style.
    from guheadlines.web.app import CONTENT_SECURITY_POLICY

    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert " style=" not in html and "<script src" not in html
    # The home page's box links here and shows today's tides.
    home = client.get("/").text
    box = home.split('class="weather"', 1)[1].split("</section>", 1)[0]
    assert 'href="/weather"' in box and "<h3>Tides today</h3>" in box
    assert "Apra Harbor, NOAA ↗" in box


def test_weather_page_before_the_first_check(client):
    page = client.get("/weather")
    assert page.status_code == 200
    assert "No recent reading from the airport." in page.text
    assert "No tide predictions yet." in page.text
    assert "Sunrise" in page.text  # worked out here, so always there


def test_no_weather_page_when_the_weather_is_off(client, monkeypatch):
    from test_web import _set

    _set(monkeypatch, weather_interval_minutes=0)
    assert client.get("/weather").status_code == 404
    assert 'href="/weather"' not in client.get("/").text
    assert "/weather</loc>" not in client.get("/sitemap.xml").text


def test_weather_page_in_the_sitemap(client):
    assert "/weather</loc>" in client.get("/sitemap.xml").text
