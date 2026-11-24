"""Guam weather for the website: the warning banner and the weather box.

The worker reads four public sources every WEATHER_INTERVAL_MINUTES and saves
the last good copy of each in the weather table:

- alerts:     National Weather Service (NWS) watches, warnings and advisories
              for Guam and the Guam coastal waters
- conditions: the latest reading at Guam International Airport (PGUM)
- forecast:   the NWS forecast for Hagåtña
- storms:     the tropical storms and typhoons that the Joint Typhoon Warning
              Center (JTWC) has warnings for in the western North Pacific

All four are United States government data, free to use, with no key. The
website reads only the table, so a slow or broken source never slows a page.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

from lxml import etree
from sqlalchemy.dialects.postgresql import insert

from .db import session_scope
from .models import WeatherReport
from .scraper.http import HttpClient
from .settings import Settings

log = logging.getLogger(__name__)

NWS_API = "https://api.weather.gov"
# GUZ001 is Guam. PMZ151 is the Guam coastal waters.
ALERTS_URL = f"{NWS_API}/alerts/active?zone=GUZ001,PMZ151&status=actual"
OBSERVATION_URL = f"{NWS_API}/stations/PGUM/observations/latest"
# Hagåtña. NWS says that the forecast grid of a place can change, so each
# check asks for the address of the forecast again.
POINT_URL = f"{NWS_API}/points/13.4757,144.7489"
JTWC_FEED = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"
# The pages that the site sends readers to for more.
NWS_GUAM_URL = "https://www.weather.gov/gum/"
JTWC_URL = "https://www.metoc.navy.mil/jtwc/jtwc.html"

# The weather box shows the conditions, forecast and storm list only while
# they are this new. Alerts stay until their end time.
STALE_AFTER = timedelta(hours=6)
FORECAST_PERIODS = 4

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


def forecast_url(point: dict) -> str:
    url = (point.get("properties") or {}).get("forecast") or ""
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
    return periods[:FORECAST_PERIODS]


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


# ------------------------------------------------------------------ checks


def _json(client: HttpClient, url: str) -> dict:
    return json.loads(client.get(url, accept="application/geo+json").content)


def _read_forecast(client: HttpClient) -> list[dict]:
    return parse_forecast(_json(client, forecast_url(_json(client, POINT_URL))))


READERS = {
    "alerts": lambda client: parse_alerts(_json(client, ALERTS_URL)),
    "conditions": lambda client: parse_observation(_json(client, OBSERVATION_URL)),
    "forecast": _read_forecast,
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


def check_weather(settings: Settings, client: HttpClient | None = None) -> list[tuple]:
    """Read each source and save each one that worked; a source that fails
    keeps its last good copy. Returns (kind, data, error) for each source."""
    own_client = client is None
    if own_client:
        client = HttpClient(
            settings.user_agent,
            timeout=settings.request_timeout,
            per_host_delay=settings.per_host_delay,
            # api.weather.gov's robots.txt turns all robots away, to keep
            # search engines out of the API. NWS publishes the API for
            # programs like this one and asks only for a User-Agent.
            respect_robots=False,
        )
    results = []
    try:
        for kind, read in READERS.items():
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


def for_page(reports: dict[str, tuple], now: datetime) -> dict:
    """What the pages show, from the saved copies ({kind: (data, fetched_at)}).

    An alert stays until its end time, also when the worker cannot reach NWS:
    a typhoon warning must not go away because a check failed. The other
    parts are shown only while they are new (STALE_AFTER)."""

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
        "checked": checked,
    }
