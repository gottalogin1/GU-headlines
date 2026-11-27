"""Reading the bid sources: each source's inputs -> open bids -> the bids table.

Each check reads every source's list. Bids seen before are updated, so a moved
due date shows up; bids a source no longer lists, or now lists as closed, are
marked removed and leave the Bids page. A bid whose due date has passed leaves
the page by itself, also between checks. When one of a source's inputs can't
be read, nothing of that source is marked removed, and the reason is shown on
the Bids page.

Award lists (a source's `awards` pages) are read about once a day, after a
bids check, for the award history page. Awards are never removed: the history
keeps growing after an agency stops listing an old award.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update

from ..config import BidSourceConfig
from ..db import BIDS_LOCK_KEY, advisory_lock, session_scope
from ..models import Bid, BidAward, BidSource
from ..scraper.http import FetchError, HttpClient
from ..scraper.text import GUAM_TZ
from ..settings import Settings
from .readers import (
    BidError,
    FoundAward,
    FoundBid,
    is_closed,
    read_awards,
    read_notices,
    read_table,
)

log = logging.getLogger(__name__)

# Sole-source notices announce a purchase from one company: nothing to bid on.
_SOLE_SOURCE_RE = re.compile(r"\bsole[\s-]*source\b", re.IGNORECASE)

# Award lists change slowly: each is read about once a day.
AWARDS_EVERY = timedelta(hours=20)
# Award fields an agency sometimes leaves out on a later read of the same row;
# what an earlier read found is kept.
_KEPT_AWARD_FIELDS = ("awarded_to", "amount_cents", "document_url", "closed_on")


@dataclass
class AwardResult:
    slug: str
    found: int = 0
    new: int = 0
    error: str | None = None
    awards: list[FoundAward] = field(default_factory=list)


@dataclass
class SourceResult:
    slug: str
    found: int = 0
    new: int = 0
    removed: int = 0
    error: str | None = None
    bids: list[FoundBid] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today(now: datetime) -> date:
    return now.astimezone(GUAM_TZ).date()


def sync_sources(session, sources: list[BidSourceConfig]) -> dict[str, int]:
    """Rows in bid_sources to match bids.yaml (removed sources are only disabled)."""
    existing = {s.slug: s for s in session.scalars(select(BidSource))}
    for source in sources:
        row = existing.get(source.slug)
        if row is None:
            row = BidSource(slug=source.slug, name=source.name)
            session.add(row)
            existing[source.slug] = row
        row.name = source.name
        row.homepage = source.homepage
        row.enabled = source.collected
    configured = {s.slug for s in sources}
    for slug, row in existing.items():
        if slug not in configured:
            row.enabled = False
    session.flush()
    return {slug: row.id for slug, row in existing.items()}


class BidChecker:
    def __init__(
        self,
        settings: Settings,
        sources: list[BidSourceConfig],
        client: HttpClient | None = None,
    ):
        self.settings = settings
        self.sources = sources
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

    def collect(
        self,
        source: BidSourceConfig,
        now: datetime | None = None,
        known: dict[str, tuple[datetime, bool]] | None = None,
    ) -> tuple[list[FoundBid], list[str], date | None]:
        """A source's open bids (no database needed), the inputs that couldn't
        be read, and the posting day of the oldest notice read (older notices
        were not looked at). Raises BidError when no input could be read.
        `known`: due dates found on bid pages by earlier checks, by uid."""
        now = now or _now()
        for url in source.addresses:
            if source.request_delay:
                self.client.set_host_delay(url, source.request_delay)
            if source.ignore_robots:
                self.client.ignore_robots_for(url)
        found: list[FoundBid] = []
        errors: list[str] = []
        covered_since: date | None = None
        for url in source.notices:
            try:
                bids, oldest = read_notices(self.client, url, _today(now))
            except (FetchError, BidError) as exc:
                errors.append(str(exc) if url in str(exc) else f"{url}: {exc}")
                continue
            found += bids
            if oldest and (covered_since is None or oldest > covered_since):
                covered_since = oldest
        for url in source.table:
            try:
                found += read_table(self.client, url, source.agency, source.details, known)
            except (FetchError, BidError) as exc:
                errors.append(str(exc) if url in str(exc) else f"{url}: {exc}")
        if errors and len(errors) == len(source.addresses):
            raise BidError("; ".join(errors))
        return self._tidy(source, found, now), errors, covered_since

    def _tidy(self, source: BidSourceConfig, bids: list[FoundBid], now: datetime) -> list[FoundBid]:
        """The bids still open: not closed, cancelled or awarded, not past their
        due date, and (without a due date) posted within undated_days."""
        oldest_undated = _today(now) - timedelta(days=source.undated_days)
        kept: dict[str, FoundBid] = {}
        for bid in bids:
            bid.agency = bid.agency or source.agency
            if (
                source.excluded(bid.title, bid.number, bid.agency)
                or source.excluded_everywhere(bid.title, bid.number, bid.agency, bid.description)
                or is_closed(bid.status)
                or any(_SOLE_SOURCE_RE.search(t or "") for t in (bid.title, bid.number))
                or (bid.due_at is not None and bid.due_at < now)
                or (bid.due_at is None and bid.posted_on and bid.posted_on < oldest_undated)
            ):
                continue
            kept.setdefault(bid.uid, bid)
        far = datetime.max.replace(tzinfo=timezone.utc)
        return sorted(kept.values(), key=lambda b: (b.due_at or far, b.title))

    # ------------------------------------------------------------------- runs

    def run(self, only: list[str] | None = None) -> list[SourceResult] | None:
        """Check every collected source (or just `only`). None if another bids
        check holds the lock."""
        with advisory_lock(BIDS_LOCK_KEY) as acquired:
            if not acquired:
                log.warning("another bids check is already running; skipping this one")
                return None
            with session_scope() as session:
                ids = sync_sources(session, self.sources)
            chosen = [s for s in self.sources if s.collected and (not only or s.slug in only)]
            results = [self.run_source(s, ids[s.slug]) for s in chosen]
            log.info(
                "bids check finished: %d open bids, %d new, from %d sources",
                sum(r.found for r in results),
                sum(r.new for r in results),
                len(results),
            )
            return results

    def run_source(self, source: BidSourceConfig, source_id: int) -> SourceResult:
        result = SourceResult(slug=source.slug)
        started = _now()
        try:
            with session_scope() as session:
                known = {
                    uid: (due_at, due_time)
                    for uid, due_at, due_time in session.execute(
                        select(Bid.uid, Bid.due_at, Bid.due_time).where(
                            Bid.source_id == source_id, Bid.due_at.is_not(None)
                        )
                    )
                }
            bids, errors, covered_since = self.collect(source, started, known)
            result.found = len(bids)
            result.bids = bids
            # An input that failed leaves its bids as they were.
            self._store(
                source_id,
                source,
                bids,
                started,
                result,
                remove_missing=not errors,
                covered_since=covered_since,
            )
            if errors:
                result.error = "; ".join(errors)
        except (FetchError, BidError) as exc:
            result.error = str(exc)
        except Exception as exc:  # never let one source break the others
            log.exception("[bids:%s] crashed", source.slug)
            result.error = f"{type(exc).__name__}: {exc}"

        values = {"last_run_at": _now(), "last_error": result.error}
        if result.error is None or result.found:
            values |= {"last_success_at": values["last_run_at"], "last_count": result.found}
        with session_scope() as session:
            session.execute(update(BidSource).where(BidSource.id == source_id).values(**values))
        log.info(
            "[bids:%s] %d open, %d new, %d removed%s",
            source.slug,
            result.found,
            result.new,
            result.removed,
            f" (error: {result.error})" if result.error else "",
        )
        return result

    def _store(
        self,
        source_id: int,
        source: BidSourceConfig,
        bids: list[FoundBid],
        now: datetime,
        result: SourceResult,
        *,
        remove_missing: bool,
        covered_since: date | None = None,
    ) -> None:
        oldest_undated = now - timedelta(days=source.undated_days)
        with session_scope() as session:
            rows = {
                row.uid: row
                for row in session.scalars(select(Bid).where(Bid.source_id == source_id))
            }
            listed: set[str] = set()
            for found in bids:
                row = rows.get(found.uid)
                # A bid without a due date or posting day leaves after
                # undated_days, counted from when it was first seen.
                if (
                    row is not None
                    and found.due_at is None
                    and found.posted_on is None
                    and row.first_seen_at < oldest_undated
                ):
                    continue
                if row is None:
                    row = Bid(source_id=source_id, uid=found.uid, first_seen_at=now)
                    session.add(row)
                    rows[found.uid] = row
                    result.new += 1
                elif row.removed_at is not None:
                    result.new += 1  # listed again (a re-opened bid)
                row.title = found.title[:500]
                row.number = found.number
                row.kind = found.kind
                row.agency = found.agency
                row.description = found.description
                row.url = found.url
                row.document_url = found.document_url
                row.posted_on = found.posted_on
                row.due_at = found.due_at
                row.due_time = found.due_time
                row.last_seen_at = now
                row.removed_at = None
                listed.add(found.uid)
            if not remove_missing:
                return
            for uid, row in rows.items():
                if uid in listed or row.removed_at is not None:
                    continue
                # Notices older than the ones read this time weren't looked at:
                # one still to come stays (it leaves at its due date).
                if (
                    covered_since
                    and uid.startswith("notice:")
                    and row.posted_on
                    and row.posted_on < covered_since
                    and row.due_at is not None
                    and row.due_at >= now
                ):
                    continue
                row.removed_at = now
                result.removed += 1

    # ----------------------------------------------------------------- awards

    def collect_awards(self, source: BidSourceConfig) -> tuple[list[FoundAward], list[str]]:
        """A source's awarded bids (no database needed), newest first, and the
        award pages that couldn't be read. Raises BidError when none could."""
        for url in source.awards:
            if source.request_delay:
                self.client.set_host_delay(url, source.request_delay)
            if source.ignore_robots:
                self.client.ignore_robots_for(url)
        found: list[FoundAward] = []
        errors: list[str] = []
        for url in source.awards:
            try:
                found += read_awards(self.client, url, source.agency)
            except (FetchError, BidError) as exc:
                errors.append(str(exc) if url in str(exc) else f"{url}: {exc}")
        if errors and len(errors) == len(source.awards):
            raise BidError("; ".join(errors))
        kept: dict[str, FoundAward] = {}
        for award in found:
            award.agency = award.agency or source.agency
            labels = (award.title, award.number, award.agency)
            if (
                source.excluded(*labels)
                or source.excluded_everywhere(*labels, award.awarded_to)
                or any(_SOLE_SOURCE_RE.search(t or "") for t in (award.title, award.number))
            ):
                continue
            kept.setdefault(award.uid, award)
        newest = sorted(
            kept.values(), key=lambda a: a.closed_on or a.posted_on or date.min, reverse=True
        )
        return newest, errors

    def run_awards(
        self, only: list[str] | None = None, force: bool = False, now: datetime | None = None
    ) -> list[AwardResult] | None:
        """Read the award lists not read in the last AWARDS_EVERY (all of them
        with `force`). None if a bids check holds the lock."""
        now = now or _now()
        with advisory_lock(BIDS_LOCK_KEY) as acquired:
            if not acquired:
                log.warning("a bids check is running; award lists not read")
                return None
            with session_scope() as session:
                ids = sync_sources(session, self.sources)
                last = {
                    slug: when
                    for slug, when in session.execute(
                        select(BidSource.slug, BidSource.awards_run_at)
                    )
                }
            chosen = [
                s
                for s in self.sources
                if s.collected
                and s.awards
                and (not only or s.slug in only)
                and (force or last.get(s.slug) is None or last[s.slug] <= now - AWARDS_EVERY)
            ]
            return [self.run_award_source(s, ids[s.slug], now) for s in chosen]

    def run_award_source(
        self, source: BidSourceConfig, source_id: int, now: datetime | None = None
    ) -> AwardResult:
        now = now or _now()
        result = AwardResult(slug=source.slug)
        try:
            awards, errors = self.collect_awards(source)
            result.found = len(awards)
            result.awards = awards
            self._store_awards(source_id, awards, now, result)
            if errors:
                result.error = "; ".join(errors)
        except (FetchError, BidError) as exc:
            result.error = str(exc)
        except Exception as exc:  # never let one source break the others
            log.exception("[awards:%s] crashed", source.slug)
            result.error = f"{type(exc).__name__}: {exc}"
        values = {"awards_run_at": now, "awards_error": result.error}
        if result.error is None or result.found:
            values["awards_count"] = result.found
        with session_scope() as session:
            session.execute(update(BidSource).where(BidSource.id == source_id).values(**values))
        log.info(
            "[awards:%s] %d awards listed, %d new%s",
            source.slug,
            result.found,
            result.new,
            f" (error: {result.error})" if result.error else "",
        )
        return result

    def _store_awards(
        self, source_id: int, awards: list[FoundAward], now: datetime, result: AwardResult
    ) -> None:
        with session_scope() as session:
            rows = {
                row.uid: row
                for row in session.scalars(select(BidAward).where(BidAward.source_id == source_id))
            }
            for found in awards:
                row = rows.get(found.uid)
                if row is None:
                    row = BidAward(source_id=source_id, uid=found.uid, first_seen_at=now)
                    session.add(row)
                    rows[found.uid] = row
                    result.new += 1
                row.title = found.title[:500]
                row.url = found.url
                for name in ("number", "kind", "agency", "posted_on", *_KEPT_AWARD_FIELDS):
                    value = getattr(found, name)
                    if value is not None or name not in _KEPT_AWARD_FIELDS:
                        setattr(row, name, value)
                if found.amount_cents is not None:
                    row.amounts = found.amounts
                row.last_seen_at = now
