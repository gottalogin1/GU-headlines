"""The scrape pipeline: discover -> skip known URLs -> fetch new pages -> extract -> store.

Incremental by design:
  * feeds and listing pages use conditional GETs (ETag / Last-Modified) and a
    content hash, so an unchanged feed is not even parsed;
  * every discovered URL is checked against the database first, so only
    article pages we have never stored are downloaded;
  * pages that failed are retried with exponential backoff, and pages that are
    not articles are remembered so they are not fetched again.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import AppConfig, SourceConfig
from ..db import SCRAPE_LOCK_KEY, advisory_lock, session_scope
from ..models import (
    CATCH_UP_NOTE,
    Article,
    HttpCache,
    IgnoredImage,
    ScrapeRun,
    SeenUrl,
    Source,
)
from ..settings import Settings
from .classify import classify, mentions_guam
from .discover import (
    Candidate,
    looks_like_article_url,
    parse_feed,
    parse_listing,
    parse_sitemap,
    sitemaps_from_robots,
)
from .extract import build_intro, extract_article
from .http import FetchError, FetchResult, HttpClient, RateLimited, RobotsDisallowed
from .images import store_image
from .text import clean_byline, clean_text, truncate
from .urls import normalize_url, same_site, url_variants

log = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 5
MAX_BACKFILL_PAUSES = 10
HTML_TYPES = ("text/html", "application/xhtml+xml", "")
_FEED_TAIL_RE = re.compile(
    r"(\s*\[(…|\.\.\.|&hellip;)\]\s*$)|(\s*The post .{3,200} appeared first on .{3,120}\.?\s*$)",
    re.IGNORECASE,
)


class Skip(Exception):
    """A candidate that will not be stored. status is 'rejected' (never retry),
    'failed' (retry later with backoff) or 'throttled' (the site is
    rate-limiting us: stop this source and catch up shortly)."""

    def __init__(self, status: str, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


@dataclass
class Draft:
    """Everything needed to insert one article."""

    url: str
    discovered_url: str | None
    title: str
    intro: str | None
    author: str | None
    section: str | None
    keywords: str | None
    categories: list[str]
    image_url: str | None
    published_at: datetime
    image_path: str | None = None
    image_width: int | None = None
    image_height: int | None = None


@dataclass
class SourceResult:
    slug: str
    candidates: int = 0
    new_articles: int = 0
    failed: int = 0
    rejected: int = 0
    reachable: int = 0  # feeds/pages that answered (including "not modified")
    errors: list[str] = field(default_factory=list)
    # New pages not processed yet (per-run cap reached or rate-limited). The
    # worker comes back for them within a minute instead of waiting an hour.
    pending: list[Candidate] = field(default_factory=list)
    # Feeds and listing pages the site rate-limited; read again on catch-up.
    pending_urls: list[str] = field(default_factory=list)
    # Feed validators to save once the pending pages are done.
    cache_updates: list[dict] = field(default_factory=list)
    # Seconds the site asked us to wait (Retry-After), if it was rate-limiting.
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return not self.errors or self.reachable > 0

    @property
    def incomplete(self) -> bool:
        return bool(self.pending or self.pending_urls)

    def slow_down(self, url: str, exc: RateLimited) -> None:
        """A feed or listing page was rate-limited: read it again on catch-up."""
        self.reachable += 1  # the site answered; it just asked for a pause
        self.pending_urls.append(url)
        if exc.retry_after:
            self.retry_after = max(self.retry_after or 0.0, exc.retry_after)
        self.errors.append(
            f"{CATCH_UP_NOTE} the site asked us to slow down, {url} is read again shortly"
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean_feed_summary(summary: str | None) -> str | None:
    if not summary:
        return None
    text = _FEED_TAIL_RE.sub("", clean_text(summary)).strip()
    return text or None


def sync_sources(session: Session, config: AppConfig) -> dict[str, int]:
    """Insert/update rows in `sources` to match sources.yaml. Returns slug -> id.

    Sources removed from the config are only disabled, never deleted, so their
    articles stay searchable.
    """
    existing = {s.slug: s for s in session.scalars(select(Source))}
    for cfg in config.sources:
        row = existing.get(cfg.slug)
        if row is None:
            row = Source(slug=cfg.slug, name=cfg.name, homepage=cfg.homepage, enabled=cfg.enabled)
            session.add(row)
            existing[cfg.slug] = row
        else:
            row.name = cfg.name
            row.homepage = cfg.homepage
            row.enabled = cfg.enabled
    configured = {cfg.slug for cfg in config.sources}
    for slug, row in existing.items():
        if slug not in configured:
            row.enabled = False
    session.flush()
    for cfg in config.sources:
        if cfg.only_in_topic:
            # Keep stories stored before only_in_topic was set in that one topic.
            session.execute(
                update(Article)
                .where(
                    Article.source_id == existing[cfg.slug].id,
                    Article.categories != [cfg.only_in_topic],
                )
                .values(categories=[cfg.only_in_topic])
            )
    return {slug: row.id for slug, row in existing.items()}


class Scraper:
    def __init__(self, settings: Settings, config: AppConfig, client: HttpClient | None = None):
        self.settings = settings
        self.config = config
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

    # ------------------------------------------------------------------ runs

    def run(
        self,
        only: list[str] | None = None,
        catch_up: dict[str, SourceResult] | None = None,
    ) -> list[SourceResult] | None:
        """Scrape all enabled sources (or just `only`). With `catch_up` (results
        of an earlier run that left pages pending), only those sources run and
        they continue with their pending pages instead of re-reading feeds.
        Returns None if another scrape already holds the lock."""
        with advisory_lock(SCRAPE_LOCK_KEY) as acquired:
            if not acquired:
                log.warning("another scrape is already running; skipping this one")
                return None
            with session_scope() as session:
                ids = sync_sources(session, self.config)
            if catch_up is not None:
                sources = [s for s in self.config.sources if s.slug in catch_up and s.collected]
            elif only:
                sources = [s for s in self.config.sources if s.slug in only]
                for source in sources:
                    if source.cannot_scrape:
                        log.warning("[%s] not scraped: %s", source.slug, source.cannot_scrape)
                sources = [s for s in sources if not s.cannot_scrape]
            else:
                sources = [s for s in self.config.sources if s.collected]
            carry = catch_up or {}
            started = _now()
            with ThreadPoolExecutor(max_workers=self.settings.max_workers) as pool:
                results = list(
                    pool.map(lambda s: self.run_source(s, ids[s.slug], carry.get(s.slug)), sources)
                )
            self._housekeeping()
            new_total = sum(r.new_articles for r in results)
            log.info(
                "scrape finished in %.0fs: %d new articles from %d sources",
                (_now() - started).total_seconds(),
                new_total,
                len(results),
            )
            return results

    def run_source(
        self, source: SourceConfig, source_id: int, carry: SourceResult | None = None
    ) -> SourceResult:
        result = SourceResult(slug=source.slug)
        started = _now()
        with session_scope() as session:
            run = ScrapeRun(source_id=source_id, started_at=started)
            session.add(run)
            session.flush()
            run_id = run.id
        self.prepare(source)
        try:
            if carry is not None:
                # Catching up: continue with the pages an earlier run left over,
                # and re-read the feeds and listing pages the site rate-limited.
                candidates, cache_updates = list(carry.pending), list(carry.cache_updates)
                result.reachable = 1
                if carry.pending_urls:
                    found, updates = self.discover(source, result, only=set(carry.pending_urls))
                    known = {c.key for c in candidates}
                    candidates += [c for c in found if c.key not in known]
                    cache_updates += updates
            else:
                candidates, cache_updates = self.discover(source, result)
                candidates += self._retry_candidates(source_id)
            result.candidates = len(candidates)
            with session_scope() as session:
                fresh = self.filter_new(session, candidates)
            # Cheap decisions from feed data first, so they do not use up the cap.
            kept = []
            for candidate in fresh:
                try:
                    self.prefilter(source, candidate)
                    kept.append(candidate)
                except Skip as skip:
                    self._remember(source_id, candidate.key, skip.status, skip.reason)
                    result.rejected += 1
            fresh = kept
            limit = self.settings.max_new_per_source
            result.pending = fresh[limit:]
            for done, candidate in enumerate(fresh[:limit]):
                try:
                    self._process_and_save(source, source_id, candidate, result)
                except Skip as skip:  # rate-limited: stop and catch up shortly
                    result.pending = fresh[done:]
                    if skip.retry_after:
                        result.retry_after = max(result.retry_after or 0.0, skip.retry_after)
                    result.errors.append(
                        f"{CATCH_UP_NOTE} the site asked us to slow down, "
                        f"{len(result.pending)} pages left for the next round"
                    )
                    break
            if result.pending:
                result.cache_updates = cache_updates
            else:
                # Only remember validators once everything they announced is
                # stored, otherwise the rest would never be seen again.
                self._save_cache(cache_updates)
        except Exception as exc:  # never let one source break the others
            log.exception("source %s crashed", source.slug)
            result.errors.append(f"{type(exc).__name__}: {exc}")

        finished = _now()
        error = "; ".join(result.errors)[:2000] or None
        with session_scope() as session:
            session.execute(
                update(ScrapeRun)
                .where(ScrapeRun.id == run_id)
                .values(
                    finished_at=finished,
                    candidates=result.candidates,
                    new_articles=result.new_articles,
                    failed=result.failed,
                    error=error,
                )
            )
            values = {"last_run_at": finished, "last_error": error}
            if result.ok:
                values["last_success_at"] = finished
            session.execute(update(Source).where(Source.id == source_id).values(**values))
        log.info(
            "[%s] %d candidates, %d new, %d failed, %d skipped%s",
            source.slug,
            result.candidates,
            result.new_articles,
            result.failed,
            result.rejected,
            f" (errors: {error})" if error else "",
        )
        return result

    def prepare(self, source: SourceConfig) -> None:
        """Apply per-source HTTP settings (pace, robots.txt) to the source's sites."""
        urls = [u for u in [source.homepage, *source.feeds, *source.listing_pages] if u]
        for url in urls:
            if source.request_delay:
                self.client.set_host_delay(url, source.request_delay)
            if source.ignore_robots:
                self.client.ignore_robots_for(url)

    # ------------------------------------------------------------- discovery

    def _conditional_fetch(
        self, url: str, cache_updates: list[dict], use_validators: bool = True
    ) -> FetchResult | None:
        """GET with If-None-Match / If-Modified-Since. None means 'unchanged'."""
        cached = None
        if use_validators:
            with session_scope() as session:
                cached = session.get(HttpCache, url)
                cached = (
                    (cached.etag, cached.last_modified, cached.content_hash) if cached else None
                )
        result = self.client.get(
            url,
            etag=cached[0] if cached else None,
            last_modified=cached[1] if cached else None,
        )
        if result.not_modified:
            return None
        digest = hashlib.sha256(result.content).hexdigest()
        cache_updates.append(
            {
                "url": url,
                "etag": result.etag,
                "last_modified": result.last_modified,
                "content_hash": digest,
            }
        )
        if cached and cached[2] == digest:
            return None
        return result

    def _save_cache(self, cache_updates: list[dict]) -> None:
        if not cache_updates:
            return
        with session_scope() as session:
            for row in cache_updates:
                stmt = pg_insert(HttpCache).values(**row, checked_at=_now())
                stmt = stmt.on_conflict_do_update(
                    index_elements=[HttpCache.url],
                    set_={
                        "etag": stmt.excluded.etag,
                        "last_modified": stmt.excluded.last_modified,
                        "content_hash": stmt.excluded.content_hash,
                        "checked_at": stmt.excluded.checked_at,
                    },
                )
                session.execute(stmt)

    def discover(
        self,
        source: SourceConfig,
        result: SourceResult,
        *,
        use_cache: bool = True,
        only: set[str] | None = None,
    ) -> tuple[list[Candidate], list[dict]]:
        """Candidates from the source's listing pages and feeds (or just the
        addresses in `only`, when catching up after a rate limit)."""
        cache_updates: list[dict] = []
        found: dict[str, Candidate] = {}
        listing_pages = [u for u in source.listing_pages if only is None or u in only]
        if only is None:
            feeds = list(source.feeds)
        else:  # may include feeds a listing page advertised
            feeds = [u for u in only if u not in source.listing_pages]

        def add(candidate: Candidate) -> None:
            if source.is_excluded(candidate.url):
                return
            existing = found.get(candidate.key)
            # Feed entries carry the most metadata; prefer them over bare links.
            if existing is None or (existing.via != "feed" and candidate.via == "feed"):
                if existing and existing.title and not candidate.title:
                    candidate.title = existing.title
                found[candidate.key] = candidate

        for page_url in listing_pages:
            try:
                # Pages that advertise feeds must be parsed every time to find them.
                page = self._conditional_fetch(
                    page_url,
                    cache_updates,
                    use_validators=use_cache and not source.autodiscover_feeds,
                )
            except RateLimited as exc:
                result.slow_down(page_url, exc)
                continue
            except FetchError as exc:
                result.errors.append(f"listing {page_url}: {exc}")
                continue
            result.reachable += 1
            if page is None:
                continue
            links, advertised = parse_listing(page.content, page.url, source)
            for candidate in links:
                add(candidate)
            for feed_url in advertised:
                if feed_url not in feeds:
                    feeds.append(feed_url)

        for feed_url in feeds:
            try:
                feed = self._conditional_fetch(feed_url, cache_updates, use_validators=use_cache)
            except RateLimited as exc:
                result.slow_down(feed_url, exc)
                continue
            except FetchError as exc:
                # Configured feeds that disappear are worth reporting; guessed
                # (auto-discovered) ones are not.
                if feed_url in source.feeds:
                    result.errors.append(f"feed {feed_url}: {exc}")
                continue
            result.reachable += 1
            if feed is None:
                continue
            for candidate in parse_feed(feed.content, feed.url):
                if same_site(candidate.url, feed_url) or same_site(
                    candidate.url, source.homepage or feed_url
                ):
                    add(candidate)
        return list(found.values()), cache_updates

    def _retry_candidates(self, source_id: int) -> list[Candidate]:
        """Previously failed URLs whose backoff has elapsed."""
        now = _now()
        with session_scope() as session:
            rows = session.scalars(
                select(SeenUrl).where(
                    SeenUrl.source_id == source_id,
                    SeenUrl.status == "failed",
                    SeenUrl.attempts < MAX_FAILED_ATTEMPTS,
                )
            ).all()
        return [
            Candidate(url=row.url, key=row.url, via="retry")
            for row in rows
            if row.last_attempt_at + timedelta(hours=2 ** (row.attempts - 1)) <= now
        ]

    def filter_new(self, session: Session, candidates: list[Candidate]) -> list[Candidate]:
        """Drop candidates already stored, rejected, or waiting for a retry."""
        unique: dict[str, Candidate] = {}
        for candidate in candidates:
            unique.setdefault(candidate.key, candidate)
        candidates = list(unique.values())
        if not candidates:
            return []
        variants = {v: c.key for c in candidates for v in url_variants(c.key)}
        known: set[str] = set()
        rows = session.execute(
            select(Article.url, Article.discovered_url).where(
                or_(Article.url.in_(list(variants)), Article.discovered_url.in_(list(variants)))
            )
        ).all()
        for url, discovered in rows:
            for value in (url, discovered):
                if value in variants:
                    known.add(variants[value])

        now = _now()
        waiting: set[str] = set()
        seen_rows = session.scalars(
            select(SeenUrl).where(SeenUrl.url.in_([c.key for c in candidates]))
        ).all()
        for row in seen_rows:
            if row.status == "rejected" or row.attempts >= MAX_FAILED_ATTEMPTS:
                waiting.add(row.url)
            elif row.last_attempt_at + timedelta(hours=2 ** (row.attempts - 1)) > now:
                waiting.add(row.url)

        fresh = [c for c in candidates if c.key not in known and c.key not in waiting]
        # Newest first; undated listing links keep their page order after dated ones.
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        order = {c.key: i for i, c in enumerate(fresh)}
        fresh.sort(key=lambda c: (c.published_at or epoch, -order[c.key]), reverse=True)
        return fresh

    # ------------------------------------------------------------ processing

    def prefilter(self, source: SourceConfig, cand: Candidate) -> None:
        """Reject from feed data alone, without fetching the page. Raises Skip."""
        excluded = source.excluded_section(*cand.tags)
        if excluded:
            raise Skip("rejected", f"excluded section: {excluded}")
        if (
            source.require_guam
            and cand.via == "feed"
            and cand.title
            and not mentions_guam(
                self.config, cand.title, cand.summary, cand.url, " ".join(cand.tags)
            )
        ):
            raise Skip("rejected", "not about Guam")

    def build_draft(self, source: SourceConfig, source_id: int | None, cand: Candidate) -> Draft:
        """Fetch and extract one article. Raises Skip when it should not be stored."""
        self.prefilter(source, cand)
        page: FetchResult | None = None
        data = None
        try:
            if not (source.feed_only and cand.via == "feed"):
                page = self.client.get(cand.url)
        except RateLimited as exc:
            raise Skip("throttled", str(exc), retry_after=exc.retry_after) from exc
        except RobotsDisallowed as exc:
            if not (cand.via == "feed" and cand.title):
                raise Skip("rejected", "disallowed by robots.txt") from exc
        except FetchError as exc:
            if exc.status in (404, 410):
                raise Skip("rejected", f"gone ({exc.status})") from exc
            if not (cand.via == "feed" and cand.title and exc.status in (401, 403, 451)):
                raise Skip("failed", str(exc)) from exc
            # Article pages blocked for bots but the feed is readable: use the feed.

        if page is not None:
            if page.content_type not in HTML_TYPES:
                raise Skip("rejected", f"not HTML ({page.content_type})")
            data = extract_article(
                page.content,
                page.url,
                body_selector=source.body_selector,
                source_name=source.name,
            )
            if cand.via in ("listing", "sitemap", "retry") and not data.is_article:
                # Pages without article metadata are still accepted when the
                # source's article_pattern vouches for the URL and the page has
                # a date and a story paragraph (section pages rarely have both).
                vouched = source.article_patterns and source.matches_article_pattern(cand.url)
                if not (vouched and data.published_at and data.intro):
                    raise Skip("rejected", "not an article page")

        final_url = normalize_url(page.url) if page else cand.key
        canonical = data.canonical_url if data and data.canonical_url else final_url
        if (
            not canonical
            or not same_site(canonical, cand.url)
            or urlsplit(canonical).path in ("", "/")  # some sites point every page at "/"
        ):
            canonical = final_url or cand.key

        title = (data.title if data else None) or cand.title
        if not title:
            raise Skip("failed", "no headline found")
        title = truncate(title, 300)

        summary = clean_feed_summary(cand.summary)
        intro = (
            (data.intro if data else None)
            or (build_intro([summary], title) if summary else None)
            or (data.description if data else None)
            or summary
        )
        if intro:
            intro = truncate(intro, 800)

        if intro and intro.strip(" .").lower() == title.strip(" .").lower():
            intro = None  # some sites repeat the headline as the description

        published = (data.published_at if data else None) or cand.published_at or _now()
        published = min(published, _now())
        if cand.via == "listing" and published < _now() - timedelta(days=source.max_age_days):
            raise Skip("rejected", f"older than {source.max_age_days} days (evergreen link)")

        section = data.section if data else None
        keyword_list = (data.keywords if data and data.keywords else cand.tags) or []
        excluded = source.excluded_section(section, *keyword_list)
        if excluded:
            raise Skip("rejected", f"excluded section: {excluded}")
        keywords = ", ".join(dict.fromkeys(k for k in keyword_list if k))[:1000] or None

        if source.require_guam and not mentions_guam(
            self.config, title, intro, canonical, keywords, section
        ):
            raise Skip("rejected", "not about Guam")

        categories = classify(
            self.config,
            title=title,
            intro=intro,
            section=section,
            keywords=keywords,
            url=canonical,
            forced=source.categories,
            only=source.only_in_topic,
        )
        image_url = (data.image_url if data else None) or cand.image_url
        if image_url and source_id is not None and self._is_generic_image(source_id, image_url):
            image_url = None

        return Draft(
            url=canonical,
            discovered_url=cand.key if cand.key != canonical else None,
            title=title,
            intro=intro,
            author=clean_byline(
                (data.author if data else None) or cand.author,
                source.name,
                data.site_name if data else None,
            ),
            section=section,
            keywords=keywords,
            categories=categories,
            image_url=image_url,
            published_at=published,
        )

    def _is_generic_image(self, source_id: int, image_url: str) -> bool:
        """A picture the source already used on two recent stories is a logo or
        placeholder, not a news photo: ignore it from now on, everywhere."""
        with session_scope() as session:
            if session.get(IgnoredImage, image_url) is not None:
                return True
            count = session.scalar(
                select(func.count())
                .select_from(Article)
                .where(
                    Article.source_id == source_id,
                    Article.published_at >= _now() - timedelta(days=60),
                    Article.image_url == image_url,
                )
            )
            if (count or 0) < 2:
                return False
            session.execute(
                pg_insert(IgnoredImage)
                .values(url=image_url, source_id=source_id)
                .on_conflict_do_nothing()
            )
            session.execute(
                update(Article)
                .where(Article.source_id == source_id, Article.image_url == image_url)
                .values(image_url=None, image_path=None, image_width=None, image_height=None)
            )
        log.info("ignoring reused image %s", image_url)
        return True

    def _process_and_save(
        self, source: SourceConfig, source_id: int, cand: Candidate, result: SourceResult
    ) -> None:
        """Fetch, extract and store one candidate. Re-raises Skip('throttled')
        when the site is rate-limiting us, so the rest of the source can wait."""
        try:
            draft = self.build_draft(source, source_id, cand)
        except Skip as skip:
            if skip.status == "throttled":
                log.warning("[%s] %s", source.slug, skip.reason)
                raise
            self._remember(source_id, cand.key, skip.status, skip.reason)
            if skip.status == "failed":
                result.failed += 1
            else:
                result.rejected += 1
            log.debug("[%s] skip %s: %s", source.slug, cand.url, skip.reason)
            return

        if draft.image_url and self.settings.store_images:
            stored = store_image(
                self.client,
                draft.image_url,
                referer=draft.url,
                media_dir=self.settings.media_dir,
                when=draft.published_at,
                max_width=self.settings.image_max_width,
                # The owner's ignore_robots choice covers the pictures the site's
                # stories point at, which may be on another host (i.redd.it).
                check_robots=not source.ignore_robots,
            )
            if stored:
                draft.image_path = stored.path
                draft.image_width = stored.width
                draft.image_height = stored.height

        with session_scope() as session:
            stmt = (
                pg_insert(Article)
                .values(
                    source_id=source_id,
                    url=draft.url,
                    discovered_url=draft.discovered_url,
                    title=draft.title,
                    intro=draft.intro,
                    author=draft.author,
                    section=draft.section,
                    keywords=draft.keywords,
                    categories=draft.categories,
                    image_url=draft.image_url,
                    image_path=draft.image_path,
                    image_width=draft.image_width,
                    image_height=draft.image_height,
                    published_at=draft.published_at,
                )
                .on_conflict_do_nothing(index_elements=[Article.url])
                .returning(Article.id)
            )
            inserted = session.execute(stmt).scalar()
            session.execute(delete(SeenUrl).where(SeenUrl.url == cand.key))
            if inserted is None:
                # Same story reached through a different URL: remember the alias
                # on the stored story if we can, otherwise in seen_urls.
                aliased = session.execute(
                    update(Article)
                    .where(Article.url == draft.url, Article.discovered_url.is_(None))
                    .values(discovered_url=cand.key)
                ).rowcount
                if not aliased and cand.key != draft.url:
                    session.add(
                        SeenUrl(
                            url=cand.key,
                            source_id=source_id,
                            status="rejected",
                            reason=f"duplicate of {draft.url}",
                        )
                    )
                result.rejected += 1
                return
        result.new_articles += 1
        log.debug("[%s] new: %s", source.slug, draft.title)

    def _remember(self, source_id: int, url: str, status: str, reason: str) -> None:
        with session_scope() as session:
            stmt = pg_insert(SeenUrl).values(
                url=url, source_id=source_id, status=status, reason=reason[:500], attempts=1
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SeenUrl.url],
                set_={
                    "status": stmt.excluded.status,
                    "reason": stmt.excluded.reason,
                    "attempts": SeenUrl.attempts + 1,
                    "last_attempt_at": func.now(),
                },
            )
            session.execute(stmt)

    def _housekeeping(self) -> None:
        now = _now()
        with session_scope() as session:
            session.execute(
                delete(ScrapeRun).where(ScrapeRun.started_at < now - timedelta(days=180))
            )
            session.execute(
                delete(SeenUrl).where(
                    SeenUrl.last_attempt_at < now - timedelta(days=90),
                )
            )

    # -------------------------------------------------------------- backfill

    def backfill(
        self, source: SourceConfig, source_id: int, since: datetime, limit: int
    ) -> SourceResult:
        """Import older articles listed in the site's XML sitemaps."""
        self.prepare(source)
        result = SourceResult(slug=source.slug)
        sitemap_urls = list(source.sitemaps)
        origin = normalize_url(source.homepage or (source.feeds + source.listing_pages)[0])
        if not sitemap_urls and origin:
            base = origin.split("/", 3)
            root = f"{base[0]}//{base[2]}"
            try:
                robots = self.client.get(root + "/robots.txt")
                sitemap_urls = sitemaps_from_robots(robots.content.decode("utf-8", "replace"))
            except FetchError:
                pass
            if not sitemap_urls:
                sitemap_urls = [
                    root + "/sitemap_index.xml",
                    root + "/sitemap.xml",
                    root + "/wp-sitemap.xml",
                    root + "/news-sitemap.xml",
                ]

        urls: dict[str, datetime | None] = {}
        queue = list(dict.fromkeys(sitemap_urls))
        fetched = 0
        while queue and fetched < 200:
            sitemap_url = queue.pop(0)
            fetched += 1
            try:
                content = self.client.get(sitemap_url).content
            except FetchError as exc:
                result.errors.append(f"sitemap {sitemap_url}: {exc}")
                continue
            entries, children = parse_sitemap(content)
            for child in children:
                # Skip child sitemaps that are clearly about years before `since`.
                years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2}|19\d{2})(?!\d)", child)]
                if years and max(years) < since.year:
                    continue
                if child not in queue:
                    queue.append(child)
            for loc, lastmod in entries:
                if lastmod and lastmod < since:
                    continue
                url = normalize_url(loc)
                if not url or source.is_excluded(url):
                    continue
                if source.article_patterns:
                    if not source.matches_article_pattern(url):
                        continue
                elif not looks_like_article_url(url):
                    continue
                urls[url] = lastmod

        candidates = [
            Candidate(url=u, key=u, via="sitemap", published_at=m) for u, m in urls.items()
        ]
        result.candidates = len(candidates)
        with session_scope() as session:
            fresh = self.filter_new(session, candidates)
        queue = list(fresh[:limit])
        throttled = 0
        while queue:
            try:
                self._process_and_save(source, source_id, queue[0], result)
            except Skip as skip:  # rate-limited: pause, then carry on with the same page
                throttled += 1
                if throttled > MAX_BACKFILL_PAUSES:
                    result.errors.append(f"still rate limited; stopped with {len(queue)} left")
                    break
                wait = min(max(self.settings.catch_up_seconds, skip.retry_after or 0), 600)
                log.info("[%s] rate limited; waiting %.0fs before continuing", source.slug, wait)
                time.sleep(wait)
                continue
            throttled = 0
            queue.pop(0)
        return result
