"""What search engines and link previews read: each page's canonical address,
description and robots rule, its Open Graph card, structured data (JSON-LD),
and the sitemap."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit
from xml.sax.saxutils import escape as xml_escape

from fastapi import Request
from markupsafe import Markup

from ..scraper.text import GUAM_TZ
from ..settings import get_settings

settings = get_settings()

# The most addresses one sitemap file may list.
SITEMAP_LIMIT = 50_000

# The picture link previews show (Facebook, Messenger, X, iMessage, Slack...):
# a 1200 x 630 JPEG in the static folder (_seo.html gives its size and type).
SOCIAL_CARD = "social-card.jpg"

# The site's logo for search results and AI answers: the square "GU" mark
# (Google wants at least 112 x 112).
LOGO = "apple-touch-icon.png"
LOGO_SIZE = 180

# Guam, the place every page is about. The Wikipedia and Wikidata links tell
# search engines and AI assistants which "Guam" is meant.
GUAM = {
    "@type": "Place",
    "name": "Guam",
    "sameAs": ["https://en.wikipedia.org/wiki/Guam", "https://www.wikidata.org/wiki/Q16635"],
}

# How many stories of a page its structured data lists.
ITEM_LIST_LIMIT = 10


def update_text() -> str:
    """How often the news is checked, in words: "every hour", "every 30 minutes"."""
    minutes = settings.scrape_interval_minutes
    if minutes % 60:
        return f"every {minutes} minutes"
    hours = minutes // 60
    return "every hour" if hours == 1 else f"every {hours} hours"


def home_title() -> str:
    """The home page's title: the site's name and the words people search for."""
    return f"{settings.site_name}: latest Guam news today from every newsroom"


def default_description() -> str:
    return (
        f"Today's Guam news and headlines from the island's newsrooms in one place, "
        f"updated {update_text()}. Plus Guam weather, tides, events, jobs and bids."
    )


def home_description(newsrooms: list[str]) -> str:
    """The home page's summary, naming the first few newsrooms (sources.yaml
    order), which people also search for."""
    if len(newsrooms) < 3:
        return default_description()
    named, more = newsrooms[:3], len(newsrooms) - 3
    names = f"{named[0]}, {named[1]}, {named[2]}" + (f" and {more} more newsrooms" if more else "")
    text = f"Today's Guam news from {names}, updated {update_text()}."
    # Search results show about 160 characters.
    endings = (" Plus Guam weather, tides, events and jobs.", " Plus Guam weather and jobs.")
    for more_text in endings:
        if len(text + more_text) <= 165:
            return text + more_text
    return text if len(text) <= 165 else default_description()


def base_url(request: Request) -> str:
    """https://news.example.com: SITE_URL when set, else the address this request came to."""
    return settings.site_url or str(request.base_url).rstrip("/")


def absolute_url(request: Request, path: str) -> str:
    return base_url(request) + path


def canonical_url(request: Request, **params: object) -> str:
    """This page's one address for search engines: no tracking or other stray
    query parameters, and no ?page=1."""
    query = {k: v for k, v in params.items() if v not in (None, "")}
    if query.get("page") == 1:
        del query["page"]
    url = absolute_url(request, quote(request.url.path))
    return f"{url}?{urlencode(query)}" if query else url


def json_ld(data: dict) -> Markup:
    """`data` as JSON that is safe inside <script type="application/ld+json">:
    <, > and & are written as \\u escapes, so text such as "</script>" in a
    headline can never end the block."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return Markup(text)


def _host(request: Request) -> str:
    """guamheadlines.com: the site's address without https:// or www."""
    return (urlsplit(base_url(request)).hostname or "").removeprefix("www.")


def website_data(request: Request) -> dict:
    """The site's name and address, which Google uses for the name shown in
    results; its address is given as another name for it."""
    data = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "@id": absolute_url(request, "/#website"),
        "name": settings.site_name,
        "url": absolute_url(request, "/"),
        "description": default_description(),
        "inLanguage": "en-US",
        "about": GUAM,
        "publisher": {"@id": absolute_url(request, "/#organization")},
    }
    host = _host(request)
    if host and host.lower() != settings.site_name.lower():
        data["alternateName"] = host
    return data


def organization_data(request: Request, static_url) -> dict:
    """Who runs the site, with its logo and its pages elsewhere
    (SOCIAL_PROFILES): what search engines and AI assistants show about the
    site itself."""
    logo = absolute_url(request, static_url(LOGO))
    data = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "@id": absolute_url(request, "/#organization"),
        "name": settings.site_name,
        "url": absolute_url(request, "/"),
        "logo": {
            "@type": "ImageObject",
            "url": logo,
            "width": LOGO_SIZE,
            "height": LOGO_SIZE,
        },
        "description": (
            f"{settings.site_name} collects Guam news from the island's newsrooms "
            f"{update_text()} and links each story to the original."
        ),
        "areaServed": GUAM,
    }
    host = _host(request)
    if host and host.lower() != settings.site_name.lower():
        data["alternateName"] = host
    if settings.social_profiles:
        data["sameAs"] = list(settings.social_profiles)
    return data


def webpage_data(
    request: Request,
    meta: PageMeta,
    page_type: str,
    items: Iterable[tuple[str, str]] = (),
    modified: datetime | None = None,
) -> dict:
    """The page itself: what kind of page it is (a list of stories is a
    CollectionPage), that it is about Guam, when it last changed, and the
    first stories it lists ([(headline, the story's address)]), each at its
    publisher."""
    data = {
        "@context": "https://schema.org",
        "@type": page_type,
        "@id": meta.canonical,
        "url": meta.canonical,
        "name": meta.title,
        "description": meta.description,
        "inLanguage": "en-US",
        "isPartOf": {"@id": absolute_url(request, "/#website")},
        "about": GUAM,
    }
    if modified is not None:
        data["dateModified"] = modified.astimezone(GUAM_TZ).isoformat(timespec="seconds")
    elements = [
        {"@type": "ListItem", "position": n, "url": url, "name": name[:200]}
        for n, (name, url) in enumerate(items, start=1)
        if n <= ITEM_LIST_LIMIT
    ]
    if elements:
        data["mainEntity"] = {"@type": "ItemList", "itemListElement": elements}
    return data


def breadcrumb_data(request: Request, trail: Iterable[tuple[str, str | None]]) -> dict:
    """[(name, path)] from the home page down to this page, for the breadcrumb
    trail search results can show in place of the address."""
    items = []
    for position, (name, path) in enumerate(trail, start=1):
        item = {"@type": "ListItem", "position": position, "name": name}
        if path:
            item["item"] = absolute_url(request, path)
        items.append(item)
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items}


def guide_data(request: Request, guide) -> dict:
    """A visitor guide (guides.py) as an article: its title, summary, cover
    photo and dates, which search engines can show with it."""
    url = absolute_url(request, f"/guides/{guide.slug}")
    site = {"@type": "Organization", "name": settings.site_name, "url": absolute_url(request, "/")}
    data = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": guide.title[:110],
        "description": guide.summary,
        "url": url,
        "mainEntityOfPage": url,
        "dateModified": guide.updated_at.isoformat(timespec="seconds"),
        "inLanguage": "en",
        "author": site,
        "publisher": site,
    }
    if guide.published_at:
        data["datePublished"] = guide.published_at.isoformat(timespec="seconds")
    if guide.photo:
        data["image"] = absolute_url(request, guide.photo)
    if guide.access == "paid":
        # Only guide pack readers read it all (guide_packs.py); search
        # engines see the opening, as every reader without a pack does.
        data["isAccessibleForFree"] = False
    return data


# A place that is only online. Google lists events at a physical place only.
_ONLINE_RE = re.compile(r"\b(zoom|webex|teams|online|virtual|teleconference|live ?stream)\b", re.I)


def event_data(
    event, *, url: str, calendar_name: str, calendar_homepage: str | None
) -> dict | None:
    """Event structured data for an event's own page (`url`), so that Google
    can list the event in its event results. None when the event has no
    physical place, which Google needs."""
    place = event.location
    if not place or _ONLINE_RE.search(place):
        return None
    start = event.starts_at.astimezone(GUAM_TZ)
    end = event.ends_at.astimezone(GUAM_TZ) if event.ends_at else None
    if event.all_day:
        # Dates only. An all-day event ends at the start of the day after its last.
        last = (end - timedelta(days=1)).date() if end else start.date()
        start_date, end_date = start.date().isoformat(), max(last, start.date()).isoformat()
    else:
        start_date, end_date = start.isoformat(), end.isoformat() if end else None
    organizer = {"@type": "Organization", "name": event.organizer or calendar_name}
    if calendar_homepage:
        organizer["url"] = calendar_homepage
    data = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": event.title,
        "url": url,
        "startDate": start_date,
        # Events marked cancelled or postponed are not collected.
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {
            "@type": "Place",
            "name": place,
            "address": {"@type": "PostalAddress", "streetAddress": place, "addressCountry": "GU"},
        },
        "organizer": organizer,
    }
    if end_date:
        data["endDate"] = end_date
    if event.description:
        data["description"] = event.description
    return data


@dataclass
class PageMeta:
    title: str
    full_title: str
    description: str
    image: str
    canonical: str | None = None
    robots: str | None = None
    prev: str | None = None
    next: str | None = None
    structured: list[Markup] = field(default_factory=list)


def page_meta(
    request: Request,
    *,
    title: str | None,
    description: str | None = None,
    robots: str | None = None,
    canonical: dict | None = None,
    breadcrumbs: list[tuple[str, str | None]] | None = None,
    structured: list[dict] | None = None,
    page_type: str | None = None,
    items: Iterable[tuple[str, str]] = (),
    modified: datetime | None = None,
    result=None,
    static_url=None,
) -> PageMeta:
    """Everything _seo.html writes into a page's <head>.

    `canonical` holds the query parameters that make this page distinct
    (e.g. {"page": 2}); None means the page gets no canonical link (search
    results, error pages, pages search engines are told to skip).
    `structured` is the page's own structured data, such as an event.
    `page_type` ("CollectionPage", "WebPage", "AboutPage") adds a description
    of the page itself, with `items` (the stories it lists) and `modified`
    (when it last changed).
    """
    page = getattr(result, "page", 1) if result is not None else 1
    later = isinstance(page, int) and page > 1
    # The browser tab and search results show the page's own title followed
    # by the site's name; the home page's title starts with the name.
    if title or later:
        # The home page's later pages are not the home page.
        title = title or "Latest Guam news"
        if later:
            # Each page of a list has its own title.
            title = f"{title}, page {page}"
        full_title = f"{title} · {settings.site_name}"
    else:
        title = full_title = home_title()
    meta = PageMeta(
        title=title,
        full_title=full_title,
        description=description or default_description(),
        image=absolute_url(request, static_url(SOCIAL_CARD)),
        robots=robots,
    )
    if canonical is not None and not robots:
        meta.canonical = canonical_url(request, **canonical)
        if result is not None and "page" in canonical:
            if result.has_prev:
                meta.prev = canonical_url(request, **{**canonical, "page": result.page - 1})
            if result.has_next:
                meta.next = canonical_url(request, **{**canonical, "page": result.page + 1})
    if request.url.path == "/" and meta.canonical and not meta.prev:
        meta.structured.append(json_ld(website_data(request)))
    if request.url.path in ("/", "/status") and meta.canonical and not meta.prev:
        meta.structured.append(json_ld(organization_data(request, static_url)))
    if page_type and meta.canonical:
        meta.structured.append(json_ld(webpage_data(request, meta, page_type, items, modified)))
    if breadcrumbs and meta.canonical:
        trail = [("Home", "/"), *breadcrumbs]
        meta.structured.append(json_ld(breadcrumb_data(request, trail)))
    if meta.canonical:
        meta.structured.extend(json_ld(data) for data in structured or [])
    return meta


# ------------------------------------------------------------------ sitemap


def _lastmod(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sitemap_xml(entries: Iterable[tuple[str, datetime | None]]) -> str:
    """A sitemap (sitemaps.org) for [(absolute address, last changed)]."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for count, (url, changed) in enumerate(entries):
        if count == SITEMAP_LIMIT:
            break
        lastmod = _lastmod(changed)
        lines.append(
            f"<url><loc>{xml_escape(url)}</loc>"
            + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
            + "</url>"
        )
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def sitemap_entries(
    request: Request,
    data: dict,
    categories,
    sources,
    weather: bool = False,
    events: Iterable[int] = (),
    outages: bool = False,
    bid_alerts: bool = False,
    listings: Iterable[str] = (),
    advertise: bool = False,
    guides: Iterable[tuple[str, datetime]] = (),
    seen: dict[str, datetime | None] | None = None,
) -> list[tuple[str, datetime | None]]:
    """Every page worth finding in a search: home, topics, news sources, jobs, events
    and each upcoming event's page (`events`: their IDs), bids (and the bid alerts
    page, when `bid_alerts` is on), the Weather page (when `weather` is on), the
    Outages page (when `outages` is on), the Advertise page (when `advertise` is
    on), the Sources page, and
    the archive down to each day that has stories, and the pages of paid
    listings that are on (`listings`: their paths), and the visitor guides
    page with each published guide (`guides`: (slug, last changed)). `data`
    comes from queries.sitemap_data; `sources` are the slugs of news sources to list.
    `seen` (queries.newest_seen) gives the Jobs, Events and Bids pages the time
    their newest item came in."""
    url = lambda path: absolute_url(request, path)  # noqa: E731
    seen = seen or {}
    latest = data["latest"]
    entries: list[tuple[str, datetime | None]] = [(url("/"), latest)]
    for c in categories:
        if c.slug in data["categories"]:
            entries.append((url(f"/category/{c.slug}"), data["categories"][c.slug]))
    for slug in sources:
        if slug in data["sources"]:
            entries.append((url(f"/source/{slug}"), data["sources"][slug]))
    entries.append((url("/jobs"), seen.get("jobs")))
    entries.append((url("/events"), seen.get("events")))
    entries.extend((url(f"/events/{event_id}"), None) for event_id in events)
    entries.append((url("/bids"), seen.get("bids")))
    entries.append((url("/bids/awards"), None))
    if bid_alerts:
        entries.append((url("/bids/alerts"), None))
    entries.extend((url(path), None) for path in listings)
    if weather:
        entries.append((url("/weather"), None))
    if outages:
        entries.append((url("/outages"), None))
    if advertise:
        entries.append((url("/advertise"), None))
    guides = list(guides)
    if guides:
        entries.append((url("/guides"), max(changed for _, changed in guides)))
        entries.extend((url(f"/guides/{slug}"), changed) for slug, changed in guides)
    entries.append((url("/status"), None))
    if data["days"]:
        entries.append((url("/archive"), latest))
        months: dict[tuple[int, int], datetime] = {}
        for day, newest in data["days"]:
            key = (day.year, day.month)
            months[key] = max(newest, months.get(key, newest))
        entries.extend((url(f"/archive/{y}/{m}"), newest) for (y, m), newest in months.items())
        entries.extend((url(_day_path(day)), newest) for day, newest in data["days"])
    return entries


def _day_path(day: date) -> str:
    return f"/archive/{day.year}/{day.month}/{day.day}"
