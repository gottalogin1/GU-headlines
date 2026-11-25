"""Download an article's main image and keep a resized local copy.

Keeping our own copy means old articles keep their pictures even after the
news site reorganizes or deletes its files. Each photo is kept twice: at
IMAGE_MAX_WIDTH, for phones, and SMALL_WIDTH wide, for the photo beside the
story on larger screens (a quarter to a third of the bytes).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from .http import FetchError, HttpClient

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MIN_WIDTH = 200
# What browsers ask for when loading a picture. Some image hosts answer a
# request for a web page with their photo viewer instead.
IMAGE_ACCEPT = "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5"
MIN_HEIGHT = 120
# The width of the small copy. Larger screens draw the photo 228 pixels wide
# (style.css, .story); 360 stays sharp there, also on screens set to 150%.
SMALL_WIDTH = 360
Image.MAX_IMAGE_PIXELS = 60_000_000
# The formats news sites use for photos. Pillow can also read many rare formats
# (TIFF, BMP, EPS, PSD...), where most of its security flaws were found. A file
# in one of those formats is refused, so its decoder never runs.
WEB_IMAGE_FORMATS = ("JPEG", "PNG", "GIF", "WEBP", "AVIF")


@dataclass
class StoredImage:
    path: str  # relative to the media directory, e.g. images/2026/09/ab12....webp
    width: int
    height: int


class ImageRejected(Exception):
    pass


def image_filename(image_url: str, when: datetime) -> str:
    digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:24]
    return f"images/{when:%Y}/{when:%m}/{digest}.webp"


def small_image_path(path: str) -> str:
    """images/2026/09/ab12.webp -> images/2026/09/ab12-sm.webp"""
    return path.removesuffix(".webp") + "-sm.webp"


def _webp(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "WEBP", quality=80, method=4)
    return out.getvalue()


def _write(target: Path, data: bytes) -> None:
    """Write the whole file or nothing: the website may be reading it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


def process_image(data: bytes, max_width: int) -> tuple[bytes, int, int]:
    """Validate and convert image bytes to a resized WebP. Raises ImageRejected."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(io.BytesIO(data), formats=WEB_IMAGE_FORMATS)
            image.load()
        except Exception as exc:
            raise ImageRejected(f"unreadable image: {exc}") from exc
    image = ImageOps.exif_transpose(image)
    if image.width < MIN_WIDTH or image.height < MIN_HEIGHT:
        raise ImageRejected(f"too small ({image.width}x{image.height})")
    ratio = image.width / image.height
    if ratio > 5 or ratio < 0.2:
        raise ImageRejected(f"odd aspect ratio ({image.width}x{image.height}), likely a banner")
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize((max_width, height), Image.Resampling.LANCZOS)
    return _webp(image), image.width, image.height


def make_small_copy(media_dir: Path, path: str) -> bool:
    """Save the SMALL_WIDTH copy of a stored photo (path as in StoredImage),
    made from the stored photo itself. False when the photo is not wider than
    that (pages then use the photo itself) or can't be read."""
    small = media_dir / small_image_path(path)
    if small.exists():
        return True
    try:
        # Our own file, so only WebP is opened.
        with Image.open(media_dir / path, formats=("WEBP",)) as image:
            if image.width <= SMALL_WIDTH:
                return False
            height = round(image.height * SMALL_WIDTH / image.width)
            data = _webp(
                image.convert("RGB").resize((SMALL_WIDTH, height), Image.Resampling.LANCZOS)
            )
    except Exception as exc:
        log.debug("no small copy of %s: %s", path, exc)
        return False
    _write(small, data)
    return True


def store_image(
    client: HttpClient,
    image_url: str,
    *,
    referer: str,
    media_dir: Path,
    when: datetime,
    max_width: int,
    check_robots: bool = True,
) -> StoredImage | None:
    relative = image_filename(image_url, when)
    target = media_dir / relative
    if target.exists():
        try:
            with Image.open(target) as existing:
                stored = StoredImage(relative, existing.width, existing.height)
        except Exception:
            target.unlink(missing_ok=True)
        else:
            make_small_copy(media_dir, relative)
            return stored
    try:
        result = client.get(
            image_url,
            referer=referer,
            accept=IMAGE_ACCEPT,
            max_bytes=MAX_IMAGE_BYTES,
            check_robots=check_robots,
        )
        content_type = result.content_type
        if content_type and not content_type.startswith(
            ("image/", "application/octet-stream", "binary/")
        ):
            raise ImageRejected(f"not an image ({content_type})")
        data, width, height = process_image(result.content, max_width)
    except (FetchError, ImageRejected) as exc:
        log.info("image skipped %s: %s", image_url, exc)
        return None
    _write(target, data)
    make_small_copy(media_dir, relative)
    return StoredImage(relative, width, height)
