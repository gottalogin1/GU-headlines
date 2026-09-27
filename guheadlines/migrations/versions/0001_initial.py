"""Initial schema: sources, articles (with full-text search), crawl bookkeeping.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('guam_english'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('guam_english'::regconfig, coalesce(intro, '')), 'B') || "
    "setweight(to_tsvector('guam_english'::regconfig, "
    "coalesce(keywords, '') || ' ' || coalesce(section, '') || ' ' || coalesce(author, '')), 'C')"
)


def upgrade() -> None:
    # Both extensions are "trusted" since PostgreSQL 13, so the database owner can
    # create them without superuser rights.
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'guam_english') THEN
                CREATE TEXT SEARCH CONFIGURATION guam_english (COPY = english);
                ALTER TEXT SEARCH CONFIGURATION guam_english
                    ALTER MAPPING FOR hword, hword_part, word WITH unaccent, english_stem;
            END IF;
        END
        $$;
        """
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("homepage", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
    )

    op.create_table(
        "articles",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer,
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("discovered_url", sa.Text),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("intro", sa.Text),
        sa.Column("author", sa.Text),
        sa.Column("section", sa.Text),
        sa.Column("keywords", sa.Text),
        sa.Column(
            "categories",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("image_url", sa.Text),
        sa.Column("image_path", sa.Text),
        sa.Column("image_width", sa.Integer),
        sa.Column("image_height", sa.Integer),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR,
            sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
        ),
    )
    # Listing pages: newest first, optionally per source / per category.
    op.create_index(
        "ix_articles_published", "articles", [sa.text("published_at DESC"), sa.text("id DESC")]
    )
    op.create_index(
        "ix_articles_source_published",
        "articles",
        ["source_id", sa.text("published_at DESC")],
    )
    op.create_index("ix_articles_discovered_url", "articles", ["discovered_url"])
    op.create_index("ix_articles_categories", "articles", ["categories"], postgresql_using="gin")
    # Full-text search.
    op.create_index("ix_articles_search", "articles", ["search_vector"], postgresql_using="gin")
    # Fuzzy title matching (typos, partial names) as a search fallback.
    op.create_index(
        "ix_articles_title_trgm",
        "articles",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )

    op.create_table(
        "seen_urls",
        sa.Column("url", sa.Text, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="CASCADE")),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_seen_urls_last_attempt", "seen_urls", ["last_attempt_at"])

    op.create_table(
        "ignored_images",
        sa.Column("url", sa.Text, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="CASCADE")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "http_cache",
        sa.Column("url", sa.Text, primary_key=True),
        sa.Column("etag", sa.Text),
        sa.Column("last_modified", sa.Text),
        sa.Column("content_hash", sa.Text),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer,
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("candidates", sa.Integer, nullable=False, server_default="0"),
        sa.Column("new_articles", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
    )
    op.create_index(
        "ix_scrape_runs_source_started", "scrape_runs", ["source_id", sa.text("started_at DESC")]
    )


def downgrade() -> None:
    op.drop_table("scrape_runs")
    op.drop_table("http_cache")
    op.drop_table("ignored_images")
    op.drop_table("seen_urls")
    op.drop_table("articles")
    op.drop_table("sources")
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS guam_english")
