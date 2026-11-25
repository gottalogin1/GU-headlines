"""The sponsor spot: one picture ad that the site owner sells directly to a
local business, at the top of the right-hand column of every page.

The picture is a file in config/sponsor/ (SPONSOR_IMAGE), so it is served from
this site: no outside script, cookie or request, and the security rules stay
as they are. The site makes a small WebP copy of it, at most MAX_WIDTH pixels
wide (twice the column's width, for sharp phone screens), so a large file from
the sponsor does not slow the pages down. A new picture shows without a
restart: each page looks at the file's size and time, and makes a new copy
when they change.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from ..scraper.images import WEB_IMAGE_FORMATS
from ..settings import Settings

log = logging.getLogger(__name__)

# The right-hand column is 300 pixels wide, with 18 pixels of room on each side.
MAX_WIDTH = 600
MAX_FILE_BYTES = 15 * 1024 * 1024


@dataclass(frozen=True)
class SponsorPicture:
    data: bytes  # WebP
    width: int
    height: int
    version: str  # part of the picture's address; changes with the picture


def sponsor_dir(settings: Settings) -> Path:
    return settings.config_dir / "sponsor"


def make_copy(data: bytes) -> tuple[bytes, int, int]:
    """A WebP copy at most MAX_WIDTH wide. Clear parts stay clear; an animated
    GIF keeps its first picture. Raises ValueError for a file that is not a
    picture."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(io.BytesIO(data), formats=WEB_IMAGE_FORMATS)
            image.load()
        except Exception as exc:
            raise ValueError(f"not a picture that the site can read: {exc}") from exc
    image = ImageOps.exif_transpose(image)
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    image = image.convert("RGBA" if has_alpha else "RGB")
    if image.width > MAX_WIDTH:
        height = max(1, round(image.height * MAX_WIDTH / image.width))
        image = image.resize((MAX_WIDTH, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "WEBP", quality=85, method=4)
    return out.getvalue(), image.width, image.height


_lock = threading.Lock()
_cached: tuple[tuple, SponsorPicture | None] | None = None


def picture(settings: Settings) -> SponsorPicture | None:
    """The sponsor's picture, or None when there is none (or it can't be read;
    the log then says why)."""
    global _cached
    if not settings.sponsor_image:
        return None
    path = sponsor_dir(settings) / settings.sponsor_image
    try:
        info = path.stat()
        key = (str(path), info.st_mtime_ns, info.st_size)
    except OSError:
        key = (str(path), None, None)
    with _lock:
        if _cached is not None and _cached[0] == key:
            return _cached[1]
        found = None
        if key[1] is None:
            log.warning(
                "SPONSOR_IMAGE: there is no file %s, so the sponsor spot is hidden. "
                "Put the picture in the config/sponsor folder",
                path,
            )
        elif info.st_size > MAX_FILE_BYTES:
            log.warning("SPONSOR_IMAGE: %s is over 15 MB, so the sponsor spot is hidden", path)
        else:
            try:
                data, width, height = make_copy(path.read_bytes())
            except (OSError, ValueError) as exc:
                log.warning("SPONSOR_IMAGE: %s: %s; the sponsor spot is hidden", path, exc)
            else:
                version = hashlib.sha256(data).hexdigest()[:10]
                found = SponsorPicture(data, width, height, version)
        _cached = (key, found)
        return found
