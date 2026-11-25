"""Sunrise, sunset and the phase of the moon on Guam, for the Weather page.

They are worked out here from the date, with the usual formulas (the
"sunrise equation" that NOAA's solar calculator is also based on, and the
average length of a lunar month), so no service is needed. Sunrise and
sunset come out within a minute or two of NOAA's tables; the moon's phase can
be a few hours early or late, which is fine for its name.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

from .scraper.text import GUAM_TZ

# Hagåtña.
LATITUDE = 13.4757
LONGITUDE = 144.7489

J2000 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)  # Julian day 2451545.0
OBLIQUITY = math.radians(23.4397)  # the tilt of the Earth's axis
# The sun's centre is 0.833° below the horizon when its top edge rises: its
# half-width plus the bending of light by the air.
HORIZON = math.radians(-0.833)

SYNODIC_MONTH = 29.530588853  # days from one new moon to the next, on average
NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
PHASES = (
    "New moon",
    "Waxing crescent",
    "First quarter",
    "Waxing gibbous",
    "Full moon",
    "Waning gibbous",
    "Last quarter",
    "Waning crescent",
)


def sun_times(
    day: date, lat: float = LATITUDE, lng: float = LONGITUDE
) -> tuple[datetime, datetime]:
    """Sunrise and sunset (in UTC) on `day` in Guam."""
    # Days from 1 Jan 2000 to the sun's highest point that day (east is +).
    j = (day - date(2000, 1, 1)).days - lng / 360
    anomaly = math.radians((357.5291 + 0.98560028 * j) % 360)
    centre = (
        1.9148 * math.sin(anomaly) + 0.02 * math.sin(2 * anomaly) + 0.0003 * math.sin(3 * anomaly)
    )
    longitude = math.radians((math.degrees(anomaly) + centre + 180 + 102.9372) % 360)
    noon = j + 0.0053 * math.sin(anomaly) - 0.0069 * math.sin(2 * longitude)
    declination = math.asin(math.sin(longitude) * math.sin(OBLIQUITY))
    phi = math.radians(lat)
    cos_hour = (math.sin(HORIZON) - math.sin(phi) * math.sin(declination)) / (
        math.cos(phi) * math.cos(declination)
    )
    half_day = math.degrees(math.acos(cos_hour)) / 360
    return J2000 + timedelta(days=noon - half_day), J2000 + timedelta(days=noon + half_day)


def moon_phase(when: datetime) -> str:
    """The name of the moon's phase at `when`: "Full moon", "Waxing crescent"..."""
    age = (when - NEW_MOON).total_seconds() / 86400 % SYNODIC_MONTH
    return PHASES[round(age / SYNODIC_MONTH * 8) % 8]


def for_day(day: date) -> dict:
    """Sunrise, sunset and the moon's phase (at noon) on `day` in Guam."""
    sunrise, sunset = sun_times(day)
    noon = datetime.combine(day, time(12), tzinfo=GUAM_TZ)
    return {"sunrise": sunrise, "sunset": sunset, "moon": moon_phase(noon)}
