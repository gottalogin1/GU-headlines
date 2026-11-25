"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
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
    # A news site or job board that has not worked for this many hours gets an alert.
    alert_after_hours: int = 24
    # How often the worker reads the weather (NWS alerts, forecast and airport
    # reading; the JTWC storm list), in minutes. 0 turns the warning banner
    # and the weather box off.
    weather_interval_minutes: int = 10
    # The password of the private visitor stats page (/stats). Empty: no
    # counting, no stats page, and story links go straight to the news sites.
    stats_password: str = ""

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
        weather_interval_minutes=max(5, weather_minutes) if weather_minutes > 0 else 0,
        stats_password=env.get("STATS_PASSWORD", "").strip(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
