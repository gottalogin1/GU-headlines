"""Remove r/guam: its stored posts and pictures, and the Community topic.

Reddit ended its public feeds on 2026-11-13 and stopped giving out API keys
on 2026-10-31, so r/guam was taken off the site. A source removed from
sources.yaml normally keeps its stories, which would put r/guam's posts on
the front page now that nothing keeps them on a Community page. So its posts
are deleted here, with their pictures and its source row, and the Community
topic is taken off any other story that still has it. This can't be undone.

Revision ID: 0003_remove_reddit_guam
Revises: 0002_jobs
Create Date: 2026-11-24
"""

from __future__ import annotations

import logging
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0003_remove_reddit_guam"
down_revision = "0002_jobs"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

SOURCE = "reddit-guam"
TOPIC = "community"


def remove_source(conn, slug: str, media_dir: Path | None) -> int:
    """Delete a source's stories and its row (its seen links, ignored pictures
    and runs go with it). Their pictures are deleted from `media_dir` unless
    another story uses the same file. Returns the number of stories deleted."""
    source_id = conn.execute(
        sa.text("SELECT id FROM sources WHERE slug = :slug"), {"slug": slug}
    ).scalar()
    if source_id is None:
        return 0
    pictures = {
        path
        for (path,) in conn.execute(
            sa.text("SELECT image_path FROM articles WHERE source_id = :id"), {"id": source_id}
        )
        if path
    }
    deleted = conn.execute(
        sa.text("DELETE FROM articles WHERE source_id = :id"), {"id": source_id}
    ).rowcount
    conn.execute(sa.text("DELETE FROM sources WHERE id = :id"), {"id": source_id})
    if media_dir is not None:
        root = media_dir.resolve()
        for relative in pictures:
            in_use = conn.execute(
                sa.text("SELECT 1 FROM articles WHERE image_path = :path LIMIT 1"),
                {"path": relative},
            ).first()
            target = (root / relative).resolve()
            if in_use or not target.is_relative_to(root):
                continue
            try:
                if target.is_file():
                    target.unlink()
            except OSError as exc:  # a picture left behind is harmless
                log.warning("could not delete %s: %s", target, exc)
    return deleted


def drop_topic(conn, topic: str) -> None:
    """Take a topic that no longer exists off the stories that have it."""
    conn.execute(
        sa.text(
            "UPDATE articles SET categories = array_remove(categories, :topic) "
            "WHERE :topic = ANY(categories)"
        ),
        {"topic": topic},
    )


def upgrade() -> None:
    from guheadlines.settings import get_settings

    conn = op.get_bind()
    deleted = remove_source(conn, SOURCE, get_settings().media_dir)
    if deleted:
        log.info("removed %d r/guam posts", deleted)
    drop_topic(conn, TOPIC)


def downgrade() -> None:
    # The deleted posts can't be brought back.
    pass
