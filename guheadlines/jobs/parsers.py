"""Readers for each kind of job board in config/jobs.yaml.

A parser turns a board's listing page into Postings, and can read a posting's
own page for what the listing leaves out (pay, description). Fields a posting
does not give stay empty; the Jobs page then shows when it was last seen.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..scraper.http import FetchResult
from ..scraper.text import clean_text, strip_html, truncate

log = logging.getLogger(__name__)

DESCRIPTION_LIMIT = 1500


class BoardError(Exception):
    """A board that was reached but gave no postings we can use."""


@dataclass
class Posting:
    url: str  # the posting's page, or its PDF when that is all there is
    title: str
    employer: str | None = None
    pay: str | None = None
    description: str | None = None
    location: str | None = None
    open_date: date | None = None
    close_date: date | None = None
    close_note: str | None = None
    pdf_url: str | None = None
    # The page to read for the details the listing leaves out, if any.
    detail_url: str | None = None
    # The board's own number for the posting, used to match duplicates.
    reference: str | None = None


@dataclass
class Listing:
    postings: list[Posting] = field(default_factory=list)
    # More listing pages to read (pagination).
    next_pages: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ helpers

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|"
    "june|july|august|september|october|november|december"
)
_DATE_RES = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"), "mdy"),
    (re.compile(rf"\b({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "Mdy"),
    (re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})\.?,?\s+(\d{{4}})\b", re.I), "dMy"),
]
_UNTIL_FILLED_RE = re.compile(
    r"until (the )?(position|positions|job)?\s*(is |are )?filled|until filled|continuous|"
    r"open until|no closing date|ongoing",
    re.I,
)
_PAY_RE = re.compile(
    r"(\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:-|–|to)\s*\$?\s?\d[\d,]*(?:\.\d+)?)?"
    r"(?:\s*(?:/|per|an|a)\s*(?:hour|hr|year|yr|annum|month|week|day))?)",
    re.I,
)


def find_date(text: str | None) -> date | None:
    """The first date written in `text` (10/30/2026, 10/30/26, October 30, 2026, 2026-10-30)."""
    if not text:
        return None
    found: list[tuple[int, date]] = []
    for pattern, order in _DATE_RES:
        match = pattern.search(text)
        if not match:
            continue
        a, b, c = match.groups()
        try:
            if order == "ymd":
                value = date(int(a), int(b), int(c))
            elif order == "mdy":
                year = int(c) + 2000 if len(c) == 2 else int(c)
                value = date(year, int(a), int(b))
            else:
                month_name, day = (a, b) if order == "Mdy" else (b, a)
                month = datetime.strptime(month_name[:3].title(), "%b").month
                value = date(int(c), month, int(day))
        except ValueError:
            continue
        if 1990 <= value.year <= 2100:
            found.append((match.start(), value))
    return min(found)[1] if found else None


def closing(text: str | None) -> tuple[date | None, str | None]:
    """A closing date and/or a note such as "Open until filled"."""
    text = clean_text(text)
    note = "Open until filled" if _UNTIL_FILLED_RE.search(text) else None
    return find_date(text), note


def find_pay(text: str | None) -> str | None:
    """A pay amount written in free text ($15.00 per hour, $45,000 - $60,000)."""
    match = _PAY_RE.search(text or "")
    return clean_text(match.group(1)) if match else None


_PAY_UNIT_RE = re.compile(
    r"^(.*?)\s*(?:/|per|an|a)?\s*\b(hour|hr|month|year|yr|week|day|annum)$", re.I
)


def tidy_pay(text: str | None) -> str | None:
    """'2400.00 month' -> '$2,400.00 per month'; 'COMMENSURATE WITH EXPERIENCE hour'
    -> 'Commensurate with experience' (a unit without an amount means nothing)."""
    text = clean_text(text)
    if not text:
        return None
    unit = None
    match = _PAY_UNIT_RE.match(text)
    if match and match.group(1):
        text, unit = match.group(1).strip(" ,-"), match.group(2).lower()
        unit = {"hr": "hour", "yr": "year"}.get(unit, unit)
    if not re.search(r"\d", text):
        return text.capitalize() if text.isupper() and " " in text else text
    if "$" not in text:
        text = re.sub(
            r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)",
            lambda m: f"${float(m.group(1).replace(',', '')):,.2f}",
            text,
        )
    return f"{text} per {unit}" if unit else text


def absolute(href: str | None, base: str) -> str | None:
    """An absolute link with spaces escaped; None for javascript:, mailto:, #anchors."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    url = urljoin(base, href).split("#")[0]
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    return quote(url, safe=":/?&=%+@,;~!$'()*[]")


def is_pdf(url: str | None) -> bool:
    return bool(url) and urlsplit(url).path.lower().endswith(".pdf")


def text_of(node: Tag | None) -> str:
    return clean_text(node.get_text(" ")) if node else ""


def short(text: str | None) -> str | None:
    text = clean_text(text)
    return truncate(text, DESCRIPTION_LIMIT) if text else None


def soup_of(result: FetchResult) -> BeautifulSoup:
    soup = BeautifulSoup(result.content, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup


def needs_javascript(result: FetchResult) -> bool:
    """A page that is only a shell for JavaScript (or says JavaScript is needed)."""
    soup = soup_of(result)
    text = text_of(soup.body or soup)
    if re.search(
        r"(enable|requires?|turn on|need) javascript|javascript is (disabled|required)", text, re.I
    ):
        return True
    return len(text) < 200 and bool(re.search(rb"<script[^>]+src=", result.content, re.I))


class Parser:
    """Reads one kind of job board."""

    detail_headers: dict[str, str] | None = None  # sent with each posting's own page

    def listing(self, result: FetchResult, board) -> Listing:
        raise NotImplementedError

    def detail(self, result: FetchResult, posting: Posting) -> None:
        """Fill in what the posting's own page adds (only called with a detail_url)."""

    def finish(self, postings: list[Posting]) -> list[Posting]:
        """Tidy the postings from all of a board's pages (e.g. merge duplicates)."""
        return postings


# ----------------------------------------------------------------- parsers


class HafaJobs(Parser):
    """apply.hafajobs.com boards (Guam Community College and its partners): a
    table of open jobs with offered-by, open date and close date."""

    _DETAIL_RE = re.compile(r"https?://apply\.hafajobs\.com/[\w/.-]+/job_detail/GJF-\d+")
    # A link's label or the announcement number ("JA030-26"), not a description.
    _NOT_DESCRIPTION_RE = re.compile(
        r"^\W*(view (the )?job announcement|[A-Z]{1,4}\s?\d+-\d+)\W*$", re.I
    )

    def listing(self, result, board):
        soup = BeautifulSoup(result.content, "lxml")
        table = soup.find("table", id="example") or soup.find("table", class_="table_for_data")
        postings = []
        for row in table.find_all("tr") if table else []:
            cells = row.find_all("td", recursive=False)
            if len(cells) < 4:
                continue
            status = text_of(cells[4]) if len(cells) > 4 else ""
            if status and "open" not in status.lower():
                continue
            title_link = cells[0].find("a")
            title = text_of(title_link) or text_of(cells[0]).split(" Category:")[0]
            if not title:
                continue
            employer = text_of(cells[1].find("strong") or cells[1].find("p")) or board.employer
            detail = self._DETAIL_RE.search(str(row))
            pdf = None
            for anchor in row.find_all("a", href=True):
                link = absolute(anchor["href"], result.url)
                if is_pdf(link) and "application" not in text_of(anchor).lower():
                    pdf = link
                    break
            url = detail.group(0) if detail else pdf
            if not url:
                continue
            close_date, close_note = closing(text_of(cells[3]))
            postings.append(
                Posting(
                    url=url,
                    title=title,
                    employer=employer,
                    location=board.location,
                    open_date=find_date(text_of(cells[2])),
                    close_date=close_date,
                    close_note=close_note,
                    pdf_url=pdf,
                    detail_url=url if detail else None,
                )
            )
        return Listing(postings)

    def detail(self, result, posting):
        soup = BeautifulSoup(result.content, "lxml")
        for item in soup.select("div.item"):
            name = text_of(item.select_one(".name-coloumn, .name-column")).lower()
            entity = item.select_one(".name-entity")
            value = text_of(entity)
            if not value:
                continue
            if "salary" in name or "pay" in name or "wage" in name:
                posting.pay = posting.pay or value
            elif "location" in name:
                posting.location = value
            elif "description" in name or "summary" in name or "duties" in name:
                if not self._NOT_DESCRIPTION_RE.match(value):
                    posting.description = short(value)
                for anchor in entity.find_all("a", href=True):
                    link = absolute(anchor["href"], result.url)
                    if is_pdf(link) and not posting.pdf_url:
                        posting.pdf_url = link


class PdfLinks(Parser):
    """A page that lists each opening as a link to its announcement PDF
    (Guam Department of Education, University of Guam). Titles come from the
    link text; dates and pay are inside the PDFs, so they stay empty."""

    # Links that are forms or notices, not openings.
    _SKIP_RE = re.compile(
        r"^(application|amendment|corrective|closing|americorps (job )?application|"
        r"view |click |recruitment flyer)|employment application|application form",
        re.I,
    )
    # Hosts some boards use for announcement files without a .pdf ending.
    _FILE_HOSTS = ("ucarecd.net", "ucarecdn.com")

    def listing(self, result, board):
        soup = soup_of(result)
        for chrome in soup.select("header, footer, nav, #leftCol, .sideNav"):
            chrome.decompose()
        root = soup.find("main") or soup.find(id="content") or soup.body or soup
        postings: dict[str, Posting] = {}
        for anchor in root.find_all("a", href=True):
            link = absolute(anchor["href"], result.url)
            host = urlsplit(link or "").hostname or ""
            if not link or not (is_pdf(link) or host.endswith(self._FILE_HOSTS)):
                continue
            title = text_of(anchor)
            if len(title) < 3 or self._SKIP_RE.search(title) or link in postings:
                continue
            postings[link] = Posting(
                url=link,
                title=title,
                employer=self._employer(anchor) or board.employer,
                location=board.location,
                pdf_url=link,
            )
        return Listing(list(postings.values()))

    @staticmethod
    def _employer(anchor: Tag) -> str | None:
        """For a page with sections per employer ("Core Tech International Job
        Announcements"), the employer from the section's heading."""
        heading = anchor.find_previous(["h2", "h3"])
        name = text_of(heading)
        match = re.match(r"(.+?)\s+job announcements?$", name, re.I)
        if match and not re.match(r"(other|view|rcuog)\b", match.group(1), re.I):
            return match.group(1)
        return None


class FederalGovernmentJobs(Parser):
    """federalgovernmentjobs.us location pages: a table of federal jobs
    (title, location, department, posted, deadline); each job's page has the
    salary and a summary."""

    def listing(self, result, board):
        soup = BeautifulSoup(result.content, "lxml")
        postings: dict[str, Posting] = {}
        for row in soup.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            link = cells[0].find("a", href=True) if len(cells) >= 6 else None
            url = absolute(link["href"], result.url) if link else None
            if not url or "/jobs/" not in url or url in postings:
                continue
            postings[url] = Posting(
                url=url,
                title=text_of(link),
                employer=text_of(cells[3]) or board.employer,
                location=text_of(cells[1]) or board.location,
                open_date=find_date(text_of(cells[4])),
                close_date=find_date(text_of(cells[5])),
                detail_url=url,
            )
        next_pages = []
        for anchor in soup.find_all("a", href=True):
            if text_of(anchor).lower().startswith("next"):
                link = absolute(anchor["href"], result.url)
                if link:
                    next_pages.append(link)
        return Listing(list(postings.values()), next_pages)

    def detail(self, result, posting):
        soup = BeautifulSoup(result.content, "lxml")
        labels = {}
        for cell in soup.select("td.job-label"):
            value = cell.find_next_sibling("td")
            labels[text_of(cell).lower()] = text_of(value)
        if labels.get("salary"):
            posting.pay = labels["salary"]
        if labels.get("department"):
            posting.employer = labels["department"]
        posting.open_date = find_date(labels.get("open date")) or posting.open_date
        posting.close_date = find_date(labels.get("close date")) or posting.close_date
        summary = soup.find(["h2", "h3"], string=re.compile(r"^\s*summary\s*$", re.I))
        if summary:
            parts = []
            for sibling in summary.find_all_next():
                if sibling.name in ("h1", "h2"):
                    break
                if sibling.name in ("p", "div") and not sibling.find(["p", "div"]):
                    parts.append(text_of(sibling))
            posting.description = short(" ".join(p for p in parts if p))


class GuamJobsOnline(Parser):
    """guamjobsonline.com search results: job cards with date, employer,
    location, pay and the start of the description."""

    _ICONS = {
        "fa-calendar": "date",
        "fa-briefcase": "employer",
        "fa-location-arrow": "location",
        "fa-usd": "pay",
    }

    def listing(self, result, board):
        soup = BeautifulSoup(result.content, "lxml")
        postings: dict[str, Posting] = {}
        for card in soup.select("div.job-card"):
            heading = card.find(["h3", "h2"])
            link = heading.find("a", href=True) if heading else None
            url = absolute(link["href"], result.url) if link else None
            if not url:
                continue
            url = self._clean(url)
            fields = {}
            for icon in card.find_all("i"):
                classes = icon.get("class") or []
                kind = next((k for c, k in self._ICONS.items() if c in classes), None)
                text = icon.next_sibling
                if kind and isinstance(text, str) and clean_text(text):
                    fields[kind] = clean_text(text)
            snippet = card.find("p")
            title = re.sub(r"^featured\s+", "", text_of(link), flags=re.I)
            postings[url] = Posting(
                url=url,
                title=title,
                employer=fields.get("employer") or board.employer,
                location=fields.get("location") or board.location,
                pay=tidy_pay(fields.get("pay")),
                open_date=find_date(fields.get("date")),
                description=short(text_of(snippet)),
                detail_url=url,
            )
        next_pages = [
            link
            for anchor in soup.select("ul.pagination a[href], a[rel=next]")
            if (link := absolute(anchor["href"], result.url))
        ]
        return Listing(list(postings.values()), next_pages)

    @staticmethod
    def _clean(url: str) -> str:
        """job_view.php?jobid=10312&keyword=&... -> job_view.php?jobid=10312"""
        match = re.search(r"[?&]jobid=(\d+)", url)
        if not match:
            return url
        return f"{url.split('?')[0]}?jobid={match.group(1)}"

    def detail(self, result, posting):
        """The full description: the text under the "Job: <title>" heading."""
        soup = soup_of(result)
        heading = soup.find(["h2", "h3"], string=re.compile(r"^\s*job:", re.I))
        if heading is None or heading.parent is None:
            return
        heading_text = text_of(heading)
        text = text_of(heading.parent).replace(heading_text, "", 1).strip()
        if len(text) > len(posting.description or ""):
            posting.description = short(text)


class SectionsPage(Parser):
    """A page with one heading per opening and its description below
    (GuamTemps). Pay is picked out of the description when it is written
    with a dollar sign."""

    def __init__(self, heading: str = "h4"):
        self.heading = heading

    def listing(self, result, board):
        soup = soup_of(result)
        for chrome in soup.select("header, footer, nav"):
            chrome.decompose()
        root = soup.find("main") or soup.body or soup
        postings = []
        current: Posting | None = None
        lines: list[str] = []

        def finish():
            if current is not None:
                text = " ".join(lines)
                posted = re.search(r"date posted:?\s*(.{0,40})", text, re.I)
                current.open_date = find_date(posted.group(1)) if posted else None
                current.pay = find_pay(text)
                # "Date posted: ... Pay: ... Job description: <the description>"
                body = re.split(r"job description:?", text, maxsplit=1, flags=re.I)
                current.description = short(body[-1])
                postings.append(current)

        for node in root.find_all([self.heading, "p", "li"]):
            if node.name == self.heading:
                finish()
                title = text_of(node)
                current = (
                    Posting(
                        url=f"{result.url.split('#')[0]}#{_slug(title)}",
                        title=title,
                        employer=board.employer,
                        location=board.location,
                    )
                    if title
                    else None
                )
                lines = []
            elif current is not None and node.find_parent(self.heading) is None:
                text = text_of(node)
                if (
                    text
                    and text.upper() != "SUBMIT APPLICATION"
                    and (node.name == "p" or not node.find("p"))
                ):
                    lines.append(text)
        finish()
        return Listing(postings)


class PeopleAdmin(Parser):
    """PeopleAdmin employment portals (*.peopleadmin.com/postings/search):
    postings with department, open date, deadline and the start of the
    duties; each posting's page has the pay."""

    def listing(self, result, board):
        soup = BeautifulSoup(result.content, "lxml")
        postings = []
        for item in soup.select("div.job-item-posting"):
            link = item.select_one("h3 a[href]")
            url = absolute(link["href"], result.url) if link else None
            if not url:
                continue
            columns = [text_of(c) for c in item.select(".job-title-text-wrap")][1:]
            dates = [d for d in (find_date(c) for c in columns) if d]
            department = columns[0] if columns and not find_date(columns[0]) else None
            number = columns[1] if len(columns) > 1 else None
            title = text_of(link)
            postings.append(
                Posting(
                    url=url,
                    title=title,
                    employer=board.employer
                    if not department or department == board.employer
                    else f"{board.employer}, {department}"
                    if board.employer
                    else department,
                    location=board.location,
                    open_date=dates[0] if dates else None,
                    close_date=dates[1] if len(dates) > 1 else None,
                    description=short(text_of(item.select_one(".job-description"))),
                    detail_url=url,
                    reference=number,
                )
            )
        next_pages = [
            link
            for anchor in soup.select("a[rel=next], a.next_page")
            if (link := absolute(anchor["href"], result.url))
        ]
        return Listing(postings, next_pages)

    def detail(self, result, posting):
        soup = BeautifulSoup(result.content, "lxml")
        for label in soup.find_all("th"):
            name = text_of(label).lower()
            value = text_of(label.find_next_sibling("td"))
            if not value:
                continue
            if any(word in name for word in ("salary", "pay", "compensation", "wage")):
                posting.pay = posting.pay or value
            elif name.startswith("location"):
                posting.location = value
            elif "close" in name or "deadline" in name:
                close_date, note = closing(value)
                posting.close_date = posting.close_date or close_date
                posting.close_note = posting.close_note or note


class UniversityOfGuam(Parser):
    """The University of Guam: its PeopleAdmin portal (dates, duties, pay)
    plus the job opportunities page, whose announcement PDFs are linked to the
    matching PeopleAdmin posting by number ("005-27 Accountant II"), and
    listed on their own when the posting is not on PeopleAdmin."""

    _NUMBER_RE = re.compile(r"^\s*(\d{3}-\d{2})\b")

    def __init__(self):
        self.portal = PeopleAdmin()
        self.page = PdfLinks()

    def listing(self, result, board):
        if "peopleadmin" in (urlsplit(result.url).hostname or ""):
            return self.portal.listing(result, board)
        return self.page.listing(result, board)

    def detail(self, result, posting):
        self.portal.detail(result, posting)

    def finish(self, postings):
        portal = [p for p in postings if p.reference]
        by_number = {p.reference: p for p in portal}
        # Titles shared by several postings ("Research Assistant I") can't be matched.
        keys = [self._title_key(p.title) for p in portal]
        by_title = {k: p for k, p in zip(keys, portal, strict=True) if keys.count(k) == 1}
        kept = []
        for posting in postings:
            twin = None
            if posting.pdf_url and not posting.reference:
                match = self._NUMBER_RE.match(posting.title)
                twin = by_number.get(match.group(1)) if match else None
                twin = twin or by_title.get(self._title_key(posting.title))
            if twin is not None:
                twin.pdf_url = twin.pdf_url or posting.pdf_url
                continue
            kept.append(posting)
        return kept

    @classmethod
    def _title_key(cls, title: str) -> str:
        """'005-27 Accountant II (PeopleAdmin)' -> 'accountant ii'"""
        title = cls._NUMBER_RE.sub("", title)
        title = re.sub(r"\((peopleadmin|amendment|please [^)]*)\)", "", title, flags=re.I)
        return re.sub(r"\s+", " ", title).strip().lower()


class UsaJobs(Parser):
    """USAJOBS, through its official search API (the website's search page is
    built with JavaScript). Needs a free key from developer.usajobs.gov in
    USAJOBS_API_KEY, with the email it was issued to in USAJOBS_EMAIL."""

    API = "https://data.usajobs.gov/api/search"
    _INTERVALS = {
        "PA": "per year",
        "PH": "per hour",
        "PM": "per month",
        "BW": "every two weeks",
        "PW": "per week",
        "PD": "per day",
    }

    def collect(self, client, board, settings) -> list[Posting]:
        if not (settings.usajobs_api_key and settings.usajobs_email):
            raise BoardError(
                "needs a free USAJOBS API key: set USAJOBS_API_KEY and USAJOBS_EMAIL "
                "(see docs/jobs.md)"
            )
        headers = {
            "Authorization-Key": settings.usajobs_api_key,
            "User-Agent": settings.usajobs_email,
            "Host": "data.usajobs.gov",
        }
        postings = []
        for page in range(1, 6):
            url = f"{self.API}?LocationName=Guam&ResultsPerPage=250&Page={page}"
            result = client.get(url, accept="application/json", headers=headers)
            data = json.loads(result.content)["SearchResult"]
            items = data.get("SearchResultItems") or []
            postings += [self._posting(item["MatchedObjectDescriptor"], board) for item in items]
            pages = int((data.get("UserArea") or {}).get("NumberOfPages") or 1)
            if page >= pages or not items:
                break
        return postings

    def _posting(self, job: dict, board) -> Posting:
        pay = None
        for amount in job.get("PositionRemuneration") or []:
            low, high = amount.get("MinimumRange"), amount.get("MaximumRange")
            interval = self._INTERVALS.get(amount.get("RateIntervalCode"), "")
            money = [f"${float(v):,.2f}" for v in (low, high) if v not in (None, "", "0")]
            if money:
                pay = " to ".join(dict.fromkeys(money)) + (f" {interval}" if interval else "")
                break
        details = (job.get("UserArea") or {}).get("Details") or {}
        locations = [
            loc.get("LocationName", "")
            for loc in job.get("PositionLocation") or []
            if "guam" in loc.get("LocationName", "").lower()
        ]
        employer = job.get("OrganizationName") or job.get("DepartmentName") or board.employer
        return Posting(
            url=job.get("PositionURI") or job.get("ApplyURI", [""])[0],
            title=clean_text(job.get("PositionTitle")),
            employer=employer,
            pay=pay,
            description=short(details.get("JobSummary") or job.get("QualificationSummary")),
            location="; ".join(locations) or job.get("PositionLocationDisplay") or board.location,
            open_date=find_date(job.get("PublicationStartDate")),
            close_date=find_date(job.get("ApplicationCloseDate")),
        )


def _pick(row: dict, *names: str):
    """The first of these fields that has a value, ignoring case and underscores."""
    folded = {k.replace("_", "").lower(): v for k, v in row.items()}
    for name in names:
        value = folded.get(name.replace("_", "").lower())
        if value not in (None, "", []):
            return value
    return None


class GovGuam(Parser):
    """jobs.guam.gov, the Government of Guam's job portal. Its pages are built
    with JavaScript; the public job list (jobs.guam.gov/public/jobs) loads
    from the portal's API, which is read here the same way: open
    announcements, newest first."""

    API = "https://gfmis-realprod-prd-jobapplicant-api.azurewebsites.us/api"
    PAGE_SIZE = 15  # what the public page asks for
    # Sent as the public page does (the API serves that site).
    HEADERS = {"Origin": "https://jobs.guam.gov", "Referer": "https://jobs.guam.gov/"}
    detail_headers = HEADERS

    def collect(self, client, board, settings) -> list[Posting]:
        postings: list[Posting] = []
        rows_total = None
        for page in range(1, 41):
            payload = {
                "tableName": "",
                "searchParams": "[]",
                "columns": "*",
                "pageNumber": page,
                "pageSize": self.PAGE_SIZE,
                "location": "global/My-job-Applications-Info",
                "sortData": " [DATE_ENTERED] desc",
                # The same filter the public page uses: announcements not yet closed.
                "additionalWhere": (
                    "(DATE_CLOSED IS NULL OR "
                    "DATEADD(HOUR, 17, CAST(DATE_CLOSED AS DATE)) > GETDATE())"
                ),
            }
            result = client.post_json(f"{self.API}/Public/Getjobs", payload, headers=self.HEADERS)
            data = json.loads(result.content)
            rows = data.get("results") or []
            rows_total = data.get("rowCount", rows_total)
            if rows and page == 1:
                log.debug("[govguam] announcement fields: %s", sorted(rows[0]))
            for row in rows:
                posting = self._posting(row, board)
                if posting is None:
                    raise BoardError(
                        "the GovGuam jobs API answered in an unexpected shape "
                        f"(fields: {', '.join(sorted(row))[:300]})"
                    )
                postings.append(posting)
            if not rows or (rows_total is not None and len(postings) >= int(rows_total)):
                break
        if not postings:
            raise BoardError("the GovGuam jobs API lists no open announcements")
        return postings

    def _posting(self, row: dict, board) -> Posting | None:
        number = _pick(row, "ANNOUNCEMENT_ID", "AnnouncementId", "ID")
        title = _pick(row, "PositionTitle", "POSITION_TITLE", "ANNOUNCEMENT_TITLE", "JobTitle")
        if number is None or not title:
            return None
        posting = Posting(
            url=f"https://jobs.guam.gov/public/jobs#announcement-{number}",
            title=clean_text(str(title)),
            employer=None,
            location=board.location,
            detail_url=f"{self.API}/Public/GetJobAnnouncementbyid?id={number}",
            reference=str(number),
        )
        self._fill(row, posting, board)
        return posting

    def _fill(self, row: dict, posting: Posting, board=None) -> None:
        """Posting fields from an API row (board: None for the announcement's own page)."""
        department = _pick(
            row, "DEPARTMENT_NAME", "DepartmentName", "DEPARTMENT", "AGENCY", "AGENCY_NAME"
        )
        if department:
            posting.employer = clean_text(str(department))
        elif board is not None:
            posting.employer = board.employer
        posting.open_date = posting.open_date or find_date(
            str(_pick(row, "DATE_OPENED", "OPEN_DATE", "START_DATE", "DATE_POSTED") or "")
        )
        close = _pick(row, "DATE_CLOSED", "CLOSE_DATE", "CLOSING_DATE", "END_DATE")
        if close:
            posting.close_date = find_date(str(close))
        elif board is not None:
            posting.close_note = "Open until filled"
        pay = _pick(row, "SALARY_RANGE", "SalaryRange", "SALARY", "PAY_RANGE", "PAY_GRADE")
        low = _pick(row, "MIN_SALARY", "SALARY_MIN", "MINIMUM_SALARY", "SALARY_FROM")
        high = _pick(row, "MAX_SALARY", "SALARY_MAX", "MAXIMUM_SALARY", "SALARY_TO")
        if not pay and (low or high):
            pay = " to ".join(f"${float(v):,.2f}" for v in (low, high) if v)
        posting.pay = clean_text(str(pay)) if pay else posting.pay
        location = _pick(row, "WORK_LOCATION", "LOCATION", "DUTY_STATION")
        posting.location = clean_text(str(location)) if location else posting.location
        text = _pick(row, "NATURE_OF_WORK", "DUTIES", "JOB_DESCRIPTION", "DESCRIPTION", "SUMMARY")
        if text:
            posting.description = short(strip_html(str(text)))

    def detail(self, result, posting):
        data = json.loads(result.content)
        row = data.get("result") if isinstance(data, dict) and "result" in data else data
        if isinstance(row, list):
            row = row[0] if row else {}
        if isinstance(row, dict):
            log.debug("[govguam] announcement detail fields: %s", sorted(row))
            self._fill(row, posting)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


PARSERS: dict[str, Parser] = {
    "hafajobs": HafaJobs(),
    "pdf_links": PdfLinks(),
    "federalgovernmentjobs": FederalGovernmentJobs(),
    "guamjobsonline": GuamJobsOnline(),
    "sections": SectionsPage(),
    "peopleadmin": PeopleAdmin(),
    "uog": UniversityOfGuam(),
    "usajobs": UsaJobs(),
    "govguam": GovGuam(),
}
