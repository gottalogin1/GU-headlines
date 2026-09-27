"""Small text and date helpers shared by the feed parser and article extractor."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from dateutil import parser as dateparser
from dateutil import tz

GUAM_TZ = tz.gettz("Pacific/Guam") or timezone(timedelta(hours=10))

_WS_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")


def clean_text(value: str | None) -> str:
    """Unescape HTML entities, drop zero-width characters, collapse whitespace."""
    if not value:
        return ""
    value = html.unescape(str(value))
    value = _ZERO_WIDTH_RE.sub("", value).replace("\xa0", " ")
    # Some CMSes leak escaped punctuation ("campaign\\, a community...").
    value = value.replace("\\,", ",").replace("\\;", ";")
    return _WS_RE.sub(" ", value).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    if "<" not in value:
        return clean_text(value)
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(value, "lxml")
    for tag in soup(["script", "style", "figure", "figcaption"]):
        tag.decompose()
    return clean_text(soup.get_text(" "))


def truncate(text: str, limit: int) -> str:
    """Shorten to at most ``limit`` characters, preferring a sentence or word boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind('." '), cut.rfind("? "), cut.rfind("! "))
    if sentence_end >= limit * 0.6:
        return cut[: sentence_end + 1].rstrip()
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:-–—") + "…"


def parse_datetime(value, *, now: datetime | None = None) -> datetime | None:
    """Parse a date from meta tags / JSON-LD / feeds into an aware UTC datetime.

    Naive values are assumed to be Guam local time. Implausible dates are dropped.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = clean_text(str(value))
        if not text:
            return None
        try:
            dt = dateparser.parse(text, fuzzy=False)
        except (ValueError, OverflowError, TypeError):
            return None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=GUAM_TZ)
    dt = dt.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if dt.year < 1990 or dt > now + timedelta(days=2):
        return None
    return dt


def struct_time_to_datetime(value) -> datetime | None:
    """feedparser gives UTC time.struct_time values."""
    if not value:
        return None
    try:
        dt = datetime(*value[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return parse_datetime(dt)


def clean_byline(author: str | None, *outlets: str | None) -> str | None:
    """Drop the outlet's own name from a byline: "Jane Cruz Pacific Daily News"
    -> "Jane Cruz"; a byline that is only the outlet's name -> None."""
    author = clean_text(author)
    for outlet in sorted({clean_text(o) for o in outlets if o}, key=len, reverse=True):
        if not outlet:
            continue
        pattern = re.compile(rf"[\s,|/–—-]*(?:of\s+|for\s+)?(the\s+)?{re.escape(outlet)}\s*$", re.I)
        author = pattern.sub("", author).strip(" ,|/–—-")
        if (
            author.lower().removeprefix("the ").strip()
            == outlet.lower().removeprefix("the ").strip()
        ):
            author = ""
    return author or None
