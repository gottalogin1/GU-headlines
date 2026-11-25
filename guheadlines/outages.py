"""Power and water outages for the Outages page.

The worker reads these public sources every OUTAGES_INTERVAL_MINUTES and
saves the last good copy of each in the weather table (the table that keeps
the last good copy of each outside source; kinds "outages_gpa",
"outages_gwa" and "outages_epa"):

- gpa: the Guam Power Authority's outage notifications on GuamALERTS, the
       official alert site that GPA's own website links to ("View Outages"):
       scheduled maintenance outages and other power outages, each with its
       date, hours and villages
- gwa: the Guam Waterworks Authority's "GWA Updates about Water Outages"
       blog, the page GWA's website calls "Current Outages": emergency and
       scheduled water outages and boil-water notices
- epa: the Guam Environmental Protection Agency's news feed, for its
       boil-water notices only

All three are public notices that these agencies post for everyone to read.
The website reads only the table, so a slow or broken source never slows a
page. Readers are always sent to the agency's own notice for the details.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, unquote, urljoin

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from lxml import etree

from .scraper.http import HttpClient
from .scraper.text import GUAM_TZ, clean_text, strip_html, truncate
from .settings import Settings

log = logging.getLogger(__name__)

GPA_URL = "https://web.guamalerts.com/notification/gpa"
GWA_FEED = "https://bloggwapio.blogspot.com/feeds/posts/default?alt=rss"
GWA_URL = "https://bloggwapio.blogspot.com/"
EPA_FEED = "https://epa.guam.gov/feed/"
EPA_URL = "https://epa.guam.gov/"
# Where readers report an outage, from each agency's website.
GPA_TROUBLE_PHONE = "671-475-1472"
GWA_DISPATCH_PHONE = "671-646-4211"

KINDS = {"gpa": "outages_gpa", "gwa": "outages_gwa", "epa": "outages_epa"}
# An outage stays on the page this many days after its day, as "Earlier".
EARLIER_DAYS = 3
# Boil-water notices (and the notices that lift them) stay this long.
BOIL_DAYS = 30
# A source that could not be read for this long is said to be out of date.
STALE_AFTER = timedelta(hours=6)

_BOIL_RE = re.compile(r"\bboil(?:ed|ing)?[\s-]+water\b|\bboil\s+(?:your\s+)?water\b", re.I)
_LIFTED_RE = re.compile(r"\b(?:lift(?:ed|s)?|rescind(?:ed|s)?|cancel(?:l?ed|s)?|ended)\b", re.I)
_MONTHS = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_WRITTEN_DATE_RE = re.compile(
    rf"\b({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I
)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


class OutageError(Exception):
    """A source answered, but not with what the site can show."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def find_date(text: str) -> date | None:
    """The first date written in a notice: "Wednesday November 25, 2026",
    "Nov. 25, 2026" or "11/25/2026"."""
    found = _WRITTEN_DATE_RE.search(text or "")
    if found:
        try:
            return dateparser.parse(" ".join(found.groups())).date()
        except (ValueError, OverflowError):
            pass
    found = _NUMERIC_DATE_RE.search(text or "")
    if found:
        month, day, year = (int(g) for g in found.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def soften_caps(title: str) -> str:
    """'PREVENTATIVE MAINTENANCE OUTAGE ANNOUNCEMENT P-311 Barrigada' ->
    'Preventative maintenance outage announcement P-311 Barrigada': words in
    capitals are lowered (agency names such as GPA and codes such as P-311
    are kept), and the title starts with a capital."""
    words = []
    for word in title.split(" "):
        letters = re.sub(r"[^A-Za-z]", "", word)
        if len(letters) > 1 and letters.isupper() and letters not in _KEEP_CAPS:
            if not any(ch.isdigit() for ch in word):
                word = word.lower()
        words.append(word)
    text = " ".join(words)
    return text[:1].upper() + text[1:]


_KEEP_CAPS = {"GPA", "GWA", "EPA", "GEPA", "CCU", "PUC", "US", "USA", "AM", "PM"}


def is_boil_water(text: str) -> bool:
    return bool(_BOIL_RE.search(text or ""))


def web_link(href: str | None, base: str) -> str | None:
    """An absolute http(s) link, with spaces and other unsafe characters
    escaped (GuamALERTS names its PDF files with spaces)."""
    if not href:
        return None
    link = urljoin(base, href.strip())
    if not link.startswith(("https://", "http://")):
        return None
    return quote(link, safe=":/?#[]@!$&'()*+,;=%~")


# --------------------------------------------------------------- GPA


_FIELDS = {
    "category": "category",
    "outage date": "date_text",
    "date of outage": "date_text",
    "time of outage": "hours",
    "outage time": "hours",
    "affected village(s)": "villages",
    "affected villages": "villages",
    "affected village": "villages",
    "affected area(s)": "areas",
    "affected areas": "areas",
}
_POSTED_RE = re.compile(
    r"Post(?:ed)?\s+Date(?:/Time)?\s*:\s*(?:[A-Za-z]+day,?\s+)?"
    r"(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}\s*[AP]\.?M\.?)?)",
    re.IGNORECASE,
)
_ALERT_ID_RE = re.compile(r"\bGPA-GA-\d+\b")
_DETAILS_RE = re.compile(r"https?://web\.guamalerts\.com/notification_details/[^\s\"'&<>]+")


def _details_link(article, soup) -> str | None:
    """The notice's own page on GuamALERTS ("Weblink for this Alert"): in
    the share box that follows each notice."""
    places = [article]
    opener = article.select_one('a[href^="#share-dialog"]')
    if opener is not None:
        box = soup.find(id=opener["href"][1:])
        if box is not None:
            places.append(box)
    for place in places:
        for anchor in place.find_all("a", href=True):
            found = _DETAILS_RE.search(unquote(anchor["href"]))
            if found:
                return found.group(0)
    return None


def parse_gpa(content: bytes, base: str = GPA_URL) -> list[dict]:
    """GPA's outage notices from its GuamALERTS page, newest first: the
    title, category ("Scheduled Maintenance"), outage day, hours and
    villages, when it was posted, and links to the notice and its PDF."""
    soup = BeautifulSoup(content, "lxml")
    articles = soup.select("article.post")
    if not articles:
        raise OutageError(f"no notices found on {base}; the page may have changed")
    notices: list[dict] = []
    seen: set[str] = set()
    for article in articles:
        heading = article.select_one(".post-title")
        title = clean_text(heading.get_text(" ")) if heading else ""
        if not title:
            continue
        fields: dict[str, str] = {}
        for item in article.select("li"):
            label = item.find("span")
            if label is None:
                continue
            name = clean_text(label.get_text(" ")).rstrip(":").strip().lower()
            key = _FIELDS.get(name)
            if key:
                value = clean_text(item.get_text(" "))
                fields[key] = clean_text(value.split(":", 1)[1] if ":" in value else value)
        text = clean_text(article.get_text(" "))
        posted = None
        found = _POSTED_RE.search(text)
        if found:
            try:
                posted = dateparser.parse(found.group(1)).replace(tzinfo=GUAM_TZ)
            except (ValueError, OverflowError):
                posted = None
        alert_id = _ALERT_ID_RE.search(text)
        day = find_date(fields.get("date_text", "")) or find_date(title)
        key = alert_id.group(0) if alert_id else f"{title}|{day}"
        if key in seen:
            continue
        seen.add(key)
        pdf = article.select_one('.post-content a[href], a[href$=".pdf"]')
        notices.append(
            {
                "id": key,
                "title": truncate(soften_caps(title), 300),
                "category": fields.get("category") or None,
                "day": day.isoformat() if day else None,
                "hours": fields.get("hours") or None,
                "villages": fields.get("villages") or None,
                "areas": fields.get("areas") or None,
                "posted": _iso(posted),
                "url": _details_link(article, soup) or base,
                "pdf": web_link(pdf.get("href"), base) if pdf else None,
            }
        )
    return notices


# --------------------------------------------------------------- GWA

_ATOM = "{http://www.w3.org/2005/Atom}"
_GWA_LABELS = (
    r"Date|Estimated\s+Start\s+Time|Start\s+Time|Estimated\s*-?\s*End\s+Time|End\s+Time|"
    r"Villages?|Areas?\s+Affected|Affected\s+Areas?|Location"
)
_GWA_FIELD_RE = re.compile(
    rf"\b(?P<label>{_GWA_LABELS})\s*:\s*(?P<value>.*?)(?=\s*-?\s*\b(?:{_GWA_LABELS})\s*:|$)",
    re.IGNORECASE,
)


def _gwa_key(label: str) -> str:
    label = label.lower()
    if "start" in label:
        return "start"
    if "end" in label:
        return "end"
    if label.startswith("village"):
        return "villages"
    if "area" in label or label == "location":
        return "areas"
    return "date_text"


def _summary_lines(text: str) -> list[str]:
    """The lines of a Blogger summary: its text keeps the post's line breaks,
    with HTML entities (&nbsp;) still in it."""
    lines = [clean_text(line) for line in (text or "").splitlines()]
    return [line for line in lines if line]


def gwa_kind(title: str, text: str = "") -> str:
    """ "boil" (a boil-water notice, or the notice that lifts one),
    "emergency", "scheduled", "extended" (an outage that runs longer than
    first said) or "notice"."""
    if is_boil_water(title) or is_boil_water(text):
        return "boil"
    lowered = title.lower()
    if "emergency" in lowered:
        return "emergency"
    if "scheduled" in lowered or "planned" in lowered:
        return "scheduled"
    if "extended" in lowered:
        return "extended"
    return "notice"


def parse_gwa(content: bytes) -> list[dict]:
    """GWA's water outage notices from its blog's feed, newest first: the
    title, kind, outage day, start and end times, villages and areas, any
    other lines (the reason), and when it was posted."""
    try:
        root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False))
    except etree.XMLSyntaxError as exc:
        raise OutageError(f"GWA feed is not valid XML: {exc}") from exc
    if root.find("./channel") is None:
        raise OutageError("GWA feed has no channel; the feed may have changed")
    notices = []
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title"))
        if not title:
            continue
        summary = item.findtext(f"{_ATOM}summary") or strip_html(item.findtext("description"))
        fields: dict[str, str] = {}
        notes: list[str] = []
        for line in _summary_lines(summary):
            matches = list(_GWA_FIELD_RE.finditer(line))
            if not matches:
                notes.append(line)
                continue
            for match in matches:
                value = clean_text(match.group("value")).strip(" -–")
                if value:
                    fields.setdefault(_gwa_key(match.group("label")), value)
        posted = None
        try:
            posted = dateparser.parse(item.findtext("pubDate") or "")
        except (ValueError, OverflowError):
            posted = None
        day = find_date(title) or find_date(fields.get("date_text", ""))
        text = " ".join(notes)
        notices.append(
            {
                "id": clean_text(item.findtext("guid")) or title,
                "title": truncate(soften_caps(title), 300),
                "kind": gwa_kind(title, text),
                "day": day.isoformat() if day else None,
                "start": fields.get("start"),
                "end": fields.get("end"),
                "villages": fields.get("villages"),
                "areas": fields.get("areas"),
                "notes": [truncate(n, 300) for n in notes[:3]],
                "posted": _iso(posted),
                "url": web_link(clean_text(item.findtext("link")), GWA_URL) or GWA_URL,
            }
        )
    return notices


# --------------------------------------------------------------- Guam EPA


def parse_epa(content: bytes) -> list[dict]:
    """Guam EPA's boil-water notices from its news feed (its other news is
    left out), newest first."""
    try:
        root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False))
    except etree.XMLSyntaxError as exc:
        raise OutageError(f"Guam EPA feed is not valid XML: {exc}") from exc
    if root.find("./channel") is None:
        raise OutageError("Guam EPA feed has no channel; the feed may have changed")
    notices = []
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title"))
        summary = strip_html(item.findtext("description"))
        summary = re.sub(r"\s*The post .+ first appeared on .+$", "", summary)
        summary = re.sub(r"\s*Read more\s*$", "", summary).strip()
        if not title or not is_boil_water(f"{title} {summary}"):
            continue
        try:
            posted = dateparser.parse(item.findtext("pubDate") or "")
        except (ValueError, OverflowError):
            posted = None
        notices.append(
            {
                "id": clean_text(item.findtext("guid")) or title,
                "title": truncate(title, 300),
                "summary": truncate(summary, 400),
                "posted": _iso(posted),
                "url": web_link(clean_text(item.findtext("link")), EPA_URL) or EPA_URL,
            }
        )
    return notices


# ------------------------------------------------------------- checking

READERS = {
    "gpa": lambda client: parse_gpa(client.get(GPA_URL).content),
    "gwa": lambda client: parse_gwa(client.get(GWA_FEED).content),
    "epa": lambda client: parse_epa(client.get(EPA_FEED).content),
}


def check_outages(settings: Settings, client: HttpClient | None = None) -> list[tuple]:
    """Read each source and save each one that worked; a source that fails
    keeps its last good copy. Returns (source, notices, error) for each."""
    from .weather import save

    own_client = client is None
    if own_client:
        client = HttpClient(
            settings.user_agent,
            timeout=settings.request_timeout,
            per_host_delay=settings.per_host_delay,
            respect_robots=settings.respect_robots,
        )
    results = []
    try:
        for name, read in READERS.items():
            try:
                notices = read(client)
            except Exception as exc:  # one broken source must not stop the others
                log.warning("outages: %s not updated: %s", name, exc)
                results.append((name, None, f"{type(exc).__name__}: {exc}"))
                continue
            save(KINDS[name], notices)
            results.append((name, notices, None))
    finally:
        if own_client:
            client.close()
    return results


# ------------------------------------------------------------------ page


def _when(notice: dict, now: datetime) -> date:
    """The day of an outage: the day its notice gives, else the day (in
    Guam) it was posted."""
    day = _day(notice.get("day"))
    if day:
        return day
    posted = _time(notice.get("posted"))
    return (posted or now).astimezone(GUAM_TZ).date()


def _sort_out(notices: list[dict], now: datetime) -> dict:
    """{"today": [...], "coming": [...], "earlier": [...]}: today's outages,
    the ones still to come (soonest first) and those of the last few days
    (newest first). Older ones are left out."""
    today = now.astimezone(GUAM_TZ).date()
    groups: dict[str, list] = {"today": [], "coming": [], "earlier": []}
    for notice in notices:
        day = _when(notice, now)
        shown = {**notice, "day": day, "posted": _time(notice.get("posted"))}
        if day == today:
            groups["today"].append(shown)
        elif day > today:
            groups["coming"].append(shown)
        elif day >= today - timedelta(days=EARLIER_DAYS):
            groups["earlier"].append(shown)
    groups["coming"].sort(key=lambda n: n["day"])
    groups["earlier"].sort(key=lambda n: n["day"], reverse=True)
    return groups


def outages_page(reports: dict[str, tuple], now: datetime) -> dict:
    """What the Outages page shows, from the saved copies ({kind: (data,
    fetched_at)}): power and water outages by day (today, coming up,
    earlier), the boil-water notices of the last month, and when each
    source was last read."""

    def saved(name: str) -> tuple[list[dict], datetime | None]:
        data, fetched_at = reports.get(KINDS[name], (None, None))
        return data or [], fetched_at

    gpa, gpa_checked = saved("gpa")
    gwa, gwa_checked = saved("gwa")
    epa, epa_checked = saved("epa")
    since = now - timedelta(days=BOIL_DAYS)
    boil = []
    for notice, source in [(n, "GWA") for n in gwa if n.get("kind") == "boil"] + [
        (n, "Guam EPA") for n in epa
    ]:
        posted = _time(notice.get("posted"))
        if posted and posted >= since:
            boil.append(
                {
                    **notice,
                    "posted": posted,
                    "source": source,
                    "lifted": bool(_LIFTED_RE.search(notice["title"])),
                }
            )
    boil.sort(key=lambda n: n["posted"], reverse=True)

    def status(checked: datetime | None) -> dict:
        return {"checked": checked, "stale": not checked or now - checked > STALE_AFTER}

    return {
        "power": _sort_out(gpa, now),
        "water": _sort_out([n for n in gwa if n.get("kind") != "boil"], now),
        "boil": boil,
        "gpa": status(gpa_checked),
        "gwa": status(gwa_checked),
        "epa": status(epa_checked),
        "today": now.astimezone(GUAM_TZ).date(),
    }
