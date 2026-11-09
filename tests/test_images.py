import io

import pytest
from conftest import make_jpeg
from PIL import Image

from guheadlines.scraper.images import ImageRejected, process_image


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
