"""When the worker should wake up next, and for what."""

from __future__ import annotations

from collections.abc import Mapping

from .scraper.pipeline import SourceResult

# Never wait longer than this for a catch-up, whatever Retry-After says.
MAX_CATCH_UP_WAIT = 600.0
# Skip a catch-up that would land this close to the regular run.
REGULAR_RUN_MARGIN = 20.0


def next_wake(
    now: float,
    interval: float,
    pending: Mapping[str, SourceResult],
    catch_up_seconds: float,
) -> tuple[float, str]:
    """Return (seconds to sleep, "catch-up" | "regular").

    Regular runs are aligned to the clock (every `interval` seconds, e.g. on the
    hour). While some sources still have pages pending, the worker comes back
    for just those sources after `catch_up_seconds` (or longer if a site's
    Retry-After asked for it), until they are done or the regular run is due.
    """
    next_regular = (now // interval + 1) * interval
    if pending:
        wait = max([catch_up_seconds] + [r.retry_after or 0.0 for r in pending.values()])
        wait = min(wait, MAX_CATCH_UP_WAIT)
        if now + wait + REGULAR_RUN_MARGIN < next_regular:
            return wait, "catch-up"
    return next_regular - now, "regular"
