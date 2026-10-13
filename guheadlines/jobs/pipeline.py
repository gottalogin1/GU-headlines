"""Reading the job boards: listing pages -> postings -> (new postings' own
pages) -> the jobs table.

Each check reads every board's listing pages. Postings seen before are only
updated (their "last seen" date moves on); a posting's own page is read once,
when it first appears. Postings no longer listed are marked removed and leave
the Jobs page. A board that can't be read (blocked, needs JavaScript, or its
page no longer has postings where expected) keeps its postings and shows the
reason on the Jobs page instead of failing quietly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update

from ..config import JobSourceConfig
from ..db import JOBS_LOCK_KEY, advisory_lock, session_scope
from ..models import Job, JobSource
from ..scraper.http import FetchError, HttpClient
from ..settings import Settings
from .parsers import PARSERS, BoardError, Posting, needs_javascript

log = logging.getLogger(__name__)

MAX_LISTING_PAGES = 10  # per board and check (pagination)
MAX_DETAILS = 40  # posting pages read per board and check; the rest next time

# Posting fields a later listing can't take back: once a posting's own page
# gave its pay or description, a listing that lacks them keeps the old value.
_KEEP_FIELDS = ("employer", "pay", "description", "location", "close_note", "pdf_url")


@dataclass
class BoardResult:
    slug: str
    found: int = 0
    new: int = 0
    removed: int = 0
    details: int = 0
    error: str | None = None
    postings: list[Posting] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sync_boards(session, boards: list[JobSourceConfig]) -> dict[str, int]:
    """Rows in job_sources to match jobs.yaml (removed boards are only disabled)."""
    existing = {s.slug: s for s in session.scalars(select(JobSource))}
    for board in boards:
        row = existing.get(board.slug)
        if row is None:
            row = JobSource(slug=board.slug, name=board.name)
            session.add(row)
            existing[board.slug] = row
        row.name = board.name
        row.homepage = board.homepage
        row.enabled = board.enabled
    configured = {b.slug for b in boards}
    for slug, row in existing.items():
        if slug not in configured:
            row.enabled = False
    session.flush()
    return {slug: row.id for slug, row in existing.items()}


class JobScraper:
    def __init__(
        self, settings: Settings, boards: list[JobSourceConfig], client: HttpClient | None = None
    ):
        self.settings = settings
        self.boards = boards
        self._own_client = client is None
        self.client = client or HttpClient(
            settings.user_agent,
            timeout=settings.request_timeout,
            per_host_delay=settings.per_host_delay,
            respect_robots=settings.respect_robots,
        )

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    # ---------------------------------------------------------------- reading

    def collect(self, board: JobSourceConfig) -> list[Posting]:
        """All postings on a board's listing pages (no database needed)."""
        parser = PARSERS[board.parser]
        if board.request_delay:
            for url in board.pages:
                self.client.set_host_delay(url, board.request_delay)
        if board.ignore_robots:
            for url in board.pages:
                self.client.ignore_robots_for(url)
        custom = getattr(parser, "collect", None)
        if custom is not None:
            postings = custom(self.client, board, self.settings)
        else:
            postings = []
            queue = list(board.pages)
            seen: set[str] = set()
            first = None
            while queue and len(seen) < MAX_LISTING_PAGES:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                result = self.client.get(url)
                first = first or result
                listing = parser.listing(result, board)
                postings += listing.postings
                queue += [u for u in listing.next_pages if u not in seen]
            if not postings and first is not None:
                if needs_javascript(first):
                    raise BoardError(
                        f"{first.url} is built with JavaScript, so its postings "
                        "can't be read without a browser"
                    )
                raise BoardError(f"no job postings found on {first.url}; the page may have changed")
        unique: dict[str, Posting] = {}
        for posting in postings:
            unique.setdefault(posting.url, posting)
        return parser.finish(list(unique.values()))

    def read_details(self, board: JobSourceConfig, postings: list[Posting]) -> set[str]:
        """Read the own pages of these postings; failures keep the listing data.
        Returns the URLs of the postings whose page was read."""
        parser = PARSERS[board.parser]
        done = set()
        for posting in postings[:MAX_DETAILS]:
            try:
                result = self.client.get(posting.detail_url, headers=parser.detail_headers)
                parser.detail(result, posting)
                done.add(posting.url)
            except FetchError as exc:
                log.info("[jobs:%s] posting page failed: %s", board.slug, exc)
            except Exception:
                log.exception("[jobs:%s] could not read %s", board.slug, posting.detail_url)
        return done

    # ------------------------------------------------------------------- runs

    def run(self, only: list[str] | None = None) -> list[BoardResult] | None:
        """Check every collected board (or just `only`). None if another job
        check holds the lock."""
        with advisory_lock(JOBS_LOCK_KEY) as acquired:
            if not acquired:
                log.warning("another job check is already running; skipping this one")
                return None
            with session_scope() as session:
                ids = sync_boards(session, self.boards)
            boards = [b for b in self.boards if b.collected and (not only or b.slug in only)]
            results = [self.run_board(board, ids[board.slug]) for board in boards]
            log.info(
                "job check finished: %d postings, %d new, from %d boards",
                sum(r.found for r in results),
                sum(r.new for r in results),
                len(results),
            )
            return results

    def run_board(self, board: JobSourceConfig, source_id: int) -> BoardResult:
        result = BoardResult(slug=board.slug)
        started = _now()
        try:
            postings = self.collect(board)
            result.found = len(postings)
            with session_scope() as session:
                known = {
                    job.url: job
                    for job in session.scalars(select(Job).where(Job.source_id == source_id))
                }
            wanted = [
                p
                for p in postings
                if p.detail_url and (p.url not in known or known[p.url].detail_fetched_at is None)
            ]
            detailed = self.read_details(board, wanted)
            result.details = len(detailed)
            self._store(source_id, postings, detailed, started, result)
        except (FetchError, BoardError) as exc:
            result.error = str(exc)
        except Exception as exc:  # never let one board break the others
            log.exception("[jobs:%s] crashed", board.slug)
            result.error = f"{type(exc).__name__}: {exc}"

        values = {"last_run_at": _now(), "last_error": result.error}
        if result.error is None:
            values |= {"last_success_at": values["last_run_at"], "last_count": result.found}
        with session_scope() as session:
            session.execute(update(JobSource).where(JobSource.id == source_id).values(**values))
        log.info(
            "[jobs:%s] %d postings, %d new, %d removed, %d pages read%s",
            board.slug,
            result.found,
            result.new,
            result.removed,
            result.details,
            f" (error: {result.error})" if result.error else "",
        )
        return result

    def _store(
        self,
        source_id: int,
        postings: list[Posting],
        detailed: set[str],
        now: datetime,
        result: BoardResult,
    ) -> None:
        with session_scope() as session:
            known = {
                job.url: job
                for job in session.scalars(select(Job).where(Job.source_id == source_id))
            }
            for posting in postings:
                job = known.get(posting.url)
                if job is None:
                    job = Job(source_id=source_id, url=posting.url, first_seen_at=now)
                    session.add(job)
                    result.new += 1
                job.title = posting.title[:500]
                for name in _KEEP_FIELDS:
                    value = getattr(posting, name)
                    if value or getattr(job, name, None) is None:
                        setattr(job, name, value)
                # Dates: what the board says now (a corrected closing date wins).
                job.open_date = posting.open_date or job.open_date
                job.close_date = posting.close_date or job.close_date
                job.last_seen_at = now
                job.removed_at = None
                if posting.url in detailed:
                    job.detail_fetched_at = now
            listed = {p.url for p in postings}
            for url, job in known.items():
                if url not in listed and job.removed_at is None:
                    job.removed_at = now
                    result.removed += 1
