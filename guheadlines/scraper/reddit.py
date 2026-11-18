"""Reading a subreddit's newest posts through Reddit's official API.

Until November 2026 r/guam was read from its public feed. Since then every
Reddit feed answers readers who aren't logged in with "Not Found", so Reddit
sources (`reddit: guam` in sources.yaml) use Reddit's Data API instead, with
a registered app's id and secret (REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET):

1. an app-only OAuth token (client credentials) from /api/v1/access_token,
   kept until shortly before it expires (a day);
2. the subreddit's newest posts from https://oauth.reddit.com/r/<name>/new.

Posts are turned into the same candidates the feed gave: the post's own text,
the full-size picture for picture posts, the author and the time.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .. import __version__
from .discover import _IMAGE_FILE_RE, Candidate, make_candidate
from .http import FetchError, HttpClient
from .text import clean_text

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_URL = "https://oauth.reddit.com"
POST_LIMIT = 50


class RedditKeyMissing(FetchError):
    def __init__(self):
        super().__init__(
            "needs a Reddit API key: set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET "
            "(see docs/news-sources.md#reddit)"
        )


def user_agent(username: str = "") -> str:
    """The User-Agent Reddit's API rules ask for: platform:app:version (by /u/name)."""
    by = f"by /u/{username}" if username else "+https://github.com/gottalogin1/GU-headlines"
    return f"server:gu-headlines:{__version__} ({by})"


def _post_text(html: str | None) -> str:
    """The text of a post's own body (selftext_html), without its markup."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one("div.md") or soup
    return clean_text(body.get_text(" "))


def parse_posts(data: dict, subreddit: str) -> list[Candidate]:
    """Candidates from a listing (/r/<name>/new, fetched with raw_json=1)."""
    candidates: list[Candidate] = []
    for child in (data.get("data") or {}).get("children") or []:
        if child.get("kind") != "t3":
            continue
        post = child.get("data") or {}
        permalink = post.get("permalink") or ""
        title = clean_text(post.get("title"))
        if not permalink.startswith("/r/") or not title:
            continue
        # Removed, deleted and adult posts are left out.
        if post.get("removed_by_category") or post.get("over_18"):
            continue
        author = post.get("author") or ""
        target = post.get("url_overridden_by_dest") or post.get("url") or ""
        # A picture post links straight to the picture (i.redd.it); Reddit's
        # own thumbnails are too small to show (140 px).
        picture = target if _IMAGE_FILE_RE.search(urlsplit(target).path) else None
        created = post.get("created_utc")
        candidate = make_candidate(
            "https://www.reddit.com" + permalink,
            "feed",
            title=title,
            summary=_post_text(post.get("selftext_html")) or None,
            image_url=picture,
            published_at=(
                datetime.fromtimestamp(float(created), tz=timezone.utc) if created else None
            ),
            author=f"u/{author}" if author and author != "[deleted]" else None,
            tags=[subreddit],
        )
        if candidate:
            candidates.append(candidate)
    return candidates


class RedditApi:
    def __init__(
        self,
        client: HttpClient,
        client_id: str,
        client_secret: str,
        username: str = "",
        clock=time.monotonic,
    ):
        self.client = client
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent(username)
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _json(self, content: bytes, what: str) -> dict:
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise FetchError(f"Reddit sent something other than JSON for {what}") from exc
        if not isinstance(data, dict):
            raise FetchError(f"Reddit sent an unexpected answer for {what}")
        return data

    def token(self, *, renew: bool = False) -> str:
        if not (self.client_id and self.client_secret):
            raise RedditKeyMissing()
        with self._lock:
            if self._token and not renew and self._clock() < self._expires:
                return self._token
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            try:
                result = self.client.post_form(
                    TOKEN_URL,
                    {"grant_type": "client_credentials"},
                    headers={"Authorization": f"Basic {basic}", "User-Agent": self.user_agent},
                )
            except FetchError as exc:
                if exc.status in (400, 401, 403):
                    raise FetchError(
                        f"Reddit refused the API key (HTTP {exc.status}): check "
                        "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET",
                        exc.status,
                    ) from exc
                raise
            data = self._json(result.content, "the sign-in")
            token = data.get("access_token")
            if not token:
                # Reddit answers 200 with {"error": ...} for some bad keys.
                raise FetchError(f"Reddit refused the API key: {data.get('error', 'no token')}")
            self._token = str(token)
            # Renew a few minutes early (tokens last a day).
            self._expires = self._clock() + max(60.0, float(data.get("expires_in", 3600)) - 300)
            return self._token

    def new_posts(self, subreddit: str, limit: int = POST_LIMIT) -> list[Candidate]:
        url = f"{API_URL}/r/{subreddit}/new?limit={limit}&raw_json=1"
        for attempt in range(2):
            headers = {
                "Authorization": f"bearer {self.token(renew=attempt > 0)}",
                "User-Agent": self.user_agent,
            }
            try:
                result = self.client.get(
                    url, headers=headers, accept="application/json", check_robots=False
                )
            except FetchError as exc:
                if exc.status == 401 and attempt == 0:
                    continue  # the token expired early: sign in again
                if exc.status == 403:
                    raise FetchError(
                        f"Reddit refused r/{subreddit} (HTTP 403): the community may be "
                        "private, or the Reddit app may still need Reddit's approval",
                        403,
                    ) from exc
                if exc.status == 404:
                    raise FetchError(f"Reddit has no community called r/{subreddit}", 404) from exc
                raise
            return parse_posts(self._json(result.content, f"r/{subreddit}"), subreddit)
        raise FetchError(f"Reddit refused the API token for r/{subreddit} (HTTP 401)", 401)
