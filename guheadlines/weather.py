"""Guam weather for the website: the warning banner and the weather box.

The worker reads these public sources every WEATHER_INTERVAL_MINUTES and
saves the last good copy of each in the weather table:

- alerts:     National Weather Service (NWS) watches, warnings and advisories
              for Guam and the Guam coastal waters
- conditions: the latest reading at Guam International Airport (PGUM)
- forecast:   the NWS forecast for Hagåtña
- hourly:     the NWS forecast for each hour of the next two days
- surf:       the NWS surf zone forecast: rip current risk, surf height on
              each side of the island, water temperature
- marine:     the NWS coastal waters forecast (wind and seas, for boating)
- storms:     the tropical storms and typhoons that the Joint Typhoon Warning
              Center (JTWC) has warnings for in the western North Pacific
- tides:      the high and low tides of the next month at the NOAA tide
              station of TIDE_STATION (Apra Harbor), read every few hours only

All of it is United States government data, free to use, with no key. The
website reads only the table, so a slow or broken source never slows a page.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from itertools import pairwise
from urllib.parse import urlencode

from lxml import etree
from sqlalchemy.dialects.postgresql import insert

from . import sky
from .db import session_scope
from .models import WeatherReport
from .scraper.http import HttpClient
from .scraper.text import GUAM_TZ
from .settings import Settings

log = logging.getLogger(__name__)

NWS_API = "https://api.weather.gov"
# GUZ001 is Guam. PMZ151 is the Guam coastal waters.
ALERTS_URL = f"{NWS_API}/alerts/active?zone=GUZ001,PMZ151&status=actual"
OBSERVATION_URL = f"{NWS_API}/stations/PGUM/observations/latest"
# Hagåtña. NWS says that the forecast grid of a place can change, so each
# check asks for the address of the forecast again.
POINT_URL = f"{NWS_API}/points/13.4757,144.7489"
# The latest surf zone forecast and coastal waters forecast of NWS Guam.
SURF_URL = f"{NWS_API}/products/types/SRF/locations/GUM/latest"
MARINE_URL = f"{NWS_API}/products/types/CWF/locations/GUM/latest"
JTWC_FEED = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"
# The pages that the site sends readers to for more.
NWS_GUAM_URL = "https://www.weather.gov/gum/"
NWS_PRODUCT_PAGE = "https://forecast.weather.gov/product.php?site=GUM&issuedby=GUM&product="
SURF_PAGE = NWS_PRODUCT_PAGE + "SRF"
MARINE_PAGE = NWS_PRODUCT_PAGE + "CWF"
JTWC_URL = "https://www.metoc.navy.mil/jtwc/jtwc.html"
# The NWS weather radar at Andersen Air Force Base (PGUA).
GUAM_RADAR_URL = "https://radar.weather.gov/station/pgua/standard"
# NOAA's tide predictions (CO-OPS data API). See read_tides.
TIDES_API = "https://api.tidesandcurrents.noaa.gov"
TIDES_PAGE = "https://tidesandcurrents.noaa.gov/noaatidepredictions.html"

# The weather box shows the conditions, forecast and storm list only while
# they are this new. Alerts stay until their end time.
STALE_AFTER = timedelta(hours=6)
# NWS writes the surf and coastal waters forecasts twice a day.
MARINE_STALE_AFTER = timedelta(hours=18)
HOURS_SAVED = 48
# Tide predictions are worked out years ahead, so one read gives the next
# month, and the worker reads them again only this often.
TIDES_EVERY = timedelta(hours=6)
TIDE_DAYS = 31
# The Weather page's tide views: Day, Week and Month (from today).
TIDE_VIEWS = {"day": 1, "week": 7, "month": 30}

SEVERITY_ORDER = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
KNOTS_TO_MPH = 1.15078
COMPASS = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()


class WeatherError(Exception):
    """A source answered, but not with what the site can show."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _paragraphs(text: str | None) -> list[str]:
    """NWS text breaks its lines at about 70 characters, with a blank line
    between paragraphs. Its "* WHAT...Dangerous rip currents." becomes
    "What: Dangerous rip currents."."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    lines = [" ".join(block.split()) for block in blocks if block.strip()]
    return [re.sub(r"^\* ([A-Z ]+)\.\.\.", lambda m: m[1].capitalize() + ": ", s) for s in lines]


# ------------------------------------------------------------------ alerts


def is_warning(event: str, severity: str) -> bool:
    """Watches and warnings (a Typhoon Warning, a Flood Watch) go in the
    banner on every page. Advisories and statements (a Small Craft Advisory,
    a Rip Current Statement) go only in the weather box on the home page."""
    return severity in ("Extreme", "Severe") or event.endswith(("Warning", "Watch"))


def parse_alerts(data: dict) -> list[dict]:
    alerts = []
    for feature in data.get("features") or []:
        p = feature.get("properties") or {}
        event = (p.get("event") or "").strip()
        if not event or p.get("messageType") == "Cancel":
            continue
        severity = p.get("severity") or "Unknown"
        alerts.append(
            {
                "event": event,
                "severity": severity,
                "area": (p.get("areaDesc") or "").replace("; ", ", "),
                # "ends" is when the danger ends, "expires" when NWS sends an
                # update. NWS leaves "ends" empty for "until further notice".
                "ends": p.get("ends") or p.get("expires"),
                "text": _paragraphs(p.get("description")) + _paragraphs(p.get("instruction")),
                "warning": is_warning(event, severity),
            }
        )
    alerts.sort(key=lambda a: (not a["warning"], SEVERITY_ORDER.get(a["severity"], 9)))
    return alerts


# -------------------------------------------------------------- conditions

_WIND_RE = re.compile(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b")
_TEMP_RE = re.compile(r"\b(M?\d{2})/(M?\d{2})\b")
# The remarks give the temperature and dew point in tenths of a degree.
_TEMP_TENTHS_RE = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")


def relative_humidity(temp_c: float, dew_c: float) -> float:
    """Magnus formula."""

    def vapour(c: float) -> float:
        return math.exp(17.625 * c / (243.04 + c))

    return 100 * vapour(dew_c) / vapour(temp_c)


def heat_index(temp_f: float, humidity: float) -> float | None:
    """The NWS heat index (how hot it feels) in °F. None below 80 °F, where
    it is about the same as the temperature."""
    t, rh = temp_f, humidity
    if t < 80:
        return None
    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * rh
        - 0.22475541 * t * rh
        - 0.00683783 * t * t
        - 0.05481717 * rh * rh
        + 0.00122874 * t * t * rh
        + 0.00085282 * t * rh * rh
        - 0.00000199 * t * t * rh * rh
    )
    if rh < 13 and t <= 112:
        hi -= (13 - rh) / 4 * math.sqrt((17 - abs(t - 95)) / 17)
    elif rh > 85 and t <= 87:
        hi += (rh - 85) / 10 * (87 - t) / 5
    return hi


def parse_metar(raw: str) -> dict:
    """Temperature, humidity and wind from an airport report (METAR), such as
    "PGUM 240954Z 08008KT 10SM FEW019 28/24 A2990 RMK AO2 SLP118 T02780244".
    A value that the report does not give is None."""
    body, _, remarks = raw.partition(" RMK ")
    out = dict.fromkeys(("temp_f", "heat_index_f", "humidity", "wind_dir", "wind_mph", "gust_mph"))
    if m := _WIND_RE.search(body):
        direction, speed, gust = m.groups()
        out["wind_mph"] = round(int(speed) * KNOTS_TO_MPH)
        if direction != "VRB" and int(speed):
            out["wind_dir"] = COMPASS[round(int(direction) / 22.5) % 16]
        if gust:
            out["gust_mph"] = round(int(gust) * KNOTS_TO_MPH)
    if m := _TEMP_TENTHS_RE.search(remarks):
        temp_c = int(m[2]) / 10 * (-1 if m[1] == "1" else 1)
        dew_c = int(m[4]) / 10 * (-1 if m[3] == "1" else 1)
    elif m := _TEMP_RE.search(body):
        temp_c, dew_c = (int(v.replace("M", "-")) for v in m.groups())
    else:
        return out
    temp_f = temp_c * 9 / 5 + 32
    humidity = relative_humidity(temp_c, dew_c)
    feels = heat_index(temp_f, humidity)
    out.update(
        temp_f=round(temp_f),
        humidity=round(humidity),
        heat_index_f=round(feels) if feels is not None else None,
    )
    return out


def parse_observation(data: dict) -> dict:
    # NWS gives the decoded values (temperature, wind) for Guam as null, so
    # they are read from the airport's own report instead.
    p = data.get("properties") or {}
    if not p.get("timestamp") or not p.get("rawMessage"):
        raise WeatherError("the latest airport reading is empty")
    sky = p.get("textDescription") or ""
    return {"time": p["timestamp"], "sky": sky, **parse_metar(p["rawMessage"])}


# ---------------------------------------------------------------- forecast


def forecast_url(point: dict, kind: str = "forecast") -> str:
    """The address of the forecast ("forecast") or of the hourly forecast
    ("forecastHourly") of the point."""
    url = (point.get("properties") or {}).get(kind) or ""
    if not url.startswith(NWS_API + "/"):
        raise WeatherError("NWS gave no forecast for Hagåtña")
    return url


def parse_forecast(data: dict) -> list[dict]:
    periods = []
    for period in (data.get("properties") or {}).get("periods") or []:
        if not period.get("name") or period.get("temperature") is None:
            continue
        temp = period["temperature"]
        if period.get("temperatureUnit") == "C":
            temp = round(temp * 9 / 5 + 32)
        periods.append(
            {
                "name": period["name"],
                "temp_f": temp,
                "daytime": bool(period.get("isDaytime")),
                "short": period.get("shortForecast") or "",
                "rain": (period.get("probabilityOfPrecipitation") or {}).get("value"),
                "detail": period.get("detailedForecast") or "",
            }
        )
    if not periods:
        raise WeatherError("the forecast has no periods")
    return periods


def parse_hourly(data: dict) -> list[dict]:
    """The hourly forecast: the start of each hour, the temperature, the
    chance of rain, the sky and the wind."""
    hours = []
    for period in (data.get("properties") or {}).get("periods") or []:
        if not period.get("startTime") or period.get("temperature") is None:
            continue
        temp = period["temperature"]
        if period.get("temperatureUnit") == "C":
            temp = round(temp * 9 / 5 + 32)
        wind = " ".join(v for v in (period.get("windDirection"), period.get("windSpeed")) if v)
        hours.append(
            {
                "time": period["startTime"],
                "temp_f": temp,
                "rain": (period.get("probabilityOfPrecipitation") or {}).get("value"),
                "short": period.get("shortForecast") or "",
                "wind": wind,
            }
        )
    if not hours:
        raise WeatherError("the hourly forecast has no hours")
    return hours[:HOURS_SAVED]


# ----------------------------------------------------------- on the water
# The surf zone and coastal waters forecasts are NWS text products: blocks
# for each group of zones, ended by "$$". In a block, "...TEXT..." lines are
# headlines (an advisory), and ".TODAY..." starts a period.

_PERIOD_RE = re.compile(r"^\.([A-Z][A-Z ]*[A-Z])\.\.\.", re.MULTILINE)
_HEADLINE_RE = re.compile(r"^\.\.\.(.+?)\.\.\.\s*$", re.MULTILINE | re.DOTALL)


def _product_text(data: dict) -> tuple[str, str]:
    text = data.get("productText") or ""
    if not text or not data.get("issuanceTime"):
        raise WeatherError("the NWS product is empty")
    return text.replace("\r", ""), data["issuanceTime"]


def _block(text: str, zone: str) -> str:
    for block in text.split("$$"):
        if re.search(rf"^{zone}\b", block, re.MULTILINE):
            return block
    raise WeatherError(f"the NWS product has no {zone} part")


def _join(text: str) -> str:
    return " ".join(text.split())


def _sentence(shout: str) -> str:
    """ "SMALL CRAFT ADVISORY WILL EXPIRE AT 5 AM CHST" -> "Small craft
    advisory will expire at 5 AM ChST." """
    text = _join(shout).lower().capitalize()
    text = re.sub(r"\b(am|pm)\b", lambda m: m[1].upper(), text)
    text = re.sub(r"\bchst\b", "ChST", text)
    text = re.sub(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lambda m: m[1].capitalize(),
        text,
    )
    return text if text.endswith(".") else text + "."


def _headlines(block: str) -> list[str]:
    return [_sentence(m[1]) for m in _HEADLINE_RE.finditer(block)]


def _periods(block: str) -> list[tuple[str, str]]:
    """[(name, text)] for each ".TODAY..." in the block: "Today",
    "Thursday Night"."""
    matches = list(_PERIOD_RE.finditer(block))
    periods = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        periods.append((match[1].title(), block[match.end() : end]))
    return periods


def parse_surf(data: dict) -> dict:
    """The Surf Zone Forecast for Guam: the rip current risk, the surf height
    on the reefs of each side of the island and the water temperature, for
    the next three periods, and the forecaster's discussion."""
    text, issued = _product_text(data)
    discussion = re.search(r"^\.DISCUSSION\.\.\.(.*?)(?:\$\$|\Z)", text, re.M | re.S)
    block = _block(text, "GUZ001")
    block = block.split("Tide information", 1)[0]  # the NOAA tides, shown apart
    periods = []
    for name, body in _periods(block):
        rip = re.search(r"Rip Current Risk\.+\s*([A-Za-z]+)", body)
        water = re.search(r"SST Analysis\.+\s*(\d+) Degrees", body)
        surf = re.findall(
            r"(North|East|South|West)\s+facing reefs\.+\s*([^\n]+?)\.?\s*$", body, re.M
        )
        periods.append(
            {
                "name": name,
                "rip": rip[1].capitalize() if rip else None,
                "surf": [[side, height.replace(" feet", " ft")] for side, height in surf],
                "water_f": int(water[1]) if water else None,
            }
        )
    if not periods:
        raise WeatherError("the surf forecast has no periods")
    return {
        "issued": issued,
        "headlines": _headlines(block),
        "periods": periods,
        "discussion": _join(discussion[1]) if discussion else "",
    }


def parse_marine(data: dict) -> dict:
    """The Coastal Waters Forecast for the Marianas coastal waters (PMZ151
    is Guam's): the wind and seas of each period, any advisory, and the
    forecaster's summary for the Marianas waters."""
    text, issued = _product_text(data)
    synopsis = re.search(r"^\.Synopsis[^.\n]*\.\.\.(.*?)(?:\$\$|\Z)", text, re.M | re.S)
    block = _block(text, "PMZ151")
    periods = [{"name": name, "text": _join(body)} for name, body in _periods(block)]
    if not periods:
        raise WeatherError("the coastal waters forecast has no periods")
    return {
        "issued": issued,
        "headlines": _headlines(block),
        "periods": periods,
        "synopsis": _join(synopsis[1]) if synopsis else "",
    }


# ------------------------------------------------------------------ storms

_BOLD_RE = re.compile(r"<b>\s*([^<]+?)\s*<", re.IGNORECASE)
_HREF_RE = re.compile(r"""href=['"](https?://[^'"]+)['"]""", re.IGNORECASE)


def parse_storms(feed: bytes) -> list[dict]:
    """The western North Pacific storms in the JTWC feed. Each storm is a
    bold title ("Typhoon 25W (Yagi) Warning #12", or "Tropical Cyclone
    Formation Alert WTPN21" for a storm that may form), then links to its
    products. JTWC closes the bold tags with a second <b>."""
    root = etree.fromstring(feed, parser=etree.XMLParser(recover=True, resolve_entities=False))
    if root is None:
        raise WeatherError("the JTWC feed is not XML")
    for item in root.iter("item"):
        if (item.findtext("guid") or "").strip() == "NWPAC-NIO-WARNINGS":
            html = item.findtext("description") or ""
            break
    else:
        raise WeatherError("the JTWC feed has no western Pacific list")
    storms = []
    titles = [m for m in _BOLD_RE.finditer(html) if not m[1].startswith("Issued at")]
    for i, match in enumerate(titles):
        title = match[1]
        # The list also has the north Indian Ocean (storm numbers ending in A
        # or B, alerts WTIO..). W is the western North Pacific.
        if not re.search(r"\b\d{2}W\b|\bWTPN\d{2}\b", title):
            continue
        end = titles[i + 1].start() if i + 1 < len(titles) else len(html)
        link = _HREF_RE.search(html, match.end(), end)
        storms.append(
            {
                "name": re.sub(r"\s+WTPN\d{2}$", "", title),
                "url": link[1] if link else JTWC_URL,
            }
        )
    if not storms and "no current tropical cyclone warnings" not in html.lower():
        raise WeatherError("the JTWC western Pacific list has changed")
    return storms


# ------------------------------------------------------------------- tides


def tides_url(station: str, first: date, last: date) -> str:
    """The high and low tides at a NOAA station from `first` to `last`
    (dates in UTC), in feet, with times in UTC."""
    query = urlencode(
        {
            "product": "predictions",
            "interval": "hilo",  # the high and low tides only
            "station": station,
            "begin_date": f"{first:%Y%m%d}",
            "end_date": f"{last:%Y%m%d}",
            # Heights above mean lower low water, the zero of tide tables.
            "datum": "MLLW",
            "units": "english",
            "time_zone": "gmt",
            "format": "json",
        }
    )
    return f"{TIDES_API}/api/prod/datagetter?{query}"


def tide_page_url(station: str) -> str:
    """NOAA's page for the station, with the tide table and a chart."""
    return f"{TIDES_PAGE}?{urlencode({'id': station})}"


def parse_tides(data: dict) -> list[dict]:
    """NOAA's answer is {"predictions": [{"t": "2026-11-24 16:42", "v":
    "0.312", "type": "L"}, ...]}: the time in UTC, the height in feet and H
    for a high tide or L for a low one. An answer with no tides is
    {"error": {"message": "..."}}."""
    error = data.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        raise WeatherError(f"NOAA: {message or 'no tides'}")
    tides = []
    for p in data.get("predictions") or []:
        kind = str(p.get("type") or "")[:1].upper()
        try:
            when = datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            # + 0.0 turns -0.0 (from -0.04 ft) into 0.0.
            feet = round(float(p["v"]), 1) + 0.0
        except (KeyError, TypeError, ValueError):
            continue
        if kind in ("H", "L"):
            tides.append({"time": when.isoformat(), "high": kind == "H", "ft": feet})
    if not tides:
        raise WeatherError("NOAA gave no tides")
    return sorted(tides, key=lambda t: t["time"])


def station_name(data: dict) -> str:
    """The place's name from NOAA's station details: "Apra Harbor" (NOAA
    says "Apra Harbor, Guam", and the whole site is about Guam)."""
    stations = data.get("stations") or [{}]
    name = str(stations[0].get("name") or "").strip()
    return re.sub(r"(^|,\s*)Guam$", "", name).strip()


def read_tides(client: HttpClient, station: str, now: datetime | None = None) -> dict:
    """The tides of the month ahead at `station`. The day before is included
    too, as Guam's day starts at 14:00 UTC the day before."""
    today = (now or _now()).astimezone(GUAM_TZ).date()
    url = tides_url(station, today - timedelta(days=1), today + timedelta(days=TIDE_DAYS))
    tides = parse_tides(json.loads(client.get(url).content))
    try:  # the place's name is a nice-to-have: the tides work without it
        name = station_name(
            json.loads(client.get(f"{TIDES_API}/mdapi/prod/webapi/stations/{station}.json").content)
        )
    except Exception as exc:
        log.info("weather: no name for tide station %s: %s", station, exc)
        name = ""
    return {"station": station, "name": name, "tides": tides}


def tides_due(station: str, now: datetime | None = None) -> bool:
    """True when the saved tides are older than TIDES_EVERY, or are for
    another station (TIDE_STATION was changed), or there are none."""
    with session_scope() as session:
        row = session.get(WeatherReport, "tides")
        if row is None or row.data.get("station") != station:
            return True
        return (now or _now()) - row.fetched_at >= TIDES_EVERY


# ------------------------------------------------------------------ checks


def _json(client: HttpClient, url: str) -> dict:
    return json.loads(client.get(url, accept="application/geo+json").content)


def _read_forecast(client: HttpClient) -> list[dict]:
    return parse_forecast(_json(client, forecast_url(_json(client, POINT_URL))))


def _read_hourly(client: HttpClient) -> list[dict]:
    url = forecast_url(_json(client, POINT_URL), "forecastHourly")
    return parse_hourly(_json(client, url))


READERS = {
    "alerts": lambda client: parse_alerts(_json(client, ALERTS_URL)),
    "conditions": lambda client: parse_observation(_json(client, OBSERVATION_URL)),
    "forecast": _read_forecast,
    "hourly": _read_hourly,
    "surf": lambda client: parse_surf(_json(client, SURF_URL)),
    "marine": lambda client: parse_marine(_json(client, MARINE_URL)),
    "storms": lambda client: parse_storms(client.get(JTWC_FEED).content),
}


def save(kind: str, data, now: datetime | None = None) -> None:
    stmt = insert(WeatherReport).values(kind=kind, data=data, fetched_at=now or _now())
    stmt = stmt.on_conflict_do_update(
        index_elements=[WeatherReport.kind],
        set_={"data": stmt.excluded.data, "fetched_at": stmt.excluded.fetched_at},
    )
    with session_scope() as session:
        session.execute(stmt)


def check_weather(
    settings: Settings, client: HttpClient | None = None, force_tides: bool = False
) -> list[tuple]:
    """Read each source and save each one that worked; a source that fails
    keeps its last good copy. Returns (kind, data, error) for each source.
    The tides are read only when due (tides_due), or with `force_tides`."""
    own_client = client is None
    if own_client:
        client = HttpClient(
            settings.user_agent,
            timeout=settings.request_timeout,
            per_host_delay=settings.per_host_delay,
            # api.weather.gov's robots.txt turns all robots away, to keep
            # search engines out of the API. NWS publishes the API for
            # programs like this one and asks only for a User-Agent. NOAA's
            # tide API is also published for programs.
            respect_robots=False,
        )
    readers = dict(READERS)
    station = settings.tide_station
    if station and (force_tides or tides_due(station)):
        readers["tides"] = lambda client: read_tides(client, station)
    results = []
    try:
        for kind, read in readers.items():
            try:
                data = read(client)
            except Exception as exc:  # one broken source must not stop the others
                log.warning("weather: %s not updated: %s", kind, exc)
                results.append((kind, None, f"{type(exc).__name__}: {exc}"))
                continue
            save(kind, data)
            results.append((kind, data, None))
    finally:
        if own_client:
            client.close()
    return results


# ------------------------------------------------------------------- pages


def tide_days(saved: dict | None, station: str, now: datetime, count: int) -> list[dict]:
    """The high and low tides of `count` days from today (days in Guam):
    [{"day": date, "tides": [...]}], each tide marked "past" once its time
    has gone by. Empty when TIDE_STATION is empty, when the saved tides are
    for another station, or when they don't reach today. Days at the end
    that the saved tides don't reach are left out."""
    if not station or not saved or saved.get("station") != station:
        return []
    today = now.astimezone(GUAM_TZ).date()
    days: dict[date, list] = {today + timedelta(days=i): [] for i in range(count)}
    for tide in saved.get("tides") or []:
        when = _time(tide["time"])
        day = when.astimezone(GUAM_TZ).date()
        if day in days:
            days[day].append({**tide, "time": when, "past": when <= now})
    return [{"day": day, "tides": tides} for day, tides in days.items() if tides]


def tides_today(saved: dict | None, station: str, now: datetime) -> dict | None:
    """Today's tides for the weather box, or None (see tide_days)."""
    days = tide_days(saved, station, now, 1)
    if not days or days[0]["day"] != now.astimezone(GUAM_TZ).date():
        return None
    return {
        "name": saved.get("name") or "",
        "url": tide_page_url(station),
        "today": days[0]["tides"],
    }


def _water_level(tides: list[tuple], when: datetime) -> float:
    """The height of the tide at `when`, between NOAA's high and low tides
    (sorted (time, ft, high)). From one to the next the water follows about
    half a cosine wave: the usual way to fill in between a tide table's times."""
    for (t1, h1, _), (t2, h2, _) in pairwise(tides):
        if t1 <= when <= t2:
            share = (when - t1) / (t2 - t1)
            return h1 + (h2 - h1) * (1 - math.cos(math.pi * share)) / 2
    raise ValueError("no tides around this time")


CHART_WIDTH, CHART_HEIGHT = 720, 190


def tide_chart(saved: dict | None, day: date, now: datetime) -> dict | None:
    """A line of the water's height through `day` in Guam, for the chart on
    the Weather page: the SVG path of the line and of the area under it, a
    dot at each high and low tide, and the time now (when it is today). None
    when the saved tides don't cover the whole day."""
    start = datetime.combine(day, time.min, tzinfo=GUAM_TZ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    tides = sorted((_time(t["time"]), t["ft"], t["high"]) for t in (saved or {}).get("tides") or [])
    before = [t for t in tides if t[0] <= start][-1:]
    after = [t for t in tides if t[0] >= end][:1]
    if not before or not after:
        return None
    tides = before + [t for t in tides if start < t[0] < end] + after
    samples = [start + timedelta(minutes=15 * i) for i in range(4 * 24 + 1)]
    levels = [_water_level(tides, when) for when in samples]
    low, high = min(levels), max(levels)
    span = (high - low) or 1.0
    top, bottom = 42, CHART_HEIGHT - 42  # room for the times above and below

    def x(when: datetime) -> float:
        return round((when - start) / (end - start) * CHART_WIDTH, 1)

    def y(ft: float) -> float:
        return round(top + (high - ft) / span * (bottom - top), 1)

    line = "M" + " L".join(f"{x(w)},{y(ft)}" for w, ft in zip(samples, levels, strict=True))
    return {
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "line": line,
        "area": f"{line} L{CHART_WIDTH},{CHART_HEIGHT} L0,{CHART_HEIGHT} Z",
        "dots": [
            {"x": x(w), "y": y(ft), "time": w, "high": is_high}
            for w, ft, is_high in tides
            if start <= w < end
        ],
        "now": x(now) if start <= now < end else None,
    }


def forecast_days(periods: list[dict]) -> list[dict]:
    """The forecast periods by day, for This week: a day period ("Thursday")
    with the night after it ("Thursday Night"). A first period that is a
    night ("Tonight") is a day of its own."""
    days: list[dict] = []
    for p in periods:
        if p["daytime"] or not days or days[-1]["low"] is not None:
            days.append(
                {"name": p["name"], "high": None, "low": None, "short": p["short"], "rain": None}
            )
        day = days[-1]
        day["high" if p["daytime"] else "low"] = p["temp_f"]
        if p.get("rain") is not None:
            day["rain"] = max(day["rain"] or 0, p["rain"])
    return days


def weather_page(reports: dict[str, tuple], now: datetime, tide_station: str) -> dict:
    """What the Weather page shows: what the box shows, every forecast
    period, sunrise, sunset and the moon, and the tides by day for the Day,
    Week and Month views, with today's tide chart."""
    shown = for_page(reports, now, tide_station)
    today = now.astimezone(GUAM_TZ).date()

    def fresh(kind: str, stale_after: timedelta = STALE_AFTER):
        data, fetched_at = reports.get(kind, (None, None))
        return data if fetched_at and now - fetched_at < stale_after else None

    hour_ago = now - timedelta(hours=1)
    hours = [{**h, "time": _time(h["time"])} for h in fresh("hourly") or []]
    shown["hourly"] = [h for h in hours if h["time"] > hour_ago][:24]
    shown["surf"] = fresh("surf", MARINE_STALE_AFTER)
    shown["marine"] = fresh("marine", MARINE_STALE_AFTER)
    for kind in ("surf", "marine"):
        if shown[kind]:
            shown[kind] = {**shown[kind], "issued": _time(shown[kind]["issued"])}

    saved = reports.get("tides", (None, None))[0]
    days = tide_days(saved, tide_station, now, max(TIDE_VIEWS.values()))
    shown["next_tides"] = [
        {**t, "tomorrow": d["day"] != today} for d in days for t in d["tides"] if not t["past"]
    ][:2]
    shown["week"] = forecast_days(shown["forecast"] or [])
    shown["sky"] = sky.for_day(today)
    shown["tide_views"] = {view: days[:count] for view, count in TIDE_VIEWS.items()}
    shown["tide_chart"] = tide_chart(saved, today, now) if days else None
    shown["tide_station"] = (
        {
            "id": tide_station,
            "name": saved.get("name") or "",
            "url": tide_page_url(tide_station),
        }
        if days
        else None
    )
    return shown


def for_page(reports: dict[str, tuple], now: datetime, tide_station: str = "") -> dict:
    """What the pages show, from the saved copies ({kind: (data, fetched_at)}).

    An alert stays until its end time, also when the worker cannot reach NWS:
    a typhoon warning must not go away because a check failed. The reading,
    forecast and storm list are shown only while they are new (STALE_AFTER).
    Tide predictions don't go out of date: the saved week is used while it
    lasts."""

    def fresh(kind: str):
        data, fetched_at = reports.get(kind, (None, None))
        return data if fetched_at and now - fetched_at < STALE_AFTER else None

    saved, checked = reports.get("alerts", ([], None))
    alerts = []
    for alert in saved:
        ends = _time(alert["ends"])
        if (ends or checked + STALE_AFTER) <= now:
            continue
        alerts.append({**alert, "ends": ends})
    conditions = fresh("conditions")
    if conditions:
        conditions = {**conditions, "time": _time(conditions["time"])}
    return {
        "warnings": [a for a in alerts if a["warning"]],
        "advisories": [a for a in alerts if not a["warning"]],
        "conditions": conditions,
        "forecast": fresh("forecast"),
        "storms": fresh("storms"),
        "tides": tides_today(reports.get("tides", (None, None))[0], tide_station, now),
        "checked": checked,
    }
