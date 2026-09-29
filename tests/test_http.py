import httpx
import pytest
from conftest import fixture_bytes

from guheadlines.scraper.http import Blocked, FetchError, FetchResult, HttpClient, is_bot_check

SITEGROUND_CHECK = b"""<!DOCTYPE html><html><head><title>One moment, please...</title>
<script src="/.well-known/sgcaptcha/?r=%2Ffeed%2F"></script></head>
<body><h1>Please wait while your request is being verified...</h1></body></html>"""


def _html(content: bytes, content_type: str = "text/html") -> FetchResult:
    return FetchResult("https://x.com/", 200, content, {"content-type": content_type})


def test_bot_check_pages_are_recognized():
    assert is_bot_check(_html(SITEGROUND_CHECK))
    assert is_bot_check(_html(b"<html><head><title>Just a moment...</title></head></html>"))
    assert not is_bot_check(_html(fixture_bytes("wordpress_article.html")))
    assert not is_bot_check(_html(fixture_bytes("wordpress_feed.xml"), "application/rss+xml"))


def _client(handler) -> HttpClient:
    return HttpClient(
        "test-agent", per_host_delay=0, transport=httpx.MockTransport(handler), sleep=lambda s: None
    )


def test_bot_checks_raise_with_the_service_named():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/feed/":
            return httpx.Response(
                200, content=SITEGROUND_CHECK, headers={"content-type": "text/html"}
            )
        return httpx.Response(403, text="Forbidden", headers={"cf-ray": "8c1f-SJC"})

    client = _client(handler)
    try:
        with pytest.raises(Blocked, match="SiteGround showed a bot check") as blocked:
            client.get("https://dol.example.gov/feed/")
        assert blocked.value.status == 403  # handled like a bot wall's 403
        with pytest.raises(FetchError, match=r"HTTP 403 .* \(blocked by Cloudflare\)"):
            client.get("https://www.example.com/story/")
    finally:
        client.close()
