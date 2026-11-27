"""IndexNow: telling Bing, and the other search engines that take part
(Yandex, Naver, Seznam, Yep), which pages are new or changed, so they read
them within minutes instead of days. Bing's results also feed Microsoft
Copilot, DuckDuckGo, Yahoo and the web searches of AI assistants such as
ChatGPT. Google does not take part; it reads the sitemap.

After each news check, the worker sends the addresses of the pages that
changed: the home page, the topic and news-site pages with new stories, the
archive day, and the Events, Jobs, Bids, guide and press release pages when
they have something new. When the worker starts, it sends the main pages once.

The key, a code that shows the search engine that these addresses come from
the site itself, is made from the database address. So it is the same in the
web and worker containers, stays the same from one day to the next, and needs
no setting. The search engine checks it by reading /<key>.txt on the site
(web/app.py).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
from collections.abc import Iterable
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .guides import PUBLISHED
from .listings import KINDS, LIVE
from .listings import is_on as listing_is_on
from .models import Article, Bid, Event, Guide, Job, Listing, Source
from .scraper.text import GUAM_TZ
from .settings import Settings

log = logging.getLogger(__name__)

# One address takes every search engine that takes part: they share what
# they are sent.
ENDPOINT = "https://api.indexnow.org/indexnow"
# The most addresses one request may hold.
BATCH = 10_000


def key(settings: Settings) -> str:
    """The site's IndexNow key: 32 letters and digits, made from the
    database address (a secret), so nobody can work it out from the site."""
    digest = hmac.new(settings.database_url.encode(), b"indexnow", hashlib.sha256)
    return digest.hexdigest()[:32]


def site(settings: Settings) -> str | None:
    """The site's public address (SITE_URL, or https:// and SITE_DOMAIN), or
    None when it has none, such as a copy on your own computer."""
    url = settings.site_url
    host = (urlsplit(url).hostname or "").lower() if url else ""
    if not host or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return url
    return None  # an IP address, not a domain name


def is_on(settings: Settings) -> bool:
    return settings.indexnow and site(settings) is not None


def main_paths(settings: Settings, categories: Iterable[str], guides: bool) -> list[str]:
    """The site's main pages, sent once when the worker starts."""
    paths = ["/", "/events", "/jobs", "/bids", "/archive", "/status"]
    if settings.weather_interval_minutes:
        paths.append("/weather")
    if settings.outages_interval_minutes:
        paths.append("/outages")
    if guides:
        paths.append("/guides")
    paths.extend(KINDS[kind].page for kind in ("press", "deal") if listing_is_on(settings, kind))
    paths.extend(f"/category/{slug}" for slug in categories)
    return paths


def changed_paths(session, since: datetime, upcoming_events: Iterable[int] = ()) -> list[str]:
    """The pages that changed since `since`: those with stories, events, jobs,
    bids, guides or press releases that came in (or were edited or approved)
    after it. `upcoming_events`
    are the IDs of the events that have a page (queries.upcoming_event_ids)."""
    paths: dict[str, None] = {}
    rows = session.execute(
        select(Article.categories, Article.published_at, Source.slug)
        .join(Source, Source.id == Article.source_id)
        .where(Article.first_seen_at >= since)
    ).all()
    if rows:
        paths["/"] = None
    days = set()
    for categories, published_at, source in rows:
        for slug in categories:
            paths[f"/category/{slug}"] = None
        paths[f"/source/{source}"] = None
        days.add(published_at.astimezone(GUAM_TZ).date())
    for day in sorted(days, reverse=True):
        paths[f"/archive/{day.year}/{day.month}/{day.day}"] = None
        paths[f"/archive/{day.year}/{day.month}"] = None
    new_events = set(session.scalars(select(Event.id).where(Event.first_seen_at >= since)))
    with_page = [event_id for event_id in upcoming_events if event_id in new_events]
    if with_page:
        paths["/events"] = None
        paths.update((f"/events/{event_id}", None) for event_id in with_page)
    if session.scalar(select(Job.id).where(Job.first_seen_at >= since).limit(1)):
        paths["/jobs"] = None
    if session.scalar(select(Bid.id).where(Bid.first_seen_at >= since).limit(1)):
        paths["/bids"] = None
    guides = session.scalars(
        select(Guide.slug).where(Guide.status == PUBLISHED, Guide.updated_at >= since)
    ).all()
    if guides:
        paths["/guides"] = None
        paths.update((f"/guides/{slug}", None) for slug in guides)
    releases = session.scalars(
        select(Listing.id).where(
            Listing.kind == "press", Listing.status == LIVE, Listing.reviewed_at >= since
        )
    ).all()
    if releases:
        paths["/press"] = None
        paths.update((f"/press/{listing_id}", None) for listing_id in releases)
    return list(paths)


def submit(
    settings: Settings, paths: Iterable[str], transport: httpx.BaseTransport | None = None
) -> int:
    """Send these pages' addresses. Returns how many were taken (0 when
    IndexNow is off or the search engines refused them; the log says why)."""
    base = site(settings)
    paths = list(dict.fromkeys(paths))
    if not (settings.indexnow and base and paths):
        return 0
    site_key = key(settings)
    sent = 0
    for start in range(0, len(paths), BATCH):
        payload = {
            "host": urlsplit(base).hostname,
            "key": site_key,
            "keyLocation": f"{base}/{site_key}.txt",
            "urlList": [base + path for path in paths[start : start + BATCH]],
        }
        try:
            with httpx.Client(timeout=20, transport=transport) as client:
                response = client.post(
                    ENDPOINT, json=payload, headers={"User-Agent": settings.user_agent}
                )
        except httpx.HTTPError as exc:
            log.warning("IndexNow: could not reach %s: %s", ENDPOINT, type(exc).__name__)
            return sent
        if response.status_code in (200, 202):
            sent += len(payload["urlList"])
            continue
        reasons = {
            403: f"the key was not accepted; is {payload['keyLocation']} reachable from outside?",
            422: f"the addresses are not on {payload['host']} (check SITE_URL)",
            429: "too many requests; the next news check sends them again",
        }
        log.warning(
            "IndexNow: %s answered %d: %s",
            ENDPOINT,
            response.status_code,
            reasons.get(response.status_code, "addresses not sent"),
        )
        return sent
    return sent
