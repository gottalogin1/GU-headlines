"""Migrations that change stored data."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone

from conftest import ROOT
from sqlalchemy import select

from guheadlines.db import session_scope
from guheadlines.models import Article, ScrapeRun, Source

VERSIONS = ROOT / "guheadlines" / "migrations" / "versions"


def _migration(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_r_guam_posts_and_pictures_are_removed(clean_db, tmp_path):
    migration = _migration("0003_remove_reddit_guam")
    now = datetime.now(timezone.utc)
    pictures = {
        "images/reddit-only.webp": True,  # deleted
        "images/shared.webp": False,  # also used by a news story: kept
        "images/news.webp": False,
    }
    for relative in pictures:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(b"webp")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.webp"
    outside.write_bytes(b"webp")
    with session_scope() as session:
        reddit = Source(slug="reddit-guam", name="r/guam (Reddit)")
        news = Source(slug="kuam", name="KUAM News")
        session.add_all([reddit, news])
        session.flush()
        session.add(ScrapeRun(source_id=reddit.id, started_at=now))
        for n, (picture, topics) in enumerate(
            [
                ("images/reddit-only.webp", ["community"]),
                ("images/shared.webp", ["community"]),
                (f"../{outside.name}", ["community"]),  # never outside the media folder
                (None, ["community", "local"]),
            ]
        ):
            session.add(
                Article(
                    source_id=reddit.id,
                    url=f"https://www.reddit.com/r/guam/comments/{n}/",
                    title=f"Post {n}",
                    image_path=picture,
                    categories=topics,
                    published_at=now,
                )
            )
        for n, (picture, topics) in enumerate(
            [("images/shared.webp", ["local"]), ("images/news.webp", ["community", "local"])]
        ):
            session.add(
                Article(
                    source_id=news.id,
                    url=f"https://www.kuam.com/story/{n}/",
                    title=f"Story {n}",
                    image_path=picture,
                    categories=topics,
                    published_at=now,
                )
            )

    with session_scope() as session:
        conn = session.connection()
        assert migration.remove_source(conn, "reddit-guam", tmp_path) == 4
        assert migration.remove_source(conn, "reddit-guam", tmp_path) == 0  # already gone
        migration.drop_topic(conn, "community")

    with session_scope() as session:
        assert session.scalars(select(Source.slug)).all() == ["kuam"]
        assert session.scalars(select(ScrapeRun)).all() == []
        stories = session.scalars(select(Article).order_by(Article.url)).all()
        assert [(a.title, a.categories) for a in stories] == [
            ("Story 0", ["local"]),
            ("Story 1", ["local"]),
        ]
    for relative, deleted in pictures.items():
        assert (tmp_path / relative).exists() is not deleted, relative
    assert outside.exists()
