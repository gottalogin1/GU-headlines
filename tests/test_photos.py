"""The worker's photo tidy-up (photos.py): small copies, unused files and the
IMAGE_STORAGE_GB limit."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from conftest import make_jpeg
from sqlalchemy import select

from guheadlines.db import session_scope
from guheadlines.models import Article, Source
from guheadlines.photos import tidy_photos
from guheadlines.scraper.images import process_image, small_image_path
from guheadlines.settings import get_settings

PHOTO, _W, _H = process_image(make_jpeg(1200, 800), 720)


def _save(media, path: str) -> None:
    (media / path).parent.mkdir(parents=True, exist_ok=True)
    (media / path).write_bytes(PHOTO)


def _paths() -> dict[str, str | None]:
    with session_scope() as session:
        return dict(session.execute(select(Article.title, Article.image_path)).all())


def test_tidy_photos(clean_db, tmp_path):
    settings = replace(get_settings(), media_dir=tmp_path, image_storage_gb=0)
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        kuam = Source(slug="kuam", name="KUAM News", homepage="https://www.kuam.com/")
        session.add(kuam)
        session.flush()
        for n in range(4):  # story 0 is the newest
            path = f"images/2026/09/story{n}.webp"
            _save(tmp_path, path)
            session.add(
                Article(
                    source_id=kuam.id,
                    url=f"https://www.kuam.com/story/{n}",
                    title=f"story {n}",
                    image_url=f"https://www.kuam.com/photo{n}.jpg",
                    image_path=path,
                    image_width=720,
                    image_height=480,
                    published_at=now - timedelta(days=n),
                )
            )
    # Files no story uses: a logo found two days ago, and a photo saved just
    # now, whose story the worker is about to save.
    _save(tmp_path, "images/2026/09/logo.webp")
    two_days_ago = time.time() - 2 * 86400
    os.utime(tmp_path / "images/2026/09/logo.webp", (two_days_ago, two_days_ago))
    _save(tmp_path, "images/2026/09/new.webp")

    report = tidy_photos(settings)
    assert (report.small_made, report.unused_deleted, report.stories_pruned) == (4, 1, 0)
    for n in range(4):
        assert (tmp_path / small_image_path(f"images/2026/09/story{n}.webp")).is_file()
    assert not (tmp_path / "images/2026/09/logo.webp").exists()
    assert (tmp_path / "images/2026/09/new.webp").exists()
    assert report.files == 9
    assert not tidy_photos(settings).changed  # nothing left to do

    # Over the limit: the photos of the oldest stories go, until the photos
    # use 90% of it. Here, room for 2.25 stories: the 2 oldest go.
    (tmp_path / "images/2026/09/new.webp").unlink()
    one_story = sum(f.stat().st_size for f in tmp_path.rglob("story0*.webp"))
    report = tidy_photos(replace(settings, image_storage_gb=2.5 * one_story / 1e9))
    assert report.stories_pruned == 2
    assert report.bytes == 2 * one_story and report.files == 4
    assert _paths() == {
        "story 0": "images/2026/09/story0.webp",
        "story 1": "images/2026/09/story1.webp",
        "story 2": None,  # these show the news site's photo now
        "story 3": None,
    }
    assert sorted(f.name for f in tmp_path.rglob("*.webp")) == [
        "story0-sm.webp",
        "story0.webp",
        "story1-sm.webp",
        "story1.webp",
    ]
