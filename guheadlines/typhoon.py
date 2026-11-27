"""The Typhoon page (/typhoon): storms near Guam, how close they may come,
the COR level, shelters and what to do.

The weather worker (weather.py) reads these public sources with the rest of
the weather, every WEATHER_INTERVAL_MINUTES, and saves the last good copy of
each in the weather table:

- storms:  each western Pacific storm in the Joint Typhoon Warning Center
           (JTWC) feed, with its latest warning: where its center is, its
           winds, where it is heading and JTWC's forecast for the next five
           days. Each check adds the center to the storm's path so far.
           A Tropical Cyclone Formation Alert (a storm that may form within
           a day) gives the area where it may form.
- outlook: JTWC's Significant Tropical Weather Advisory (ABPW10): the areas
           that JTWC is watching because a storm may form there, each with
           its chance (low, medium or high) for the next day.
- ghs:     the newest releases of Guam Homeland Security / Office of Civil
           Defense (GHS/OCD), from its feed: the COR level, shelters, the all
           clear. Its robots.txt allows the feed.

This module reads those texts and works out what the page shows: how far
each storm is from Guam, how close JTWC's forecast takes it and when, and a
map drawn here (an SVG, with no picture from another site). It doesn't read
any site itself.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import pairwise

from lxml import etree

from .scraper.text import GUAM_TZ

# The middle of Guam.
GUAM = (13.444, 144.794)
EARTH_MILES = 3958.8
KNOTS_TO_MPH = 1.15078
NM_TO_MILES = 1.15078

JTWC_OUTLOOK_URL = "https://www.metoc.navy.mil/jtwc/products/abpwweb.txt"
GHS_FEED = "https://ghs.guam.gov/rss.xml"
GHS_URL = "https://ghs.guam.gov/"
GHS_NEWS_URL = "https://ghs.guam.gov/news"
NWS_TROPICAL_URL = "https://www.weather.gov/gum/tropical"
READY_URL = "https://www.ready.gov/hurricanes"

# A storm that JTWC's forecast takes this close to Guam (or closer) is "near
# Guam": the page says so first, and the weather box and banner point to it.
NEAR_MILES = 500
# The rings around Guam on the map.
RINGS_MILES = (100, 300, 500)
# The path so far keeps this many centers (a storm lasts about two weeks, and
# JTWC writes a warning every six hours).
TRACK_POINTS = 80
# The page shows GHS/OCD releases from the last month, and takes the COR
# level from those of the last COR_DAYS days.
GHS_DAYS = 30
COR_DAYS = 10
# Shelter releases and news are shown for this many days.
SHELTER_DAYS = 7
# The JTWC outlook is written once a day, and again when something changes.
OUTLOOK_STALE_AFTER = timedelta(hours=36)

COMPASS_WORDS = (
    "north",
    "north-northeast",
    "northeast",
    "east-northeast",
    "east",
    "east-southeast",
    "southeast",
    "south-southeast",
    "south",
    "south-southwest",
    "southwest",
    "west-southwest",
    "west",
    "west-northwest",
    "northwest",
    "north-northwest",
)

# What each Condition of Readiness (COR) means. The Governor sets the level
# on the advice of GHS/OCD and the National Weather Service. Damaging winds
# are winds of 39 mph or more.
COR_LEVELS = {
    4: "Damaging winds are possible within 72 hours. Guam stays in COR 4 all year.",
    3: "Damaging winds of 39 mph or more are possible within 48 hours.",
    2: "Damaging winds are possible within 24 hours.",
    1: "Damaging winds are possible within 12 hours. Stay indoors until officials say it is safe.",
}

# Places on the map: (latitude, longitude, name). Only those inside the map
# are drawn.
PLACES = (
    (15.18, 145.75, "Saipan"),
    (9.53, 138.13, "Yap"),
    (7.34, 134.48, "Palau"),
    (7.45, 151.85, "Chuuk"),
    (6.88, 158.22, "Pohnpei"),
    (5.33, 163.01, "Kosrae"),
    (8.72, 167.73, "Kwajalein"),
    (7.09, 171.38, "Majuro"),
    (19.29, 166.65, "Wake Island"),
    (24.78, 141.32, "Iwo To"),
    (27.09, 142.19, "Chichijima"),
    (26.33, 127.80, "Okinawa"),
    (23.70, 121.00, "Taiwan"),
    (14.60, 120.98, "Manila"),
    (10.31, 123.89, "Cebu"),
    (35.68, 139.69, "Tokyo"),
    (22.30, 114.17, "Hong Kong"),
)

_LATLON = r"(\d{1,2}(?:\.\d)?)([NS])\s*(\d{1,3}(?:\.\d)?)([EW])"
_NUMBER_WORDS = re.compile(
    r"^(ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|ELEVEN|TWELVE|THIRTEEN|"
    r"FOURTEEN|FIFTEEN|SIXTEEN|SEVENTEEN|EIGHTEEN|NINETEEN|TWENTY|THIRTY|FORTY|FIFTY)"
)


class StormTextError(ValueError):
    """A JTWC or GHS/OCD text is not in the layout this module reads."""


# ------------------------------------------------------------------ helpers


def _flat(text: str) -> str:
    """JTWC breaks its lines at about 70 characters, also in the middle of a
    sentence; the parsers read the text as one line."""
    return " ".join(text.split())


def latlon(lat: str, ns: str, lon: str, ew: str) -> tuple[float, float]:
    """A JTWC position ("15.2N", "148.9E") as degrees north and degrees east.
    West longitudes become more than 180 east, so a storm crossing the date
    line keeps a straight path."""
    north = float(lat) * (1 if ns == "N" else -1)
    east = float(lon) if ew == "E" else 360 - float(lon)
    return north, east


def jtwc_time(value: str, near: datetime) -> datetime:
    """A JTWC time: day, hour and minute in UTC ("060300" is the 6th at
    03:00Z), with no month. The month is the one that puts the time nearest
    to `near`."""
    day, hour, minute = int(value[:2]), int(value[2:4]), int(value[4:6])
    best = None
    for shift in (-1, 0, 1):
        month = near.month + shift
        year = near.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        try:
            when = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:  # the 31st of a short month
            continue
        if best is None or abs(when - near) < abs(best - near):
            best = when
    if best is None:
        raise StormTextError(f"not a JTWC time: {value}")
    return best


def distance_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    """The distance between two points (degrees north, degrees east), over
    the earth's surface, in miles."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_MILES * math.asin(math.sqrt(min(1.0, h)))


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """The compass direction from a to b, in degrees (0 is north, 90 east)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(degrees: float) -> str:
    """135 -> "southeast"."""
    return COMPASS_WORDS[round(degrees / 22.5) % 16]


def mph(knots: float | None) -> int | None:
    return None if knots is None else round(knots * KNOTS_TO_MPH)


def strength(knots: float | None) -> str:
    """JTWC's names for a storm of this strength (one-minute winds, knots)."""
    if knots is None:
        return "Tropical system"
    if knots >= 130:
        return "Super typhoon"
    if knots >= 64:
        return "Typhoon"
    if knots >= 34:
        return "Tropical storm"
    return "Tropical depression"


def category(knots: float | None) -> int | None:
    """The hurricane category (Saffir-Simpson scale) of a typhoon this
    strong, which news on Guam also gives: "like a Category 4 hurricane"."""
    if knots is None or knots < 64:
        return None
    for floor, number in ((137, 5), (113, 4), (96, 3), (83, 2)):
        if knots >= floor:
            return number
    return 1


def _strength_class(knots: float | None) -> str:
    """The map's color for a storm of this strength (style.css .ty-*)."""
    if knots is None or knots < 34:
        return "is-depression"
    if knots < 64:
        return "is-storm"
    if knots < 130:
        return "is-typhoon"
    return "is-super"


def _title_name(name: str) -> str:
    """ "MA-ON" -> "Ma-on"; JTWC names a depression by its number in words
    ("TWENTYFIVE"), which is left out."""
    return "" if _NUMBER_WORDS.match(name) else name.strip().capitalize()


# ----------------------------------------------------------- JTWC warnings

_ISSUED_RE = re.compile(r"\bWT[A-Z]{2}\d{2} PGTW (\d{6})\b")
_SUBJ_RE = re.compile(r"SUBJ/(.+?)//")
_STORM_RE = re.compile(
    r"^(SUPER TYPHOON|TYPHOON|TROPICAL STORM|TROPICAL DEPRESSION|TROPICAL CYCLONE|"
    r"SUBTROPICAL STORM|SUBTROPICAL DEPRESSION)\s+(\d{2}[A-Z])\s*(?:\(([A-Z0-9 -]+)\))?"
    r"\s+WARNING NR\s*(\d+)"
)
_POSITION_RE = re.compile(r"WARNING POSITION:\s*(\d{6})Z\s*-*\s*NEAR\s+" + _LATLON)
_MOVE_RE = re.compile(r"MOVEMENT PAST \w+ HOURS\s*-\s*(\d{3}) DEGREES AT (\d+) KTS")
_WINDS_RE = re.compile(r"MAX SUSTAINED WINDS\s*-\s*(\d+) KT,\s*GUSTS\s*(\d+) KT")
_RADIUS_RE = re.compile(r"RADIUS OF (\d{3}) KT WINDS\s*-\s*((?:\d{3} NM [A-Z ]*?QUADRANT\s*)+)")
_FORECAST_RE = re.compile(
    r"(\d+) HRS, VALID AT:\s*(\d{6})Z\s*-*\s*" + _LATLON + r"(.*?)(?=\d+ HRS, VALID AT:|REMARKS:|$)"
)
_PRESSURE_RE = re.compile(r"MINIMUM CENTRAL PRESSURE AT \d{6}Z IS (\d{3,4}) MB")
_WAVES_RE = re.compile(r"MAXIMUM SIGNIFICANT WAVE HEIGHT AT \d{6}Z IS (\d+) FEET")
_NEXT_RE = re.compile(r"NEXT WARNINGS? AT (\d{6})Z")
# Tropical Cyclone Formation Alerts.
_INVEST_RE = re.compile(r"\bINVEST (\d{2}[A-Z])\b")
_ALERT_LINE_RE = re.compile(
    r"WITHIN (\d+) NM EITHER SIDE OF A LINE FROM " + _LATLON + r" TO " + _LATLON
)
_ALERT_CENTER_RE = re.compile(r"(?:CIRCULATION CENTER IS|NOW) LOCATED NEAR " + _LATLON)
_ALERT_WINDS_RE = re.compile(r"WINDS IN THE AREA ARE ESTIMATED TO BE (\d+) TO (\d+) KNOTS")
_ALERT_UNTIL_RE = re.compile(r"(?:CANCELLED|CANCELED) BY (\d{6})Z")


def _radii(block: str) -> dict[str, int]:
    """The farthest reach of 34, 50 and 64 knot winds (any quadrant), in
    nautical miles: {"34": 120, "64": 30}."""
    found = {}
    for speed, quadrants in _RADIUS_RE.findall(block):
        reach = [int(n) for n in re.findall(r"(\d{3}) NM", quadrants)]
        if reach:
            found[str(int(speed))] = max(reach)
    return found


def parse_jtwc_text(text: str, now: datetime) -> dict:
    """A JTWC warning (WTPN3x) or Tropical Cyclone Formation Alert (WTPN2x),
    as JTWC writes it in its "web.txt" products. Times are worked out near
    `now` (see jtwc_time)."""
    flat = _flat(text)
    subject = _SUBJ_RE.search(flat)
    if not subject:
        raise StormTextError("the JTWC text has no subject line")
    subj = subject[1].strip()
    issued = _ISSUED_RE.search(flat)
    issued_at = jtwc_time(issued[1], now) if issued else None
    if subj.startswith("TROPICAL CYCLONE FORMATION ALERT"):
        return _parse_alert(flat, subj, issued_at, now)
    storm = _STORM_RE.search(subj)
    position = _POSITION_RE.search(flat)
    if not storm or not position:
        raise StormTextError(f"not a JTWC warning: {subj[:80]}")
    kind, number, name, warning_nr = storm.groups()
    when = jtwc_time(position[1], now)
    lat, lon = latlon(*position.groups()[1:])
    forecasts_at = flat.find("FORECASTS:")
    present = flat[position.end() : forecasts_at if forecasts_at > 0 else len(flat)]
    winds = _WINDS_RE.search(present)
    move = _MOVE_RE.search(present)
    forecast = []
    remarks_at = flat.find("REMARKS:")
    forecast_text = flat[forecasts_at:remarks_at] if forecasts_at > 0 else ""
    for hours, valid, *place, rest in _FORECAST_RE.findall(forecast_text):
        point_winds = _WINDS_RE.search(rest)
        note = ""
        if "DISSIPAT" in rest:
            note = "Dissipating"
        elif "EXTRATROPICAL" in rest:
            note = "Becoming extratropical"
        f_lat, f_lon = latlon(*place)
        forecast.append(
            {
                "hours": int(hours),
                "time": jtwc_time(valid, when).isoformat(),
                "lat": f_lat,
                "lon": f_lon,
                "wind_kt": int(point_winds[1]) if point_winds else None,
                "gust_kt": int(point_winds[2]) if point_winds else None,
                "note": note,
            }
        )
    remarks = flat[remarks_at:] if remarks_at > 0 else ""
    pressure = _PRESSURE_RE.search(remarks)
    waves = _WAVES_RE.search(remarks)
    upcoming = _NEXT_RE.search(remarks)
    storm_name = _title_name(name or "")
    kind = kind.capitalize()
    return {
        "kind": "warning",
        "type": kind,
        "number": number,
        "storm_name": storm_name,
        "title": f"{kind} {storm_name}" if storm_name else f"{kind} {number}",
        "warning_nr": int(warning_nr),
        "issued": issued_at.isoformat() if issued_at else None,
        "time": when.isoformat(),
        "lat": lat,
        "lon": lon,
        "wind_kt": int(winds[1]) if winds else None,
        "gust_kt": int(winds[2]) if winds else None,
        "move_dir": int(move[1]) if move else None,
        "move_kt": int(move[2]) if move else None,
        "radii": _radii(present),
        "pressure_mb": int(pressure[1]) if pressure else None,
        "waves_ft": int(waves[1]) if waves else None,
        "next": jtwc_time(upcoming[1], when).isoformat() if upcoming else None,
        "final": "FINAL WARNING" in flat,
        "forecast": forecast,
    }


def _parse_alert(flat: str, subj: str, issued_at: datetime | None, now: datetime) -> dict:
    invest = _INVEST_RE.search(subj) or _INVEST_RE.search(flat)
    center = _ALERT_CENTER_RE.search(flat)
    line = _ALERT_LINE_RE.search(flat)
    if not center and not line:
        raise StormTextError("the JTWC alert gives no position")
    winds = _ALERT_WINDS_RE.search(flat)
    until = _ALERT_UNTIL_RE.search(flat)
    if center:
        lat, lon = latlon(*center.groups())
    else:  # the start of the line where it may form
        lat, lon = latlon(*line.groups()[1:5])
    number = invest[1] if invest else ""
    return {
        "kind": "alert",
        "type": "Tropical Cyclone Formation Alert",
        "number": number,
        "storm_name": "",
        "title": f"Possible storm {number}".strip(),
        "issued": issued_at.isoformat() if issued_at else None,
        "time": (issued_at or now).isoformat(),
        "lat": lat,
        "lon": lon,
        "wind_kt": int(winds[2]) if winds else None,
        "wind_low_kt": int(winds[1]) if winds else None,
        "line": (
            {
                "width_nm": int(line[1]),
                "start": list(latlon(*line.groups()[1:5])),
                "end": list(latlon(*line.groups()[5:9])),
            }
            if line
            else None
        ),
        "until": jtwc_time(until[1], issued_at or now).isoformat() if until else None,
        "forecast": [],
    }


def storm_key(url: str) -> str:
    """JTWC's name for a storm's products: wp2524 is storm 25W of 2024."""
    match = re.search(r"/(wp\d{4})(?:web\.txt|\.gif|\.tcw)?$", url or "")
    return match[1] if match else ""


def carry_tracks(storms: list[dict], saved: list[dict] | None) -> list[dict]:
    """Each storm's path so far: its centers from earlier checks (`saved`,
    the last saved storm list) and from this one, oldest first."""
    earlier = {s.get("key"): s.get("track") or [] for s in saved or [] if s.get("key")}
    for storm in storms:
        if storm.get("kind") != "warning" or not storm.get("key"):
            continue
        track = {p["time"]: p for p in earlier.get(storm["key"], [])}
        track[storm["time"]] = {
            "time": storm["time"],
            "lat": storm["lat"],
            "lon": storm["lon"],
            "wind_kt": storm["wind_kt"],
        }
        storm["track"] = [track[t] for t in sorted(track)][-TRACK_POINTS:]
    return storms


# -------------------------------------------------------- JTWC outlook

_OUTLOOK_ISSUED_RE = re.compile(r"/(\d{6})Z-\d{6}Z([A-Z]{3})(\d{4})//")
_CHANCE_RE = re.compile(
    r"WITHIN THE NEXT (\d+) HOURS (?:IS|REMAINS)\s+(?:[A-Z]+\s+TO\s+)?(LOW|MEDIUM|HIGH)"
)
_AREA_WINDS_RE = re.compile(r"SURFACE WINDS ARE ESTIMATED AT (\d+) TO (\d+) KNOTS")


def parse_outlook(text: str) -> dict:
    """The western North Pacific part of JTWC's Significant Tropical Weather
    Advisory (ABPW10): {"issued": ..., "areas": [...]}, each area one that
    JTWC watches because a storm may form there. Areas that have faded or
    became a storm (with its own warning) are left out."""
    flat = _flat(text)
    issued = _OUTLOOK_ISSUED_RE.search(flat)
    if not issued or "WESTERN NORTH PACIFIC AREA" not in flat:
        raise StormTextError("the JTWC advisory is not in its usual layout")
    issued_at = datetime.strptime(f"{issued[3]} {issued[2]} {issued[1]}", "%Y %b %d%H%M").replace(
        tzinfo=timezone.utc
    )
    west = flat.split("WESTERN NORTH PACIFIC AREA", 1)[1].split("2. SOUTH PACIFIC AREA")[0]
    parts = re.split(r"\b[ABC]\. (?=TROPICAL|SUBTROPICAL)", west)
    disturbances = next((p for p in parts if p.startswith("TROPICAL DISTURBANCE")), "")
    areas = []
    for item in re.split(r"\(\d+\)\s*", disturbances)[1:]:
        chance = _CHANCE_RE.search(item)
        positions = re.findall(r"NEAR " + _LATLON, item)
        if not chance or not positions:
            continue
        moved = "PREVIOUSLY LOCATED NEAR" in item and len(positions) > 1
        lat, lon = latlon(*positions[1 if moved else 0])
        invest = _INVEST_RE.search(item)
        winds = _AREA_WINDS_RE.search(item)
        areas.append(
            {
                "name": f"Invest {invest[1]}" if invest else "An area of storms",
                "lat": lat,
                "lon": lon,
                "chance": chance[2].lower(),
                "hours": int(chance[1]),
                "wind_kt": int(winds[2]) if winds else None,
                "alert": "WTPN2" in item,
            }
        )
    return {"issued": issued_at.isoformat(), "areas": areas}


# ---------------------------------------------------------------- GHS/OCD


def parse_ghs(feed: bytes) -> list[dict]:
    """The newest GHS/OCD releases in its feed, newest first:
    [{"title", "url", "time"}]."""
    root = etree.fromstring(feed, parser=etree.XMLParser(recover=True, resolve_entities=False))
    if root is None or root.find("channel") is None:
        raise StormTextError("the GHS/OCD feed is not RSS")
    items = []
    for item in root.iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        url = (item.findtext("link") or "").strip()
        if not title or not url.startswith(("https://", "http://")):
            continue
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        items.append({"title": title, "url": url, "time": when.isoformat() if when else None})
    items.sort(key=lambda i: i["time"] or "", reverse=True)
    return items


_COR_RE = re.compile(
    r"\b(?:COR|Condition of Readiness)\s*-?\s*([1-4])\b(?:\s*[-–]?\s*(Caution|Emergency)\b)?",
    re.IGNORECASE,
)


def cor_level(releases: list[dict], now: datetime) -> dict:
    """Guam's COR level from the newest GHS/OCD release that names one in its
    title ("Guam in COR 3; Prepare Now", "Guam in COR 4, All Clear") in the
    last COR_DAYS days. Without one, Guam is in COR 4, as it is all year."""
    for release in releases:  # newest first
        when = _time(release.get("time"))
        if not when or now - when > timedelta(days=COR_DAYS):
            continue
        found = list(_COR_RE.finditer(release["title"]))
        if found:
            last = found[-1]  # "from COR 3 to COR 2"
            level = int(last[1])
            return {
                "level": level,
                "extra": (last[2] or "").capitalize(),
                "meaning": COR_LEVELS[level],
                "all_clear": "all clear" in release["title"].lower(),
                "release": {**release, "time": when},
            }
    return {"level": 4, "extra": "", "meaning": COR_LEVELS[4], "all_clear": False, "release": None}


# ----------------------------------------------------------------- the page


def _time(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value) if value else None


def _points(storm: dict) -> list[dict]:
    """The storm's center now, then JTWC's forecast centers."""
    first = {"time": _time(storm["time"]), "lat": storm["lat"], "lon": storm["lon"]}
    first["wind_kt"] = storm.get("wind_kt")
    rest = [{**p, "time": _time(p["time"])} for p in storm.get("forecast") or []]
    return [first] + rest


def closest_approach(points: list[dict]) -> dict | None:
    """How close the storm comes to Guam on its forecast path, and when: the
    path between forecast centers is taken as a straight line, checked every
    half hour. {"miles", "time", "wind_kt", "now"}; "now" is True when the
    storm is closest now (it is moving away)."""
    if not points:
        return None
    best = None
    for a, b in pairwise(points) if len(points) > 1 else [(points[0], points[0])]:
        span = (b["time"] - a["time"]).total_seconds()
        steps = max(1, int(span // 1800))
        for i in range(steps + 1):
            f = i / steps
            here = (a["lat"] + (b["lat"] - a["lat"]) * f, a["lon"] + (b["lon"] - a["lon"]) * f)
            miles = distance_miles(GUAM, here)
            if best is None or miles < best["miles"] - 0.01:
                wind = None
                if a.get("wind_kt") is not None and b.get("wind_kt") is not None:
                    wind = a["wind_kt"] + (b["wind_kt"] - a["wind_kt"]) * f
                best = {
                    "miles": miles,
                    "time": a["time"] + timedelta(seconds=span * f),
                    "wind_kt": wind,
                    "where": compass(bearing(GUAM, here)),
                }
    best["now"] = best["time"] == points[0]["time"]
    best["miles"] = round(best["miles"])
    best["wind_mph"] = mph(best["wind_kt"])
    return best


def _storm_view(storm: dict, now: datetime) -> dict:
    """What the page shows of one storm from the saved storm list."""
    if "lat" not in storm:  # JTWC lists it, but its text could not be read
        return {**storm, "title": storm["name"], "details": False}
    here = (storm["lat"], storm["lon"])
    miles = round(distance_miles(GUAM, here))
    points = _points(storm)
    closest = closest_approach(points) if storm["kind"] == "warning" else None
    forecast = []
    for p in points[1:]:
        p_miles = round(distance_miles(GUAM, (p["lat"], p["lon"])))
        forecast.append(
            {
                **p,
                "miles": p_miles,
                "where": compass(bearing(GUAM, (p["lat"], p["lon"]))),
                "wind_mph": mph(p.get("wind_kt")),
                "strength": strength(p.get("wind_kt")),
            }
        )
    wind = storm.get("wind_kt")
    return {
        **storm,
        "details": True,
        "time": _time(storm["time"]),
        "issued": _time(storm.get("issued")),
        "next": _time(storm.get("next")),
        "until": _time(storm.get("until")),
        "miles": miles,
        "where": compass(bearing(GUAM, here)),
        "position": _position_text(storm["lat"], storm["lon"]),
        "wind_mph": mph(wind),
        "gust_mph": mph(storm.get("gust_kt")),
        "wind_low_mph": mph(storm.get("wind_low_kt")),
        "strength": strength(wind),
        "category": category(wind),
        "heading": compass(storm["move_dir"]) if storm.get("move_dir") is not None else None,
        "speed_mph": mph(storm.get("move_kt")),
        "storm_force_miles": _radius_miles(storm, "34"),
        "typhoon_force_miles": _radius_miles(storm, "64"),
        "forecast": forecast,
        "closest": closest,
        "near": bool(
            (closest and closest["miles"] <= NEAR_MILES)
            or (storm["kind"] == "alert" and miles <= NEAR_MILES)
        ),
        "anchor": storm.get("key") or re.sub(r"\W+", "-", storm.get("number", "storm")).lower(),
        "past": max(0, len(storm.get("track") or []) - 1),
    }


def _radius_miles(storm: dict, speed: str) -> int | None:
    reach = (storm.get("radii") or {}).get(speed)
    return round(reach * NM_TO_MILES) if reach else None


def _position_text(lat: float, lon: float) -> str:
    """13.4, 144.8 -> "13.4°N 144.8°E"."""
    ns = f"{abs(lat):.1f}°{'N' if lat >= 0 else 'S'}"
    ew = f"{lon:.1f}°E" if lon <= 180 else f"{360 - lon:.1f}°W"
    return f"{ns} {ew}"


def _area_view(area: dict) -> dict:
    here = (area["lat"], area["lon"])
    return {
        **area,
        "miles": round(distance_miles(GUAM, here)),
        "where": compass(bearing(GUAM, here)),
        "position": _position_text(*here),
        "wind_mph": mph(area.get("wind_kt")),
    }


def typhoon_page(
    shown: dict,
    reports: dict[str, tuple],
    stories: list[dict],
    now: datetime,
    stale_after: timedelta = timedelta(hours=6),
) -> dict:
    """What the Typhoon page shows. `shown` is weather.for_page (the alerts
    and the storm list while it is new), `reports` the saved copies
    ({kind: (data, fetched_at)}), `stories` the recent storm news from the
    site's own stories (queries.storm_stories)."""
    storms = [_storm_view(s, now) for s in shown.get("storms") or []]
    storms.sort(key=lambda s: (not s["details"], s.get("kind") != "warning", s.get("miles", 1e9)))

    outlook, outlook_read = reports.get("outlook", (None, None))
    areas = None
    outlook_issued = None
    if outlook and outlook_read and now - outlook_read < stale_after:
        outlook_issued = _time(outlook["issued"])
        if now - outlook_issued < OUTLOOK_STALE_AFTER:
            # An area that already has a storm or alert of its own is shown once.
            listed = {s.get("number") for s in storms}
            areas = [
                _area_view(a)
                for a in outlook["areas"]
                if a["name"].removeprefix("Invest ") not in listed
            ]
            areas.sort(key=lambda a: a["miles"])

    saved_ghs, ghs_read = reports.get("ghs", (None, None))
    releases = []
    if saved_ghs and ghs_read and now - ghs_read < timedelta(days=7):
        for release in saved_ghs:
            when = _time(release.get("time"))
            if when and now - when <= timedelta(days=GHS_DAYS):
                releases.append({**release, "time": when})
    shelter_since = now - timedelta(days=SHELTER_DAYS)
    shelters = [
        r for r in releases if "shelter" in r["title"].lower() and r["time"] >= shelter_since
    ]
    shelter_news = [
        s for s in stories if "shelter" in s["title"].lower() and s["published_at"] >= shelter_since
    ]

    tropical = [
        a for a in shown.get("warnings", []) + shown.get("advisories", []) if is_tropical(a)
    ]
    near = [s for s in storms if s.get("near")]
    return {
        "storms": storms,
        "storms_checked": shown.get("storms") is not None,
        "near": near,
        "areas": areas,
        "outlook_issued": outlook_issued,
        "alerts": tropical,
        "cor": cor_level(releases if saved_ghs else [], now),
        "ghs_checked": bool(saved_ghs is not None and ghs_read),
        "releases": releases[:6],
        "shelters": shelters,
        "shelter_news": shelter_news,
        "stories": stories,
        "map": track_map(storms, areas or []),
        "checked": shown.get("checked"),
    }


_TROPICAL_RE = re.compile(r"Typhoon|Tropical|Hurricane|Storm Surge", re.IGNORECASE)


def is_tropical(alert: dict) -> bool:
    """A Typhoon Watch, a Tropical Storm Warning, a Tropical Cyclone
    Statement..."""
    return bool(_TROPICAL_RE.search(alert.get("event", "")))


def storm_near(shown: dict | None) -> bool:
    """True while a tropical watch or warning is out for Guam, or a storm's
    forecast takes it within NEAR_MILES: the weather box and the banner then
    point to the Typhoon page first."""
    if not shown:
        return False
    if any(is_tropical(a) for a in shown.get("warnings", [])):
        return True
    for storm in shown.get("storms") or []:
        if "lat" not in storm:
            continue
        if storm.get("kind") == "alert":
            if distance_miles(GUAM, (storm["lat"], storm["lon"])) <= NEAR_MILES:
                return True
            continue
        closest = closest_approach(_points(storm))
        if closest and closest["miles"] <= NEAR_MILES:
            return True
    return False


# ------------------------------------------------------------------- the map

MAP_WIDTH = 720
# The smallest area the map shows: Palau and Yap to Pohnpei, Chuuk to the
# Northern Marianas.
MAP_FRAME = (5.0, 22.0, 132.0, 160.0)  # south, north, west, east
MAP_LIMITS = (-10.0, 45.0, 100.0, 200.0)
MAP_MARGIN = 2.5  # degrees around the farthest point


def track_map(storms: list[dict], areas: list[dict]) -> dict:
    """An SVG map (drawn in typhoon.html) of Guam, the islands around it,
    rings 100, 300 and 500 miles from Guam, each storm's path so far and
    forecast path, and the areas JTWC watches. The map grows to show every
    storm. Longitude is scaled by the cosine of Guam's latitude, so near
    Guam a mile is about as long across as up and down.

    Labels are placed so they don't cover each other or a storm's path:
    the storm's name beside it, its dates on the other side, then Guam,
    the ring distances and the islands, each left off where it would not
    fit. A label marked "wide_only" fits only on a wide screen (the text
    is about twice as big, for the map's size, on a phone) and is hidden
    on a narrow one."""
    lats, lons = [GUAM[0]], [GUAM[1]]
    for storm in storms:
        if not storm.get("details"):
            continue
        for p in (storm.get("track") or []) + _points(storm):
            lats.append(p["lat"])
            lons.append(p["lon"])
        line = storm.get("line")
        if line:  # the formation band, all of it
            half = line["width_nm"] / 60  # nautical miles to degrees
            for lat, lon in (line["start"], line["end"]):
                lats += [lat - half, lat + half]
                lons += [lon - half, lon + half]
    for area in areas:
        lats.append(area["lat"])
        lons.append(area["lon"])
    south = max(MAP_LIMITS[0], min(MAP_FRAME[0], min(lats) - MAP_MARGIN))
    north = min(MAP_LIMITS[1], max(MAP_FRAME[1], max(lats) + MAP_MARGIN))
    west = max(MAP_LIMITS[2], min(MAP_FRAME[2], min(lons) - MAP_MARGIN))
    east = min(MAP_LIMITS[3], max(MAP_FRAME[3], max(lons) + MAP_MARGIN))
    squeeze = math.cos(math.radians(GUAM[0]))
    # Keep the map between a wide strip and a square, by widening the
    # narrower side around its middle.
    across, down = (east - west) * squeeze, north - south
    if down < across * 0.5:
        extra = (across * 0.5 - down) / 2
        south, north = south - extra, north + extra
    elif down > across:
        extra = (down - across) / squeeze / 2
        west, east = west - extra, east + extra
    across, down = (east - west) * squeeze, north - south
    scale = MAP_WIDTH / across  # pixels per degree of latitude
    height = round(down * scale)
    labels = _Labels(MAP_WIDTH, height)

    def xy(lat: float, lon: float) -> tuple[float, float]:
        return round((lon - west) * squeeze * scale, 1), round((north - lat) * scale, 1)

    step = 5 if across < 45 else 10
    grid = {
        "lats": [
            {"y": xy(lat, west)[1], "label": f"{abs(lat)}°{'N' if lat > 0 else 'S' if lat else ''}"}
            for lat in range(math.ceil(south / step) * step, math.floor(north) + 1, step)
        ],
        "lons": [
            {"x": xy(north, lon)[0], "label": _lon_label(lon)}
            for lon in range(math.ceil(west / step) * step, math.floor(east) + 1, step)
        ],
    }
    for g in grid["lats"]:
        labels.block(0, g["y"] - 16, 40, g["y"], text=True)
    for g in grid["lons"]:
        labels.block(g["x"], height - 18, g["x"] + 48, height, text=True)
    gx, gy = xy(*GUAM)
    labels.block(gx - 8, gy - 8, gx + 8, gy + 8)

    shown = [s for s in storms if s.get("details")]
    drawn = []
    for storm in shown:
        cx, cy = xy(storm["lat"], storm["lon"])
        past = [xy(p["lat"], p["lon"]) for p in storm.get("track") or []]
        ahead = [xy(p["lat"], p["lon"]) for p in _points(storm)]
        labels.block_line(past)
        labels.block_line(ahead)
        labels.block(cx - 10, cy - 10, cx + 10, cy + 10)
        item = {
            "title": storm["title"],
            "anchor": storm["anchor"],
            "x": cx,
            "y": cy,
            "class": _strength_class(storm.get("wind_kt")),
            "alert": storm["kind"] == "alert",
            "past": " ".join(f"{x},{y}" for x, y in past),
            "ahead": " ".join(f"{x},{y}" for x, y in ahead),
            "dots": [],
            "wind_r": None,
        }
        reach = (storm.get("radii") or {}).get("34")
        if reach:
            item["wind_r"] = round(reach * NM_TO_MILES / 69.05 * scale, 1)
        for p in storm.get("forecast") or []:
            x, y = xy(p["lat"], p["lon"])
            when = _time(p["time"]).astimezone(GUAM_TZ)
            item["dots"].append(
                {
                    "x": x,
                    "y": y,
                    "class": _strength_class(p.get("wind_kt")),
                    "daily": p["hours"] % 24 == 0,  # the date once a day
                    "text": f"{when:%a} {when.hour % 12 or 12} {when:%p}",
                    "label": None,
                }
            )
            labels.block(x - 7, y - 7, x + 7, y + 7)
        line = storm.get("line")
        if line:
            (x1, y1), (x2, y2) = xy(*line["start"]), xy(*line["end"])
            item["line"] = {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "width": round(line["width_nm"] * 2 * NM_TO_MILES / 69.05 * scale, 1),
            }
        heading = storm.get("move_dir")
        if heading is None and line:
            heading = bearing(tuple(line["start"]), tuple(line["end"]))
        item["side"] = _beside(heading)
        drawn.append(item)
    # Most needed first: storm names, the areas JTWC watches, Guam, dates.
    for item in drawn:
        dx, dy = item["side"]
        item["label"] = labels.place(
            item["title"],
            item["x"],
            item["y"],
            [(dx, dy), (-dx, -dy), (dx, dy * 2)],
            16,
            13,
            phone=True,
        )
    marks = []
    for area in areas:
        x, y = xy(area["lat"], area["lon"])
        labels.block(x - 12, y - 12, x + 12, y + 12)
        marks.append({"x": x, "y": y, "name": area["name"], "chance": area["chance"]})
    for mark in marks:
        mark["label"] = labels.place(
            mark["name"],
            mark["x"],
            mark["y"],
            [(1, 0), (-1, 0), (0, 1), (0, -1)],
            15,
            12,
            phone=True,
        )
    guam = {"x": gx, "y": gy}
    guam["label"] = labels.place(
        "Guam",
        gx,
        gy,
        [(1, -0.7), (-1, -0.7), (1, 0.7), (-1, 0.7), (0, -1), (0, 1)],
        9,
        14,
        phone=True,
    )
    for item in drawn:
        dx, dy = item["side"]
        for dot in item["dots"]:
            if dot["daily"]:
                dot["label"] = labels.place(
                    dot["text"], dot["x"], dot["y"], _sides(-dx, -dy), 12, 11
                )
    rings = []
    for miles in RINGS_MILES:
        r = round(miles / 69.05 * scale, 1)
        # Each ring's distance at its lower left, where storms seldom come from.
        x, y = round(gx - r * 0.7071, 1), round(gy + r * 0.7071, 1)
        label = labels.place(f"{miles} mi", x, y, [(-0.7, 0.7), (0.7, -0.7)], 3, 11, must=False)
        rings.append({"r": r, "miles": miles, "label": label})
    places = []
    for lat, lon, name in PLACES:
        if south < lat < north and west < lon < east:
            x, y = xy(lat, lon)
            if labels.hits(x - 3, y - 3, x + 3, y + 3):
                continue  # under a storm or its label
            label = labels.place(name, x, y, [(1, 0), (-1, 0)], 6, 12, must=False)
            if label:
                places.append({"x": x, "y": y, "name": name, "label": label})
    return {
        "width": MAP_WIDTH,
        "height": height,
        "guam": guam,
        "rings": rings,
        "grid": grid,
        "places": places,
        "storms": drawn,
        "areas": marks,
    }


def _beside(heading: float | None) -> tuple[float, float]:
    """Which way (on the map, y down) to put a storm's label: square to its
    path, on the south side, so it is not on the path behind or ahead."""
    if heading is None:
        return 0.7, 0.7
    for turn in (90, -90):
        angle = math.radians(heading + turn)
        dx, dy = math.sin(angle), -math.cos(angle)
        if dy > 0.05:
            return round(dx, 2), round(dy, 2)
    return 1.0, 0.0  # moving due north or south


def _sides(dx: float, dy: float) -> list[tuple[float, float]]:
    """(dx, dy) first, then the four corners, then the other way."""
    corners = [(0.75, -0.75), (-0.75, -0.75), (0.75, 0.75), (-0.75, 0.75)]
    return [(dx, dy), *corners, (-dx, -dy)]


PHONE_TEXT = 2.1  # how much bigger, for the map's size, its text is on a phone


class _Labels:
    """Places map labels where they don't cover each other or what is
    drawn. Each placed label and each blocked spot is a box, kept twice:
    at the text size on a wide screen and at the bigger size on a phone."""

    def __init__(self, width: float, height: float):
        self.width, self.height = width, height
        self.wide: list[tuple[float, float, float, float]] = []
        self.phone: list[tuple[float, float, float, float]] = []

    def block(self, x1: float, y1: float, x2: float, y2: float, text: bool = False) -> None:
        self.wide.append((x1, y1, x2, y2))
        if text:  # text is bigger on a phone, so its box is too
            x2, y1 = x1 + (x2 - x1) * PHONE_TEXT, y2 - (y2 - y1) * PHONE_TEXT
        self.phone.append((x1, y1, x2, y2))

    def block_line(self, points: list[tuple[float, float]]) -> None:
        """Small boxes every few pixels along a path."""
        for (x1, y1), (x2, y2) in pairwise(points):
            steps = max(1, int(math.hypot(x2 - x1, y2 - y1) // 8))
            for i in range(steps + 1):
                x, y = x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps
                self.block(x - 3, y - 3, x + 3, y + 3)
        for x, y in points[-1:]:
            self.block(x - 3, y - 3, x + 3, y + 3)

    def hits(self, x1: float, y1: float, x2: float, y2: float, boxes=None) -> bool:
        return any(
            x1 < b[2] and b[0] < x2 and y1 < b[3] and b[1] < y2
            for b in (self.wide if boxes is None else boxes)
        )

    def _boxes(self, text, x, y, dx, dy, gap, size):
        """Where a label goes for each screen: (x, y, anchor) for the SVG
        <text>, and its box on a wide screen and on a phone. The anchor is
        picked so the label fits inside the map even on a phone."""
        wide_w, phone_w = len(text) * size * 0.6, len(text) * size * 0.6 * PHONE_TEXT
        anchor = "start" if dx > 0.35 else "end" if dx < -0.35 else "middle"
        lx = x + dx * gap
        if anchor == "start" and lx + phone_w > self.width - 2:
            anchor, lx = "end", x - abs(dx * gap) - 4
        elif anchor == "end" and lx - phone_w < 2:
            anchor, lx = "start", x + abs(dx * gap) + 4
        elif anchor == "middle":
            lx = min(max(lx, phone_w / 2 + 2), self.width - phone_w / 2 - 2)
        # The text's middle `gap` away, up or down; y is its baseline.
        mid = y + dy * (gap + size * 0.4 if dy else 0)
        ly = min(max(mid + size * 0.35, size * PHONE_TEXT + 2), self.height - 4)

        def box(w: float, h: float) -> tuple[float, float, float, float]:
            left = lx if anchor == "start" else lx - w if anchor == "end" else lx - w / 2
            return (left, ly - h, left + w, ly + h * 0.3)

        return (lx, ly, anchor), box(wide_w, size), box(phone_w, size * PHONE_TEXT)

    def place(
        self,
        text: str,
        x: float,
        y: float,
        sides: list[tuple[float, float]],
        gap: float,
        size: float,
        must: bool = True,
        phone: bool = False,
    ) -> dict | None:
        """A label `gap` pixels from (x, y), on the first of `sides` (map
        directions, y down) where it covers nothing: {"x", "y", "anchor",
        "wide_only"}. When it fits on none, the first side, or None if it
        isn't a `must`. On a phone it is shown only where it fits there
        too, unless `phone` says to show it anyway."""
        tries = [self._boxes(text, x, y, dx, dy, gap, size) for dx, dy in sides]
        for spot, wide, small in tries:  # fits on both screens
            if not self.hits(*wide) and not self.hits(*small, boxes=self.phone):
                return self._keep(spot, wide, small, False)
        for spot, wide, small in tries:  # fits on a wide screen
            if not self.hits(*wide):
                return self._keep(spot, wide, small, not phone)
        if not must:
            return None
        spot, wide, small = tries[0]
        return self._keep(spot, wide, small, not phone and self.hits(*small, boxes=self.phone))

    def _keep(self, spot, wide, small, wide_only: bool) -> dict:
        self.wide.append(wide)
        if not wide_only:
            self.phone.append(small)
        lx, ly, anchor = spot
        return {"x": round(lx, 1), "y": round(ly, 1), "anchor": anchor, "wide_only": wide_only}


def _lon_label(lon: float) -> str:
    if lon == 180:
        return "180°"
    return f"{lon}°E" if lon < 180 else f"{360 - lon}°W"
