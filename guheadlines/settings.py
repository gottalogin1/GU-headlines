"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

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
    user_agent: str = (
        "Mozilla/5.0 (compatible; GUHeadlinesBot/1.0; +https://github.com/gottalogin1/GU-headlines)"
    )
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
    return Settings(
        database_url=_normalize_db_url(db_url),
        config_dir=Path(env.get("CONFIG_DIR", PROJECT_DIR / "config")),
        media_dir=Path(env.get("MEDIA_DIR", PROJECT_DIR / "data" / "media")),
        site_name=env.get("SITE_NAME", defaults.site_name),
        site_tagline=env.get("SITE_TAGLINE", defaults.site_tagline),
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
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
