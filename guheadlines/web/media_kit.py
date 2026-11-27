"""The media kit (/advertise/media-kit.pdf): one page, as a PDF, with the
site's real numbers from the visitor counts and the prices of what a
business can buy. The site's owner sends it to local businesses, and
businesses can download it from the Advertise page. web/app.py collects the
numbers (Kit) and makes the file again at most every ten minutes.

The page uses the fonts that every PDF reader has (Helvetica and Times), so
the file holds no font and stays small. These fonts have the letters of
Windows-1252 only: other letters show as "?"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from fpdf import FPDF

# Letter paper, in points (72 to an inch): the paper size of Guam and the US.
WIDTH, HEIGHT = 612, 792
MARGIN = 44
INNER = WIDTH - 2 * MARGIN

# The site's light colors (static/style.css).
INK = (28, 27, 25)
SOFT = (59, 57, 52)
MUTED = (95, 90, 81)
LINE = (216, 211, 199)
WELL = (235, 231, 222)
ACCENT = (47, 82, 119)
WHITE = (255, 255, 255)

# The chart's height and the rows in the two small tables. With many prices
# for sale, the page takes the next, smaller layout, so that everything fits
# on one page above the booking box.
LAYOUTS = ((72, 6), (56, 6), (48, 5), (40, 4), (32, 3))
# The bottom of the prices: the booking box and the footnote are below it.
PRICES_END = HEIGHT - 132
# Where the price and the words of each price start.
PRICE_X, WHAT_X = 124, 226


@dataclass(frozen=True)
class Kit:
    site_name: str
    tagline: str
    address: str  # "guamheadlines.com"
    made: date
    days: int
    daily: list[int]  # page views on each day of the period, oldest first
    views: int
    per_day: int
    per_week: int
    sponsor_clicks: int
    newsletter_clicks: int
    stories: int  # new stories in the period
    sources: int  # news sites that the site collects
    sections: list[tuple[str, int]]  # (name, page views), the most read first
    referrers: list[tuple[str, int]]  # (website, visits), the most first; "" = no website
    prices: list[tuple[str, str, str]]  # (name, price, what the business gets)


def _day(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


class _Page(FPDF):
    def text_in(self, text: str) -> str:
        """`text` with each letter that the PDF fonts do not have as "?"."""
        return text.encode("cp1252", "replace").decode("cp1252")

    def write_at(self, x: float, y: float, text: str, size: float, style: str = "", color=INK):
        """Write `text` with its top at (x, y)."""
        self.set_font("Times" if style == "serif" else "Helvetica", "B" if style else "", size)
        self.set_text_color(*color)
        self.set_xy(x, y)
        self.cell(text=self.text_in(text))

    def write_right(self, right: float, y: float, text: str, size: float, style="", color=INK):
        """Write `text` with its right end at `right`."""
        self.set_font("Helvetica", style, size)
        self.write_at(
            right - self.get_string_width(self.text_in(text)), y, text, size, style, color
        )

    def heading(self, y: float, text: str, note: str = "") -> float:
        self.write_at(MARGIN, y, text, 12.5, "B")
        if note:
            self.set_font("Helvetica", "B", 12.5)
            left = MARGIN + self.get_string_width(self.text_in(text)) + 6
            self.write_at(left, y + 2, note, 9.5, color=MUTED)
        return y + 20

    def table(self, x: float, y: float, width: float, title: str, rows: list[tuple[str, str]]):
        """A small table: a title, then one name and one number a line."""
        self.write_at(x, y, title, 10.5, "B")
        y += 18
        for name, value in rows:
            self.set_draw_color(*LINE)
            self.line(x, y - 3, x + width, y - 3)
            self.write_at(x, y, name, 9.5, color=SOFT)
            self.write_right(x + width, y, value, 9.5, "B")
            y += 16
        return y


def pdf(kit: Kit) -> bytes:
    """The media kit, one Letter page, as the bytes of a PDF file."""
    page, y = _fit(kit)
    _bottom(page, kit, y)
    return bytes(page.output())


def _fit(kit: Kit) -> tuple[_Page, float]:
    """The top of the page in the first layout that leaves room for the
    booking box (or in the smallest one), and where it ends."""
    for chart, rows in LAYOUTS:
        page, y = _top(kit, chart, rows)
        if y <= PRICES_END:
            break
    return page, y


def _top(kit: Kit, chart: int, rows: int) -> tuple[_Page, float]:
    """Everything above the booking box, with a chart `chart` points high and
    `rows` rows in each small table. Returns the page and where it ends."""
    page = _Page(unit="pt", format="letter")
    page.core_fonts_encoding = "windows-1252"
    page.set_auto_page_break(False)
    page.set_margins(MARGIN, MARGIN)
    page.set_title(f"{kit.site_name}: media kit")
    page.set_author(kit.site_name)
    page.set_lang("en")
    page.add_page()

    # The name of the site, and what this is.
    page.set_fill_color(*ACCENT)
    page.rect(MARGIN, 40, 36, 36, style="F", round_corners=True, corner_radius=6)
    page.set_font("Helvetica", "B", 14)
    page.write_at(MARGIN + 18 - page.get_string_width("GU") / 2, 51, "GU", 14, "B", WHITE)
    page.write_at(MARGIN + 48, 38, kit.site_name, 24, "serif")
    page.write_at(MARGIN + 50, 66, kit.tagline, 10, color=MUTED)
    page.write_right(WIDTH - MARGIN, 40, "MEDIA KIT", 9, "B", ACCENT)
    page.write_right(WIDTH - MARGIN, 54, f"{kit.made:%B %Y}", 10, color=SOFT)
    page.write_right(WIDTH - MARGIN, 68, kit.address, 10, "B", ACCENT)
    page.set_draw_color(*LINE)
    page.line(MARGIN, 90, WIDTH - MARGIN, 90)

    page.set_xy(MARGIN, 100)
    page.set_font("Helvetica", "", 10.5)
    page.set_text_color(*SOFT)
    page.multi_cell(
        INNER,
        14,
        page.text_in(
            f"{kit.site_name} brings Guam's local news together in one place: the headlines of"
            f" {kit.sources} Guam news sources, checked every hour, with jobs, events, government"
            f" bids and more. {kit.stories:,} new stories in the last {kit.days} days."
        ),
        align="L",
    )

    # The numbers.
    first = kit.made - timedelta(days=kit.days - 1)
    y = page.heading(
        144, f"Readers in the last {kit.days} days", f"{_day(first)} to {_day(kit.made)}"
    )
    tiles = [
        ("Page views", kit.views),
        ("A day, on average", kit.per_day),
        ("A week, on average", kit.per_week),
    ]
    if kit.newsletter_clicks:
        tiles.append(("Newsletter story clicks", kit.newsletter_clicks))
    if kit.sponsor_clicks:
        tiles.append(("Sponsor spot clicks", kit.sponsor_clicks))
    gap = 8
    tile = (INNER - gap * (len(tiles) - 1)) / len(tiles)
    for n, (label, value) in enumerate(tiles):
        x = MARGIN + n * (tile + gap)
        page.set_fill_color(*WELL)
        page.rect(x, y, tile, 54, style="F", round_corners=True, corner_radius=6)
        page.write_at(x + 10, y + 8, f"{value:,}", 20, "serif")
        page.write_at(x + 10, y + 36, label, 8.5, color=MUTED)
    y += 70

    peak = max(kit.daily, default=0)
    if peak:
        page.write_at(MARGIN, y, "Page views per day", 9, "B", SOFT)
        page.write_right(WIDTH - MARGIN, y, f"Highest: {peak:,}", 9, color=MUTED)
        top, height = y + 16, chart
        slot = INNER / len(kit.daily)
        page.set_fill_color(*ACCENT)
        for n, views in enumerate(kit.daily):
            bar = height * views / peak
            if bar:
                page.rect(MARGIN + n * slot + 1, top + height - bar, slot - 2, bar, style="F")
        page.set_draw_color(*LINE)
        page.line(MARGIN, top + height, WIDTH - MARGIN, top + height)
        page.write_at(MARGIN, top + height + 4, _day(first), 8, color=MUTED)
        page.write_right(WIDTH - MARGIN, top + height + 4, _day(kit.made), 8, color=MUTED)
        y = top + height + 26

    # Where the readers go, and where they come from.
    half = (INNER - 28) / 2
    sections = [(name, f"{views:,}") for name, views in kit.sections if views][:rows]
    visits = sum(n for _, n in kit.referrers)
    came = [
        (site or "Typed in, bookmarks and apps", f"{round(n * 100 / visits)}%")
        for site, n in kit.referrers[:rows]
        if visits
    ]
    bottom = y
    if sections:
        bottom = page.table(MARGIN, y, half, "Most-read sections (page views)", sections)
    if came:
        right = MARGIN + half + 28
        bottom = max(bottom, page.table(right, y, half, "Where readers come from", came))
    y = bottom + 10

    # What a business can buy.
    if kit.prices:
        y = page.heading(y, "Ways to reach these readers")
        for name, price, what in kit.prices:
            page.set_draw_color(*LINE)
            page.line(MARGIN, y - 4, WIDTH - MARGIN, y - 4)
            page.write_at(MARGIN, y, name, 10, "B")
            page.write_at(MARGIN + PRICE_X, y, price, 10, "B", ACCENT)
            page.set_xy(MARGIN + WHAT_X, y)
            page.set_font("Helvetica", "", 9)
            page.set_text_color(*SOFT)
            page.multi_cell(INNER - WHAT_X, 11.5, page.text_in(what), align="L")
            y = max(y + 18, page.get_y() + 4)
    return page, y


def _bottom(page: _Page, kit: Kit, y: float) -> None:
    """How to book, and where the numbers come from, at the bottom of the page."""
    y = max(y + 4, HEIGHT - 128)
    page.set_fill_color(*WELL)
    page.rect(MARGIN, y, INNER, 52, style="F", round_corners=True, corner_radius=8)
    page.write_at(MARGIN + 14, y + 10, f"Book and pay online: {kit.address}/advertise", 11.5, "B")
    page.write_at(
        MARGIN + 14,
        y + 30,
        "Pay with PayPal or a card. Each ad is checked before it goes up; if it isn't accepted,"
        " you get all of your money back.",
        8.5,
        color=SOFT,
    )
    page.set_xy(MARGIN, HEIGHT - 60)
    page.set_font("Helvetica", "", 7.5)
    page.set_text_color(*MUTED)
    page.multi_cell(
        INNER,
        10,
        page.text_in(
            "The numbers are counted by the site's own server from real visits, as totals for"
            " each Guam day, with no cookies. Search engines, link previews and other robots are"
            f" left out. Made on {_day(kit.made)}."
        ),
        align="L",
    )
