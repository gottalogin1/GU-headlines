import base64
import json
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import ROOT, fixture_bytes

from guheadlines.config import REDDIT_KEY_MISSING, load_config
from guheadlines.scraper.http import FetchError, HttpClient
from guheadlines.scraper.reddit import RedditApi, RedditKeyMissing, parse_posts, user_agent
from guheadlines.settings import get_settings

LISTING = "https://oauth.reddit.com/r/guam/new"
TOKEN = "https://www.reddit.com/api/v1/access_token"


def test_parse_posts():
    items = parse_posts(json.loads(fixture_bytes("reddit_new.json")), "guam")
    # Removed posts and adult posts are left out.
    assert [c.title for c in items] == [
        "DMV need help Guam ID",
        "Sunset at Ypao Beach tonight",
        "Port Authority approves new gantry cranes & more",
    ]
    text_post, picture_post, link_post = items
    assert text_post.url == "https://www.reddit.com/r/guam/comments/1wszcb6/dmv_need_help_guam_id/"
    assert text_post.via == "feed"  # built from the API's data, like a feed entry
    assert text_post.summary.startswith("My appointment at the Department of Revenue")
    assert text_post.summary.endswith("Thanks in advance!")
    assert text_post.author == "u/islandcommuter"
    assert text_post.image_url is None
    assert text_post.published_at == datetime(2026, 9, 29, 3, 22, 48, tzinfo=timezone.utc)
    # A picture post shows the full-size picture, not Reddit's 140 px thumbnail.
    assert picture_post.image_url == "https://i.redd.it/k2v9w8ypao1.jpeg"
    assert picture_post.summary is None
    # A shared link has neither text of its own nor a usable picture.
    assert link_post.summary is None and link_post.image_url is None


class FakeReddit:
    """Reddit's token endpoint and API, counting what was asked."""

    def __init__(self, token_status=200, listing_statuses=(200,)):
        self.token_status = token_status
        self.listing_statuses = list(listing_statuses)
        self.tokens = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == TOKEN:
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {"grant_type": ["client_credentials"]}
            basic = base64.b64encode(b"app-id:app-secret").decode()
            assert request.headers["authorization"] == f"Basic {basic}"
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            self.tokens += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{self.tokens}",
                    "token_type": "bearer",
                    "expires_in": 86400,
                },
            )
        if url.startswith(LISTING):
            assert "raw_json=1" in url and "limit=50" in url
            status = self.listing_statuses.pop(0) if self.listing_statuses else 200
            if status != 200:
                return httpx.Response(status, json={"message": "Unauthorized"})
            assert request.headers["authorization"] == f"bearer token-{self.tokens}"
            return httpx.Response(
                200,
                content=fixture_bytes("reddit_new.json"),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)


def _api(fake, client_id="app-id", client_secret="app-secret"):
    client = HttpClient(
        "test-agent", per_host_delay=0, transport=httpx.MockTransport(fake), sleep=lambda s: None
    )
    return RedditApi(client, client_id, client_secret, "guamreader"), client


def test_signs_in_once_and_reads_the_newest_posts():
    fake = FakeReddit()
    api, client = _api(fake)
    try:
        assert len(api.new_posts("guam")) == 3
        assert len(api.new_posts("guam")) == 3
    finally:
        client.close()
    assert fake.tokens == 1  # the token is kept between runs
    # Reddit's API rules: a descriptive User-Agent naming the app's account,
    # on the sign-in and on every API call; robots.txt is never asked for.
    assert {r.headers["user-agent"] for r in fake.requests} == {user_agent("guamreader")}
    assert user_agent("guamreader").endswith("(by /u/guamreader)")
    assert not any(r.url.path == "/robots.txt" for r in fake.requests)


def test_an_expired_token_is_renewed():
    fake = FakeReddit(listing_statuses=[401, 200])
    api, client = _api(fake)
    try:
        assert len(api.new_posts("guam")) == 3
    finally:
        client.close()
    assert fake.tokens == 2


def test_a_wrong_key_says_so():
    api, client = _api(FakeReddit(token_status=401))
    try:
        with pytest.raises(FetchError, match=r"Reddit refused the API key \(HTTP 401\)"):
            api.new_posts("guam")
    finally:
        client.close()


def test_without_a_key_nothing_is_asked():
    fake = FakeReddit()
    api, client = _api(fake, client_id="", client_secret="")
    try:
        with pytest.raises(RedditKeyMissing, match="set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET"):
            api.new_posts("guam")
    finally:
        client.close()
    assert fake.requests == []


def test_reddit_sources_wait_for_a_key():
    settings = replace(get_settings(), reddit_client_id="", reddit_client_secret="")
    reddit = load_config(ROOT / "config", settings).source("reddit-guam")
    assert reddit.reddit == "guam" and reddit.feed_only
    assert reddit.cannot_scrape == REDDIT_KEY_MISSING and not reddit.collected
    keyed = replace(settings, reddit_client_id="app-id", reddit_client_secret="app-secret")
    reddit = load_config(ROOT / "config", keyed).source("reddit-guam")
    assert reddit.cannot_scrape is None and reddit.collected
