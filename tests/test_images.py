import io

import pytest
from conftest import make_jpeg
from PIL import Image

from guheadlines.scraper.images import (
    ImageRejected,
    make_small_copy,
    process_image,
    small_image_path,
)


def test_process_image_resizes_to_webp():
    data, width, height = process_image(make_jpeg(1200, 800), 720)
    assert (width, height) == (720, 480)
    assert Image.open(io.BytesIO(data)).format == "WEBP"


def test_process_image_rejects_icons_and_banners():
    with pytest.raises(ImageRejected):
        process_image(make_jpeg(64, 64), 720)
    with pytest.raises(ImageRejected):
        process_image(make_jpeg(1600, 200), 720)
    with pytest.raises(ImageRejected):
        process_image(b"not an image", 720)


@pytest.mark.parametrize("fmt", ["BMP", "TIFF"])
def test_process_image_opens_only_web_formats(fmt):
    # A good photo in a format news sites do not use: its decoder is never run.
    out = io.BytesIO()
    Image.open(io.BytesIO(make_jpeg(1200, 800))).save(out, fmt)
    with pytest.raises(ImageRejected, match="unreadable image"):
        process_image(out.getvalue(), 720)


def _save(media, path: str, width: int, height: int) -> None:
    data, _w, _h = process_image(make_jpeg(width, height), 720)
    (media / path).parent.mkdir(parents=True, exist_ok=True)
    (media / path).write_bytes(data)


def test_small_copy_for_larger_screens(tmp_path):
    assert small_image_path("images/2026/09/ab12.webp") == "images/2026/09/ab12-sm.webp"
    _save(tmp_path, "images/2026/09/wide.webp", 1200, 800)
    assert make_small_copy(tmp_path, "images/2026/09/wide.webp")
    small = Image.open(tmp_path / "images/2026/09/wide-sm.webp")
    assert (small.format, small.size) == ("WEBP", (360, 240))
    assert not list(tmp_path.rglob("*.tmp"))


def test_no_small_copy_of_a_narrow_or_broken_photo(tmp_path):
    # Pages then use the photo itself.
    _save(tmp_path, "images/narrow.webp", 300, 200)
    assert not make_small_copy(tmp_path, "images/narrow.webp")
    (tmp_path / "images/broken.webp").write_bytes(b"RIFF0000WEBP")
    assert not make_small_copy(tmp_path, "images/broken.webp")
    assert not make_small_copy(tmp_path, "images/missing.webp")
    assert sorted(p.name for p in (tmp_path / "images").iterdir()) == [
        "broken.webp",
        "narrow.webp",
    ]
