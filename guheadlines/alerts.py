"""Alerts to the site owner: a source that stops working or goes quiet, and
new stories that mention the words in ALERT_KEYWORDS.

After each regular run the worker calls check_sources(). A news site, job
board or event calendar whose latest check failed (the Sources page says
"failed"), and that has not worked for ALERT_AFTER_HOURS, gets one alert.
When it works again, one more alert says so. A news site that has gone quiet
(see health.py) gets one alert too, and one more when it has new stories
again.

After each run, also the quick catch-up runs, the worker calls
check_keywords(): one alert lists the new stories whose headline or intro
mentions one of the words. A story is never in two of these alerts.

The alerts are posted to a Discord channel through DISCORD_WEBHOOK_URL (a
webhook: a secret link that lets a program post in one channel; see
.env.example).

scripts/auto-update.sh sends the alerts about the server itself: the site
not answering, the news not checked for hours, an update that failed.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, exists, select

from .db import session_scope
from .health import QUIET, WORKING, NewsHealth, duration, failed, news_health
from .models import AlertState, Article, EventCalendar, JobSource, KeywordAlert, Source
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

# AlertState keys: "<prefix>:<slug>" for a failing source (the prefix says
# which kind), "quiet:<slug>" for a news site that went quiet.
KINDS = {"news": "news site", "jobs": "job board", "events": "event calendar"}
QUIET_KEY = "quiet:"


@dataclass
class Failure:
    name: str
    kind: str  # "news site", "job board" or "event calendar"
    since: datetime | None  # when it last worked; None: never
    error: str | None


def _collected(session, news_slugs, board_slugs, calendar_slugs):
    """({key: name} of every source that is read, {key: Failure} of those failing)."""
    names: dict[str, str] = {}
    failures: dict[str, Failure] = {}
    for model, slugs, prefix in (
        (Source, list(news_slugs), "news"),
        (JobSource, list(board_slugs), "jobs"),
        (EventCalendar, list(calendar_slugs), "events"),
    ):
        if not slugs:
            continue
        for row in session.scalars(select(model).where(model.slug.in_(slugs))):
            key = f"{prefix}:{row.slug}"
            names[key] = row.name
            if failed(row):
                failures[key] = Failure(
                    row.name, KINDS[prefix], row.last_success_at, row.last_error
                )
    return names, failures


def guam_time(moment: datetime) -> str:
    """Mon Nov 23, 2:05 PM (in Guam time)."""
    local = moment.astimezone(GUAM_TZ)
    hour = local.hour % 12 or 12
    return f"{local:%a %b} {local.day}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def _page(settings: Settings, path: str) -> str:
    return f"{settings.site_url}{path}" if settings.site_url else ""


def _kinds(kinds: Iterable[str]) -> str:
    """ "news sites", "news sites and job boards", "news sites, job boards and
    event calendars" (in that order)."""
    present = set(kinds)
    words = [f"{kind}s" for kind in KINDS.values() if kind in present]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def failing_alert(settings: Settings, failures: list[Failure]) -> Alert:
    if len(failures) == 1:
        title = f"{failures[0].name} has stopped working"
    else:
        title = f"{len(failures)} {_kinds(f.kind for f in failures)} have stopped working"
    lines = []
    for f in failures:
        since = f"since {guam_time(f.since)}" if f.since else "since it was added"
        line = f"The {f.kind} {f.name} has not worked {since} (Guam time)."
        if f.error:
            error = f.error if len(f.error) <= 300 else f.error[:299] + "…"
            line += f"\nLatest error: {error}"
        lines.append(line)
    kinds = {f.kind for f in failures}
    if "news site" in kinds:
        path, where = "/status", "the Sources page"
    elif "job board" in kinds:
        path, where = "/jobs", "the Jobs page"
    else:
        path, where = "/events", "the Events page"
    page = _page(settings, path)
    lines.append(f"More on {where}: {page}" if page else f"More on {where} of the site.")
    return Alert(title=title, message="\n\n".join(lines))


def recovered_alert(settings: Settings, names: list[tuple[str, str]], now: datetime) -> Alert:
    """names: (name, kind) of each source that works again."""
    if len(names) == 1:
        title = f"{names[0][0]} works again"
        message = f"{names[0][0]} was read again at {guam_time(now)} (Guam time)."
    else:
        title = f"{len(names)} {_kinds(kind for _, kind in names)} work again"
        message = "\n".join(f"• {name}" for name, _ in names)
        message += f"\n\nRead again at {guam_time(now)} (Guam time)."
    return Alert(title=title, message=message, emoji="✅")


# ---------------------------------------------------------- quiet news sites


def _health_page(settings: Settings) -> str:
    """Where the owner can see more: the stats page while it is on (it has the
    source health), else the Sources page."""
    if settings.stats_password:
        page, where = _page(settings, "/stats#health"), "the stats page"
    else:
        page, where = _page(settings, "/status"), "the Sources page"
    return f"More on {where}: {page}" if page else f"More on {where} of the site."


def quiet_alert(settings: Settings, quiet: list[NewsHealth]) -> Alert:
    if len(quiet) == 1:
        title = f"{quiet[0].name} has gone quiet"
    else:
        title = f"{len(quiet)} news sites have gone quiet"
    lines = []
    for h in quiet:
        if h.newest:
            line = (
                f"{h.name} still answers, but has had no new story since "
                f"{guam_time(h.newest)} (Guam time)."
            )
            if h.longest_wait:
                line += (
                    " In the two months before, its longest wait between two stories "
                    f"was {duration(h.longest_wait)}."
                )
        else:
            line = f"{h.name} still answers, but no story has been found on it since it was added."
        lines.append(line)
    lines.append(
        "Its feed or pages may have changed, so that the stories are no longer found, "
        "or it may have stopped posting."
        if len(quiet) == 1
        else "Their feeds or pages may have changed, so that the stories are no longer "
        "found, or they may have stopped posting."
    )
    lines.append(_health_page(settings))
    return Alert(title=title, message="\n\n".join(lines))


def lively_alert(back: list[NewsHealth]) -> Alert:
    if len(back) == 1:
        h = back[0]
        title = f"{h.name} has new stories again"
        message = f"Newest story: {guam_time(h.newest)} (Guam time)." if h.newest else ""
    else:
        title = f"{len(back)} news sites have new stories again"
        message = "\n".join(f"• {h.name}" for h in back)
    return Alert(title=title, message=message, emoji="✅")


def _check_quiet(
    settings: Settings,
    session,
    news_slugs: list[str],
    states: dict[str, AlertState],
    now: datetime,
    transport: httpx.BaseTransport | None,
) -> None:
    """One alert for the news sites that went quiet, one for those that have
    new stories again. `states`: the quiet sites already reported."""
    health = {f"{QUIET_KEY}{h.slug}": h for h in news_health(session, news_slugs, now)}
    gone_quiet = [h for key, h in health.items() if h.status == QUIET and key not in states]
    back: list[tuple[AlertState, NewsHealth]] = []
    for key, state in states.items():
        h = health.get(key)
        if h is None:  # no longer read
            session.delete(state)
        elif h.status == WORKING:
            back.append((state, h))
        # Still quiet, or failing now (that has an alert of its own): wait.

    if gone_quiet and send(settings, quiet_alert(settings, gone_quiet), transport):
        for h in gone_quiet:
            session.add(
                AlertState(
                    key=f"{QUIET_KEY}{h.slug}", failing_since=h.quiet_since or now, notified_at=now
                )
            )
    if back and send(settings, lively_alert([h for _, h in back]), transport):
        for state, _ in back:
            session.delete(state)


def check_sources(
    settings: Settings,
    news_slugs: Iterable[str],
    board_slugs: Iterable[str],
    calendar_slugs: Iterable[str] = (),
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Send an alert for each news site, job board or event calendar (of those
    the worker reads) that has not worked for ALERT_AFTER_HOURS, and one when
    it works again; and one for each news site that went quiet, and one when
    it has new stories again. Nothing is sent twice: the database remembers
    what was reported. An alert that can't be sent is tried again on the next
    check."""
    if not settings.discord_webhook_url:
        return
    now = now or datetime.now(timezone.utc)
    news_slugs = list(news_slugs)
    limit = timedelta(hours=settings.alert_after_hours)
    with session_scope() as session:
        names, failures = _collected(session, news_slugs, board_slugs, calendar_slugs)
        states = {state.key: state for state in session.scalars(select(AlertState))}
        quiet_states = {k: s for k, s in states.items() if k.startswith(QUIET_KEY)}
        states = {k: s for k, s in states.items() if k not in quiet_states}

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
            settings,
            recovered_alert(
                settings,
                [(names[s.key], KINDS[s.key.partition(":")[0]]) for s in recovered],
                now,
            ),
            transport,
        ):
            for state in recovered:
                session.delete(state)

        _check_quiet(settings, session, news_slugs, quiet_states, now, transport)


# ------------------------------------------------------------ keyword alerts

# Only stories first found in the last 6 hours are looked at (so an alert
# that could not be sent is tried again for that long), and only stories
# published in the last 2 days (a site that is added brings older stories).
KEYWORD_LOOKBACK = timedelta(hours=6)
KEYWORD_FRESH = timedelta(days=2)
# Discord refuses longer messages; stories that don't fit are counted at the end.
KEYWORD_ROOM = MAX_MESSAGE - 200


def fold(text: str) -> str:
    """Lower case, without accents: "Hagåtña" -> "hagatna"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def keyword_patterns(words: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    """(word, pattern) for each word or phrase of ALERT_KEYWORDS. A pattern
    finds the whole word, in any case and with or without accents, also with
    "s" or "es" after it ("shooting" finds "shootings"). A word that ends in
    "*" finds every word that starts with it ("evacuat*" finds "evacuation").
    The pattern is meant for fold()ed text."""
    patterns = []
    for word in words:
        folded = fold(word).strip()
        starts_with = folded.endswith("*")
        parts = folded.rstrip("*").split()
        if not parts:
            continue
        body = r"\s+".join(re.escape(part) for part in parts)
        ending = r"\w*" if starts_with else r"(?:s|es)?(?!\w)"
        patterns.append((word, re.compile(rf"(?<!\w){body}{ending}")))
    return patterns


def _said(words: list[str]) -> str:
    """typhoon / typhoon and shooting / typhoon, shooting and fire"""
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


_MARKDOWN = re.compile(r"([\\*_~`|])")


def _plain(text: str) -> str:
    """A headline as plain text in Discord (no bold, strike-through...)."""
    text = _MARKDOWN.sub(r"\\\1", " ".join(text.split()))
    # A line that starts with these is a heading, a quote or a list item.
    return "\\" + text if text and text[0] in "#>-" else text


@dataclass
class Mention:
    title: str
    source: str
    url: str
    words: list[str]


def keyword_alert(mentions: list[Mention]) -> Alert:
    words = list(dict.fromkeys(w for m in mentions for w in m.words))
    if len(mentions) == 1:
        title = f"A new story mentions {_said(words)}"
    else:
        title = f"{len(mentions)} new stories mention {_said(words)}"
    blocks: list[str] = []
    used = len(title)
    for n, m in enumerate(mentions):
        block = f"{_plain(m.title)}\n{m.source} · {', '.join(m.words)}\n{m.url}"
        if used + len(block) > KEYWORD_ROOM:
            blocks.append(f"…and {len(mentions) - n} more.")
            break
        blocks.append(block)
        used += len(block) + 2
    return Alert(title=title, message="\n\n".join(blocks), emoji="🔔")


def check_keywords(
    settings: Settings,
    muted: Iterable[str] = (),
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Send one alert for the new stories whose headline or intro mentions a
    word of ALERT_KEYWORDS. Stories that are muted (`muted` in sources.yaml)
    are left out, like on the site. Returns the number of stories sent. A
    story is never sent twice; when the alert can't be sent, it is tried
    again on the next check."""
    patterns = keyword_patterns(settings.alert_keywords)
    if not patterns or not settings.discord_webhook_url:
        return 0
    now = now or datetime.now(timezone.utc)
    hidden = [fold(phrase) for phrase in muted if phrase.strip()]
    with session_scope() as session:
        # A story can come back only within KEYWORD_LOOKBACK; keep a day.
        session.execute(delete(KeywordAlert).where(KeywordAlert.sent_at < now - timedelta(days=1)))
        sent_before = exists().where(KeywordAlert.article_id == Article.id)
        rows = session.execute(
            select(Article.id, Article.title, Article.intro, Article.url, Source.name)
            .join(Source, Source.id == Article.source_id)
            .where(
                Article.first_seen_at >= now - KEYWORD_LOOKBACK,
                Article.published_at >= now - KEYWORD_FRESH,
                ~sent_before,
            )
            .order_by(Article.first_seen_at, Article.id)
        ).all()
        found: list[tuple[int, Mention]] = []
        for article_id, title, intro, url, source in rows:
            text = fold(f"{title}\n{intro or ''}")
            if any(phrase in text for phrase in hidden):
                continue
            words = [word for word, pattern in patterns if pattern.search(text)]
            if words:
                found.append((article_id, Mention(title, source, url, words)))
        if not found or not send(settings, keyword_alert([m for _, m in found]), transport):
            return 0
        session.add_all(KeywordAlert(article_id=article_id, sent_at=now) for article_id, _ in found)
    return len(found)


def send_test_alert(settings: Settings, transport: httpx.BaseTransport | None = None) -> bool:
    message = (
        "Alerts work. One comes when a news site, job board or event calendar has not "
        f"worked for {settings.alert_after_hours} hours, and one when it works again. "
        "One comes when a news site goes quiet (no new story for much longer than usual)."
    )
    words = [word for word, _ in keyword_patterns(settings.alert_keywords)]
    if words:
        message += f"\n\nKeyword alerts are on for: {', '.join(words)}."
    else:
        message += "\n\nKeyword alerts are off (ALERT_KEYWORDS is empty)."
    return send(
        settings,
        Alert(title=f"{settings.site_name}: test alert", message=message, emoji="✅"),
        transport,
    )
