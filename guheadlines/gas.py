"""Guam gas prices, picked up from the local news.

No agency publishes Guam's pump prices in a form a program may read (the
Guam Energy Office stopped its price survey in 2024, and GasBuddy forbids
automated collection), and prices are the same all over the island. The
local news reports them each time they change ("Gas prices up again: $5.94
a gallon of regular"), so the site reads them from the headlines and intros
of the stories it already collects, and links each price to its story.

Only regular, premium and diesel are shown. A price must be next to its
grade's name in the same sentence; sentences about other places (the
mainland, Saipan, Hawaii...) and old prices ("up from $4.88") are skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

GRADES = ("regular", "premium", "diesel")
LOWEST, HIGHEST = 2.0, 15.0  # a price per gallon outside this is not one

# The names Guam's stations and newsrooms use for each grade. "Extra" (one
# company's premium) only counts in a list or before "grade"/"fuel".
_GRADE = (
    r"regular(?:\s+unleaded)?|unleaded|premium|super(?:\s+unleaded)?|supreme|"
    r"extra(?=\s*(?:,|and\b|grade|fuel|gasoline|unleaded))|diesel"
)
_KIND = r"(?:\s+(?:grade|gasoline|gas|fuel|unleaded))*"
_PRICE = r"\$\s?(\d{1,2}\.\d{2,3})(?!\d)"
_ONE_PRICE = r"\$\s?(?P<price>\d{1,2}\.\d{2,3})(?!\d)"
_LIST_RE = re.compile(
    rf"\b(?P<grades>(?:{_GRADE}){_KIND}(?:\s*,\s*(?:and\s+)?|\s+and\s+)(?:{_GRADE}){_KIND}"
    rf"(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+)(?:{_GRADE}){_KIND})?)\b"
    rf"[^$]{{0,80}}?(?P<prices>{_PRICE}(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+){_PRICE}){{1,2}})",
    re.IGNORECASE,
)
# "$5.49 for a gallon of regular", "$5.69 per gallon of regular grade",
# "$5.67 for regular".
_PRICE_FIRST_RE = re.compile(
    rf"{_ONE_PRICE}\s*(?:(?:for\s+)?(?:a|per|each)\s+gallon\s+(?:of|for)\s+|for\s+)(?:a\s+gallon\s+of\s+)?"
    rf"(?P<grade>{_GRADE})\b",
    re.IGNORECASE,
)
# "regular grade fuel rose to $5.07", "Diesel is $6.49".
_GRADE_FIRST_RE = re.compile(
    rf"\b(?P<grade>{_GRADE}){_KIND}\b[^$.;:]{{0,45}}?{_ONE_PRICE}",
    re.IGNORECASE,
)
# Old prices and comparisons ("up from $4.88 last week", "from last month"),
# taken out before reading a sentence.
_AGO = r"(?:last\s+(?:week|month|year)|a\s+(?:week|month|year)\s+ago|yesterday)"
_OLD_RE = re.compile(
    rf"\b(?:(?:up|down)\s+)?from\s+\$\s?\d+(?:\.\d+)?(?:\s+{_AGO})?|"
    rf"\b(?:compared\s+(?:with|to)|versus|vs\.?|which\s+was|was|were|had\s+been)"
    rf"\s+\$\s?\d+(?:\.\d+)?(?:\s+{_AGO})?|"
    rf"\b(?:from|since|than)\s+{_AGO}",
    re.IGNORECASE,
)
# Sentences about other places or other times are skipped.
_ELSEWHERE_RE = re.compile(
    r"\b(?:mainland|national(?:ly)?|nationwide|u\.\s?s\.\s+average|stateside|hawai.?i|"
    r"saipan|cnmi|tinian|rota|northern\s+marianas|philippines|manila|japan|korea|"
    r"last\s+(?:week|month|year)|a\s+year\s+ago|in\s+20[0-2]\d|per\s+barrel|a\s+barrel|barrel)\b",
    re.IGNORECASE,
)
_GUAM_RE = re.compile(r"\bguam\b|\bguåhan\b", re.IGNORECASE)
# Sentences, and the parts of one that compare ("..., versus $4.59 on the
# mainland").
_SENTENCE_RE = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z\"“(])|\s+[-–—|]\s+|\n+|;\s*|"
    r",\s*(?=(?:versus|vs\.?|compared|while|whereas|but)\b)"
)


def grade_of(name: str) -> str:
    name = name.lower()
    if name.startswith("diesel"):
        return "diesel"
    if name.startswith(("regular", "unleaded")):
        return "regular"
    return "premium"


def _price(text: str) -> float | None:
    value = float(text)
    return value if LOWEST <= value <= HIGHEST else None


def read_prices(text: str) -> dict[str, float]:
    """{grade: price} from a headline or intro; grades it doesn't give are
    left out. The first price given for a grade wins."""
    found: dict[str, float] = {}

    def keep(grade: str, price: float | None) -> None:
        if price is not None:
            found.setdefault(grade, price)

    for sentence in _SENTENCE_RE.split(text or ""):
        sentence = _OLD_RE.sub(" ", sentence)
        if _ELSEWHERE_RE.search(sentence):
            continue
        # Lists first ("regular, extra and diesel ... $5.07, $5.46 and
        # $5.74"), then "$5.49 a gallon of regular", then "regular ... $5.49";
        # each part of the sentence is read once.
        for pattern in (_LIST_RE, _PRICE_FIRST_RE, _GRADE_FIRST_RE):
            for match in pattern.finditer(sentence):
                if pattern is _LIST_RE:
                    names = re.findall(_GRADE, match.group("grades"), re.IGNORECASE)
                    prices = re.findall(_PRICE, match.group("prices"))
                    if len(names) != len(prices):
                        continue
                    for name, price in zip(names, prices, strict=True):
                        keep(grade_of(name), _price(price))
                else:
                    keep(grade_of(match.group("grade")), _price(match.group("price")))
            sentence = pattern.sub(" ", sentence)
    return found


@dataclass
class Story:
    """A news story that may give gas prices (from queries.gas_price_stories)."""

    id: int
    url: str
    title: str
    intro: str | None
    published_at: datetime
    source: str


@dataclass
class Price:
    grade: str
    price: float
    story: Story

    @property
    def text(self) -> str:
        """$5.39, or $6.079 when the story gives tenths of a cent."""
        return f"${self.price:.2f}" if round(self.price, 2) == self.price else f"${self.price:.3f}"


def is_about_guam(story: Story) -> bool:
    """Leaves out a story about Saipan's (or anywhere else's) prices that
    doesn't name Guam."""
    text = f"{story.title} {story.intro or ''}"
    return bool(_GUAM_RE.search(text)) or not _ELSEWHERE_RE.search(text)


def latest_prices(stories: list[Story]) -> list[Price]:
    """The newest price of each grade, regular first, each with the story it
    came from. `stories` are newest first."""
    newest: dict[str, Price] = {}
    for story in stories:
        if not is_about_guam(story):
            continue
        prices = read_prices(story.title)
        for grade, price in read_prices(story.intro or "").items():
            prices.setdefault(grade, price)
        for grade, price in prices.items():
            newest.setdefault(grade, Price(grade, price, story))
        if len(newest) == len(GRADES):
            break
    return [newest[g] for g in GRADES if g in newest]
