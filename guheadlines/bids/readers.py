"""Reading each kind of input in config/bids.yaml.

Every reader returns FoundBids. A bid's uid starts with its input kind
("notice:", "table:"), so two inputs of one source never mix up their bids.

Two kinds of input:
  notices  a listing on notices.guam.gov, the Government of Guam's public
           notices portal, narrowed to its procurement topics (IFB, RFP and
           emergency procurement). Many agencies post their bids there.
  table    a web page with a table of bids, one row each: the Guam Power
           Authority's and GHURA's bid portal, the Guam Memorial Hospital's and
           the Guam Waterworks Authority's procurement pages, the Port's list.
           Columns are found by their headings (number, title, issue date,
           due date, status), so one reader fits all of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..events.readers import _ACRONYMS as _EVENT_ACRONYMS
from ..events.readers import _TOKEN_RE, _next_page, _token_date, _token_time, tidy_name
from ..scraper.http import FetchError, FetchResult, HttpClient
from ..scraper.text import GUAM_TZ, clean_text, truncate

DESCRIPTION_LIMIT = 400
NOTICE_PAGES = 6  # notices.guam.gov listing pages read per check (10 notices each)
NOTICE_MAX_AGE = timedelta(days=150)  # older notices end the reading of pages
MAX_DETAILS = 15  # a table source's bid pages read per check, for their due dates


class BidError(ValueError):
    """The address answered, but not with a list of bids we can read."""


@dataclass
class FoundBid:
    # Stays the same when the bid is edited (or posted again as a "2nd notice"),
    # so it is updated instead of added again.
    uid: str
    title: str
    url: str
    number: str | None = None
    kind: str | None = None
    agency: str | None = None
    description: str | None = None
    document_url: str | None = None
    posted_on: date | None = None
    due_at: datetime | None = None  # always with a time zone
    due_time: bool = False  # False: only the day is known (due_at is its last minute)
    # What the source says about it ("Open", "Closed", "Awarded"...), if anything.
    status: str | None = None

    @property
    def number_key(self) -> str | None:
        return number_key(self.number, self.agency)


# ---------------------------------------------------------- due dates in text


def parse_due(text: str | None) -> tuple[datetime, bool] | None:
    """(when, has_time) from the first date in `text` and the time written
    nearest to it, before or after: "10:00 a.m., Tuesday, November 10, 2026",
    "January 06, 2027 10:00 AM ChST", "10:00 AM, 11/11/26". Guam time. A day
    without a time is due at its last minute."""
    dates, times = [], []
    for match in _TOKEN_RE.finditer(text or ""):
        if match.group("date"):
            value = _token_date(match)
            if value is not None:
                dates.append((match.start(), match.end(), value))
        else:
            value = _token_time(match)
            if value is not None:
                times.append((match.start(), match.end(), value))
    if not dates:
        return None
    start, end, day = dates[0]
    # The nearest time that isn't closer to another date.
    best = None
    for t_start, t_end, value in times:
        gap = t_start - end if t_start >= end else start - t_end
        if gap > 60:
            continue
        others = [d for d in dates[1:] if (d[0] - t_end if d[0] >= t_end else t_start - d[1]) < gap]
        if others:
            continue
        if best is None or gap < best[0]:
            best = (gap, value)
    if best is None:
        return datetime.combine(day, time(23, 59), tzinfo=GUAM_TZ), False
    return datetime.combine(day, best[1], tzinfo=GUAM_TZ), True


# Labels a due date follows, best first. Bid openings come last: bids are due
# at or just before the opening.
_DUE_LABELS = [
    r"(?:submission|proposals?|bids?|offers?|responses?)\s+(?:deadline|due(?:\s+date)?)",
    r"deadline\s+(?:for|of)\s+(?:the\s+)?(?:submission|receipt|proposals?|bids?)(?:\s+of\s+\w+)?",
    r"(?:bid|proposal)\s+submission(?:\s+date)?",
    r"(?:close|closing)\s+date(?:/time)?",
    r"due\s+(?:date|time)(?:\s*(?:&|and)\s*(?:date|time))?",
    r"submission(?:\s+date)?",
    r"deadline",
    r"(?:bid\s+)?opening(?:\s+date)?",
]
_DUE_LABEL_RES = [
    re.compile(rf"\b{label}\b\s*(?:\(\w+\))?\s*[:\-–]?", re.I) for label in _DUE_LABELS
]
# Lines and labels about other dates of a solicitation (questions, meetings).
_OTHER_DATE_RE = re.compile(
    r"\b(rfi|rfc|questions?|inquir|clarification|intent|conference|site visit|demonstration|"
    r"shortlist|notification|award|pre-?bid|pre-?proposal|registration|q\s*&\s*c|posted)",
    re.I,
)


def due_from_text(text: str | None) -> tuple[datetime, bool] | None:
    """The due date written after a label such as "Submission Deadline:",
    "DEADLINE for SUBMISSION:", "Bid Submission:" or "Close Date:"."""
    text = clean_text(text)
    for label_re in _DUE_LABEL_RES:
        for match in label_re.finditer(text):
            before = text[max(0, match.start() - 30) : match.start()]
            if _OTHER_DATE_RE.search(before.split(".")[-1]) or _OTHER_DATE_RE.search(
                match.group(0)
            ):
                continue
            found = parse_due(_until_next_label(text[match.end() : match.end() + 90]))
            if found is not None:
                return found
    return None


# What starts the next item after a due date ("... Bid Opening Date: 10:15 a.m.").
_NEXT_LABEL_RE = re.compile(
    r"\b(opening|conference|deadline|due|submission|site visit|questions?|date\s*[:(])", re.I
)


def _until_next_label(text: str) -> str:
    """`text` up to the label that follows its first date, so the time of the
    next item isn't taken for the due time."""
    first = next((m for m in _TOKEN_RE.finditer(text) if m.group("date")), None)
    if first is None:
        return text
    following = _NEXT_LABEL_RE.search(text, first.end())
    return text[: following.start()] if following else text


def due_from_lines(lines: list[str]) -> tuple[datetime, bool] | None:
    """The due date among a table cell's lines ("RFI Due: ...", "Submission
    Due: ..."): a submission or deadline line if there is one, else the latest
    date on a line that isn't about questions or meetings."""
    kept = [line for line in lines if not _OTHER_DATE_RE.search(line)]
    labelled = [found for found in map(due_from_text, kept) if found]
    if labelled:
        return max(labelled)
    joined = " ".join(kept)
    if sum(1 for m in _TOKEN_RE.finditer(joined) if m.group("date")) <= 1:
        return parse_due(joined)  # "January 06, 2027" / "10:00 AM ChST" on two lines
    dated = [found for found in map(parse_due, kept) if found]
    return max(dated) if dated else None


def _posted(text: str | None) -> date | None:
    found = parse_due(text)
    return found[0].date() if found else None


# -------------------------------------------------- numbers, kinds, agencies

# A solicitation number: words of capitals and digits joined by hyphens, with
# a digit in it ("GSA-003-27", "IFB-PAG-010-26", "GIAA-004-FY27", "C-0040FY26",
# "GU-THS-014B(005)"), optionally after a kind ("ITB GPA-001-27").
_NUMBER = r"[A-Z0-9]{1,8}(?:\([0-9A-Z]{1,5}\))?(?:[-/][A-Z0-9]{1,8}(?:\([0-9A-Z]{1,5}\))?){1,5}"
_NUMBER_RE = re.compile(rf"(?<![\w-])(?=[A-Z0-9()/-]*\d)(?=[A-Z0-9()/-]*[A-Z])({_NUMBER})(?![\w-])")
_LABELLED_NUMBER_RE = re.compile(
    rf"(?:\bno\.?|\bnumber|#|\b(?:ifb|rfp|rfq|itb|bid|proposals?))\s*[:.,#]?\s*({_NUMBER})(?![\w-])",
    re.I,
)
_NOT_NUMBERS = re.compile(r"^(COVID|H1N1|FY|CY|P\.?L|PL|MP3|4X2|4X4|2X4|K-12|PRE-K)\b", re.I)
_KIND_WORDS = r"(?:ifb|itb|rfp|rfq|rfi|soi|ms|multi-step|re-?bid|re-?solicitation|no)"
_LEADING_KIND_RE = re.compile(rf"^(?:{_KIND_WORDS}(?![a-z])[\s\-/.:]*)+", re.I)


def find_number(*texts: str | None) -> str | None:
    """The solicitation number in a notice's title or text: one written after
    "No.", "IFB" or "RFP" if there is one, else the first that looks like one."""
    for text in texts:
        for match in _LABELLED_NUMBER_RE.finditer(clean_text(text)):
            number = match.group(1).upper()
            if re.search(r"\d", number) and not _NOT_NUMBERS.match(number):
                return number
    for text in texts:
        for match in _NUMBER_RE.finditer(clean_text(text)):
            if not _NOT_NUMBERS.match(match.group(1)):
                return match.group(1)
    return None


def _initials(name: str | None) -> str:
    """'Guam Waterworks Authority' -> 'GWA' (the same however it's capitalized)."""
    words = re.findall(r"[A-Za-z]+", name or "")
    return "".join(w[0] for w in words if w.lower() not in {"of", "and", "the", "for"}).upper()


def number_key(number: str | None, agency: str | None = None) -> str | None:
    """What two listings of one solicitation share, however each writes its
    number: 'ITB GPA-001-27' and 'GPA-001-27' -> 'GPA00127'. A number of digits
    only ('IFB 2026-27') gets the agency's initials, so two agencies' "2026-27"
    stay apart."""
    if not number:
        return None
    key = re.sub(r"[^A-Z0-9]", "", _LEADING_KIND_RE.sub("", number.upper()))
    if len(key) < 4:
        return None
    if not re.search(r"[A-Z]", key):
        key = _initials(agency) + key
    return key


_KINDS = [
    (re.compile(r"\b(ms[\s-]?ifb|multi-?step)\b", re.I), "Multi-step bid"),
    (re.compile(r"\bemergency\s+(procurement|purchas\w*)\b", re.I), "Emergency procurement"),
    (re.compile(r"\b(rfp|requests? for proposals?|proposals?)\b", re.I), "Request for proposals"),
    (re.compile(r"\b(rfq|requests? for quot\w*|quotations?)\b", re.I), "Request for quotations"),
    (re.compile(r"\b(rfi|requests? for information)\b", re.I), "Request for information"),
    (
        re.compile(r"\b(soi|statements? of (?:qualifications?|interest))\b", re.I),
        "Statement of qualifications",
    ),
    (re.compile(r"\b(surplus|salvage)\b", re.I), "Surplus sale"),
    (
        re.compile(
            r"\b(ifb|itb|iqb|invitations? (?:for|to) bids?|bid invitation|re-?bid|"
            r"sealed bids?|bids?)\b",
            re.I,
        ),
        "Invitation for bid",
    ),
]


def kind_of(*texts: str | None) -> str | None:
    """'IFB', 'Invitation for Bid No. ...', 'Multi-Step GPA-078-26' -> a plain name."""
    for text in texts:
        for pattern, kind in _KINDS:
            if text and pattern.search(text):
                return kind
    return None


# ---------------------------------------------------------------- titles

_TITLE_NOISE_RE = re.compile(
    r"^\W*(?:(?:\d+(?:st|nd|rd|th)|first|second|third|final|amended|revised)\s+)?"
    r"(?:notice(?:\s+of)?\b\s*[-–:]?\s*)?",
    re.I,
)
_KIND_PHRASE = (
    r"(?:invitations?\s+(?:for|to)\s+bids?|bid\s+invitation|requests?\s+for\s+"
    r"(?:proposals?|quotations?|information)|ifb|itb|rfp|rfq|rfi|multi-?step(?:\s+bid)?)"
)
# A kind written before the title: "Invitation for Bid: ...", "IFB-PAG-001-27
# ...", "REQUEST FOR PROPOSAL No. ...", "Bid Invitation for ...".
_LEADING_KIND_PHRASE_RE = re.compile(
    rf"^(?:bid\s+invitation\s+for\s+|{_KIND_PHRASE}(?:\s*(?:no\.?|number|#))?\s*[:.,#\-–]\s*"
    rf"|{_KIND_PHRASE}\s+(?=[A-Z0-9()/-]*\d[A-Z0-9()/-]*(?:\s|$))"
    # "DRT RFP for CFE ...", "RFP-001-2027 For an Enterprise ..."
    rf"|(?:[A-Z]{{2,6}}\s+)?{_KIND_PHRASE}\s+for\s+(?:an?\s+|the\s+)?|for\s+(?:an?|the)\s+)",
    re.I,
)
_ONLY_KIND_RE = re.compile(rf"^\W*{_KIND_PHRASE}\W*$", re.I)
_SMALL_WORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
# Words kept in capitals when a title written in capitals is tidied.
_ACRONYMS = _EVENT_ACRONYMS | {
    "ABIS", "ACCU", "ADA", "ADV", "AHU", "AMP", "AMPS", "CCTV", "CFE", "CIP", "DMA", "DPHSS",
    "DRT", "EHR", "EMS", "EV", "FY", "GIAA", "GHURA", "GPD", "GPLS", "GU", "HVAC", "ID", "IDIQ",
    "IFB", "II", "III", "IQB", "IT", "ITB", "IV", "LED", "LLC", "MS", "OPA", "PA", "PAG", "PPE",
    "PV", "RFI", "RFP", "RFQ", "SUV", "TV", "UPS", "VFD", "VI",
}  # fmt: skip


def tidy_caps(text: str) -> str:
    """'CABLE RACK BID IDIQ' -> 'Cable Rack Bid IDIQ'. Titles not written all
    in capitals are kept as they are."""
    if not text or text != text.upper() or not re.search(r"[A-Z]{4}", text):
        return text
    words = []
    for number, word in enumerate(text.split(" ")):
        bare = word.strip("().,-:;\"'“”")
        initials = re.fullmatch(r"[A-Z](?:\.[A-Z])+\.?", bare)
        if bare in _ACRONYMS or re.search(r"\d", bare) or initials:
            words.append(word)
        elif number and bare.lower() in _SMALL_WORDS:
            words.append(word.lower())
        else:
            words.append("-".join(_capitalize(part) for part in word.split("-")))
    return " ".join(words)


def _capitalize(word: str) -> str:
    """'“C”' -> '“C”' and 'WAREHOUSE' -> 'Warehouse' (str.capitalize lowercases
    a letter after a quote mark)."""
    lower = word.lower()
    for index, char in enumerate(lower):
        if char.isalpha():
            return lower[:index] + char.upper() + lower[index + 1 :]
    return lower


def tidy_title(title: str, number: str | None = None) -> str:
    """A solicitation's name without the notice wording, its kind and its
    number (shown apart): 'GSA-003-27 Cargo Van (Latest Model)' -> 'Cargo Van
    (Latest Model)'; 'REQUEST FOR PROPOSAL No. OPA-RFP-26-014 Guam Department
    of Education Independent Financial Audit Services' -> 'Guam Department of
    Education Independent Financial Audit Services'."""
    text = clean_text(title)
    numbers = []
    if number:
        numbers = [number] + [n for n in [_LEADING_KIND_RE.sub("", number)] if n and n != number]
    for _ in range(4):
        before = text
        text = _TITLE_NOISE_RE.sub("", text)
        for value in numbers:
            # "GIAA RFP No. C-0040FY26 - ..." and "GSA-003-27 ..."
            text = re.sub(
                rf"^(?:[A-Z]{{2,6}}\s+)?(?:{_KIND_PHRASE}\s*)?(?:(?:no\.?|number|#)\s*[:.,#]?\s*)?"
                rf"{re.escape(value)}(?![\w-])",
                "",
                text,
                flags=re.I,
            )
        text = _LEADING_KIND_PHRASE_RE.sub("", text).strip(" -–—:|,.")
        if text == before:
            break
    text = tidy_caps(text)
    return text[:1].upper() + text[1:]


def _link(base: str, href: str) -> str:
    """A link's full address, with spaces written as %20 (the OpenGov portal
    puts bid numbers with spaces in its links)."""
    return urljoin(base, href.strip()).replace(" ", "%20")


# ------------------------------------------------------- notices.guam.gov

# Notices about a solicitation that is over, not one to bid on.
CLOSED_RE = re.compile(
    r"\b(cancel+(?:ed|ation)|rejection of all|stay of procurement|notice of (?:intent to )?award|"
    r"awarded|postponed|suspended|withdrawn|terminated)\b",
    re.I,
)


def _li_value(article: Tag, label: str) -> Tag | None:
    for item in article.select("li"):
        strong = item.find("strong")
        if strong and clean_text(strong.get_text(" ")).lower().startswith(label):
            return item
    return None


# "Bid For: ... Destination: Department of Agriculture Submission Deadline: ..."
_DESTINATION_RE = re.compile(
    r"\bDestination:\s*(?:the\s+)?(.+?)(?=\s+(?:Submission|Opening|Bid|Date|Deadline|Pre-Bid)"
    r"\b[^:]{0,30}:|$)",
    re.I,
)
_FORM_LABEL_RE = re.compile(r"\b[A-Z][A-Za-z ]{1,30}:(?=\s)")
_KIND_WORDS_RE = re.compile(
    rf"{_KIND_PHRASE}|\b(?:invitation|bids?|proposals?|requests?|no|number|notice|of|for|the|"
    r"and|an?|to)\b",
    re.I,
)


def notice_description(body: str | None, title: str, number: str | None) -> str | None:
    """The part of a notice's text worth showing under its title: who the
    General Services Agency is buying for ("For the Guam Public Library
    System."), or a sentence about the bid. Text that only repeats the title,
    number and dates (shown apart) is left out."""
    if not body:
        return None
    destination = _DESTINATION_RE.search(body)
    if destination:
        return f"For the {destination.group(1).strip(' .')}."
    if len(_FORM_LABEL_RE.findall(body)) >= 2:
        return None  # "Bid No: ... Bid Description: ... Bid Submission: ..."
    rest = body.lower()
    for part in (title.lower(), (number or "").lower()):
        if part:
            rest = rest.replace(part, " ")
    rest = _KIND_WORDS_RE.sub(" ", rest)
    title_words = set(re.findall(r"[a-z]{3,}", title.lower()))
    new_words = [w for w in re.findall(r"[a-z]{3,}", rest) if w not in title_words]
    return body if len(new_words) >= 3 else None


def parse_notices(result: FetchResult) -> list[FoundBid]:
    """Solicitations from a notices.guam.gov listing page."""
    soup = BeautifulSoup(result.content, "lxml")
    articles = soup.select("article.post")
    if not articles:
        raise BidError(f"no notices found on {result.url}; the page may have changed")
    found: list[FoundBid] = []
    for article in articles:
        for hidden in article.select(".mfp-hide"):
            hidden.decompose()  # the share dialog repeats the title
        heading = article.select_one(".post-title a[href]")
        if heading is None:
            continue
        raw_title = clean_text(heading.get_text(" "))
        body_node = article.select_one(".contxt")
        body = clean_text(body_node.get_text(" ")) if body_node else ""
        department = article.select_one('a[href*="department_id="]')
        agency = tidy_name(department.get_text(" ")) if department else None
        topics_item = _li_value(article, "notice topic")
        topics = clean_text(topics_item.get_text(" ")) if topics_item else None
        posted_item = _li_value(article, "posted on")
        posted = _posted(posted_item.get_text(" ")) if posted_item else None
        deadline = article.select_one(".deadline-text")
        due = due_from_text(body) or due_from_text(raw_title)
        if due is None and deadline is not None:
            due = parse_due(deadline.get_text(" "))
        url = _link(result.url, heading["href"])
        notice = re.search(r"/notice_detail/(\d+)", url)
        number = find_number(raw_title, body)
        title = tidy_title(raw_title, number)
        if not title or _ONLY_KIND_RE.match(raw_title) and body:
            # "INVITATION FOR BID" with the project in the text
            title = tidy_title(body.split(". ")[0][:200], number) or title
        document = None
        for link in article.select(".contxt a[href]"):
            href = _link(result.url, link["href"])
            if href.lower().split("?")[0].endswith(".pdf"):
                document = href
                break
        description = notice_description(body, title, number)
        found.append(
            FoundBid(
                uid=f"notice:{notice.group(1) if notice else url}"[:500],
                title=truncate(title or raw_title, 300),
                url=url,
                number=number,
                kind=kind_of(raw_title, number, topics, body),
                agency=agency,
                description=truncate(description, DESCRIPTION_LIMIT) if description else None,
                document_url=document,
                posted_on=posted,
                due_at=due[0] if due else None,
                due_time=due[1] if due else False,
                status="Closed" if CLOSED_RE.search(raw_title) else None,
            )
        )
    return found


def read_notices(
    client: HttpClient, url: str, today: date, max_pages: int = NOTICE_PAGES
) -> tuple[list[FoundBid], date | None]:
    """Solicitations from a notices.guam.gov listing and its next pages (newest
    first), and the posting date of the oldest notice read: notices posted
    before it were not looked at, so they aren't taken as removed. A
    solicitation's later notices ("2nd Notice of ...") come first and win."""
    bids: list[FoundBid] = []
    seen_pages: set[str] = set()
    oldest: date | None = None
    while url and url not in seen_pages and len(seen_pages) < max_pages:
        seen_pages.add(url)
        try:
            result = client.get(url)
            page_bids = parse_notices(result)
        except (FetchError, BidError):
            if len(seen_pages) == 1:
                raise
            break  # keep the pages read so far; older notices aren't taken as removed
        bids += page_bids
        dated = [b.posted_on for b in page_bids if b.posted_on]
        if dated:
            oldest = min(dated) if oldest is None else min(oldest, *dated)
        if oldest and today - oldest > NOTICE_MAX_AGE:
            break
        url = _next_page(result)
    # Newest first: by posting day, then by notice number (a corrected notice
    # posted the same day comes after the one it corrects).
    bids.sort(key=lambda b: (b.posted_on or date.min, _notice_id(b.uid)), reverse=True)
    kept: dict[str, FoundBid] = {}
    for bid in bids:
        key = bid.number_key or f"{bid.agency}|{bid.title.lower()}"
        kept.setdefault(key, bid)
    return list(kept.values()), oldest


def _notice_id(uid: str) -> int:
    found = re.search(r"(\d+)$", uid)
    return int(found.group(1)) if found else 0


# -------------------------------------------------------------- tables

_COLUMN_ROLES = [
    ("due", re.compile(r"due|deadline|submission|clos", re.I)),
    ("posted", re.compile(r"issu|posted|open date|available", re.I)),
    ("status", re.compile(r"status", re.I)),
    ("kind", re.compile(r"^\W*type\W*$", re.I)),
    ("number", re.compile(r"number|\bno\b\.?|announcement", re.I)),
    ("title", re.compile(r"title|description|project", re.I)),
]
_DETAIL_LINK_RE = re.compile(r"details|download .*documents|view more|more info", re.I)
_DOCUMENT_LINK_RE = re.compile(
    r"advertisement|announcement|invitation|bid packet|public notice|\bad\b", re.I
)
_FILE_LINK_RE = re.compile(r"\.(pdf|docx?|xlsx?)$", re.I)
_NOT_TITLE_RE = re.compile(
    r"^(view|download|click|•|>|amendment|addend|rfi|response|notice of|advertisement)", re.I
)


def _columns(header: list[str]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for index, name in enumerate(header):
        for role, pattern in _COLUMN_ROLES:
            if role not in roles and pattern.search(name):
                roles[role] = index
                break
    return roles


def _header_cells(table: Tag) -> list[Tag]:
    head = table.select("thead th")
    if head:
        return head
    first = table.find("tr")
    return first.find_all(["th", "td"], recursive=False) if first else []


def _rows(table: Tag, width: int, starts_with_kind: bool = False) -> list[list[Tag]]:
    """The table's rows below its headings. Some pages leave the <tr> tags out
    and run every cell together; those are cut into rows at each cell that
    names a kind ("IFB", "RFP") when the first column is the kind, else into
    rows of `width` cells."""
    header = {id(c) for c in _header_cells(table)}
    own = [td for td in table.find_all("td") if td.find_parent("table") is table]
    if all(td.parent.name == "tr" for td in own):
        rows = []
        for tr in table.find_all("tr"):
            if tr.find_parent("table") is not table:
                continue
            cells = [td for td in tr.find_all("td", recursive=False) if id(td) not in header]
            if len(cells) >= 2:
                rows.append(cells)
        return rows
    cells = [td for td in own if id(td) not in header]
    if starts_with_kind:
        rows, row = [], []
        for td in cells:
            text = clean_text(td.get_text(" "))
            if len(text) <= 10 and kind_of(text) and row:
                rows.append(row)
                row = []
            row.append(td)
        return [r for r in rows + [row] if len(r) >= 2]
    return [cells[i : i + width] for i in range(0, len(cells) - width + 1, width)]


def _first_text(cell: Tag) -> str:
    """The first meaningful text of a cell: its title, not its links to
    amendments or the contact's email."""
    for text in cell.stripped_strings:
        text = clean_text(text)
        if len(text) > 2 and not _NOT_TITLE_RE.match(text) and "@" not in text:
            return text
    return ""


def _lines(cell: Tag) -> list[str]:
    for br in cell.find_all("br"):
        br.replace_with("\n")
    return [clean_text(line) for line in cell.get_text("\n").split("\n") if clean_text(line)]


def find_table(soup: BeautifulSoup) -> tuple[Tag, dict[str, int], int] | None:
    """The page's table of bids: the first with a number and a title column."""
    for table in soup.find_all("table"):
        header = [clean_text(c.get_text(" ")) for c in _header_cells(table)]
        columns = _columns(header)
        if "title" in columns and ("number" in columns or "due" in columns):
            return table, columns, len(header)
    return None


def _cell_getter(cells: list[Tag], columns: dict[str, int]):
    def cell(role: str) -> Tag | None:
        index = columns.get(role)
        return cells[index] if index is not None and index < len(cells) else None

    return cell


def parse_table(result: FetchResult, agency: str | None = None) -> list[FoundBid]:
    """Solicitations from a page with a table of bids, one row each."""
    return [bid for _, bid in _table_rows(result, agency)[1]]


def _table_rows(
    result: FetchResult, agency: str | None = None
) -> tuple[list[str], list[tuple[list[Tag], FoundBid]]]:
    """The table's headings, and each row's cells with the solicitation read
    from them (the awards reader reads more cells of the same rows)."""
    soup = BeautifulSoup(result.content, "lxml")
    found_table = find_table(soup)
    if found_table is None:
        raise BidError(f"no table of bids found on {result.url}; the page may have changed")
    table, columns, width = found_table
    header = [clean_text(c.get_text(" ")) for c in _header_cells(table)]
    bids = []
    for cells in _rows(table, width, starts_with_kind=columns.get("kind") == 0):
        cell = _cell_getter(cells, columns)
        title_cell, number_cell = cell("title"), cell("number")
        if title_cell is None:
            continue
        number = _first_text(number_cell) if number_cell is not None else None
        title = _first_text(title_cell)
        if not title:
            continue
        links = [
            (clean_text(a.get_text(" ")), _link(result.url, a["href"]))
            for c in cells
            for a in c.find_all("a", href=True)
            if not a["href"].startswith(("mailto:", "/cdn-cgi/", "#", "javascript:"))
        ]
        page = next((href for text, href in links if _DETAIL_LINK_RE.search(text)), None)
        if page is None and number_cell is not None:
            link = number_cell.find("a", href=True)
            page = _link(result.url, link["href"]) if link else None
        if page is None:
            link = title_cell.find("a", href=True)
            href = _link(result.url, link["href"]) if link else None
            page = href if href and not _FILE_LINK_RE.search(href.split("?")[0]) else None
        document = next(
            (
                href
                for text, href in links
                if _DOCUMENT_LINK_RE.search(text) and _FILE_LINK_RE.search(href.split("?")[0])
            ),
            None,
        )
        due_cell, posted_cell = cell("due"), cell("posted")
        due = due_from_lines(_lines(due_cell)) if due_cell is not None else None
        status_cell, kind_cell = cell("status"), cell("kind")
        status = _first_text(status_cell) if status_cell is not None else None
        if not status:
            # "CLOSED" or "CANCELLED" in place of the due date or in a cell
            # of its own, or "*CANCELLED*" after the title.
            words = [clean_text(c.get_text(" ")) for c in cells]
            status = next((w for w in words if _STATUS_WORD_RE.match(w)), None)
            if status is None and CLOSED_RE.search(title):
                status = "Cancelled"
        kind_text = _first_text(kind_cell) if kind_cell is not None else None
        number = number or find_number(title)
        key = number or title
        bids.append(
            (
                cells,
                FoundBid(
                    uid=f"table:{key}"[:500],
                    title=truncate(tidy_title(title, number) or title, 300),
                    # Without a page of its own, a bid links to the list it is on.
                    url=page or result.url,
                    number=number,
                    kind=kind_of(kind_text, number, title),
                    agency=agency,
                    document_url=document,
                    posted_on=_posted(posted_cell.get_text(" "))
                    if posted_cell is not None
                    else None,
                    due_at=due[0] if due else None,
                    due_time=due[1] if due else False,
                    status=status or None,
                ),
            )
        )
    return header, bids


_STATUS_WORD_RE = re.compile(
    r"^\W*(closed|cancel+ed|awarded|withdrawn|postponed|suspended|terminated)\W*$", re.I
)
_OPEN_STATUS_RE = re.compile(r"^\W*(open|available|active|current|new|re-?opened)\b", re.I)


def is_closed(status: str | None) -> bool:
    """Whether a table's status says the solicitation is over: "Closed",
    "Awarded", "Cancelled", or the name of the company it was awarded to."""
    return bool(status and status.strip()) and not _OPEN_STATUS_RE.match(status)


def read_table(
    client: HttpClient,
    url: str,
    agency: str | None = None,
    details: bool = False,
    known: dict[str, tuple[datetime, bool]] | None = None,
) -> list[FoundBid]:
    """Solicitations from a page with a table of bids. With `details`, a bid
    the table lists as open but without a due date has its own page read for
    one ("Close Date: ..."), unless an earlier check found it (`known`: uid ->
    (due_at, due_time))."""
    bids = parse_table(client.get(url), agency)
    if details:
        known = known or {}
        wanted = []
        for bid in bids:
            if bid.due_at is not None or is_closed(bid.status) or bid.url == url:
                continue
            if bid.uid in known:
                bid.due_at, bid.due_time = known[bid.uid]
            else:
                wanted.append(bid)
        for bid in wanted[:MAX_DETAILS]:
            try:
                page = client.get(bid.url)
            except FetchError:
                continue  # listed without a due date, as before
            soup = BeautifulSoup(page.content, "lxml")
            for junk in soup(["script", "style", "nav", "header", "footer"]):
                junk.decompose()
            due = due_from_text(soup.get_text(" "))
            if due:
                bid.due_at, bid.due_time = due
    return bids


# -------------------------------------------------------------- awards

AWARD_PAGES = 12  # pages of one award list read per check, at most


@dataclass
class FoundAward:
    """A solicitation its agency has awarded: who won it, and for how much."""

    # Stays the same when the agency edits the row, so it is updated.
    uid: str
    title: str
    url: str
    number: str | None = None
    kind: str | None = None
    agency: str | None = None
    # The company (or companies) that won, as the agency writes it.
    awarded_to: str | None = None
    # The amount awarded in cents (the total, when several are given), and the
    # amounts as written when there are several ("$69,000.00 and $15,600.00").
    amount_cents: int | None = None
    amounts: str | None = None
    # The bid abstract (every bidder's price) or the contract, when linked.
    document_url: str | None = None
    posted_on: date | None = None
    # When the bids were due. The lists don't give the day of the award itself,
    # which comes some weeks later.
    closed_on: date | None = None

    @property
    def number_key(self) -> str | None:
        return number_key(self.number, self.agency)


_MONEY_RE = re.compile(r"\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)")
_AWARDED_TO_RE = re.compile(r"\bawarded\s+to\b\s*:?\s*", re.I)
# What a winner's cell says when there is no winner (yet).
_NO_WINNER_RE = re.compile(
    r"^\W*$|^\W*(n/?a|none|tbd|tba|awarded|see\b.*)\W*$|"
    r"evaluat|pending|protest|review|negotiat|cancel|closed|\bopen\b|reject|no award|"
    r"not awarded|re-?bid|postpone|\bstay\b|withdraw|suspend|terminat|in progress|"
    r"intent to award",
    re.I,
)
_AWARD_DOCUMENT_RES = [
    re.compile(r"abstract|tabulation|bid results?", re.I),
    re.compile(r"\baward", re.I),
    re.compile(r"\bcontract\b", re.I),
]
# Amounts above this are a mistake in the list (or not an amount at all).
_MAX_AMOUNT_CENTS = 10_000_000_000 * 100


def split_award(text: str | None) -> tuple[str | None, int | None, str | None]:
    """(winner, total in cents, the amounts as written when several) from an
    award cell: "$39,600.00 Awarded to: Pacific Revolution Corp.", "HEMA
    Corporation dba Deesoniis. - $8,990.00", "N/A Awarded to: N/A" or just a
    company's name."""
    text = clean_text(text)
    if not text:
        return None, None, None
    found = _AWARDED_TO_RE.search(text)
    if found:
        money, winner = text[: found.start()], text[found.end() :]
    else:
        money, winner = text, _MONEY_RE.sub(" ", text)
    winner = re.sub(r"(?:[\s\-–—:,;/|]|\band\b)+$", "", re.sub(r"\s+", " ", winner)).strip()
    winner = re.sub(r"^(?:[\s\-–—:,;/|]|\band\b)+", "", winner).strip()
    if _NO_WINNER_RE.search(winner):
        winner = ""
    amounts = _MONEY_RE.findall(money)
    cents = sum(round(float(a.replace(",", "")) * 100) for a in amounts) if amounts else None
    if cents is not None and not 0 < cents < _MAX_AMOUNT_CENTS:
        cents = None
    written = " and ".join(f"${a}" for a in amounts) if len(amounts) > 1 else None
    return truncate(winner, 300) or None, cents, written


def _award_column(header: list[str]) -> int | None:
    """The column naming the winner or the amount: "Awarded Amount",
    "Status/Awarded"."""
    found = [i for i, name in enumerate(header) if re.search(r"award", name, re.I)]
    return found[-1] if found else None


def _award_document(cells: list[Tag], base: str) -> str | None:
    links = [
        (clean_text(a.get_text(" ")), _link(base, a["href"]))
        for c in cells
        for a in c.find_all("a", href=True)
        if not a["href"].startswith(("mailto:", "/cdn-cgi/", "#", "javascript:"))
    ]
    for pattern in _AWARD_DOCUMENT_RES:
        for text, href in links:
            if pattern.search(text) and _FILE_LINK_RE.search(href.split("?")[0]):
                return href
    return None


def parse_awards(result: FetchResult, agency: str | None = None) -> list[FoundAward]:
    """Awarded solicitations from a page with a table of bids that names the
    winner or the amount: the OpenGov Guam portal's "Awarded" list, the Port's
    list (all states), the Memorial Hospital's table."""
    header, rows = _table_rows(result, agency)
    column = _award_column(header)
    if column is None:
        raise BidError(f"no awarded column in the table on {result.url}; the page may have changed")
    awards = []
    for cells, bid in rows:
        text = cells[column].get_text(" ") if column < len(cells) else ""
        winner, cents, written = split_award(text)
        said_awarded = bool(bid.status and re.match(r"\W*awarded\b", bid.status, re.I))
        if not (winner or cents or said_awarded):
            continue
        if bid.status and not said_awarded and _NO_WINNER_RE.search(bid.status):
            # "Cancelled", "Closed": a name in another column isn't a winner.
            if not winner or clean_text(bid.status) != winner:
                continue
        awards.append(
            FoundAward(
                uid=f"award:{bid.uid.removeprefix('table:')}"[:500],
                title=bid.title,
                url=bid.url,
                number=bid.number,
                kind=bid.kind,
                agency=bid.agency,
                awarded_to=winner,
                amount_cents=cents,
                amounts=written,
                document_url=_award_document(cells, result.url),
                posted_on=bid.posted_on,
                closed_on=bid.due_at.astimezone(GUAM_TZ).date() if bid.due_at else None,
            )
        )
    return awards


def _next_award_page(result: FetchResult) -> str | None:
    """The next page of a list split into pages: a "next" link, or a Drupal
    pager's (the Port's site)."""
    found = _next_page(result)
    if found:
        return found
    soup = BeautifulSoup(result.content, "lxml")
    link = soup.select_one('li.pager-next a[href], a[title="Go to next page"]')
    return _link(result.url, link["href"]) if link else None


def read_awards(
    client: HttpClient, url: str, agency: str | None = None, max_pages: int = AWARD_PAGES
) -> list[FoundAward]:
    """Awarded solicitations from an award list and its next pages. A later
    page that fails keeps the pages read before it."""
    awards: list[FoundAward] = []
    seen_pages: set[str] = set()
    while url and url not in seen_pages and len(seen_pages) < max_pages:
        seen_pages.add(url)
        try:
            result = client.get(url)
            awards += parse_awards(result, agency)
        except (FetchError, BidError):
            if len(seen_pages) == 1:
                raise
            break
        url = _next_award_page(result)
    kept: dict[str, FoundAward] = {}
    for award in awards:
        kept.setdefault(award.uid, award)
    return list(kept.values())
