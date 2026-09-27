"""Finding article URLs: RSS/Atom feeds, section/listing pages and sitemaps.

These functions only parse content that has already been fetched, which keeps
them easy to test; the pipeline decides what to fetch and when.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import feedparser
from bs4 import BeautifulSoup
from lxml import etree

from ..config import SourceConfig
from .text import clean_text, parse_datetime, strip_html, struct_time_to_datetime
from .urls import normalize_url, same_site


@dataclass
class Candidate:
    """A URL that might be a new article, plus whatever the feed told us about it."""

    url: str  # absolute URL to fetch
    key: str  # normalized URL used for de-duplication
    via: str  # 'feed' | 'listing' | 'sitemap'
    title: str | None = None
    summary: str | None = None
    image_url: str | None = None
    published_at: datetime | None = None
    author: str | None = None
    tags: list[str] = field(default_factory=list)


_FEED_TYPES = re.compile(r"application/(rss|atom)\+xml|application/(rdf\+)?xml|text/xml", re.I)
_IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
_DATE_PATH_RE = re.compile(r"/(19|20)\d{2}/\d{1,2}(/\d{1,2})?/")


# News-search feeds (Bing, Google) wrap article links in a redirect that carries
# the real URL in a query parameter.
_REDIRECT_HOSTS = ("bing.com", "google.com", "news.google.com")


def unwrap_redirect(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _REDIRECT_HOSTS):
        target = parse_qs(parts.query).get("url", [None])[0]
        if target and target.startswith(("http://", "https://")):
            return target
    return url


def make_candidate(url: str, via: str, base: str | None = None, **info) -> Candidate | None:
    absolute = normalize_url(unwrap_redirect(url), base=base)
    if not absolute:
        return None
    return Candidate(url=absolute, key=absolute, via=via, **info)


def _entry_image(entry) -> str | None:
    for media in entry.get("media_content", []) or []:
        url = media.get("url")
        medium = (media.get("medium") or media.get("type") or "image").lower()
        if url and ("image" in medium or re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.I)):
            return url
    for thumb in entry.get("media_thumbnail", []) or []:
        if thumb.get("url"):
            return thumb["url"]
    for enclosure in entry.get("enclosures", []) or []:
        if (enclosure.get("type") or "").startswith("image") and enclosure.get("href"):
            return enclosure["href"]
    for block in [entry.get("summary", "")] + [
        c.get("value", "") for c in entry.get("content", [])
    ]:
        match = _IMG_SRC_RE.search(block or "")
        if match:
            return match.group(1)
    return None


def parse_feed(content: bytes, feed_url: str) -> list[Candidate]:
    parsed = feedparser.parse(content)
    candidates: list[Candidate] = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not link:
            guid = entry.get("id") or ""
            link = guid if guid.startswith("http") else None
        if not link:
            continue
        published = struct_time_to_datetime(
            entry.get("published_parsed") or entry.get("updated_parsed")
        ) or parse_datetime(entry.get("published") or entry.get("updated"))
        summary = strip_html(entry.get("summary") or "")
        image = _entry_image(entry)
        candidate = make_candidate(
            link,
            "feed",
            base=feed_url,
            title=clean_text(entry.get("title")) or None,
            summary=summary or None,
            image_url=normalize_url(image, base=link) if image else None,
            published_at=published,
            author=clean_text(entry.get("author")) or None,
            tags=[clean_text(t.get("term")) for t in entry.get("tags", []) if t.get("term")],
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def looks_like_article_url(url: str) -> bool:
    """Heuristic used when a source has no article_pattern: a dated path or a long slug."""
    path = url.split("?", 1)[0].split("://", 1)[-1].partition("/")[2].strip("/")
    if not path:
        return False
    if _DATE_PATH_RE.search("/" + path + "/"):
        return True
    last = path.rsplit("/", 1)[-1]
    last = re.sub(r"\.(html?|php|aspx?)$", "", last)
    return last.count("-") >= 3 and len(last) >= 20


def parse_listing(
    content: bytes, page_url: str, source: SourceConfig
) -> tuple[list[Candidate], list[str]]:
    """Links to articles on a section/front page, and any feeds it advertises."""
    soup = BeautifulSoup(content, "lxml")
    page_key = (normalize_url(page_url) or page_url).rstrip("/")
    candidates: dict[str, Candidate] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = normalize_url(href, base=page_url)
        if not url or not same_site(url, page_url):
            continue
        # Several links often point at the same story (image, headline, "read
        # more"); keep the longest anchor text as a title hint.
        text = clean_text(anchor.get_text(" "))
        title = text if len(text) > 15 else None
        if url in candidates:
            existing = candidates[url]
            if title and len(title) > len(existing.title or ""):
                existing.title = title
            continue
        if url.rstrip("/") == page_key or source.is_excluded(url):
            continue
        if source.article_patterns:
            if not source.matches_article_pattern(url):
                continue
        elif not looks_like_article_url(url):
            continue
        candidates[url] = Candidate(url=url, key=url, via="listing", title=title)
    feeds: list[str] = []
    if source.autodiscover_feeds:
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            if "alternate" not in rel or not _FEED_TYPES.search(link.get("type") or ""):
                continue
            feed_url = normalize_url(link["href"], base=page_url)
            if feed_url and "comments" not in feed_url and feed_url not in feeds:
                feeds.append(feed_url)
    return list(candidates.values()), feeds


def parse_sitemap(content: bytes) -> tuple[list[tuple[str, datetime | None]], list[str]]:
    """Returns (article URLs with lastmod, child sitemap URLs)."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        root = etree.fromstring(
            content, parser=etree.XMLParser(recover=True, resolve_entities=False)
        )
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []

    def local(tag) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    urls: list[tuple[str, datetime | None]] = []
    children: list[str] = []
    kind = local(root.tag)
    for node in root:
        name = local(node.tag)
        if name not in ("url", "sitemap"):
            continue
        loc = lastmod = None
        for child in node.iter():
            child_name = local(child.tag)
            if child_name == "loc" and child.text:
                loc = child.text.strip()
            elif child_name in ("lastmod", "publication_date") and child.text and not lastmod:
                lastmod = parse_datetime(child.text.strip())
        if not loc:
            continue
        if kind == "sitemapindex" or name == "sitemap":
            children.append(loc)
        else:
            urls.append((loc, lastmod))
    return urls, children


def sitemaps_from_robots(robots_txt: str) -> list[str]:
    return [
        line.split(":", 1)[1].strip()
        for line in robots_txt.splitlines()
        if line.lower().startswith("sitemap:") and line.split(":", 1)[1].strip()
    ]
