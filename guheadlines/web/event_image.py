"""The picture of an event for search engines (/events/<id>.png): a card that
shows the event's own details, its name, when and where it is and who holds
it, since the calendars give no photos.

Google asks for an image with each event in its event results, at least 720
pixels wide and best 1920, in the shape of a wide screen (16:9). The card is
drawn in the site's light look with the event calendar's colour down its
left side, in Lora, the free font in the fonts folder (its licence is
fonts/OFL.txt)."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).parent / "fonts"
WIDTH, HEIGHT = 1920, 1080

# The light look's colours (style.css), and each calendar colour's light shade.
PAPER = "#eeebe3"
TEXT = "#1c1b19"
TEXT_SOFT = "#3b3934"
MUTED = "#5f5a51"
COLORS = {
    "green": "#2f7a45",
    "red": "#b03a2e",
    "blue": "#2f62a0",
    "purple": "#6b4a9a",
    "gold": "#9a6f12",
}

LEFT, RIGHT = 150, 140  # margins; the colour band is inside the left one
BAND = 30
LINE = 1.22  # line height, times the font size
TITLE_SIZES = (110, 96, 84)  # the largest at which the title fits


@lru_cache(maxsize=16)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Lora-{weight}.ttf"), size)


def _cut(line: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    """`line` shortened to end with "…" within `width`."""
    while line and font.getlength(f"{line}…") > width:
        line = line[:-1]
    return f"{line.rstrip(' ,.;:–-')}…"


def _lines(text: str, font: ImageFont.FreeTypeFont, width: int, most: int) -> list[str]:
    """`text` cut into lines no wider than `width`, at most `most` of them.
    A line ends with "…" where the text does not fit: the last line when
    there is more text, or a single word longer than a line."""
    lines: list[str] = []
    words = text.split()
    while words and len(lines) < most:
        line = words.pop(0)
        while words and font.getlength(f"{line} {words[0]}") <= width:
            line = f"{line} {words.pop(0)}"
        lines.append(_cut(line, font, width) if font.getlength(line) > width else line)
    if words:
        lines[-1] = _cut(lines[-1], font, width)
    return lines


def _title(text: str, width: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """The largest title size at which the whole title fits on 3 lines (the
    smallest size, cut short with "…", for a very long title)."""
    for size in TITLE_SIZES:
        font = _font("Bold", size)
        lines = _lines(text, font, width, 3)
        if not any(line.endswith("…") for line in lines):
            break
    return font, lines


@lru_cache(maxsize=256)
def event_card(
    title: str,
    when: str,
    place: str | None,
    held_by: str | None,
    color: str | None,
    site: str,
) -> bytes:
    """The card of one event, as a PNG file. `color` is the calendar's colour
    name (style.css --cal-*), or None for the plain one."""
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, BAND - 1, HEIGHT), fill=COLORS.get(color or "", MUTED))
    width = WIDTH - LEFT - RIGHT

    draw.text((LEFT, 96), f"{site} · Events", font=_font("Bold", 46), fill=MUTED)

    y = 240
    font, lines = _title(title, width)
    for line in lines:
        draw.text((LEFT, y), line, font=font, fill=TEXT)
        y += round(font.size * LINE)

    y += 40
    font = _font("Regular", 64)
    for line in _lines(when, font, width, 1):
        draw.text((LEFT, y), line, font=font, fill=TEXT_SOFT)
        y += round(font.size * LINE)
    if place:
        font = _font("Regular", 56)
        for line in _lines(place, font, width, 2):
            draw.text((LEFT, y), line, font=font, fill=TEXT_SOFT)
            y += round(font.size * LINE)

    if held_by:
        font = _font("Regular", 46)
        for line in _lines(f"Held by {held_by}", font, width, 1):
            draw.text((LEFT, HEIGHT - 96 - font.size), line, font=font, fill=MUTED)

    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()
