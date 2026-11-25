"""Visitor counts for the private stats page (/stats): page views and story
clicks, counted on the server.

No script runs in the visitor's browser, no cookie is set and no IP address is
kept: a view or a click only adds 1 to a total for its Guam day. Each web
worker process adds up its counts in memory, and a thread of its own writes
them to the database every FLUSH_SECONDS, so a page view costs no database
write.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from datetime import date, datetime

from fastapi import Request
from sqlalchemy import BigInteger, Date, Integer, column, select, values
from sqlalchemy.dialects.postgresql import insert

from ..db import session_scope
from ..models import Article, PageViews, StoryClicks
from ..scraper.text import GUAM_TZ

log = logging.getLogger(__name__)

FLUSH_SECONDS = 30

# Search engines, link previews (Facebook, Discord, iMessage...), PageSpeed
# and other programs: their names hold one of these words or a web address.
_ROBOT = re.compile(
    r"bot|crawl|spider|slurp|preview|lighthouse|headless|inspection|compatible;|https?:", re.I
)


def is_reader(request: Request) -> bool:
    """True for a person who opens a page in a browser. False for robots, link
    previews, and pages that a browser loads before the person opens them."""
    if request.method != "GET":
        return False
    headers = request.headers
    agent = headers.get("user-agent", "")
    if not agent.startswith("Mozilla/") or _ROBOT.search(agent):
        return False
    if "prefetch" in f"{headers.get('sec-purpose', '')} {headers.get('purpose', '')}".lower():
        return False
    # Over https, a browser tells the site that a person opened the page
    # (Sec-Fetch-Mode: navigate). Most programs that pretend to be a browser
    # do not. Browsers do not send the header over plain http.
    mode = headers.get("sec-fetch-mode")
    if mode is None:
        return request.url.scheme == "http"
    return mode == "navigate"


class Counts:
    """The counts of one web worker process that are not in the database yet."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._views: Counter[tuple[date, str]] = Counter()
        self._clicks: Counter[tuple[date, int]] = Counter()
        self._writer: threading.Thread | None = None

    def view(self, path: str) -> None:
        self._add(self._views, path)

    def click(self, article_id: int) -> None:
        self._add(self._clicks, article_id)

    def _add(self, counter: Counter, key: str | int) -> None:
        with self._lock:
            counter[(datetime.now(GUAM_TZ).date(), key)] += 1
            if self._writer is None:
                self._writer = threading.Thread(
                    target=self._write_every, name="visitor-counts", daemon=True
                )
                self._writer.start()

    def _write_every(self) -> None:
        while True:
            time.sleep(FLUSH_SECONDS)
            self.flush()

    def flush(self) -> None:
        """Add the counts to the totals in the database. When the database
        cannot be reached, the counts are kept for the next try."""
        with self._lock:
            views, clicks = self._views, self._clicks
            self._views, self._clicks = Counter(), Counter()
        if not views and not clicks:
            return
        try:
            with session_scope() as session:
                if views:
                    rows = [{"day": d, "path": p, "views": n} for (d, p), n in views.items()]
                    stmt = insert(PageViews).values(rows)
                    session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=[PageViews.day, PageViews.path],
                            set_={"views": PageViews.views + stmt.excluded.views},
                        )
                    )
                if clicks:
                    # Only stories that are still stored (one can be deleted
                    # between the click and this write).
                    rows = values(
                        column("day", Date),
                        column("article_id", BigInteger),
                        column("clicks", Integer),
                        name="new_clicks",
                    ).data([(d, article_id, n) for (d, article_id), n in clicks.items()])
                    stored = select(rows.c.day, rows.c.article_id, rows.c.clicks).join(
                        Article, Article.id == rows.c.article_id
                    )
                    stmt = insert(StoryClicks).from_select(["day", "article_id", "clicks"], stored)
                    session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=[StoryClicks.day, StoryClicks.article_id],
                            set_={"clicks": StoryClicks.clicks + stmt.excluded.clicks},
                        )
                    )
        except Exception:
            log.warning("could not save the visitor counts; trying again later", exc_info=True)
            with self._lock:
                self._views.update(views)
                self._clicks.update(clicks)


counts = Counts()
