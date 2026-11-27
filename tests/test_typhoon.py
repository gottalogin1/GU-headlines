"""The Typhoon page: JTWC warnings, formation alerts and outlook, GHS/OCD
releases (the COR level, shelters), the distances from Guam, the map, and
the page itself.

jtwc_quiet.rss, jtwc_abpw_quiet.txt (JTWC, 2026-11-27) and ghs.rss
(ghs.guam.gov/rss.xml, 2026-11-27) are real answers. No storm was active
when the tests were written, so jtwc_warning.txt, jtwc_tcfa.txt and
jtwc_abpw.txt are written by hand in JTWC's layout: a made-up Typhoon 25W
(Yagi) passing just north of Guam, with a storm that may form near Pohnpei."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import fixture_bytes

from guheadlines import typhoon, weather
from guheadlines.scraper.http import HttpClient

# The hand-written warning is for 00:00Z on 26 Nov 2026.
NOW = datetime(2026, 11, 26, 4, 0, tzinfo=timezone.utc)


def _text(name: str) -> str:
    return fixture_bytes(name).decode()


def _warning(now: datetime = NOW) -> dict:
    return typhoon.parse_jtwc_text(_text("jtwc_warning.txt"), now)


def test_a_jtwc_warning():
    w = _warning()
    assert w["kind"] == "warning"
    assert w["title"] == "Typhoon Yagi" and w["number"] == "25W" and w["warning_nr"] == 12
    assert w["time"] == "2026-11-26T00:00:00+00:00"
    assert w["issued"] == "2026-11-26T03:00:00+00:00"
    assert (w["lat"], w["lon"]) == (12.0, 150.5)
    assert (w["wind_kt"], w["gust_kt"]) == (85, 105)
    assert (w["move_dir"], w["move_kt"]) == (285, 12)
    # The farthest reach of each wind speed, from the present winds only.
    assert w["radii"] == {"64": 25, "50": 50, "34": 140}
    assert (w["pressure_mb"], w["waves_ft"]) == (960, 28)
    assert w["next"] == "2026-11-26T09:00:00+00:00"
    assert not w["final"]
    assert [f["hours"] for f in w["forecast"]] == [12, 24, 36, 48, 72, 96, 120]
    # The 120-hour forecast is on 1 December: the month comes from the time.
    assert w["forecast"][-1] == {
        "hours": 120,
        "time": "2026-12-01T00:00:00+00:00",
        "lat": 19.8,
        "lon": 131.8,
        "wind_kt": 115,
        "gust_kt": 140,
        "note": "",
    }


def test_depressions_final_warnings_and_dissipating_storms():
    text = (
        _text("jtwc_warning.txt")
        .replace("TYPHOON 25W (YAGI)", "TROPICAL DEPRESSION 25W (TWENTYFIVE)")
        .replace("NEXT\nWARNINGS AT", "THIS IS THE FINAL WARNING ON THIS SYSTEM. NEXT WARNINGS AT")
        .replace(
            "MAX SUSTAINED WINDS - 115 KT, GUSTS 140 KT",
            "MAX SUSTAINED WINDS - 025 KT, GUSTS 035 KT\n"
            "   DISSIPATED AS A SIGNIFICANT TROPICAL CYCLONE OVER WATER",
        )
    )
    w = typhoon.parse_jtwc_text(text, NOW)
    assert w["title"] == "Tropical depression 25W"
    assert w["storm_name"] == "" and w["final"]
    assert w["forecast"][-1]["note"] == "Dissipating"


def test_a_formation_alert():
    a = typhoon.parse_jtwc_text(_text("jtwc_tcfa.txt"), NOW)
    assert a["kind"] == "alert" and a["number"] == "96W"
    assert (a["lat"], a["lon"]) == (8.2, 157.6)
    assert (a["wind_low_kt"], a["wind_kt"]) == (25, 30)
    assert a["line"] == {"width_nm": 120, "start": [8.1, 158.0], "end": [9.6, 153.9]}
    assert a["until"] == "2026-11-27T04:00:00+00:00"
    with pytest.raises(typhoon.StormTextError):
        typhoon.parse_jtwc_text("SUBJ/SOMETHING ELSE//", NOW)
    with pytest.raises(typhoon.StormTextError):
        typhoon.parse_jtwc_text("not a JTWC text", NOW)


def test_jtwc_times_and_positions():
    # A JTWC time has no month: the one nearest to "now" is taken.
    end_of_month = datetime(2026, 11, 30, 18, tzinfo=timezone.utc)
    assert typhoon.jtwc_time("010000", end_of_month) == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert typhoon.jtwc_time("291800", end_of_month).month == 11
    new_year = datetime(2026, 12, 31, 12, tzinfo=timezone.utc)
    assert typhoon.jtwc_time("020600", new_year).year == 2027
    # South and west, as degrees north and east (west past 180).
    assert typhoon.latlon("12.1", "S", "170.3", "W") == (-12.1, 189.7)


def test_distances_from_guam():
    saipan = (15.18, 145.75)
    miles = typhoon.distance_miles(typhoon.GUAM, saipan)
    assert 125 < miles < 140
    assert typhoon.compass(typhoon.bearing(typhoon.GUAM, saipan)) == "north-northeast"
    assert typhoon.compass(359) == "north" and typhoon.compass(100) == "east"
    assert typhoon.strength(30) == "Tropical depression"
    assert typhoon.strength(64) == "Typhoon" and typhoon.strength(130) == "Super typhoon"
    assert typhoon.category(63) is None and typhoon.category(115) == 4
    assert typhoon.mph(100) == 115


def test_how_close_the_forecast_comes():
    points = typhoon._points(_warning())
    closest = typhoon.closest_approach(points)
    # Between the 24 and 48 hour forecasts, about 40 miles north of Guam.
    assert 35 <= closest["miles"] <= 50
    assert closest["where"] == "north-northeast"
    assert datetime(2026, 11, 27, tzinfo=timezone.utc) < closest["time"]
    assert closest["time"] < datetime(2026, 11, 27, 18, tzinfo=timezone.utc)
    assert not closest["now"] and closest["wind_mph"] > 120
    # A storm moving away is closest now.
    away = typhoon.closest_approach(list(reversed(points[:3])))
    assert away["now"]


def test_the_jtwc_outlook():
    outlook = typhoon.parse_outlook(_text("jtwc_abpw.txt"))
    assert outlook["issued"] == "2026-11-26T06:00:00+00:00"
    # 95W has faded; 98P is in the South Pacific: both left out.
    assert [(a["name"], a["chance"], a["alert"]) for a in outlook["areas"]] == [
        ("Invest 96W", "high", True),
        ("Invest 97W", "low", False),
    ]
    # Where 96W is now, not where it was.
    assert (outlook["areas"][0]["lat"], outlook["areas"][0]["lon"]) == (8.2, 157.6)
    assert outlook["areas"][1]["wind_kt"] == 15
    # The real advisory of a quiet day.
    quiet = typhoon.parse_outlook(_text("jtwc_abpw_quiet.txt"))
    assert quiet == {"issued": "2026-11-27T06:00:00+00:00", "areas": []}
    with pytest.raises(typhoon.StormTextError):
        typhoon.parse_outlook("ABPW10 PGTW 270600\nNNNN")


def test_ghs_releases():
    releases = typhoon.parse_ghs(fixture_bytes("ghs.rss"))
    assert len(releases) == 10
    assert releases[0] == {
        "title": "Small Craft Advisory and Rip Current Risk Warning",
        "url": "https://ghs.guam.gov/small-craft-advisory-and-rip-current-risk-warning",
        "time": "2026-11-22T22:59:04+00:00",
    }
    assert releases[1]["title"] == "High Surf Advisory & Rip Current Statement Extended"
    with pytest.raises(typhoon.StormTextError):
        typhoon.parse_ghs(b"<html><body>Maintenance</body></html>")


def _release(title: str, hours_ago: float, now: datetime = NOW) -> dict:
    when = now - timedelta(hours=hours_ago)
    slug = title.lower().replace(" ", "-")[:40]
    return {"title": title, "url": f"https://ghs.guam.gov/{slug}", "time": when.isoformat()}


def test_the_cor_level_comes_from_the_newest_ghs_release():
    # No release names a level: Guam is in COR 4, as all year.
    real = typhoon.parse_ghs(fixture_bytes("ghs.rss"))
    level = typhoon.cor_level(real, datetime(2026, 11, 27, tzinfo=timezone.utc))
    assert level["level"] == 4 and level["release"] is None
    releases = [
        _release("JIC Release No. 6 - Guam in COR 2; Damaging Winds Expected", 2),
        _release("JIC Release No. 2 - Guam in COR 3; Stay Out of the Water; Prepare Now", 30),
    ]
    level = typhoon.cor_level(releases, NOW)
    assert level["level"] == 2 and "24 hours" in level["meaning"]
    assert level["release"]["title"].startswith("JIC Release No. 6")
    # The last level named in a title, and COR 1's two stages.
    for title, found in (
        ("Guam moves from COR 3 to COR 2", (2, "")),
        ("Guam in COR 1-Emergency; Stay Indoors", (1, "Emergency")),
        ("COR 1 Caution in effect", (1, "Caution")),
        ("JIC Release No. 8 - Guam in COR 4 at 10 a.m., All Clear", (4, "")),
    ):
        level = typhoon.cor_level([_release(title, 1)], NOW)
        assert (level["level"], level["extra"]) == found, title
    assert typhoon.cor_level([_release("Guam in COR 4, All Clear", 1)], NOW)["all_clear"]
    # A level from more than COR_DAYS ago no longer counts.
    old = [_release("Guam in COR 1", 24 * (typhoon.COR_DAYS + 1))]
    assert typhoon.cor_level(old, NOW)["level"] == 4


def test_paths_are_kept_between_checks():
    first = [{**_warning(), "key": "wp2524"}]
    typhoon.carry_tracks(first, None)
    assert [p["time"] for p in first[0]["track"]] == ["2026-11-26T00:00:00+00:00"]
    later = _warning(NOW + timedelta(hours=6))
    later.update(key="wp2524", time="2026-11-26T06:00:00+00:00", lat=12.3, lon=149.5)
    typhoon.carry_tracks([later], first)
    assert [(p["lat"], p["lon"]) for p in later["track"]] == [(12.0, 150.5), (12.3, 149.5)]
    # The same warning read twice is one point.
    typhoon.carry_tracks([later], [later])
    assert len(later["track"]) == 2


def test_the_map_grows_to_show_every_storm():
    quiet = typhoon.track_map([], [])
    assert quiet["width"] == typhoon.MAP_WIDTH
    assert 0.5 * quiet["width"] <= quiet["height"] <= quiet["width"]
    assert [r["miles"] for r in quiet["rings"]] == [100, 300, 500]
    names = {p["name"] for p in quiet["places"]}
    assert {"Saipan", "Yap", "Chuuk"} <= names and "Tokyo" not in names
    storm = typhoon._storm_view({**_warning(), "key": "wp2524"}, NOW)
    drawn = typhoon.track_map([storm], [])
    # The forecast reaches 131.8°E, west of the smallest map (132°E).
    assert quiet["grid"]["lons"][0]["label"] == "135°E"
    assert drawn["grid"]["lons"][0]["label"] == "130°E"
    s = drawn["storms"][0]
    assert s["class"] == "is-typhoon" and s["wind_r"] > 0
    assert len(s["dots"]) == 7 and sum(d["daily"] for d in s["dots"]) == 5
    assert all(d["label"] for d in s["dots"] if d["daily"])
    for x, y in (pair.split(",") for pair in s["ahead"].split()):
        assert 0 <= float(x) <= drawn["width"] and 0 <= float(y) <= drawn["height"]


def _label_boxes(m: dict) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Each label on a map, and roughly where its text is."""
    found = [("Guam", m["guam"]["label"], 14)]
    found += [(f"{r['miles']} mi", r["label"], 11) for r in m["rings"] if r["label"]]
    found += [(p["name"], p["label"], 12) for p in m["places"]]
    found += [(a["name"], a["label"], 12) for a in m["areas"]]
    for s in m["storms"]:
        found.append((s["title"], s["label"], 13))
        found += [(d["text"], d["label"], 11) for d in s["dots"] if d["label"]]
    boxes = []
    for text, label, size in found:
        width = len(text) * size * 0.6
        left = {"start": 0, "middle": width / 2, "end": width}[label["anchor"]]
        x = label["x"] - left
        boxes.append((text, (x + 1, label["y"] - size + 1, x + width - 1, label["y"] - 1)))
    return boxes


def test_map_labels_fit_and_dont_cover_each_other():
    storm = typhoon._storm_view({**_warning(), "key": "wp2524"}, NOW)
    alert = typhoon._storm_view(
        {**typhoon.parse_jtwc_text(_text("jtwc_tcfa.txt"), NOW), "key": "wp9624"}, NOW
    )
    area = {"name": "Invest 97W", "lat": 9.5, "lon": 152.0, "chance": "low"}
    for m in (typhoon.track_map([], []), typhoon.track_map([storm, alert], [area])):
        boxes = _label_boxes(m)
        for text, (x1, y1, x2, y2) in boxes:
            assert 0 <= x1 and x2 <= m["width"] and 0 <= y1 and y2 <= m["height"], text
        for i, (a, ab) in enumerate(boxes):
            for b, bb in boxes[i + 1 :]:
                overlap = ab[0] < bb[2] and bb[0] < ab[2] and ab[1] < bb[3] and bb[1] < ab[3]
                assert not overlap, (a, b)
    # Labels that would cover another on a phone are hidden there.
    shown = typhoon.track_map([storm, alert], [area])
    assert not shown["storms"][0]["label"]["wide_only"]
    assert not shown["guam"]["label"]["wide_only"]


def test_a_storm_near_guam():
    storm = {**_warning(), "key": "wp2524", "name": "Typhoon 25W (Yagi) Warning #12"}
    assert typhoon.storm_near({"warnings": [], "storms": [storm]})
    far = {**storm, "lat": 25.0, "lon": 128.0, "forecast": []}
    assert not typhoon.storm_near({"warnings": [], "storms": [far]})
    assert typhoon.storm_near({"warnings": [{"event": "Tropical Storm Watch"}], "storms": []})
    assert not typhoon.storm_near({"warnings": [{"event": "Flood Watch"}], "storms": None})
    assert not typhoon.storm_near(None)


# ------------------------------------------------------------ with a database

ACTIVE = (
    fixture_bytes("jtwc.rss")
    .decode()
    .replace(
        "<ul><li><font color='red'>No Current Tropical Cyclone Warnings.</font></li></ul>",
        "<p><b>Typhoon 25W (Yagi) Warning #12 </b><br><b>Issued at 26/0300Z<b><ul>"
        "<li><a href='https://www.metoc.navy.mil/jtwc/products/wp2524web.txt'"
        " target='newwin'>TC Warning Text </a></li></ul>"
        "<b>Tropical Cyclone Formation Alert WTPN21 <b><br><b>Issued at 26/0400Z<b><ul>"
        "<li><a href='https://www.metoc.navy.mil/jtwc/products/wp9624web.txt'"
        " target='newwin'>TCFA Text </a></li></ul>",
        1,
    )
)


def _fake_jtwc(warning: bytes | None = None):
    files = {
        "/jtwc/rss/jtwc.rss": ACTIVE.encode(),
        "/jtwc/products/wp2524web.txt": warning or fixture_bytes("jtwc_warning.txt"),
        "/jtwc/products/wp9624web.txt": fixture_bytes("jtwc_tcfa.txt"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(request.url.path)
        return httpx.Response(200, content=body) if body else httpx.Response(404)

    return HttpClient(
        "test-agent",
        per_host_delay=0,
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )


def test_the_worker_reads_each_storms_warning(clean_db):
    storms = weather.read_storms(_fake_jtwc())
    assert [s["key"] for s in storms] == ["wp2524", "wp9624"]
    yagi, alert = storms
    assert yagi["title"] == "Typhoon Yagi" and yagi["wind_kt"] == 85
    assert len(yagi["track"]) == 1
    assert alert["kind"] == "alert" and alert["number"] == "96W"
    weather.save("storms", storms)
    # The same warning read again adds nothing to the path.
    again = weather.read_storms(_fake_jtwc())
    assert len(again[0]["track"]) == 1
    # The next warning adds the new center.
    moved = fixture_bytes("jtwc_warning.txt").replace(
        b"260000Z --- NEAR 12.0N 150.5E", b"260600Z --- NEAR 12.3N 149.5E"
    )
    later = weather.read_storms(_fake_jtwc(moved))
    assert [(p["lat"], p["lon"]) for p in later[0]["track"]] == [(12.0, 150.5), (12.3, 149.5)]


def test_a_warning_that_cannot_be_read_keeps_the_storm(clean_db):
    files = {
        "/jtwc/rss/jtwc.rss": ACTIVE.encode(),
        "/jtwc/products/wp2524web.txt": b"Service unavailable",
    }
    transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(200, content=files[request.url.path])
            if request.url.path in files
            else httpx.Response(503)
        )
    )
    client = HttpClient(
        "test-agent", per_host_delay=0, respect_robots=False, transport=transport, sleep=lambda s: 0
    )
    storms = weather.read_storms(client)
    assert storms[0]["name"] == "Typhoon 25W (Yagi) Warning #12" and "lat" not in storms[0]


def _moved(storm: dict, to: datetime) -> dict:
    """The storm with its times moved so that its warning is for `to`."""
    shift = to - datetime.fromisoformat(storm["time"])

    def move(value):
        return (datetime.fromisoformat(value) + shift).isoformat() if value else value

    storm = {**storm, "time": move(storm["time"]), "issued": move(storm.get("issued"))}
    for key in ("next", "until"):
        if key in storm:
            storm[key] = move(storm[key])
    storm["forecast"] = [{**f, "time": move(f["time"])} for f in storm.get("forecast", [])]
    storm["track"] = [{**p, "time": move(p["time"])} for p in storm.get("track", [])]
    return storm


def _save_storm_season(now: datetime) -> None:
    from guheadlines.db import session_scope
    from guheadlines.models import Article, Source

    yagi = {
        **_warning(),
        "name": "Typhoon 25W (Yagi) Warning #12",
        "url": "https://www.metoc.navy.mil/jtwc/products/wp2524web.txt",
        "key": "wp2524",
        "track": [
            {"time": "2026-11-25T12:00:00+00:00", "lat": 11.4, "lon": 152.6, "wind_kt": 65},
            {"time": "2026-11-25T18:00:00+00:00", "lat": 11.7, "lon": 151.5, "wind_kt": 75},
            {"time": "2026-11-26T00:00:00+00:00", "lat": 12.0, "lon": 150.5, "wind_kt": 85},
        ],
    }
    alert = {
        **typhoon.parse_jtwc_text(_text("jtwc_tcfa.txt"), NOW),
        "name": "Tropical Cyclone Formation Alert",
        "url": "https://www.metoc.navy.mil/jtwc/products/wp9624web.txt",
        "key": "wp9624",
    }
    weather.save("storms", [_moved(yagi, now - timedelta(hours=2)), _moved(alert, now)])
    outlook = typhoon.parse_outlook(_text("jtwc_abpw.txt"))
    outlook["issued"] = (now - timedelta(hours=3)).isoformat()
    weather.save("outlook", outlook)
    weather.save(
        "ghs",
        [
            _release(
                "GHS/OCD Public Advisory No. 6 - Emergency Shelters to Open at 6 p.m.", 1, now
            ),
            _release(
                "JIC Release No. 2 - Guam in COR 3; Stay Out of the Water; Prepare Now", 3, now
            ),
            _release("FEMA Monitoring Results", 24 * 40, now),
        ],
    )
    alerts = weather.parse_alerts(
        {
            "features": [
                {
                    "properties": {
                        "event": "Typhoon Watch",
                        "severity": "Severe",
                        "areaDesc": "Guam",
                        "ends": (now + timedelta(hours=12)).isoformat(),
                        "description": "* WHAT...Typhoon conditions possible within 48 hours.",
                    }
                },
                {
                    "properties": {
                        "event": "Small Craft Advisory",
                        "severity": "Minor",
                        "areaDesc": "Guam Coastal Waters",
                        "ends": (now + timedelta(hours=12)).isoformat(),
                        "description": "Winds 25 knots.",
                    }
                },
            ]
        }
    )
    weather.save("alerts", alerts)
    with session_scope() as session:
        source = Source(slug="kuam", name="KUAM News", homepage="https://www.kuam.com/")
        session.add(source)
        session.flush()
        for hours, title in (
            (1, "Typhoon Yagi could pass just north of Guam on Friday"),
            (2, "Emergency shelters open at 6 p.m. as Guam goes to COR 3"),
            (3, "Guam Chamber of Commerce typhoon fair postponed"),  # muted
            (4, "Minimum wage hearing set for Monday"),
            (24 * 9, "Tropical storm forms far to the east"),  # older than a week
        ):
            session.add(
                Article(
                    source_id=source.id,
                    url=f"https://www.kuam.com/story/{hours}",
                    title=title,
                    categories=["local"],
                    published_at=now - timedelta(hours=hours),
                )
            )


@pytest.fixture()
def client(clean_db):
    from fastapi.testclient import TestClient

    from guheadlines.web.app import app, llms_cache, sidebar_cache, sitemap_cache, weather_cache

    caches = (sidebar_cache, sitemap_cache, weather_cache, llms_cache)
    for cache in caches:
        cache._data.clear()
    yield TestClient(app)
    for cache in caches:  # the next test file must not see this one's pages
        cache._data.clear()


def test_typhoon_page_during_a_storm(client):
    now = datetime.now(timezone.utc)
    _save_storm_season(now)
    page = client.get("/typhoon")
    assert page.status_code == 200
    html = page.text
    assert "<h1>Guam typhoon tracker</h1>" in html
    assert "<title>Guam typhoon tracker: storms near Guam, COR level and shelters" in html
    # The COR level from the GHS/OCD release, and the NWS watch, open.
    assert '<div class="ty-cor ty-cor-3">' in html
    assert '<span class="wx-big">COR 3</span>' in html
    assert "Damaging winds of 39 mph or more are possible within 48 hours." in html
    assert '<details class="wx-alert is-warning" open>' in html
    assert "<strong>Typhoon Watch</strong> for Guam" in html
    assert "Small Craft Advisory" not in html.split('id="now"')[1].split("</section>")[0]
    # The storm in words, on the map and in its own card.
    assert '<strong><a href="#wp2524">Typhoon Yagi</a></strong> is 3' in html
    assert "miles east-southeast of Guam, moving west-northwest at 14 mph" in html
    assert "JTWC's forecast takes it closest to Guam, about 4" in html
    assert '<strong><a href="#wp9624">A storm may form</a></strong>' in html
    assert '<svg viewBox="0 0 720 ' in html and 'class="ty-storm is-typhoon"' in html
    assert 'class="ty-past"' in html and 'class="ty-ahead"' in html
    assert 'class="ty-alert-band"' in html
    assert '<article class="ty-storm-card" id="wp2524"' in html
    assert '<span class="wx-mid">98 mph</span>' in html
    assert "Typhoon, like a Category 2 hurricane" in html
    assert "<dt>Winds of 39+ mph</dt><dd>Up to 161 miles out</dd>" in html
    assert html.count('<tr>\n          <th scope="row">') == 7  # the forecast table
    assert "The path so far is from 3 warnings read here." in html
    # 96W has its own alert: listed once. 97W is an area to watch.
    watch = html.split('id="watch"')[1].split("</section>")[0]
    assert "Invest 97W" in watch and "Invest 96W" not in watch
    assert '<span class="ty-chance is-low">low chance</span>' in watch
    # Shelters and storm news; muted and old stories are left out.
    shelters = html.split('id="shelters"')[1].split("</section>")[0]
    assert "Emergency Shelters to Open at 6 p.m." in shelters
    assert "Emergency shelters open at 6 p.m. as Guam goes to COR 3" in shelters
    news = html.split('id="news"')[1].split("</section>")[0]
    assert "Typhoon Yagi could pass just north of Guam" in news
    assert "Chamber" not in html and "Minimum wage" not in news
    assert "Tropical storm forms far to the east" not in news
    assert "FEMA Monitoring Results" not in html
    # The COR 3 step is marked as now.
    assert '<li class="ty-cor-3 is-now">' in html
    # Drawn on the server: the same policy as every page, no inline style.
    from guheadlines.web.app import CONTENT_SECURITY_POLICY

    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert " style=" not in html and "<script src" not in html
    assert "github" not in html.lower()
    # The home page's box, the banner and the Weather page point here.
    home = client.get("/").text
    assert '<a class="weather-storm" href="/typhoon">Typhoon tracker →</a>' in home
    assert '<a class="weather-storm" href="/typhoon#wp2524">Typhoon Yagi</a>' in home
    assert 'href="/typhoon">The storm on a map, the COR level and shelters' in home
    weather_page = client.get("/weather").text
    assert '<a href="/typhoon#wp2524">Typhoon Yagi</a>' in weather_page
    assert 'href="/typhoon">Typhoon tracker: storm map' in weather_page


def test_typhoon_page_on_a_quiet_day(client):
    now = datetime.now(timezone.utc)
    weather.save("storms", weather.parse_storms(fixture_bytes("jtwc_quiet.rss")))
    weather.save(
        "outlook",
        {**typhoon.parse_outlook(_text("jtwc_abpw_quiet.txt")), "issued": now.isoformat()},
    )
    weather.save("ghs", typhoon.parse_ghs(fixture_bytes("ghs.rss")))
    weather.save("alerts", [])
    html = client.get("/typhoon").text
    assert '<span class="wx-big">COR 4</span>' in html
    assert "Unless the Governor raises it." in html
    assert "No typhoon or tropical storm watches or warnings for Guam." in html
    assert "No tropical storms or typhoons in the western Pacific right now." in html
    assert "JTWC is not watching any area for a new storm" in html
    assert "No shelters are open" in html
    assert 'id="news"' not in html
    assert '<li class="ty-cor-4 is-now">' in html
    # The quiet map: Guam, the rings and the islands, no storm.
    assert 'class="ty-guam"' in html and 'class="ty-storm' not in html
    home = client.get("/").text
    assert "None in the western Pacific" in home
    assert '<a class="muted" href="/typhoon">Typhoon tracker →</a>' in home


def test_typhoon_page_before_the_first_check(client):
    page = client.get("/typhoon")
    assert page.status_code == 200
    assert "No recent word from the Joint Typhoon Warning Center." in page.text
    assert "No recent outlook from JTWC." in page.text
    assert '<span class="wx-big">COR 4</span>' in page.text


def test_typhoon_page_is_listed_and_goes_with_the_weather(client, monkeypatch):
    from test_web import _set

    assert "/typhoon</loc>" in client.get("/sitemap.xml").text
    assert 'href="/typhoon">Guam typhoon tracker</a>' in client.get("/").text
    assert "/typhoon)" in client.get("/llms.txt").text
    _set(monkeypatch, weather_interval_minutes=0)
    from guheadlines.web.app import sitemap_cache

    sitemap_cache._data.clear()
    assert client.get("/typhoon").status_code == 404
    assert 'href="/typhoon"' not in client.get("/").text
    assert "/typhoon</loc>" not in client.get("/sitemap.xml").text


def test_storm_news_query(clean_db):
    from guheadlines.db import session_scope
    from guheadlines.web import queries

    now = datetime.now(timezone.utc)
    _save_storm_season(now)
    with session_scope() as session:
        found = queries.storm_stories(
            session, now - timedelta(days=7), mute=("chamber of commerce", "guam chamber")
        )
    assert [s["title"] for s in found] == [
        "Typhoon Yagi could pass just north of Guam on Friday",
        "Emergency shelters open at 6 p.m. as Guam goes to COR 3",
    ]
