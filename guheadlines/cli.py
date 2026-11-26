"""Command line entry point: `guheadlines <command>` (or `python -m guheadlines`)."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update

from .config import (
    AppConfig,
    ConfigError,
    load_bids_config,
    load_config,
    load_events_config,
    load_jobs_config,
)
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
        server_header=False,  # do not tell visitors which web server software runs the site
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
    def left(r) -> str:
        parts = [f"{len(r.pending)} pages"] if r.pending else []
        if r.pending_urls:
            parts.append(f"{len(r.pending_urls)} feeds/listings")
        return ", ".join(parts)

    return ", ".join(f"{slug} ({left(r)})" for slug, r in pending.items())


def _print_results(results: list) -> None:
    for r in results:
        status = "ok" if r.ok else "ERROR"
        left = f" left={len(r.pending)}" if r.pending else ""
        if r.pending_urls:
            left += f" feeds_left={len(r.pending_urls)}"
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
    from .bids import alerts as bid_alerts
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
    next_jobs = 0.0  # when the job boards are due (time.monotonic); 0: on the first run
    next_events = 0.0  # when the event calendars are due
    next_bids = 0.0  # when the bid lists are due

    def run_jobs_if_due() -> None:
        """Job boards change more slowly than news: read them every JOBS_INTERVAL_HOURS."""
        nonlocal next_jobs
        if time.monotonic() < next_jobs:
            return
        # A minute's slack, so a clock-aligned run at the due hour is not skipped.
        next_jobs = time.monotonic() + settings.jobs_interval_hours * 3600 - 60
        try:
            boards = load_jobs_config(settings.config_dir)
        except ConfigError as exc:
            log.error("jobs.yaml error, job boards not read: %s", exc)
            return
        if not any(b.collected for b in boards):
            return
        try:
            _run_jobs(settings, boards)
        except Exception:
            log.exception("job check failed")

    def run_events_if_due() -> None:
        """Calendars are read every EVENTS_INTERVAL_HOURS."""
        nonlocal next_events
        if time.monotonic() < next_events:
            return
        next_events = time.monotonic() + settings.events_interval_hours * 3600 - 60
        try:
            calendars = load_events_config(settings.config_dir)
        except ConfigError as exc:
            log.error("events.yaml error, calendars not read: %s", exc)
            return
        if not any(c.collected for c in calendars):
            return
        try:
            _run_events(settings, calendars)
        except Exception:
            log.exception("events check failed")

    def run_bids_if_due() -> None:
        """Bid lists are read every BIDS_INTERVAL_HOURS."""
        nonlocal next_bids
        if time.monotonic() < next_bids:
            return
        next_bids = time.monotonic() + settings.bids_interval_hours * 3600 - 60
        try:
            sources = load_bids_config(settings.config_dir)
        except ConfigError as exc:
            log.error("bids.yaml error, bid lists not read: %s", exc)
            return
        if not any(s.collected for s in sources):
            return
        try:
            _run_bids(settings, sources)
        except Exception:
            log.exception("bids check failed")
            return
        if bid_alerts.is_on(settings):
            try:
                bid_alerts.send_alerts(settings)
            except Exception:
                log.exception("bid alerts failed")

    def look_after_bid_alerts() -> None:
        """Ask PayPal about new and paying bid alert subscriptions (bids/alerts.py)."""
        if not bid_alerts.is_on(settings):
            return
        try:
            bid_alerts.look_after(settings)
        except Exception:
            log.exception("bid alerts check with PayPal failed")

    def check_alerts() -> None:
        """Alerts for sources that stopped working or went quiet."""
        if not settings.discord_webhook_url:
            return
        from .alerts import check_sources

        try:
            boards = load_jobs_config(settings.config_dir)
        except ConfigError:
            boards = []
        try:
            calendars = load_events_config(settings.config_dir)
        except ConfigError:
            calendars = []
        try:
            check_sources(
                settings,
                [s.slug for s in config.sources if s.collected],
                [b.slug for b in boards if b.collected],
                [c.slug for c in calendars if c.collected],
            )
        except Exception:
            log.exception("alert check failed")

    def check_keywords() -> None:
        """Alerts for new stories that mention the words in ALERT_KEYWORDS."""
        if not (settings.discord_webhook_url and settings.alert_keywords):
            return
        from .alerts import check_keywords as check

        try:
            check(settings, config.muted)
        except Exception:
            log.exception("keyword alert check failed")

    def look_after_photos() -> None:
        """Small copies of older photos, and the IMAGE_STORAGE_GB limit (photos.py)."""
        from .photos import tidy_photos

        try:
            report = tidy_photos(settings)
        except Exception:
            log.exception("photo tidy-up failed")
            return
        if report.changed:
            log.info("saved photos: %s", report.summary(settings.image_storage_gb))

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
    if settings.discord_webhook_url:
        log.info(
            "alerts on: a source that has not worked for %d hours, or a news site that went "
            "quiet, is reported",
            settings.alert_after_hours,
        )
        if settings.alert_keywords:
            log.info("keyword alerts on for: %s", ", ".join(settings.alert_keywords))
    if bid_alerts.is_on(settings):
        log.info(
            "bid alerts on: %s a month through PayPal (%s)",
            bid_alerts.price_text(settings.bid_alerts_price_cents),
            settings.paypal_mode,
        )
    if settings.weather_interval_minutes:
        threading.Thread(
            target=_weather_loop, args=(settings, stop), name="weather", daemon=True
        ).start()
    if settings.outages_interval_minutes:
        threading.Thread(
            target=_outages_loop, args=(settings, stop), name="outages", daemon=True
        ).start()
    if settings.newsletter_days:
        from .newsletter import schedule_text

        log.info(
            "newsletter feed on: a new edition every %s (Guam time)",
            schedule_text(settings.newsletter_days, settings.newsletter_time),
        )
        threading.Thread(
            target=_newsletter_loop, args=(settings, stop), name="newsletter", daemon=True
        ).start()
    from . import reports

    if reports.is_on(settings):
        log.info("reports on: %s", reports.describe(settings))
        threading.Thread(
            target=_reports_loop, args=(settings, stop), name="reports", daemon=True
        ).start()
    if settings.scrape_on_start:
        run_once()
        check_keywords()
        run_jobs_if_due()
        run_events_if_due()
        run_bids_if_due()
        look_after_bid_alerts()
        check_alerts()
        look_after_photos()
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
        check_keywords()
        if kind == "regular":
            run_jobs_if_due()
            run_events_if_due()
            run_bids_if_due()
            look_after_bid_alerts()
            check_alerts()
            look_after_photos()
    log.info("worker stopped")


def _weather_loop(settings: Settings, stop: threading.Event) -> None:
    """Weather changes faster than the news, and a typhoon warning must not
    wait for the hourly check, so the weather has a thread of its own."""
    from .weather import check_weather

    while True:
        try:
            check_weather(settings)
        except Exception:
            log.exception("weather check failed")
        if stop.wait(settings.weather_interval_minutes * 60):
            return


def _outages_loop(settings: Settings, stop: threading.Event) -> None:
    """Outage notices are posted the same day an outage starts, so they are
    read every few minutes in a thread of their own (see outages.py)."""
    from .outages import check_outages

    while True:
        try:
            check_outages(settings)
        except Exception:
            log.exception("outage check failed")
        if stop.wait(settings.outages_interval_minutes * 60):
            return


def _newsletter_loop(settings: Settings, stop: threading.Event) -> None:
    """Makes each newsletter edition on time (see newsletter.py), also while
    a news check is running. At the start, it makes the newest edition that
    is due, so the feed has stories at once."""
    from .newsletter import form_edition, next_edition_time

    while True:
        now = datetime.now(timezone.utc)
        try:
            made = form_edition(settings, load_config(settings.config_dir), now)
        except Exception:
            log.exception("newsletter edition failed; trying again in 5 minutes")
            wait = 300.0
        else:
            if made:
                log.info(
                    "newsletter edition of %s made: %d stories",
                    f"{made[0]:%a %b %d %H:%M}",
                    made[1],
                )
            due = next_edition_time(now, settings.newsletter_days, settings.newsletter_time)
            wait = (due - datetime.now(timezone.utc)).total_seconds() + 1
        if stop.wait(max(wait, 1)):
            return


def _reports_loop(settings: Settings, stop: threading.Event) -> None:
    """Posts the money summary and the weekly report in Discord on time (see
    reports.py), also while a news check is running. A report that could not
    be posted is tried again 10 minutes later."""
    from .reports import next_report_time, send_due

    while True:
        try:
            for key in send_due(settings):
                log.info("%s posted in Discord", key)
        except Exception:
            log.exception("report failed; trying again in 10 minutes")
        now = datetime.now(timezone.utc)
        due = next_report_time(settings, now)
        wait = (due - now).total_seconds() + 1 if due else 600.0
        if stop.wait(min(max(wait, 1.0), 600.0)):
            return


def cmd_report(args, settings: Settings) -> None:
    """Post the money summary of yesterday, or the report of the last week,
    in Discord now (or only show it with --show)."""
    from . import reports
    from .alerts import discord_text, send

    wait_for_db()
    migrate()
    now = datetime.now(timezone.utc)
    if args.which == "daily":
        alert = reports.daily_alert(settings, now)
    else:
        day = settings.weekly_report_day if settings.weekly_report_day is not None else 0
        due = reports.edition_time(now, (day,), settings.report_time) or now
        alert = reports.weekly_alert(settings, due)
    if args.show or not settings.discord_webhook_url:
        print(discord_text(alert))
        if not settings.discord_webhook_url:
            print("\n(DISCORD_WEBHOOK_URL is empty, so it was only shown here.)")
        return
    if not send(settings, alert):
        raise SystemExit("the report could not be posted; see the message above")
    print("Posted in your Discord channel. It shows within a few seconds.")


def cmd_weather(args, settings: Settings) -> None:
    """Read the weather and the tides now and save them (the worker does
    this every WEATHER_INTERVAL_MINUTES, and every few hours for the tides)."""
    from .weather import check_weather

    wait_for_db()
    migrate()
    for kind, data, error in check_weather(settings, force_tides=True):
        shown = f"ERROR: {error}" if error else json.dumps(data, ensure_ascii=False)[:300]
        print(f"{kind:<11} {shown}")


def cmd_outages(args, settings: Settings) -> None:
    """Read the power and water outage notices now and save them (the worker
    does this every OUTAGES_INTERVAL_MINUTES)."""
    from .outages import check_outages

    wait_for_db()
    migrate()
    for name, notices, error in check_outages(settings):
        if error:
            print(f"{name:<4} ERROR: {error}")
            continue
        print(f"{name:<4} {len(notices)} notices")
        for notice in notices[:5]:
            print(f"     {notice.get('day') or '?':<10} {notice['title'][:90]}")


def cmd_photos(args, settings: Settings) -> None:
    """Show the space the saved photos use, and tidy them up now (the worker
    does this after each news check; see photos.py)."""
    from .photos import tidy_photos

    wait_for_db()
    migrate()
    report = tidy_photos(settings, max_small=None)
    limit = f"{settings.image_storage_gb:g} GB" if settings.image_storage_gb else "no limit"
    print(f"Saved photos: {report.files:,} files, {report.size}")
    print(f"Space allowed (IMAGE_STORAGE_GB): {limit}")
    print(f"Small copies made just now: {report.small_made:,}")
    print(f"Unused files deleted just now: {report.unused_deleted:,}")
    print(f"Oldest stories whose photos were deleted just now: {report.stories_pruned:,}")


def cmd_check(args, settings: Settings) -> None:
    """Dry run for one source: what is discovered and what would be stored."""
    from .scraper.pipeline import Scraper, Skip, SourceResult, sync_sources

    wait_for_db()
    migrate()
    config = _config(settings)
    source = config.source(args.source)
    if not source:
        raise SystemExit(f"unknown source: {args.source}")
    if source.cannot_scrape:
        print(
            f"note: {source.slug} is marked cannot_scrape ({source.cannot_scrape}); the worker "
            "never reads it. This check tries it anyway, to see whether it works again."
        )
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
                only=source.only_in_topic if source else None,
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


def _jobs_config(settings: Settings):
    try:
        return load_jobs_config(settings.config_dir)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from None


def _run_jobs(settings: Settings, boards, only: list[str] | None = None):
    from .jobs.pipeline import JobScraper

    scraper = JobScraper(settings, boards)
    try:
        return scraper.run(only=only)
    finally:
        scraper.close()


def cmd_jobs(args, settings: Settings) -> None:
    """Read the job boards in config/jobs.yaml now (or, with --check, only show
    what each board gives, without a database)."""
    boards = _jobs_config(settings)
    known = {b.slug for b in boards}
    for slug in args.source or []:
        if slug not in known:
            raise SystemExit(f"unknown job board {slug!r}; see config/jobs.yaml")
    chosen = [b for b in boards if not args.source or b.slug in args.source]
    if not args.check:
        wait_for_db()
        migrate()
        results = _run_jobs(settings, boards, only=args.source)
        if results is None:
            raise SystemExit("another job check is running")
        for r in results:
            note = f"  ERROR: {r.error}" if r.error else ""
            print(
                f"{r.slug:<18} {r.found:>4} postings  {r.new:>4} new  "
                f"{r.removed:>4} removed  {r.details:>4} pages read{note}"
            )
        return

    from .jobs.parsers import BoardError
    from .jobs.pipeline import JobScraper
    from .scraper.http import FetchError

    scraper = JobScraper(settings, boards)
    try:
        for board in chosen:
            print(f"### {board.slug}: {board.name}")
            if board.cannot_scrape:
                print(f"  not collected: {board.cannot_scrape}\n")
                continue
            if not board.enabled:
                print("  paused (enabled: false)\n")
                continue
            try:
                postings = scraper.collect(board)
            except (FetchError, BoardError) as exc:
                print(f"  ERROR: {exc}\n")
                continue
            print(f"  {len(postings)} postings")
            wanted = [p for p in postings if p.detail_url][: args.details]
            scraper.read_details(board, wanted)
            for p in postings[: args.limit]:
                print(f"  - {p.title}\n    {p.url}")
                for name in ("employer", "location", "pay", "open_date", "close_date"):
                    print(f"    {name}: {getattr(p, name) or '-'}")
                if p.close_note:
                    print(f"    close_note: {p.close_note}")
                if p.pdf_url and p.pdf_url != p.url:
                    print(f"    pdf: {p.pdf_url}")
                print(f"    description: {(p.description or '-')[:240]}")
            print()
    finally:
        scraper.close()


def _events_config(settings: Settings):
    try:
        return load_events_config(settings.config_dir)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from None


def _run_events(settings: Settings, calendars, only: list[str] | None = None):
    from .events.pipeline import EventChecker

    checker = EventChecker(settings, calendars)
    try:
        return checker.run(only=only)
    finally:
        checker.close()


def cmd_events(args, settings: Settings) -> None:
    """Read the calendars in config/events.yaml now (or, with --check, only show
    what each calendar gives, without a database)."""
    calendars = _events_config(settings)
    known = {c.slug for c in calendars}
    for slug in args.source or []:
        if slug not in known:
            raise SystemExit(f"unknown calendar {slug!r}; see config/events.yaml")
    if not args.check:
        wait_for_db()
        migrate()
        results = _run_events(settings, calendars, only=args.source)
        if results is None:
            raise SystemExit("another events check is running")
        for r in results:
            note = f"  ERROR: {r.error}" if r.error else ""
            counts = f"{r.found:>4} upcoming  {r.new:>4} new  {r.removed:>4} removed"
            print(f"{r.slug:<18} {counts}{note}")
        return

    from .events.ical import CalendarError
    from .events.pipeline import EventChecker
    from .scraper.http import FetchError
    from .scraper.text import GUAM_TZ

    checker = EventChecker(settings, calendars)
    try:
        for calendar in calendars:
            if args.source and calendar.slug not in args.source:
                continue
            print(f"### {calendar.slug}: {calendar.name}")
            if calendar.cannot_scrape:
                print(f"  not collected: {calendar.cannot_scrape}\n")
                continue
            if not calendar.enabled:
                print("  paused (enabled: false)\n")
                continue
            try:
                events, errors = checker.collect(calendar)
            except (FetchError, CalendarError) as exc:
                print(f"  ERROR: {exc}\n")
                continue
            for error in errors:
                print(f"  ERROR (other inputs still read): {error}")
            print(f"  {len(events)} upcoming events")
            for e in events[: args.limit]:
                local = e.starts_at.astimezone(GUAM_TZ)
                when = f"{local:%a %Y-%m-%d}" + ("" if e.all_day else f" {local:%H:%M}")
                if e.ends_at:
                    ends = e.ends_at.astimezone(GUAM_TZ)
                    if e.all_day:
                        ends -= timedelta(days=1)  # the end of an all-day event is the next day
                    if ends.date() != local.date():
                        when += f" to {ends:%a %Y-%m-%d}"
                    if not e.all_day:
                        when += f"{' ' if ends.date() != local.date() else ' to '}{ends:%H:%M}"
                print(f"  - {when}  {e.title}")
                for name in ("location", "organizer", "url", "agenda_url"):
                    if getattr(e, name):
                        print(f"    {name}: {getattr(e, name)}")
                if e.description:
                    print(f"    description: {e.description[:200]!r}")
            print()
    finally:
        checker.close()


def _bids_config(settings: Settings):
    try:
        return load_bids_config(settings.config_dir)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from None


def _run_bids(settings: Settings, sources, only: list[str] | None = None):
    from .bids.pipeline import BidChecker

    checker = BidChecker(settings, sources)
    try:
        return checker.run(only=only)
    finally:
        checker.close()


def cmd_bids(args, settings: Settings) -> None:
    """Read the bid lists in config/bids.yaml now (or, with --check, only show
    what each source gives, without a database)."""
    sources = _bids_config(settings)
    known = {s.slug for s in sources}
    for slug in args.source or []:
        if slug not in known:
            raise SystemExit(f"unknown bid source {slug!r}; see config/bids.yaml")
    if not args.check:
        wait_for_db()
        migrate()
        results = _run_bids(settings, sources, only=args.source)
        if results is None:
            raise SystemExit("another bids check is running")
        for r in results:
            note = f"  ERROR: {r.error}" if r.error else ""
            counts = f"{r.found:>4} open  {r.new:>4} new  {r.removed:>4} removed"
            print(f"{r.slug:<18} {counts}{note}")
        return

    from .bids.pipeline import BidChecker
    from .bids.readers import BidError
    from .scraper.http import FetchError
    from .scraper.text import GUAM_TZ

    checker = BidChecker(settings, sources)
    try:
        for source in sources:
            if args.source and source.slug not in args.source:
                continue
            print(f"### {source.slug}: {source.name}")
            if source.cannot_scrape:
                print(f"  not collected: {source.cannot_scrape}\n")
                continue
            if not source.enabled:
                print("  paused (enabled: false)\n")
                continue
            try:
                bids, errors, _ = checker.collect(source)
            except (FetchError, BidError) as exc:
                print(f"  ERROR: {exc}\n")
                continue
            for error in errors:
                print(f"  ERROR (other inputs still read): {error}")
            print(f"  {len(bids)} open bids")
            for b in bids[: args.limit]:
                if b.due_at:
                    due = b.due_at.astimezone(GUAM_TZ)
                    when = f"due {due:%a %Y-%m-%d}" + (f" {due:%H:%M}" if b.due_time else "")
                else:
                    when = "no due date"
                print(f"  - {when}  {b.number or '(no number)'}  {b.title}")
                for name in ("kind", "agency", "posted_on", "url", "document_url"):
                    if getattr(b, name):
                        print(f"    {name}: {getattr(b, name)}")
            print()
    finally:
        checker.close()


def cmd_probe(args, settings: Settings) -> None:
    """Describe pages/feeds (no database needed) to help write sources.yaml."""
    from .scraper.http import HttpClient
    from .scraper.probe import outline, probe

    urls = list(args.url or [])
    if args.source or args.all:
        config = _config(settings)
        if args.all:  # sites that can't be scraped are left alone
            chosen = [s for s in config.sources if not s.cannot_scrape]
        else:
            chosen = [config.source(s) for s in args.source]
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
            if args.outline:
                outline(client, url)
                print()
                continue
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
        state = "can't" if s.cannot_scrape else ("on" if s.enabled else "off")
        print(f"{s.slug:<12} {state:<5} {counts.get(s.slug, 0):>7} articles  {s.name}")


def cmd_alert_test(args, settings: Settings) -> None:
    """Post a test alert to the Discord channel of DISCORD_WEBHOOK_URL."""
    from .alerts import send_test_alert

    if not settings.discord_webhook_url:
        raise SystemExit(
            "DISCORD_WEBHOOK_URL is empty, so alerts are off. Set it in .env (see .env.example), "
            "then run: docker compose up -d"
        )
    if not send_test_alert(settings):
        raise SystemExit("the test alert could not be sent; see the message above")
    print("Test alert sent. It shows in your Discord channel within a few seconds.")


def cmd_sendfox_check(args, settings: Settings) -> None:
    """Show the SendFox account of SENDFOX_API_TOKEN, its lists, and the list
    that sign-ups from the website join."""
    from . import sendfox

    if not settings.sendfox_api_token:
        raise SystemExit(
            "SENDFOX_API_TOKEN is empty, so the newsletter sign-up box is off. Set it in .env "
            "(see .env.example), then run: docker compose up -d"
        )
    try:
        me = sendfox.account(settings)
        found = sendfox.lists(settings)
    except sendfox.SignupError as exc:
        raise SystemExit(f"SendFox did not answer as expected: {exc}") from None
    print(f"SendFox account: {me.get('email') or me.get('name') or '(no email shown)'}")
    print("Lists:" if found else "Lists: none yet. Make one in SendFox (Audience > Lists).")
    for item in found:
        print(f"  {item.id:>10}  {item.name}")
    try:
        picked = sendfox.pick_list(settings.sendfox_list, found)
    except sendfox.SignupError as exc:
        raise SystemExit(f"Problem: {exc}") from None
    if picked is None:
        print("Sign-ups join no list (the account has none), only your contacts.")
    else:
        print(f'Sign-ups from the website join the list "{picked.name}" ({picked.id}).')


def cmd_paypal_check(args, settings: Settings) -> None:
    """Check that PayPal accepts PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET."""
    from . import paypal

    if not paypal.enabled(settings):
        raise SystemExit(
            "PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET is empty, so the site takes no payments. "
            "Set both in .env (see .env.example), then run: docker compose up -d"
        )
    try:
        paypal.access_token(settings)
    except paypal.PayPalError as exc:
        raise SystemExit(f"Problem: {exc}") from None
    mode = "Live (real payments)" if settings.paypal_mode == "live" else "Sandbox (test payments)"
    print(f"OK: PayPal accepts PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET. Mode: {mode}.")


def cmd_mail_test(args, settings: Settings) -> None:
    """Send a test email to the address given, through the SMTP settings."""
    from . import mail

    if not mail.enabled(settings):
        raise SystemExit(
            "SMTP_HOST or MAIL_FROM is empty, so the site sends no email. Set them in .env "
            "(see .env.example), then run: docker compose up -d"
        )
    try:
        mail.send(
            settings,
            args.to,
            f"{settings.site_name}: test email",
            "Email works. The site can send bid alert emails from this address.\n",
        )
    except mail.MailError as exc:
        raise SystemExit(f"Problem: {exc}") from None
    print(f"Test email sent to {args.to} from {settings.mail_from}. Look in that inbox (and in")
    print("its spam folder) in a minute.")


def cmd_bid_alerts(args, settings: Settings) -> None:
    """Show whether bid alerts are on and why not, and list the subscribers.
    --check asks PayPal about the subscriptions now; --send sends the alert
    emails for new bids now."""
    from . import mail, paypal
    from .bids import alerts as bid_alerts

    missing = []
    if settings.bid_alerts_price_cents <= 0:
        missing.append("BID_ALERTS_PRICE (the monthly price)")
    if not paypal.enabled(settings):
        missing.append("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET")
    if not mail.enabled(settings):
        missing.append("SMTP_HOST and MAIL_FROM (sending email)")
    if not settings.site_url:
        missing.append("SITE_DOMAIN or SITE_URL (the site's address, for the links)")
    if missing:
        print("Bid alerts are off. Still to set in .env:")
        for item in missing:
            print(f"  - {item}")
        print("Then run: docker compose up -d")
        return
    wait_for_db()
    migrate()
    price = bid_alerts.price_text(settings.bid_alerts_price_cents)
    print(f"Bid alerts are on: {price} a month, PayPal {settings.paypal_mode}.")
    print(f"Sign-up page: {settings.site_url}/bids/alerts")
    if args.check:
        bid_alerts.look_after(settings)
        print("Asked PayPal about the subscriptions.")
    if args.send:
        print(f"Alert emails sent: {bid_alerts.send_alerts(settings)}")
    from sqlalchemy import select

    from .db import session_scope
    from .models import BidAlertSubscriber

    with session_scope() as session:
        subs = session.scalars(select(BidAlertSubscriber).order_by(BidAlertSubscriber.id)).all()
    if not subs:
        print("No subscribers yet.")
        return
    print(f"{'status':<10} {'paid until':<12} {'email':<34} alerts for")
    for sub in subs:
        until = f"{sub.paid_until:%Y-%m-%d}" if sub.paid_until else "-"
        what = bid_alerts.choices_text(sub.trades, sub.keywords)
        print(f"{sub.status:<10} {until:<12} {sub.email:<34} {what}")


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

    jobs = sub.add_parser("jobs", help="read the job boards in config/jobs.yaml now")
    jobs.add_argument("--source", action="append", help="only this board (repeatable)")
    jobs.add_argument(
        "--check", action="store_true", help="only show what each board gives; save nothing"
    )
    jobs.add_argument("--limit", type=int, default=4, help="with --check: postings to show")
    jobs.add_argument(
        "--details", type=int, default=2, help="with --check: posting pages to read per board"
    )

    events = sub.add_parser("events", help="read the event calendars in config/events.yaml now")
    events.add_argument("--source", action="append", help="only this calendar (repeatable)")
    events.add_argument(
        "--check", action="store_true", help="only show what each calendar gives; save nothing"
    )
    events.add_argument("--limit", type=int, default=6, help="with --check: events to show")

    bids = sub.add_parser("bids", help="read the bid and proposal lists in config/bids.yaml now")
    bids.add_argument("--source", action="append", help="only this source (repeatable)")
    bids.add_argument(
        "--check", action="store_true", help="only show what each source gives; save nothing"
    )
    bids.add_argument("--limit", type=int, default=8, help="with --check: bids to show")

    sub.add_parser("alert-test", help="post a test alert to your Discord channel")
    report = sub.add_parser("report", help="post a report in your Discord channel now")
    report.add_argument(
        "which", choices=["daily", "weekly"], help="daily: yesterday's money; weekly: last week"
    )
    report.add_argument(
        "--show", action="store_true", help="only show it here; post nothing in Discord"
    )
    sub.add_parser("sendfox-check", help="show the SendFox lists, and the one that sign-ups join")
    sub.add_parser("paypal-check", help="check that PayPal accepts the PayPal settings")
    mail_test = sub.add_parser("mail-test", help="send a test email through the SMTP settings")
    mail_test.add_argument("to", help="the email address to send it to")
    alerts = sub.add_parser("bid-alerts", help="show the bid alert subscribers")
    alerts.add_argument("--check", action="store_true", help="ask PayPal about them now")
    alerts.add_argument("--send", action="store_true", help="send alerts for new bids now")
    sub.add_parser("weather", help="read the weather, the storm list and the tides now")
    sub.add_parser("outages", help="read the power and water outage notices now")
    sub.add_parser("photos", help="show the space the saved photos use, and tidy them up now")
    sub.add_parser("reclassify", help="re-apply categories.yaml to all stored articles")
    sub.add_parser("sources", help="list configured sources")

    probe = sub.add_parser("probe", help="describe a page or feed (helps configure sources)")
    probe.add_argument("url", nargs="*")
    probe.add_argument("--source", action="append", help="probe this source's URLs")
    probe.add_argument("--all", action="store_true", help="probe every configured source")
    probe.add_argument(
        "--outline",
        action="store_true",
        help="show the page's structure (tables, repeated blocks, links) instead",
    )

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
        "jobs": cmd_jobs,
        "events": cmd_events,
        "bids": cmd_bids,
        "alert-test": cmd_alert_test,
        "report": cmd_report,
        "sendfox-check": cmd_sendfox_check,
        "paypal-check": cmd_paypal_check,
        "mail-test": cmd_mail_test,
        "bid-alerts": cmd_bid_alerts,
        "weather": cmd_weather,
        "outages": cmd_outages,
        "photos": cmd_photos,
    }[args.command]
    handler(args, settings)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
