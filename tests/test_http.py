import httpx
import pytest
from conftest import fixture_bytes

from guheadlines.scraper.http import (
    Blocked,
    FetchError,
    FetchResult,
    HttpClient,
    is_bot_check,
    is_public_host,
)

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


def test_local_network_addresses_are_refused(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"):
        assert not is_public_host(host)
    assert is_public_host("8.8.8.8")
    # A photo or redirect that points into the local network is never fetched.
    client = HttpClient("test-agent", per_host_delay=0, respect_robots=False, retries=0)
    try:
        for url in ("http://127.0.0.1:5432/", "http://192.168.1.1/admin", "http://[::1]/"):
            with pytest.raises(FetchError, match="not a public internet address"):
                client.get(url)
    finally:
        client.close()
