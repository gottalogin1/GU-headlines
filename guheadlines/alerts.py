"""Alerts to the site owner when a news site or job board stops working.

After each regular run the worker calls check_sources(). A news site or job
board whose latest check failed (the Sources page says "failed"), and that
has not worked for ALERT_AFTER_HOURS, gets one alert. When it works again,
one more alert says so. The alerts are posted to a Discord channel through
DISCORD_WEBHOOK_URL (a webhook: a secret link that lets a program post in one
channel; see .env.example).

scripts/auto-update.sh sends the alerts about the server itself: the site
not answering, the news not checked for hours, an update that failed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .db import session_scope
from .models import AlertState, JobSource, Source
from .scraper.text import GUAM_TZ
from .settings import Settings

log = logging.getLogger(__name__)

# Discord refuses longer messages.
MAX_MESSAGE = 2000
# Discord message flags: no preview cards for links; no phone notification.
SUPPRESS_EMBEDS = 1 << 2
SUPPRESS_NOTIFICATIONS = 1 << 12

WEBHOOK_PATH = re.compile(r"/api(/v\d+)?/webhooks/\d+/[\w.-]+/?")


@dataclass
class Alert:
    title: str
    message: str
    emoji: str = "⚠️"
    # Post it without a phone notification (for good news that can wait).
    quiet: bool = False


def is_webhook(url: str) -> bool:
    """https://discord.com/api/webhooks/<number>/<secret> (or discordapp.com)."""
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    return (
        parts.scheme == "https"
        and (
            host in ("discord.com", "discordapp.com")
            or host.endswith((".discord.com", ".discordapp.com"))
        )
        and WEBHOOK_PATH.fullmatch(parts.path) is not None
    )


def discord_text(alert: Alert) -> str:
    text = f"{alert.emoji} **{alert.title}**\n{alert.message}"
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def send(settings: Settings, alert: Alert, transport: httpx.BaseTransport | None = None) -> bool:
    """Post one alert. Returns False, with a warning in the log, if it can't."""
    url = settings.discord_webhook_url
    if not is_webhook(url):
        if url:
            log.warning(
                "DISCORD_WEBHOOK_URL is not a Discord webhook link like "
                "https://discord.com/api/webhooks/...; alert not sent: %s",
                alert.title,
            )
        return False
    payload = {
        "content": discord_text(alert),
        # Never notify anyone because of text in an error message ("@everyone").
        "allowed_mentions": {"parse": []},
        "flags": SUPPRESS_EMBEDS | (SUPPRESS_NOTIFICATIONS if alert.quiet else 0),
    }
    try:
        with httpx.Client(timeout=20, transport=transport) as client:
            response = client.post(
                url.strip(), json=payload, headers={"User-Agent": settings.user_agent}
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        # The link itself is a secret: say only what went wrong.
        reason = (
            f"HTTP {exc.response.status_code}"
            if isinstance(exc, httpx.HTTPStatusError)
            else type(exc).__name__
        )
        log.warning("could not send the alert %r to Discord: %s", alert.title, reason)
        return False
    log.info("alert sent: %s", alert.title)
    return True


# ------------------------------------------------------------ failing sources


@dataclass
class Failure:
    name: str
    kind: str  # "news site" or "job board"
    since: datetime | None  # when it last worked; None: never
    error: str | None


def _failed(row: Source | JobSource) -> bool:
    """The latest check went wrong (the Sources page shows "failed")."""
    return row.last_run_at is not None and (
        row.last_success_at is None or row.last_success_at < row.last_run_at
    )


def _collected(session, news_slugs: Iterable[str], board_slugs: Iterable[str]):
    """({key: name} of every source that is read, {key: Failure} of those failing)."""
    names: dict[str, str] = {}
    failures: dict[str, Failure] = {}
    for model, slugs, prefix, kind in (
        (Source, list(news_slugs), "news", "news site"),
        (JobSource, list(board_slugs), "jobs", "job board"),
    ):
        if not slugs:
            continue
        for row in session.scalars(select(model).where(model.slug.in_(slugs))):
            key = f"{prefix}:{row.slug}"
            names[key] = row.name
            if _failed(row):
                failures[key] = Failure(row.name, kind, row.last_success_at, row.last_error)
    return names, failures


def guam_time(moment: datetime) -> str:
    """Mon Nov 23, 2:05 PM (in Guam time)."""
    local = moment.astimezone(GUAM_TZ)
    hour = local.hour % 12 or 12
    return f"{local:%a %b} {local.day}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def _page(settings: Settings, path: str) -> str:
    return f"{settings.site_url}{path}" if settings.site_url else ""


def failing_alert(settings: Settings, failures: list[Failure]) -> Alert:
    if len(failures) == 1:
        title = f"{failures[0].name} has stopped working"
    else:
        title = f"{len(failures)} news sites and job boards have stopped working"
    lines = []
    for f in failures:
        since = f"since {guam_time(f.since)}" if f.since else "since it was added"
        line = f"The {f.kind} {f.name} has not worked {since} (Guam time)."
        if f.error:
            error = f.error if len(f.error) <= 300 else f.error[:299] + "…"
            line += f"\nLatest error: {error}"
        lines.append(line)
    news = any(f.kind == "news site" for f in failures)
    page = _page(settings, "/status" if news else "/jobs")
    where = "the Sources page" if news else "the Jobs page"
    lines.append(f"More on {where}: {page}" if page else f"More on {where} of the site.")
    return Alert(title=title, message="\n\n".join(lines))


def recovered_alert(settings: Settings, names: list[str], now: datetime) -> Alert:
    if len(names) == 1:
        title = f"{names[0]} works again"
        message = f"{names[0]} was read again at {guam_time(now)} (Guam time)."
    else:
        title = f"{len(names)} news sites and job boards work again"
        message = "\n".join(f"• {name}" for name in names)
        message += f"\n\nRead again at {guam_time(now)} (Guam time)."
    return Alert(title=title, message=message, emoji="✅")


def check_sources(
    settings: Settings,
    news_slugs: Iterable[str],
    board_slugs: Iterable[str],
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Send an alert for each news site or job board (of those the worker reads)
    that has not worked for ALERT_AFTER_HOURS, and one when it works again.
    Nothing is sent twice: the database remembers what was reported. An alert
    that can't be sent is tried again on the next check."""
    if not settings.discord_webhook_url:
        return
    now = now or datetime.now(timezone.utc)
    limit = timedelta(hours=settings.alert_after_hours)
    with session_scope() as session:
        names, failures = _collected(session, news_slugs, board_slugs)
        states = {state.key: state for state in session.scalars(select(AlertState))}

        newly: list[tuple[AlertState, Failure]] = []
        for key, failure in failures.items():
            state = states.get(key)
            if state is None:
                state = AlertState(key=key, failing_since=now)
                session.add(state)
            if state.notified_at is None and now - (failure.since or state.failing_since) >= limit:
                newly.append((state, failure))

        recovered: list[AlertState] = []
        for key, state in states.items():
            if key in failures:
                continue
            if state.notified_at is not None and key in names:
                recovered.append(state)
            else:  # fixed before anyone was told, or no longer read
                session.delete(state)

        if newly and send(settings, failing_alert(settings, [f for _, f in newly]), transport):
            for state, _ in newly:
                state.notified_at = now
        if recovered and send(
            settings, recovered_alert(settings, [names[s.key] for s in recovered], now), transport
        ):
            for state in recovered:
                session.delete(state)


def send_test_alert(settings: Settings, transport: httpx.BaseTransport | None = None) -> bool:
    return send(
        settings,
        Alert(
            title=f"{settings.site_name}: test alert",
            message=(
                "Alerts work. One comes when a news site or job board has not worked "
                f"for {settings.alert_after_hours} hours, and one when it works again."
            ),
            emoji="✅",
        ),
        transport,
    )
