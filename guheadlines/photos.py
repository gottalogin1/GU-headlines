"""Keeps the saved photos tidy. The worker runs this after each regular news
check, and `guheadlines photos` runs it by hand. It:

- makes the small copy (scraper/images.py) of photos saved before small
  copies existed, newest stories first;
- deletes copies that no story uses any more (a photo found to be a news
  site's logo, the second copy of a story stored twice), once they are a
  day old;
- keeps the photos within IMAGE_STORAGE_GB: above it, the photos of the
  oldest stories are deleted until the photos use 90% of it. Those stories
  then show the news site's own photo, for as long as the news site has it.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update

from .db import session_scope
from .models import Article
from .scraper.images import SMALL_WIDTH, make_small_copy, small_image_path
from .settings import Settings

log = logging.getLogger(__name__)

GB = 1_000_000_000
# A copy that no story uses is deleted only once it is this old (in seconds):
# the worker saves a story's photo a moment before it saves the story.
UNUSED_AGE = 24 * 3600
# Once over the limit, photos are deleted until they use this share of it, so
# that the next few new photos don't start the deleting again.
PRUNE_TO = 0.9
BATCH = 500


@dataclass
class PhotoReport:
    files: int = 0
    bytes: int = 0
    small_made: int = 0
    unused_deleted: int = 0
    stories_pruned: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.small_made or self.unused_deleted or self.stories_pruned)

    @property
    def size(self) -> str:
        """The space used, as "820 MB" or "1.25 GB"."""
        if self.bytes < GB:
            return f"{self.bytes / 1_000_000:.0f} MB"
        return f"{self.bytes / GB:.2f} GB"

    def summary(self, limit_gb: float) -> str:
        limit = f"{limit_gb:g} GB allowed" if limit_gb else "no limit"
        return (
            f"{self.files:,} files, {self.size} ({limit}); "
            f"{self.small_made:,} small copies made, {self.unused_deleted:,} unused files "
            f"deleted, photos of {self.stories_pruned:,} oldest stories deleted"
        )


def tidy_photos(settings: Settings, *, max_small: int | None = 3000) -> PhotoReport:
    """See the module note. max_small: the most small copies to make in one
    go (each takes a few milliseconds); None: all of them."""
    media = settings.media_dir
    report = PhotoReport(small_made=_make_small_copies(media, max_small))

    # The files are listed before the stories are read, so a story saved in
    # between counts as using its photo.
    files = _list_files(media)
    with session_scope() as session:
        used = set(
            session.scalars(select(Article.image_path).where(Article.image_path.isnot(None)))
        )
    used |= {small_image_path(path) for path in used}
    old = time.time() - UNUSED_AGE
    for path, (_size, mtime) in list(files.items()):
        if path not in used and mtime < old:
            (media / path).unlink(missing_ok=True)
            del files[path]
            report.unused_deleted += 1

    total = sum(size for size, _mtime in files.values())
    limit = settings.image_storage_gb * GB
    if limit and total > limit:
        total, report.stories_pruned = _delete_oldest(media, files, total, limit * PRUNE_TO)
        log.info(
            "saved photos were over IMAGE_STORAGE_GB (%g GB): deleted the photos of the "
            "%d oldest stories",
            settings.image_storage_gb,
            report.stories_pruned,
        )
    report.files, report.bytes = len(files), total
    return report


def _make_small_copies(media: Path, most: int | None) -> int:
    with session_scope() as session:
        paths = session.scalars(
            select(Article.image_path)
            .where(Article.image_path.isnot(None), Article.image_width > SMALL_WIDTH)
            .order_by(Article.published_at.desc())
        ).all()
    made = 0
    for path in dict.fromkeys(paths):  # two stories can share one photo
        if most is not None and made >= most:
            break
        if (media / small_image_path(path)).exists() or not (media / path).exists():
            continue
        if make_small_copy(media, path):
            made += 1
    return made


def _list_files(media: Path) -> dict[str, tuple[int, float]]:
    """{"images/2026/09/ab12.webp": (size in bytes, last change)} of every
    file in the photo folder."""
    found: dict[str, tuple[int, float]] = {}
    for folder, _dirs, names in os.walk(media / "images"):
        for name in names:
            full = os.path.join(folder, name)
            try:
                info = os.stat(full)
            except FileNotFoundError:
                continue
            path = os.path.relpath(full, media).replace(os.sep, "/")
            found[path] = (info.st_size, info.st_mtime)
    return found


def _delete_oldest(
    media: Path, files: dict[str, tuple[int, float]], total: int, target: float
) -> tuple[int, int]:
    """Delete the photos of the oldest stories until `total` is at most
    `target`. The stories forget their copy, so they show the news site's
    photo. Returns the new total and the number of stories."""
    stories = 0
    while total > target:
        with session_scope() as session:
            rows = session.execute(
                select(Article.image_path)
                .where(Article.image_path.isnot(None))
                .order_by(Article.published_at, Article.id)
                .limit(BATCH)
            ).all()
            if not rows:
                break
            paths: list[str] = []
            for (path,) in rows:
                if total <= target:
                    break
                paths.append(path)
                for name in (path, small_image_path(path)):
                    total -= files.pop(name, (0, 0.0))[0]
                    (media / name).unlink(missing_ok=True)
            stories += session.execute(
                update(Article)
                .where(Article.image_path.in_(paths))
                .values(image_path=None, image_width=None, image_height=None)
            ).rowcount
    return total, stories
