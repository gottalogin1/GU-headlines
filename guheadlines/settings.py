"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str
    config_dir: Path
    media_dir: Path
    site_name: str = "GU Headlines"
    site_tagline: str = "Guam news, updated every hour"
    # The site's public address (https://news.example.com), used for the links
    # search engines are given: canonical links, the sitemap, social cards.
    # Empty: https://<first SITE_DOMAIN name> when SITE_DOMAIN is set (the
    # domain Caddy gets a certificate for), else each request's own address.
    site_url: str = ""
    # Optional ownership proof for Google Search Console and Bing Webmaster
    # Tools: the content="..." value of the meta tag each one offers.
    google_site_verification: str = ""
    bing_site_verification: str = ""
    # Google AdSense: the publisher ID (ca-pub-...) and the ad unit's slot
    # number, both from the ad code AdSense gives. With both, an ad block is
    # shown beside the stories on the home, Sources and Jobs pages.
    adsense_client: str = ""
    adsense_slot: str = ""
    user_agent: str = "Mozilla/5.0 (compatible; GUHeadlinesBot/1.0)"
    scrape_interval_minutes: int = 60
    # When a run leaves pages behind (site rate-limited us, or the per-run cap
    # was reached), come back for just those sources after this many seconds.
    catch_up_seconds: int = 60
    scrape_on_start: bool = True
    request_timeout: float = 25.0
    per_host_delay: float = 1.5
    max_workers: int = 4
    max_new_per_source: int = 40
    respect_robots: bool = True
    store_images: bool = True
    image_max_width: int = 720
    page_size: int = 30
    log_level: str = "INFO"
    # How often the worker reads the job boards in config/jobs.yaml.
    jobs_interval_hours: int = 6
    # A free key from developer.usajobs.gov, and the email it was issued to,
    # for the USAJOBS board; without them that board is skipped.
    usajobs_api_key: str = ""
    usajobs_email: str = ""
    # How often the worker reads the event calendars in config/events.yaml.
    events_interval_hours: int = 3
    # A free Google API key with the Google Calendar API turned on, for the
    # calendars in events.yaml that are public Google Calendars.
    google_calendar_api_key: str = ""
    # Alerts are posted to the Discord channel of this webhook link
    # (https://discord.com/api/webhooks/...). Empty: no alerts. See alerts.py.
    discord_webhook_url: str = ""
    # A news site, job board or event calendar that has not worked for this
    # many hours gets an alert.
    alert_after_hours: int = 24
    # Words and phrases: a new story whose headline or intro mentions one of
    # them is posted in Discord (see alerts.check_keywords). Empty: no
    # keyword alerts.
    alert_keywords: tuple[str, ...] = ()
    # How often the worker reads the weather (NWS alerts, forecast and airport
    # reading; the JTWC storm list), in minutes. 0 turns the warning banner
    # and the weather box off.
    weather_interval_minutes: int = 10
    # The NOAA tide station whose high and low tides the weather box shows.
    # 1630000 is Apra Harbor. Empty: no tides.
    tide_station: str = "1630000"
    # The password of the private visitor stats page (/stats). Empty: no
    # counting, no stats page, and story links go straight to the news sites.
    stats_password: str = ""
    # The newsletter feed (/feed.xml, see newsletter.py): a new edition of the
    # top stories is made on these days of the week (0 is Monday), at
    # newsletter_time, Guam time. No days: no feed.
    newsletter_days: tuple[int, ...] = (1, 4)
    newsletter_time: time = time(10, 0)
    # The number of stories in each edition.
    newsletter_stories: int = 12

    @property
    def sources_file(self) -> Path:
        return self.config_dir / "sources.yaml"

    @property
    def categories_file(self) -> Path:
        return self.config_dir / "categories.yaml"

    @property
    def image_dir(self) -> Path:
        return self.media_dir / "images"


def _normalize_db_url(url: str) -> str:
    # Accept plain postgres:// URLs and point SQLAlchemy at psycopg 3.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _site_url(value: str | None) -> str:
    """https://news.example.com (no trailing slash), or "" if not a web address."""
    value = (value or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return value


def _domain_site_url(value: str | None) -> str:
    """SITE_DOMAIN "guamheadlines.com, www.guamheadlines.com" ->
    https://guamheadlines.com (the first name), or "" if empty or not a name."""
    names = (value or "").replace(",", " ").split()
    if not names:
        return ""
    name = names[0].removeprefix("https://").removeprefix("http://").rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", name):
        return ""
    return "https://" + name.lower()


def _adsense_client(value: str | None) -> str:
    """ca-pub-1234567890123456, or "" if the value has no publisher ID. AdSense
    shows the ID in different forms: without "ca-" on its account page, inside
    the ads.txt line, and inside its verification code. The ID is taken out of
    any of them."""
    found = re.search(r"\bpub-(\d{10,20})\b", value or "")
    return f"ca-pub-{found.group(1)}" if found else ""


def _adsense_slot(value: str | None) -> str:
    value = (value or "").strip()
    return value if re.fullmatch(r"\d{5,20}", value) else ""


def _tide_station(value: str | None, default: str) -> str:
    """A NOAA station ID (letters and digits, such as 1630000), the default
    when the setting is missing, or "" (no tides) when it is empty or not an
    ID."""
    if value is None:
        return default
    value = value.strip()
    return value if re.fullmatch(r"[A-Za-z0-9]{1,16}", value) else ""


def _words(value: str | None) -> tuple[str, ...]:
    """ALERT_KEYWORDS "typhoon, shooting, power outage" -> ("typhoon",
    "shooting", "power outage"). Commas separate the words; quotes around one
    are dropped, and so are repeats."""
    words = (part.strip().strip("\"'").strip() for part in (value or "").split(","))
    return tuple(dict.fromkeys(word for word in words if word.strip("*")))


DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _days(value: str | None, default: tuple[int, ...]) -> tuple[int, ...]:
    """NEWSLETTER_DAYS "tue,fri" -> (1, 4) (0 is Monday). Each day is its
    English name or the first three letters of it. Empty: () (no feed).
    Missing, or no day name in it: the default."""
    if value is None:
        return default
    words = value.replace(",", " ").lower().split()
    if not words:
        return ()
    short = [name[:3] for name in DAY_NAMES]
    days = {short.index(word[:3]) for word in words if word[:3] in short}
    return tuple(sorted(days)) or default


def _clock(value: str | None, default: time) -> time:
    """NEWSLETTER_TIME "10:00", "14:30", "9" or "2:30 pm" -> a time of day;
    the default when the setting is missing or not a time."""
    text = (value or "").strip().lower()
    found = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(?:\s*([ap])\.?m?\.?)?", text)
    if not found:
        return default
    hour, minute = int(found[1]), int(found[2] or 0)
    if found[3]:  # a 12-hour clock: 12 am is midnight, 12 pm is noon
        if not 1 <= hour <= 12:
            return default
        hour = hour % 12 + (12 if found[3] == "p" else 0)
    if hour > 23 or minute > 59:
        return default
    return time(hour, minute)


def load_settings() -> Settings:
    env = os.environ
    db_url = env.get("DATABASE_URL")
    if not db_url:
        user = env.get("POSTGRES_USER", "guheadlines")
        password = env.get("POSTGRES_PASSWORD", "guheadlines")
        host = env.get("POSTGRES_HOST", "localhost")
        port = env.get("POSTGRES_PORT", "5432")
        name = env.get("POSTGRES_DB", "guheadlines")
        # Quote credentials so passwords with @, / or : still work.
        db_url = (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{name}"
        )

    defaults = Settings(database_url="", config_dir=Path(), media_dir=Path())
    weather_minutes = _int(env.get("WEATHER_INTERVAL_MINUTES"), defaults.weather_interval_minutes)
    return Settings(
        database_url=_normalize_db_url(db_url),
        config_dir=Path(env.get("CONFIG_DIR", PROJECT_DIR / "config")),
        media_dir=Path(env.get("MEDIA_DIR", PROJECT_DIR / "data" / "media")),
        site_name=env.get("SITE_NAME", defaults.site_name),
        site_tagline=env.get("SITE_TAGLINE", defaults.site_tagline),
        site_url=_site_url(env.get("SITE_URL")) or _domain_site_url(env.get("SITE_DOMAIN")),
        google_site_verification=env.get("GOOGLE_SITE_VERIFICATION", "").strip(),
        bing_site_verification=env.get("BING_SITE_VERIFICATION", "").strip(),
        adsense_client=_adsense_client(env.get("ADSENSE_CLIENT")),
        adsense_slot=_adsense_slot(env.get("ADSENSE_SLOT")),
        user_agent=env.get("SCRAPER_USER_AGENT", defaults.user_agent),
        scrape_interval_minutes=max(5, _int(env.get("SCRAPE_INTERVAL_MINUTES"), 60)),
        catch_up_seconds=max(30, _int(env.get("CATCH_UP_SECONDS"), 60)),
        scrape_on_start=_bool(env.get("SCRAPE_ON_START"), True),
        request_timeout=_float(env.get("REQUEST_TIMEOUT"), defaults.request_timeout),
        per_host_delay=_float(env.get("PER_HOST_DELAY"), defaults.per_host_delay),
        max_workers=max(1, _int(env.get("SCRAPER_WORKERS"), defaults.max_workers)),
        max_new_per_source=max(1, _int(env.get("MAX_NEW_PER_SOURCE"), defaults.max_new_per_source)),
        respect_robots=_bool(env.get("RESPECT_ROBOTS_TXT"), True),
        store_images=_bool(env.get("STORE_IMAGES"), True),
        image_max_width=_int(env.get("IMAGE_MAX_WIDTH"), defaults.image_max_width),
        page_size=max(5, min(100, _int(env.get("PAGE_SIZE"), defaults.page_size))),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        jobs_interval_hours=max(1, _int(env.get("JOBS_INTERVAL_HOURS"), 6)),
        usajobs_api_key=env.get("USAJOBS_API_KEY", "").strip(),
        usajobs_email=env.get("USAJOBS_EMAIL", "").strip(),
        events_interval_hours=max(1, _int(env.get("EVENTS_INTERVAL_HOURS"), 3)),
        google_calendar_api_key=env.get("GOOGLE_CALENDAR_API_KEY", "").strip(),
        discord_webhook_url=env.get("DISCORD_WEBHOOK_URL", "").strip(),
        alert_after_hours=max(1, _int(env.get("ALERT_AFTER_HOURS"), defaults.alert_after_hours)),
        alert_keywords=_words(env.get("ALERT_KEYWORDS")),
        weather_interval_minutes=max(5, weather_minutes) if weather_minutes > 0 else 0,
        tide_station=_tide_station(env.get("TIDE_STATION"), defaults.tide_station),
        stats_password=env.get("STATS_PASSWORD", "").strip(),
        newsletter_days=_days(env.get("NEWSLETTER_DAYS"), defaults.newsletter_days),
        newsletter_time=_clock(env.get("NEWSLETTER_TIME"), defaults.newsletter_time),
        newsletter_stories=max(
            1, min(50, _int(env.get("NEWSLETTER_STORIES"), defaults.newsletter_stories))
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
