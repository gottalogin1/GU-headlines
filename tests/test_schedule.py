from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from guheadlines.schedule import next_wake
from guheadlines.scraper.http import retry_after_seconds
from guheadlines.scraper.pipeline import SourceResult

HOUR = 3600.0
TOP = 1_790_000_000 // 3600 * 3600  # some top of the hour


def pending(retry_after=None):
    return {"postguam": SourceResult(slug="postguam", pending=[object()], retry_after=retry_after)}


def test_regular_runs_on_the_hour():
    assert next_wake(TOP + 600, HOUR, {}, 60) == (3000, "regular")


def test_catch_up_after_a_minute_while_pages_are_pending():
    assert next_wake(TOP + 600, HOUR, pending(), 60) == (60, "catch-up")


def test_catch_up_honours_retry_after_up_to_ten_minutes():
    assert next_wake(TOP + 600, HOUR, pending(retry_after=180), 60) == (180, "catch-up")
    assert next_wake(TOP + 60, HOUR, pending(retry_after=5000), 60) == (600, "catch-up")


def test_no_catch_up_right_before_the_regular_run():
    # The regular run in 70s re-reads everything anyway.
    assert next_wake(TOP + HOUR - 70, HOUR, pending(), 60) == (70, "regular")


def test_retry_after_header_parsing():
    assert retry_after_seconds("120") == 120
    later = datetime.now(timezone.utc) + timedelta(seconds=90)
    assert 80 <= retry_after_seconds(format_datetime(later, usegmt=True)) <= 90
    assert retry_after_seconds("soon") is None
    assert retry_after_seconds(None) is None


def test_worker_tracks_pending_sources_between_rounds():
    from guheadlines.cli import _merge_pending

    post_left = SourceResult(slug="postguam", pending=[object()])
    pdn_done = SourceResult(slug="guampdn")
    kuam_left = SourceResult(slug="kuam", pending=[object(), object()])

    # After a regular run: exactly the sources with pages left.
    after_hourly = _merge_pending({}, [post_left, pdn_done, kuam_left], catch_up=False)
    assert set(after_hourly) == {"postguam", "kuam"}

    # A catch-up round finishes the Post; KUAM still has pages left.
    kuam_still = SourceResult(slug="kuam", pending=[object()])
    after_round = _merge_pending(
        after_hourly, [SourceResult(slug="postguam"), kuam_still], catch_up=True
    )
    assert after_round == {"kuam": kuam_still}

    # A source disabled meanwhile (no result) stops being tracked.
    assert _merge_pending(after_round, [], catch_up=True) == {}
