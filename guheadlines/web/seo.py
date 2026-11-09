"""What search engines and link previews read: each page's canonical address,
description and robots rule, its Open Graph card, structured data (JSON-LD),
and the sitemap."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as xml_escape

from fastapi import Request
from markupsafe import Markup

from ..settings import get_settings

settings = get_settings()

# The most addresses one sitemap file may list.
SITEMAP_LIMIT = 50_000

# The picture link previews show (Facebook, Messenger, X, iMessage, Slack...):
# a 1200 x 630 JPEG in the static folder (_seo.html gives its size and type).
SOCIAL_CARD = "social-card.jpg"


def default_description() -> str:
    return (
        f"{settings.site_tagline}: local stories, military, business, labor, tourism, "
        "sports, food and Saipan news from Guam's newsrooms in one place."
    )


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


def website_data(request: Request) -> dict:
    """The site's name and address, which Google uses for the name shown in results."""
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": settings.site_name,
        "url": absolute_url(request, "/"),
        "description": default_description(),
        "inLanguage": "en",
    }


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


@dataclass
class PageMeta:
    title: str
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
    result=None,
    static_url=None,
) -> PageMeta:
    """Everything _seo.html writes into a page's <head>.

    `canonical` holds the query parameters that make this page distinct
    (e.g. {"page": 2}); None means the page gets no canonical link (search
    results, error pages, pages search engines are told to skip).
    """
    meta = PageMeta(
        title=title or f"{settings.site_name} · {settings.site_tagline}",
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
    if breadcrumbs and meta.canonical:
        trail = [("Home", "/"), *breadcrumbs]
        meta.structured.append(json_ld(breadcrumb_data(request, trail)))
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
    request: Request, data: dict, categories, sources
) -> list[tuple[str, datetime | None]]:
    """Every page worth finding in a search: home, topics, news sources, jobs,
    and the archive down to each day that has stories. `data` comes from
    queries.sitemap_data; `sources` are the slugs of news sources to list."""
    url = lambda path: absolute_url(request, path)  # noqa: E731
    latest = data["latest"]
    entries: list[tuple[str, datetime | None]] = [(url("/"), latest)]
    for c in categories:
        if c.slug in data["categories"]:
            entries.append((url(f"/category/{c.slug}"), data["categories"][c.slug]))
    for slug in sources:
        if slug in data["sources"]:
            entries.append((url(f"/source/{slug}"), data["sources"][slug]))
    entries.append((url("/jobs"), None))
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
