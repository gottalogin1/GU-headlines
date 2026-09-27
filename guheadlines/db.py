"""Database engine and session helpers."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import get_settings


@lru_cache(maxsize=4)
def get_engine(url: str | None = None) -> Engine:
    return create_engine(
        url or get_settings().database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        future=True,
    )


@lru_cache(maxsize=4)
def _session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """A transactional session: commits on success, rolls back on error."""
    session = _session_factory(url)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


# Fixed keys for pg_advisory_lock so that only one migration / scrape runs at a time,
# even if several containers are started.
MIGRATION_LOCK_KEY = 0x6775_0001
SCRAPE_LOCK_KEY = 0x6775_0002


@contextmanager
def advisory_lock(key: int, *, wait: bool = False, url: str | None = None) -> Iterator[bool]:
    """Hold a Postgres session-level advisory lock on a dedicated connection.

    Yields True when the lock was acquired, False when another process holds it
    (only possible with wait=False).
    """
    conn = get_engine(url).connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if wait:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            acquired = True
        else:
            acquired = bool(
                conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
            )
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    finally:
        conn.close()
