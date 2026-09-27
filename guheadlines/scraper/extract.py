"""Extract headline, main image, dates, author and intro paragraphs from an article page.

Works on any news site by combining, in order of reliability:
OpenGraph / article:* meta tags, JSON-LD (schema.org NewsArticle), common CMS
body containers (WordPress, BLOX/TownNews, Drupal, Wix, DVIDS, af.mil), and
trafilatura's main-content extraction as a fallback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from .text import clean_text, parse_datetime, truncate
from .urls import normalize_url, same_site

log = logging.getLogger(__name__)

INTRO_TARGET_CHARS = 280
INTRO_MAX_CHARS = 700
INTRO_MAX_PARAGRAPHS = 3

ARTICLE_TYPES = {
    "newsarticle",
    "article",
    "reportagenewsarticle",
    "analysisnewsarticle",
    "backgroundnewsarticle",
    "blogposting",
    "report",
    "opinionnewsarticle",
}

# Article body containers used by common news CMSes, most specific first.
BODY_SELECTORS = [
    "[itemprop='articleBody']",
    "#article-body",  # BLOX / TownNews (postguam, many US papers)
    ".asset-content",
    ".td-post-content",  # WordPress "Newspaper" theme
    ".entry-content",  # WordPress
    ".post-content",
    ".article-body",
    ".article-content",
    ".article__body",
    ".story-body",
    ".story-content",
    ".field--name-body",  # Drupal
    ".field-name-body",
    "[data-hook='post-description']",  # Wix blog
    ".news-body",
    ".body-text",
    "article",
    "main",
]

_JUNK_PARAGRAPH_RE = re.compile(
    r"^(\(?photo|photos? (by|courtesy)|image (by|courtesy)|file photo|courtesy (photo|of)|"
    r"posted (on|by|in)|published( on|:)|updated( on|:)|by [A-Z][\w.'-]+( [A-Z][\w.'-]+){0,3}$|"
    r"share (this|on)|subscribe|sign up|log ?in|click here|read more|"
    r"related( stories| articles| coverage|:)|"
    r"advertisement|sponsored|copyright|©|all rights reserved|follow us|download (the|our)|"
    r"listen (to|live)|watch (the|live|below)|this (story|article) (was|has been|is)|"
    r"story continues|continue reading|for more (news|information)|email:|tel:|phone:|"
    r"you (must|need to) be logged in|to continue reading|already a subscriber|"
    r"support local journalism|get (unlimited|full) access|for immediate release|"
    r"press release\b|media contact)",
    re.IGNORECASE,
)
_BAD_IMAGE_RE = re.compile(
    r"(logo|favicon|icon|sprite|avatar|gravatar|placeholder|default[-_]?(image|share|og)|"
    r"blank\.|spacer|pixel|tracking|badge|banner[-_]?ad|/ads?/|emoji|\.svg(\?|$))",
    re.IGNORECASE,
)
_TITLE_SEPARATORS = (" | ", " - ", " – ", " — ", " :: ", " » ")


@dataclass
class ExtractedArticle:
    url: str
    canonical_url: str | None = None
    title: str | None = None
    intro: str | None = None
    description: str | None = None
    image_url: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    author: str | None = None
    section: str | None = None
    keywords: list[str] = field(default_factory=list)
    site_name: str | None = None
    is_article: bool = False


def _meta(soup: BeautifulSoup) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name") or tag.get("itemprop")
        content = tag.get("content")
        if not key or content is None:
            continue
        found.setdefault(key.strip().lower(), []).append(content.strip())
    return found


def _first(meta: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        for value in meta.get(key, []):
            if value:
                return value
    return None


def _jsonld_objects(soup: BeautifulSoup) -> list[dict]:
    objects: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            objects.append(node)
            if "@graph" in node:
                walk(node["@graph"])
            main = node.get("mainEntity")
            if isinstance(main, (dict, list)):
                walk(main)

    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            walk(json.loads(raw))
        except (ValueError, TypeError):
            # Some sites emit trailing commas or several objects; skip them.
            continue
    return objects


def _types(obj: dict) -> set[str]:
    value = obj.get("@type")
    if isinstance(value, str):
        return {value.lower()}
    if isinstance(value, list):
        return {str(v).lower() for v in value}
    return set()


def _jsonld_article(objects: list[dict]) -> dict | None:
    for obj in objects:
        if _types(obj) & ARTICLE_TYPES:
            return obj
    return None


def _ld_text(value) -> str | None:
    if isinstance(value, str):
        return clean_text(value) or None
    if isinstance(value, dict):
        return _ld_text(value.get("name") or value.get("@value"))
    if isinstance(value, list):
        names = [n for n in (_ld_text(v) for v in value) if n]
        return ", ".join(dict.fromkeys(names)) or None
    return None


def _ld_image(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl") or value.get("@id")
    if isinstance(value, list):
        for item in value:
            found = _ld_image(item)
            if found:
                return found
    return None


def _clean_title(title: str, site_name: str | None, source_name: str | None) -> str:
    title = clean_text(title)
    names = {n.lower() for n in (site_name, source_name) if n}
    for sep in _TITLE_SEPARATORS:
        if sep not in title:
            continue
        head, _, tail = title.rpartition(sep)
        tail_l = tail.strip().lower()
        # Drop a trailing site name ("Story title | postguam.com").
        if head and (
            tail_l in names
            or any(n in tail_l or tail_l in n for n in names if len(tail_l) > 3)
            or re.search(r"\.(com|net|org|gov|mil)$", tail_l)
        ):
            return _clean_title(head, site_name, source_name)
    return title


def _usable_image(url: str | None, page_url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith("data:"):
        return None
    absolute = normalize_url(url, base=page_url)
    if not absolute or _BAD_IMAGE_RE.search(absolute):
        return None
    return absolute


def _find_body(soup: BeautifulSoup, selector: str | None) -> Tag | None:
    selectors = [selector] if selector else []
    selectors += BODY_SELECTORS
    for css in selectors:
        try:
            candidates = soup.select(css)
        except Exception:  # invalid selector in config
            log.warning("invalid CSS selector %r", css)
            continue
        for node in candidates:
            if len(node.find_all("p")) >= 1 and len(node.get_text(" ", strip=True)) > 150:
                return node
    return None


def _paragraphs_from_body(body: Tag) -> list[str]:
    for junk in body.select(
        "script, style, noscript, figure, figcaption, aside, nav, form, iframe, "
        ".caption, .wp-caption-text, .photo-caption, .sharedaddy, .share, .social, "
        ".related, .advertisement, .ad, [class*='newsletter'], [class*='byline']"
    ):
        junk.decompose()
    return [clean_text(p.get_text(" ")) for p in body.find_all("p")]


# Pages rendered by JavaScript often embed the whole story as JSON state.
_STATE_ASSIGN_RE = re.compile(
    r"(?:window\.)?(__PAGE_MODEL__|__NEXT_DATA__|__NUXT__|__INITIAL_STATE__|"
    r"__PRELOADED_STATE__|__APOLLO_STATE__)\s*=\s*"
)


def _json_strings(node, out: list[str], limit: int = 5000) -> None:
    if len(out) >= limit:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _json_strings(value, out, limit)
    elif isinstance(node, list):
        for value in node:
            _json_strings(value, out, limit)


_JS_ESCAPE_RE = re.compile(r"\\\\|\\(.)", re.S)


def _decode_js_object(text: str):
    """Decode the JSON value at the start of text. JavaScript allows escapes that
    JSON does not (e.g. "\\!"), so retry once with those backslashes dropped."""
    decoder = json.JSONDecoder()
    try:
        return decoder.raw_decode(text)[0]
    except ValueError:
        pass

    def fix(match: re.Match) -> str:
        char = match.group(1)
        if char is None or char in '"\\/bfnrtu':
            return match.group(0)
        return char

    return decoder.raw_decode(_JS_ESCAPE_RE.sub(fix, text))[0]


def _paragraphs_from_embedded_json(soup: BeautifulSoup) -> list[str]:
    """Story paragraphs from JSON page state (window.__PAGE_MODEL__, __NEXT_DATA__...)."""
    for script in soup.find_all("script"):
        text = script.string or ""
        if len(text) < 500:
            continue
        data = None
        try:
            if script.get("id") == "__NEXT_DATA__":
                data = json.loads(text)
            else:
                match = _STATE_ASSIGN_RE.search(text)
                if match:
                    data = _decode_js_object(text[match.end() :].lstrip())
        except ValueError:
            continue
        if data is None:
            continue
        strings: list[str] = []
        _json_strings(data, strings)
        paragraphs: list[str] = []
        for value in strings:
            if "<p" in value:
                fragment = BeautifulSoup(value, "lxml")
                paragraphs += [clean_text(p.get_text(" ")) for p in fragment.find_all("p")]
            elif len(value) >= 80 and value.count(" ") >= 10 and "://" not in value[:12]:
                if "<" not in value and "{" not in value:
                    paragraphs.append(clean_text(value))
        if paragraphs:
            return paragraphs
    return []


# "Date: 08.12.2026", "Posted: September 3, 2026" in the page text (e.g. DVIDS).
_TEXT_DATE_RE = re.compile(
    r"\b(?:date posted|posted on|posted|published on|published|date)\s*[:|]\s*"
    r"(\d{1,2}[./-]\d{1,2}[./-]\d{4}(?:\s+\d{1,2}:\d{2})?|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


def _date_from_text(soup: BeautifulSoup) -> datetime | None:
    body = soup.body or soup
    text = body.get_text(" ", strip=True)[:60000]
    for match in _TEXT_DATE_RE.finditer(text):
        found = parse_datetime(match.group(1))
        if found:
            return found
    return None


def _paragraphs_from_trafilatura(html: bytes | str, url: str) -> list[str]:
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return []
    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            include_images=False,
            favor_precision=True,
            deduplicate=True,
        )
    except Exception as exc:  # trafilatura is robust, but never let it kill a run
        log.debug("trafilatura failed on %s: %s", url, exc)
        return []
    if not text:
        return []
    return [clean_text(line) for line in text.split("\n")]


def _good_paragraph(text: str, title: str | None) -> bool:
    if len(text) < 40:
        return False
    if _JUNK_PARAGRAPH_RE.match(text):
        return False
    if title and text.lower().strip(" .") == title.lower().strip(" ."):
        return False
    # Mostly non-letters (tables, codes, lists of links).
    letters = sum(c.isalpha() for c in text)
    return letters >= len(text) * 0.55


def build_intro(paragraphs: list[str], title: str | None = None) -> str | None:
    """First paragraph(s) of the story: at least ~280 characters, at most ~700."""
    chosen: list[str] = []
    length = 0
    for para in paragraphs:
        if not _good_paragraph(para, title):
            continue
        if para in chosen:
            continue
        chosen.append(para)
        length += len(para)
        if length >= INTRO_TARGET_CHARS or len(chosen) >= INTRO_MAX_PARAGRAPHS:
            break
    if not chosen:
        return None
    intro = "\n\n".join(chosen)
    return truncate(intro, INTRO_MAX_CHARS)


def extract_article(
    content: bytes | str,
    url: str,
    *,
    body_selector: str | None = None,
    source_name: str | None = None,
) -> ExtractedArticle:
    # Raw bytes let lxml and trafilatura honour the page's declared charset.
    soup = BeautifulSoup(content, "lxml")
    meta = _meta(soup)
    ld_objects = _jsonld_objects(soup)
    ld = _jsonld_article(ld_objects) or {}
    result = ExtractedArticle(url=url)

    result.site_name = clean_text(_first(meta, "og:site_name")) or None

    # Canonical URL (only trusted when it stays on the same site).
    canonical = None
    link = soup.find("link", rel=lambda v: v and "canonical" in v)
    for candidate in (link.get("href") if link else None, _first(meta, "og:url")):
        normalized = normalize_url(candidate, base=url) if candidate else None
        if normalized and same_site(normalized, url):
            canonical = normalized
            break
    result.canonical_url = canonical

    # Headline.
    h1 = soup.find("h1")
    raw_title = (
        _first(meta, "og:title", "twitter:title")
        or _ld_text(ld.get("headline"))
        or (h1.get_text(" ") if h1 else None)
        or (soup.title.get_text(" ") if soup.title else None)
    )
    if raw_title:
        result.title = _clean_title(raw_title, result.site_name, source_name) or None

    result.description = (
        clean_text(
            _first(meta, "og:description", "description", "twitter:description")
            or _ld_text(ld.get("description"))
        )
        or None
    )

    # Main image.
    image_candidates = [
        _first(meta, "og:image:secure_url", "og:image", "og:image:url"),
        _first(meta, "twitter:image", "twitter:image:src"),
        _ld_image(ld.get("image")) if ld else None,
        _ld_image(ld.get("thumbnailUrl")) if ld else None,
    ]
    image_link = soup.find("link", rel=lambda v: v and "image_src" in v)
    if image_link:
        image_candidates.append(image_link.get("href"))
    for candidate in image_candidates:
        usable = _usable_image(candidate, url)
        if usable:
            result.image_url = usable
            break

    # Dates.
    result.published_at = (
        parse_datetime(
            _first(
                meta,
                "article:published_time",
                "og:article:published_time",
                "datepublished",
                "pubdate",
                "publishdate",
                "publish-date",
                "parsely-pub-date",
                "sailthru.date",
                "dc.date.issued",
                "dc.date",
                "date",
            )
        )
        or parse_datetime(ld.get("datePublished"))
        or parse_datetime(ld.get("dateCreated"))
    )
    if not result.published_at:
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            result.published_at = parse_datetime(time_tag["datetime"])
    if not result.published_at:
        result.published_at = _date_from_text(soup)
    result.modified_at = parse_datetime(
        _first(meta, "article:modified_time", "og:updated_time", "datemodified")
    ) or parse_datetime(ld.get("dateModified"))

    # Byline, section, tags.
    author = _ld_text(ld.get("author")) or _first(
        meta, "author", "parsely-author", "sailthru.author"
    )
    if not author:
        meta_author = _first(meta, "article:author")
        if meta_author and not meta_author.startswith("http"):
            author = meta_author
    if author:
        author = clean_text(re.sub(r"^by\s+", "", author, flags=re.I))
        result.author = author[:200] or None

    section = _first(meta, "article:section") or _ld_text(ld.get("articleSection"))
    result.section = (clean_text(section)[:120] or None) if section else None

    tags: list[str] = list(meta.get("article:tag", []))
    for key in ("news_keywords", "keywords", "parsely-tags"):
        for value in meta.get(key, []):
            tags.extend(value.split(","))
    ld_keywords = ld.get("keywords")
    if isinstance(ld_keywords, str):
        tags.extend(ld_keywords.split(","))
    elif isinstance(ld_keywords, list):
        tags.extend(str(k) for k in ld_keywords)
    seen: dict[str, str] = {}
    for tag in tags:
        tag = clean_text(tag)
        if tag and len(tag) <= 60 and tag.lower() not in seen:
            seen[tag.lower()] = tag
    result.keywords = list(seen.values())[:25]

    # Intro paragraphs: CMS body container first, then JSON page state (for
    # JavaScript-rendered sites), then trafilatura as a general fallback.
    intro = None
    embedded = _paragraphs_from_embedded_json(soup)
    body = _find_body(soup, body_selector)
    if body is not None:
        intro = build_intro(_paragraphs_from_body(body), result.title)
    if not intro and embedded:
        intro = build_intro(embedded, result.title)
    if not intro:
        intro = build_intro(_paragraphs_from_trafilatura(content, url), result.title)
    if not intro and ld.get("articleBody"):
        intro = build_intro(re.split(r"\n\s*\n|\r?\n", str(ld["articleBody"])), result.title)
    result.intro = intro

    og_type = (_first(meta, "og:type") or "").lower()
    result.is_article = bool(
        og_type == "article"
        or ld
        or _first(meta, "article:published_time")
        or (soup.find("article") is not None and result.published_at and result.intro)
    )
    return result
