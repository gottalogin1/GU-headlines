"""Command line entry point: `guheadlines <command>` (or `python -m guheadlines`)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select, text, update

from .config import AppConfig, ConfigError, load_config
from .db import MIGRATION_LOCK_KEY, advisory_lock, get_engine, session_scope
from .models import Article
from .settings import PACKAGE_DIR, Settings, get_settings

log = logging.getLogger("guheadlines")


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in (
        "httpx",
        "httpcore",
        "trafilatura",
        "htmldate",
        "charset_normalizer",
        "alembic.runtime.plugins",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # trafilatura warns about every page it cannot use; we have other extractors.
    logging.getLogger("trafilatura").setLevel(logging.ERROR)


def wait_for_db(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"database not reachable: {exc}") from None
            log.info("waiting for database... (%s)", type(exc).__name__)
            time.sleep(3)


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_DIR / "migrations"))
    # configparser treats % specially; passwords may contain it.
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    with advisory_lock(MIGRATION_LOCK_KEY, wait=True):
        command.upgrade(cfg, "head")


def _config(settings: Settings) -> AppConfig:
    try:
        return load_config(settings.config_dir)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from None


# ---------------------------------------------------------------- commands


def cmd_migrate(args, settings: Settings) -> None:
    wait_for_db()
    migrate()
    log.info("database schema is up to date")


def cmd_web(args, settings: Settings) -> None:
    import uvicorn

    wait_for_db()
    migrate()
    uvicorn.run(
        "guheadlines.web.app:app",
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        workers=args.workers,
        log_level=settings.log_level.lower(),
    )


def _run_scrape(
    settings: Settings,
    config: AppConfig,
    only: list[str] | None = None,
    catch_up: dict | None = None,
):
    from .scraper.pipeline import Scraper

    scraper = Scraper(settings, config)
    try:
        return scraper.run(only=only, catch_up=catch_up)
    finally:
        scraper.close()


def _merge_pending(pending: dict, results: list, catch_up: bool) -> dict:
    """Sources that still have pages left after a run (full or catch-up)."""
    by_slug = {r.slug: r for r in results}
    if not catch_up:
        return {slug: r for slug, r in by_slug.items() if r.incomplete}
    merged = {}
    for slug in pending:
        result = by_slug.get(slug)  # missing if the source was disabled meanwhile
        if result is not None and result.incomplete:
            merged[slug] = result
    return merged


def _describe_pending(pending: dict) -> str:
    return ", ".join(f"{slug} ({len(r.pending)} pages)" for slug, r in pending.items())


def _print_results(results: list) -> None:
    for r in results:
        status = "ok" if r.ok else "ERROR"
        left = f" left={len(r.pending)}" if r.pending else ""
        print(
            f"{r.slug:<12} {status:<6} candidates={r.candidates:<4} new={r.new_articles:<4} "
            f"failed={r.failed:<3} skipped={r.rejected:<3}{left} {'; '.join(r.errors)}"
        )


def cmd_scrape(args, settings: Settings) -> None:
    wait_for_db()
    migrate()
    config = _config(settings)
    only = args.source or None
    if only:
        unknown = [s for s in only if not config.source(s)]
        if unknown:
            raise SystemExit(f"unknown source(s): {', '.join(unknown)}")
    results = _run_scrape(settings, config, only)
    if results is None:
        raise SystemExit("another scrape is running")
    _print_results(results)
    if not args.until_done:
        return
    # Keep catching up, like the worker does, until every source is loaded.
    pending = _merge_pending({}, results, catch_up=False)
    rounds = 0
    while pending and rounds < args.max_rounds:
        rounds += 1
        wait = max([settings.catch_up_seconds] + [r.retry_after or 0 for r in pending.values()])
        wait = min(wait, 600)
        print(f"\ncatching up in {wait:.0f}s: {_describe_pending(pending)}", flush=True)
        time.sleep(wait)
        results = _run_scrape(settings, config, catch_up=pending)
        if results is None:
            raise SystemExit("another scrape is running")
        _print_results(results)
        pending = _merge_pending(pending, results, catch_up=True)
    if pending:
        print(f"\nstill pending after {rounds} rounds: {_describe_pending(pending)}")


def cmd_worker(args, settings: Settings) -> None:
    """Scrape every SCRAPE_INTERVAL_MINUTES, aligned to the clock (e.g. on the hour).

    If a run leaves pages behind (a site rate-limited us, or there were more new
    pages than MAX_NEW_PER_SOURCE), the worker comes back for just those sources
    every CATCH_UP_SECONDS until they are fully loaded, then returns to the
    regular schedule.
    """
    from .schedule import next_wake

    wait_for_db()
    migrate()
    stop = threading.Event()

    def handle_signal(signum, frame):
        log.info("received signal %s, stopping after the current run", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    interval = settings.scrape_interval_minutes * 60
    config = _config(settings)
    pending: dict = {}

    def run_once(catch_up: bool = False) -> None:
        nonlocal config, pending
        try:
            config = load_config(settings.config_dir)  # pick up YAML edits
        except ConfigError as exc:
            log.error("config error, using previous config: %s", exc)
        try:
            results = _run_scrape(settings, config, catch_up=pending if catch_up else None)
        except Exception:
            log.exception("scrape run failed")
            return
        if results is None:
            return  # another scrape holds the lock; keep what is pending
        pending = _merge_pending(pending, results, catch_up)
        if pending:
            log.info("not fully loaded yet: %s", _describe_pending(pending))

    log.info(
        "worker started; scraping every %d minutes (catch-up every %ds when needed)",
        settings.scrape_interval_minutes,
        settings.catch_up_seconds,
    )
    if settings.scrape_on_start:
        run_once()
    while not stop.is_set():
        delay, kind = next_wake(time.time(), interval, pending, settings.catch_up_seconds)
        when = datetime.fromtimestamp(time.time() + delay, tz=timezone.utc)
        if kind == "catch-up":
            log.info("catching up at %s UTC: %s", f"{when:%H:%M:%S}", _describe_pending(pending))
        else:
            log.info("next scrape at %s UTC", f"{when:%Y-%m-%d %H:%M}")
        if stop.wait(delay):
            break
        run_once(catch_up=kind == "catch-up")
    log.info("worker stopped")


def cmd_check(args, settings: Settings) -> None:
    """Dry run for one source: what is discovered and what would be stored."""
    from .scraper.pipeline import Scraper, Skip, SourceResult, sync_sources

    wait_for_db()
    migrate()
    config = _config(settings)
    source = config.source(args.source)
    if not source:
        raise SystemExit(f"unknown source: {args.source}")
    with session_scope() as session:
        source_id = sync_sources(session, config)[source.slug]
    scraper = Scraper(settings, config)
    try:
        scraper.prepare(source)
        result = SourceResult(slug=source.slug)
        candidates, _ = scraper.discover(source, result, use_cache=False)
        print(f"== {source.name} ({source.slug})")
        for error in result.errors:
            print(f"   ! {error}")
        by_via: dict[str, int] = {}
        for c in candidates:
            by_via[c.via] = by_via.get(c.via, 0) + 1
        print(f"   discovered {len(candidates)} article links {by_via or ''}")
        with session_scope() as session:
            fresh = scraper.filter_new(session, candidates)
        print(f"   {len(fresh)} not yet in the database")
        sample = fresh[: args.limit] if fresh else candidates[: args.limit]
        for cand in sample:
            print(f"\n-- {cand.url}  (via {cand.via})")
            try:
                draft = scraper.build_draft(source, source_id, cand)
            except Skip as skip:
                print(f"   SKIP ({skip.status}): {skip.reason}")
                continue
            print(f"   title:      {draft.title}")
            print(f"   published:  {draft.published_at:%Y-%m-%d %H:%M} UTC")
            print(f"   author:     {draft.author or '-'}")
            print(f"   categories: {', '.join(draft.categories)}")
            print(f"   image:      {draft.image_url or '-'}")
            print(f"   intro:      {(draft.intro or '-')[:400]}")
    finally:
        scraper.close()


def cmd_backfill(args, settings: Settings) -> None:
    from .scraper.pipeline import Scraper, sync_sources

    wait_for_db()
    migrate()
    config = _config(settings)
    source = config.source(args.source)
    if not source:
        raise SystemExit(f"unknown source: {args.source}")
    try:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit("--since must look like 2025-01-31") from None
    with session_scope() as session:
        source_id = sync_sources(session, config)[source.slug]
    scraper = Scraper(settings, config)
    try:
        result = scraper.backfill(source, source_id, since, args.limit)
    finally:
        scraper.close()
    print(
        f"{source.slug}: {result.candidates} sitemap URLs since {args.since}, "
        f"{result.new_articles} imported, {result.failed} failed, {result.rejected} skipped"
    )
    for error in result.errors[:10]:
        print(f"  ! {error}")


def cmd_reclassify(args, settings: Settings) -> None:
    from .models import Source
    from .scraper.classify import classify

    wait_for_db()
    config = _config(settings)
    changed = total = 0
    with session_scope() as session:
        slugs = dict(session.execute(select(Source.id, Source.slug)).all())
        rows = session.execute(
            select(
                Article.id,
                Article.source_id,
                Article.title,
                Article.intro,
                Article.section,
                Article.keywords,
                Article.url,
                Article.categories,
            ).execution_options(yield_per=500)
        )
        updates = []
        for row in rows:
            total += 1
            source = config.source(slugs.get(row.source_id, ""))
            categories = classify(
                config,
                title=row.title,
                intro=row.intro,
                section=row.section,
                keywords=row.keywords,
                url=row.url,
                forced=source.categories if source else None,
            )
            if categories != list(row.categories or []):
                updates.append((row.id, categories))
    for i in range(0, len(updates), 500):
        with session_scope() as session:
            for article_id, categories in updates[i : i + 500]:
                session.execute(
                    update(Article).where(Article.id == article_id).values(categories=categories)
                )
                changed += 1
    print(f"re-tagged {changed} of {total} articles")


def cmd_probe(args, settings: Settings) -> None:
    """Describe pages/feeds (no database needed) to help write sources.yaml."""
    from .scraper.http import HttpClient
    from .scraper.probe import probe

    urls = list(args.url or [])
    if args.source or args.all:
        config = _config(settings)
        chosen = config.sources if args.all else [config.source(s) for s in args.source]
        for source in chosen:
            if source is None:
                raise SystemExit("unknown source")
            urls += [source.homepage or ""] + source.listing_pages + source.feeds
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        raise SystemExit("give one or more URLs, --source SLUG or --all")
    with HttpClient(
        settings.user_agent,
        timeout=settings.request_timeout,
        per_host_delay=settings.per_host_delay,
        respect_robots=False,  # we want to see the page even if robots.txt says no
    ) as client:
        for url in urls:
            probe(client, url)
            if not client.robots_permits(url):
                print(f"  NOTE: robots.txt disallows this URL for {client.robots_agent}")
            print()


def cmd_sources(args, settings: Settings) -> None:
    config = _config(settings)
    counts: dict[str, int] = {}
    try:
        with session_scope() as session:
            counts = dict(
                session.execute(
                    text(
                        "SELECT s.slug, count(a.id) FROM sources s "
                        "LEFT JOIN articles a ON a.source_id = s.id GROUP BY s.slug"
                    )
                ).all()
            )
    except Exception as exc:
        print(f"(database unavailable: {type(exc).__name__})")
    for s in config.sources:
        state = "on " if s.enabled else "off"
        print(f"{s.slug:<12} {state} {counts.get(s.slug, 0):>7} articles  {s.name}")


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    setup_logging(settings)

    parser = argparse.ArgumentParser(prog="guheadlines", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create/upgrade the database schema")

    web = sub.add_parser("web", help="run the website")
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--workers", type=int, default=2)

    sub.add_parser("worker", help="run the hourly scraper loop")

    scrape = sub.add_parser("scrape", help="scrape once, now")
    scrape.add_argument("--source", action="append", help="only this source (repeatable)")
    scrape.add_argument(
        "--until-done",
        action="store_true",
        help="if pages are left (rate limit / per-run cap), catch up every CATCH_UP_SECONDS",
    )
    scrape.add_argument("--max-rounds", type=int, default=60, help="catch-up rounds limit")

    check = sub.add_parser("check", help="dry-run a source and show what would be stored")
    check.add_argument("--source", required=True)
    check.add_argument("--limit", type=int, default=3)

    backfill = sub.add_parser("backfill", help="import older articles from a site's sitemaps")
    backfill.add_argument("--source", required=True)
    backfill.add_argument("--since", required=True, help="YYYY-MM-DD")
    backfill.add_argument("--limit", type=int, default=300)

    sub.add_parser("reclassify", help="re-apply categories.yaml to all stored articles")
    sub.add_parser("sources", help="list configured sources")

    probe = sub.add_parser("probe", help="describe a page or feed (helps configure sources)")
    probe.add_argument("url", nargs="*")
    probe.add_argument("--source", action="append", help="probe this source's URLs")
    probe.add_argument("--all", action="store_true", help="probe every configured source")

    args = parser.parse_args(argv)
    handler = {
        "migrate": cmd_migrate,
        "web": cmd_web,
        "worker": cmd_worker,
        "scrape": cmd_scrape,
        "check": cmd_check,
        "backfill": cmd_backfill,
        "reclassify": cmd_reclassify,
        "sources": cmd_sources,
        "probe": cmd_probe,
    }[args.command]
    handler(args, settings)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
