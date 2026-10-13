"""SQLAlchemy models. The schema itself is owned by Alembic migrations."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Text search configuration created in the initial migration: English stemming
# plus unaccent, so "Hagatna" matches "Hagåtña" and "Guahan" matches "Guåhan".
SEARCH_CONFIG = "guam_english"

SEARCH_VECTOR_SQL = (
    f"setweight(to_tsvector('{SEARCH_CONFIG}'::regconfig, coalesce(title, '')), 'A') || "
    f"setweight(to_tsvector('{SEARCH_CONFIG}'::regconfig, coalesce(intro, '')), 'B') || "
    f"setweight(to_tsvector('{SEARCH_CONFIG}'::regconfig, "
    f"coalesce(keywords, '') || ' ' || coalesce(section, '') || ' ' || coalesce(author, '')), 'C')"
)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    articles: Mapped[list[Article]] = relationship(back_populates="source")


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    # Canonical, normalized URL; the unique key that prevents re-fetching.
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # The URL as it was discovered (feed/listing), when different from the canonical one.
    discovered_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    intro: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    image_url: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(Text)
    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_vector = mapped_column(TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True))

    source: Mapped[Source] = relationship(back_populates="articles")


class SeenUrl(Base):
    """URLs that were fetched but not stored (failed or rejected), so they are
    not fetched again every hour. Failed URLs are retried with backoff."""

    __tablename__ = "seen_urls"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text, nullable=False)  # 'failed' | 'rejected'
    reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IgnoredImage(Base):
    """Images a source reuses on many stories (logos, placeholders): never shown."""

    __tablename__ = "ignored_images"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HttpCache(Base):
    """Validators for conditional GETs of feeds and listing pages."""

    __tablename__ = "http_cache"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ScrapeRun.error / Source.last_error messages that start with this report a
# normal pause (the site asked us to slow down), not a problem.
CATCH_UP_NOTE = "catching up:"


class ScrapeRun(Base):
    """One row per source per scheduled run; powers the /status page."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_articles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class JobSource(Base):
    """One row per job board in jobs.yaml; powers the source list on /jobs."""

    __tablename__ = "job_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Postings found on the last successful check.
    last_count: Mapped[int | None] = mapped_column(Integer)

    jobs: Mapped[list[Job]] = relationship(back_populates="source")


class Job(Base):
    """A job posting. Fields a posting does not give are left empty."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("job_sources.id", ondelete="CASCADE"), nullable=False
    )
    # The posting's page (or its PDF when that is all there is); the unique key.
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    employer: Mapped[str | None] = mapped_column(Text)
    pay: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    open_date: Mapped[date | None] = mapped_column(Date)
    close_date: Mapped[date | None] = mapped_column(Date)
    # A closing condition that is not a date, e.g. "Open until filled".
    close_note: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # The scrape date: when the posting was last found on its board.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # When the posting's own page was last read (None: only the listing so far).
    detail_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the posting is no longer listed on its board.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[JobSource] = relationship(back_populates="jobs")
