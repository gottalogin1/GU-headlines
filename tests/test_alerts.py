"""Alerts for news sites and job boards that stop working."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from guheadlines.alerts import (
    Alert,
    check_sources,
    guam_time,
    is_webhook,
    send,
    send_test_alert,
)
from guheadlines.db import session_scope
from guheadlines.models import AlertState, Article, JobSource, Source
from guheadlines.settings import get_settings

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)  # 4:00 PM in Guam
WEBHOOK = "https://discord.com/api/webhooks/1234567890/abc-DEF_123"


class Outbox:
    """A stand-in for Discord that keeps the messages it was sent."""

    def __init__(self, status: int = 204):
        self.status = status
        self.sent: list[dict] = []
        self.urls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.urls.append(str(request.url))
            self.sent.append(json.loads(request.content))
            return httpx.Response(self.status)

        return httpx.MockTransport(handle)

    def texts(self) -> list[str]:
        return [message["content"] for message in self.sent]


def _settings(**changes):
    base = replace(get_settings(), discord_webhook_url=WEBHOOK, alert_after_hours=24)
    return replace(base, **changes)


def test_webhook_links():
    assert is_webhook(WEBHOOK)
    assert is_webhook(" https://discordapp.com/api/webhooks/42/x.y-z ")
    assert is_webhook("https://ptb.discord.com/api/v10/webhooks/42/token/")
    for wrong in (
        "",
        "http://discord.com/api/webhooks/42/token",  # not https
        "https://discord.gg/invite-code",  # an invite, not a webhook
        "https://discord.com/channels/42/43",  # a channel link
        "https://evil.example/api/webhooks/42/token",
        "https://discord.com.evil.example/api/webhooks/42/token",
    ):
        assert not is_webhook(wrong), wrong


def test_send_posts_to_the_webhook():
    outbox = Outbox()
    assert send(_settings(), Alert("Pågat stopped", "Body @everyone"), outbox.transport())
    assert outbox.urls == [WEBHOOK]
    assert outbox.sent == [
        {
            "content": "⚠️ **Pågat stopped**\nBody @everyone",
            "allowed_mentions": {"parse": []},  # "@everyone" notifies no one
            "flags": 4,  # no preview cards for links
        }
    ]
    send(_settings(), Alert("Updated", "x", emoji="🚀", quiet=True), outbox.transport())
    assert outbox.sent[1]["flags"] == 4 | 4096  # posted without a phone notification
    send(_settings(), Alert("Long", "x" * 3000), outbox.transport())
    assert len(outbox.sent[2]["content"]) == 2000


def test_send_reports_failures_and_bad_links(caplog):
    assert not send(_settings(), Alert("t", "m"), Outbox(status=404).transport())
    assert "could not send the alert 't' to Discord: HTTP 404" in caplog.text
    assert "abc-DEF_123" not in caplog.text  # the link is a secret
    outbox = Outbox()
    assert not send(
        _settings(discord_webhook_url="https://discord.gg/abc"), Alert("t", "m"), outbox.transport()
    )
    assert not send(_settings(discord_webhook_url=""), Alert("t", "m"), outbox.transport())
    assert outbox.sent == []
    assert "not a Discord webhook link" in caplog.text


def test_test_alert():
    outbox = Outbox()
    assert send_test_alert(_settings(site_name="Guam Headlines"), outbox.transport())
    assert outbox.texts()[0].startswith("✅ **Guam Headlines: test alert**\nAlerts work.")
    assert "24 hours" in outbox.texts()[0]


def test_guam_time():
    assert guam_time(NOW) == "Tue Nov 24, 4:00 PM"
    assert guam_time(datetime(2026, 11, 23, 14, 5, tzinfo=timezone.utc)) == "Tue Nov 24, 12:05 AM"


# ---------------------------------------------------------------- database


def _source(slug, name, last_run=None, last_success=None, error=None, model=Source):
    with session_scope() as session:
        row = model(
            slug=slug,
            name=name,
            last_run_at=last_run,
            last_success_at=last_success,
            last_error=error,
        )
        session.add(row)


def _set(slug, model=Source, **values):
    with session_scope() as session:
        row = session.scalar(select(model).where(model.slug == slug))
        for name, value in values.items():
            setattr(row, name, value)


def _states():
    with session_scope() as session:
        return {s.key: s.notified_at for s in session.scalars(select(AlertState))}


def test_alert_once_after_a_day_and_again_when_fixed(clean_db):
    settings = _settings(site_url="https://guamheadlines.com")
    outbox = Outbox()
    # KUAM last worked 23 hours ago; its latest check failed.
    _source("kuam", "KUAM News", NOW - timedelta(hours=1), NOW - timedelta(hours=23), "HTTP 403")
    _source("postguam", "The Guam Daily Post", NOW, NOW)  # fine
    check = lambda now: check_sources(  # noqa: E731
        settings, ["kuam", "postguam"], [], now=now, transport=outbox.transport()
    )

    check(NOW)
    assert outbox.sent == []  # not a day yet
    assert _states() == {"news:kuam": None}

    later = NOW + timedelta(hours=1)
    _set("kuam", last_run_at=later)
    check(later)
    assert len(outbox.sent) == 1
    alert = outbox.texts()[0]
    assert alert.startswith("⚠️ **KUAM News has stopped working**\n")
    assert "The news site KUAM News has not worked since Mon Nov 23, 5:00 PM" in alert
    assert "Latest error: HTTP 403" in alert
    assert "More on the Sources page: https://guamheadlines.com/status" in alert
    assert _states() == {"news:kuam": later}

    # Still failing an hour later: no second alert.
    _set("kuam", last_run_at=later + timedelta(hours=1))
    check(later + timedelta(hours=1))
    assert len(outbox.sent) == 1

    # Works again: one more alert, and the slate is clean.
    fixed = later + timedelta(hours=2)
    _set("kuam", last_run_at=fixed, last_success_at=fixed, last_error=None)
    check(fixed)
    assert len(outbox.sent) == 2
    assert outbox.texts()[1].startswith("✅ **KUAM News works again**")
    assert _states() == {}


def test_short_failures_and_unread_sources_are_never_reported(clean_db):
    settings = _settings()
    outbox = Outbox()
    _source("kuam", "KUAM News", NOW, NOW - timedelta(hours=2))
    _source("gvb", "Guam Visitors Bureau", NOW, NOW - timedelta(days=30))  # not read any more
    check_sources(settings, ["kuam"], [], now=NOW, transport=outbox.transport())
    assert _states() == {"news:kuam": None}
    _set("kuam", last_success_at=NOW + timedelta(hours=1), last_run_at=NOW + timedelta(hours=1))
    check_sources(
        settings, ["kuam"], [], now=NOW + timedelta(hours=1), transport=outbox.transport()
    )
    assert outbox.sent == []
    assert _states() == {}


def test_several_at_once_and_job_boards(clean_db):
    settings = _settings(site_url="")
    outbox = Outbox()
    day_ago = NOW - timedelta(hours=30)
    _source("kuam", "KUAM News", NOW, day_ago, "feed https://kuam/rss: timed out")
    _source("gdol", "Guam Department of Labor", NOW, None)  # never worked
    _source("hireguam", "HireGuam", NOW, day_ago, "HTTP 500", model=JobSource)
    with session_scope() as session:  # it was first seen failing a day ago
        session.add(AlertState(key="news:gdol", failing_since=NOW - timedelta(hours=25)))
    check_sources(settings, ["kuam", "gdol"], ["hireguam"], now=NOW, transport=outbox.transport())
    assert len(outbox.sent) == 1
    alert = outbox.texts()[0]
    assert "**3 news sites and job boards have stopped working**" in alert
    assert "The job board HireGuam has not worked since" in alert
    assert "Guam Department of Labor has not worked since it was added" in alert
    assert "More on the Sources page of the site." in alert
    assert set(_states()) == {"news:kuam", "news:gdol", "jobs:hireguam"}


def test_a_failed_send_is_tried_again(clean_db):
    settings = _settings()
    _source("kuam", "KUAM News", NOW, NOW - timedelta(days=2))
    check_sources(settings, ["kuam"], [], now=NOW, transport=Outbox(status=503).transport())
    assert _states() == {"news:kuam": None}
    outbox = Outbox()
    check_sources(settings, ["kuam"], [], now=NOW, transport=outbox.transport())
    assert len(outbox.sent) == 1
    assert _states() == {"news:kuam": NOW}


def test_nothing_happens_without_a_webhook(clean_db):
    _source("kuam", "KUAM News", NOW, NOW - timedelta(days=2))
    outbox = Outbox()
    check_sources(
        _settings(discord_webhook_url=""), ["kuam"], [], now=NOW, transport=outbox.transport()
    )
    assert outbox.sent == []
    assert _states() == {}


def test_alert_test_command(monkeypatch, capsys):
    from guheadlines import cli

    with pytest.raises(SystemExit, match="DISCORD_WEBHOOK_URL is empty"):
        cli.cmd_alert_test(None, _settings(discord_webhook_url=""))
    monkeypatch.setattr("guheadlines.alerts.send_test_alert", lambda settings: True)
    cli.cmd_alert_test(None, _settings())
    assert "Test alert sent" in capsys.readouterr().out
    monkeypatch.setattr("guheadlines.alerts.send_test_alert", lambda settings: False)
    with pytest.raises(SystemExit, match="could not be sent"):
        cli.cmd_alert_test(None, _settings())


# ---------------------------------------------------------- keyword alerts


def test_keyword_words_from_the_setting(monkeypatch):
    from guheadlines.settings import load_settings

    monkeypatch.setenv("ALERT_KEYWORDS", ' "typhoon", Shooting ,, power  outage, typhoon, * ')
    assert load_settings().alert_keywords == ("typhoon", "Shooting", "power  outage")
    monkeypatch.setenv("ALERT_KEYWORDS", "")
    assert load_settings().alert_keywords == ()


def test_keyword_patterns():
    from guheadlines.alerts import fold, keyword_patterns

    def found(words, text):
        return [word for word, pattern in keyword_patterns(words) if pattern.search(fold(text))]

    words = ["Typhoon", "shooting", "power outage", "evacuat*", "Hagåtña", "U.S."]
    assert found(words, "TYPHOON'S winds reach Rota") == ["Typhoon"]
    assert found(words, "Two typhoons form east of Guam") == ["Typhoon"]
    assert found(words, "Police look into shootings") == ["shooting"]
    assert found(words, "GPA: Power\noutages expected tonight") == ["power outage"]
    assert found(words, "Evacuation centers open") == ["evacuat*"]
    assert found(words, "Hagatna mayor speaks") == ["Hagåtña"]
    assert found(words, "U.S. Navy ship arrives") == ["U.S."]
    # Only whole words.
    assert found(words, "Typhoonish weather; sharpshooting contest; empowerment") == []
    assert keyword_patterns(["", " * "]) == []


def _story(source_id, title, intro=None, found=NOW, published=None, url=None):
    with session_scope() as session:
        session.add(
            Article(
                source_id=source_id,
                url=url or f"https://www.kuam.com/story/{abs(hash(title))}",
                title=title,
                intro=intro,
                published_at=published or found,
                first_seen_at=found,
            )
        )


def test_keyword_alert_once_per_story(clean_db):
    from guheadlines.alerts import check_keywords

    settings = _settings(alert_keywords=("typhoon", "shooting"))
    outbox = Outbox()
    with session_scope() as session:
        kuam = Source(slug="kuam", name="KUAM News")
        session.add(kuam)
        session.flush()
        kuam_id = kuam.id
    _story(kuam_id, "Typhoon watch for Guam", url="https://www.kuam.com/story/1")
    _story(kuam_id, "Council meets on budget")  # no word
    _story(kuam_id, "Old typhoon story", published=NOW - timedelta(days=3))  # not news now
    _story(kuam_id, "Typhoon seen 7 hours ago", found=NOW - timedelta(hours=7))  # too late
    # Muted stories are left out, like on the site.
    _story(kuam_id, "Chamber of Commerce talks typhoon prep")
    muted = ("chamber of commerce",)

    assert check_keywords(settings, muted, now=NOW, transport=outbox.transport()) == 1
    assert outbox.texts() == [
        "🔔 **A new story mentions typhoon**\n"
        "Typhoon watch for Guam\nKUAM News · typhoon\nhttps://www.kuam.com/story/1"
    ]
    # Phone notifications on, and nobody is mentioned.
    assert outbox.sent[0]["flags"] == 4 and outbox.sent[0]["allowed_mentions"] == {"parse": []}

    # Not twice.
    later = NOW + timedelta(minutes=5)
    assert check_keywords(settings, muted, now=later, transport=outbox.transport()) == 0
    assert len(outbox.sent) == 1

    # The next ones come together, with the words each one has.
    _story(kuam_id, "Shooting in Dededo", "Police say the typhoon shelter was closed.", found=later)
    _story(kuam_id, "Second shooting", found=later, url="https://www.kuam.com/story/3")
    assert check_keywords(settings, muted, now=later, transport=outbox.transport()) == 2
    text = outbox.texts()[1]
    assert text.startswith("🔔 **2 new stories mention typhoon and shooting**\n")
    assert "Shooting in Dededo\nKUAM News · typhoon, shooting\n" in text
    assert "Second shooting\nKUAM News · shooting\nhttps://www.kuam.com/story/3" in text

    # Remembered for a day, then forgotten (they are too old to come back).
    from guheadlines.models import KeywordAlert

    check_keywords(settings, muted, now=later + timedelta(days=2), transport=outbox.transport())
    with session_scope() as session:
        assert session.scalars(select(KeywordAlert)).all() == []


def test_keyword_alert_that_fails_is_tried_again(clean_db):
    from guheadlines.alerts import check_keywords

    settings = _settings(alert_keywords=("typhoon",))
    with session_scope() as session:
        kuam = Source(slug="kuam", name="KUAM News")
        session.add(kuam)
        session.flush()
        kuam_id = kuam.id
    _story(kuam_id, "Typhoon watch for Guam")
    assert check_keywords(settings, now=NOW, transport=Outbox(status=500).transport()) == 0
    outbox = Outbox()
    assert check_keywords(settings, now=NOW, transport=outbox.transport()) == 1
    # Nothing without words or without a webhook.
    _story(kuam_id, "Typhoon warning for Guam")
    for off in (_settings(alert_keywords=()), _settings(discord_webhook_url="")):
        assert check_keywords(off, now=NOW, transport=outbox.transport()) == 0
    assert len(outbox.sent) == 1


def test_keyword_alert_fits_in_one_discord_message():
    from guheadlines.alerts import MAX_MESSAGE, Mention, discord_text, keyword_alert

    mentions = [
        Mention(
            f"Typhoon update number {n} for **all** of_Guam",
            "Pacific Island Times",
            f"https://www.pacificislandtimes.com/post/{'x' * 80}{n}",
            ["typhoon"],
        )
        for n in range(30)
    ]
    text = discord_text(keyword_alert(mentions))
    assert len(text) <= MAX_MESSAGE and not text.endswith("…")  # nothing cut off
    assert text.startswith("🔔 **30 new stories mention typhoon**\n")
    shown = text.count("pacificislandtimes.com")
    assert 5 <= shown < 30
    assert text.endswith(f"…and {30 - shown} more.")
    # Headlines can't make bold or other Markdown.
    assert "Typhoon update number 0 for \\*\\*all\\*\\* of\\_Guam" in text
    assert keyword_alert(
        [Mention("# Breaking", "KUAM", "https://k/1", ["breaking"])]
    ).message.startswith("\\# Breaking")


def test_test_alert_names_the_keywords():
    outbox = Outbox()
    send_test_alert(_settings(alert_keywords=("typhoon", "shooting")), outbox.transport())
    send_test_alert(_settings(alert_keywords=()), outbox.transport())
    assert "Keyword alerts are on for: typhoon, shooting." in outbox.texts()[0]
    assert "Keyword alerts are off (ALERT_KEYWORDS is empty)." in outbox.texts()[1]
