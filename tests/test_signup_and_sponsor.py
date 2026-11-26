"""The newsletter sign-up box (sendfox.py), the sponsor spot (web/sponsor.py) and
the "Buy me a coffee" box (PAYPAL_DONATE_LINK)."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import re
from itertools import pairwise

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from guheadlines import sendfox
from guheadlines.settings import load_settings

PAGES = ("/", "/jobs", "/events", "/status", "/archive", "/search?q=wage", "/category/labor")
TOKEN = "a-sendfox-token"
FROM_SITE = {"origin": "http://testserver"}
BROWSER = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "sec-fetch-mode": "navigate",
}


@pytest.fixture()
def client(clean_db):
    from guheadlines.web.app import app, sidebar_cache

    sidebar_cache._data.clear()
    return TestClient(app)


def _set(monkeypatch, **values):
    """Change website settings for one test."""
    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, **values)
    monkeypatch.setattr(web, "settings", site)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)


def _sidebar(html: str) -> str:
    return html.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]


# --------------------------------------------------------------- sign-up


class FakeSendFox:
    """SendFox's API, as far as the site uses it."""

    def __init__(self, lists=None, contact_status=200, pages=1):
        self.lists = [{"id": 11, "name": "Guam Headlines"}] if lists is None else lists
        self.contact_status = contact_status
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/me":
            return httpx.Response(200, json={"id": 1, "email": "owner@example.com"})
        if request.url.path == "/lists":
            page = int(request.url.params.get("page", "1"))
            rows = self.lists if page == 1 else [{"id": 20 + page, "name": f"Page {page}"}]
            return httpx.Response(
                200, json={"current_page": page, "last_page": self.pages, "data": rows}
            )
        if request.url.path == "/contacts" and request.method == "POST":
            if self.contact_status != 200:
                return httpx.Response(self.contact_status, json={"message": "no"})
            return httpx.Response(200, json={"id": 5, **json.loads(request.content)})
        return httpx.Response(404)

    @property
    def contacts(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path == "/contacts"]


@pytest.fixture()
def fake_sendfox(monkeypatch):
    fake = FakeSendFox()
    real_client = sendfox._client
    monkeypatch.setattr(
        sendfox,
        "_client",
        lambda settings, transport: real_client(settings, httpx.MockTransport(fake.handler)),
    )
    sendfox._list_cache.clear()
    yield fake
    sendfox._list_cache.clear()


@pytest.fixture()
def signup(client, monkeypatch, fake_sendfox):
    """The sign-up box on, with a fresh limit on sign-ups."""
    from guheadlines.web import app as web

    _set(monkeypatch, sendfox_api_token=TOKEN, sendfox_list="")
    monkeypatch.setattr(web, "signup_limit", web.SignupLimit())
    return client


def test_no_signup_box_until_sendfox_is_set_up(client):
    for path in PAGES:
        html = client.get(path).text
        assert "signup-form" not in html and 'href="/newsletter"' not in html, path
    assert client.get("/newsletter").status_code == 404
    assert client.post("/newsletter", data={"email": "a@example.com"}).status_code == 404


def test_signup_box_on_every_page(signup):
    for path in PAGES:
        page = signup.get(path)
        sidebar = _sidebar(page.text)
        assert sidebar.count('<form class="signup-form" action="/newsletter" method="post">') == 1
        assert "The top Guam stories by email every Tuesday and Friday." in sidebar
        assert 'type="email" name="email" required' in sidebar
        # Readers are told to confirm their address in the email that SendFox sends.
        assert "confirm your address in the email that comes to\n    your inbox" in sidebar
        # A plain form: no SendFox script or address, and the strict security
        # rules are unchanged (forms may go only to this site).
        assert "sendfox.com" not in page.text.lower()
        csp = page.headers["content-security-policy"]
        assert "form-action 'self';" in csp and "sendfox" not in csp
        footer = page.text.split('<footer class="footer">', 1)[1]
        assert '<a href="/newsletter">Newsletter</a>' in footer
    # The newsletter page has the form once, in the page itself, and can be
    # found in a search; the page after a sign-up cannot.
    page = signup.get("/newsletter").text
    assert page.count('class="signup-form"') == 1 and "signup-form" not in _sidebar(page)
    assert "noindex" not in page and '<link rel="canonical"' in page
    assert "SendFox" in page  # where the address goes
    assert "The newsletter starts only\n    after you confirm." in page
    assert "github" not in page.lower()
    levels = [int(n) for n in re.findall(r"<h([1-6])[\s>]", page)]
    assert levels[0] == 1 and all(b <= a + 1 for a, b in pairwise(levels)), levels
    assert (
        '<meta name="robots" content="noindex, follow">' in signup.get("/newsletter?signup=ok").text
    )
    assert "SendFox, the service that sends the emails" in signup.get("/status").text


def test_signup_pitch_follows_the_newsletter_days(signup, monkeypatch):
    _set(monkeypatch, sendfox_api_token=TOKEN, newsletter_days=(0, 2, 4))
    assert "every Monday, Wednesday and Friday." in signup.get("/").text
    assert "next email on Monday, Wednesday or Friday." in signup.get("/newsletter?signup=ok").text
    _set(monkeypatch, sendfox_api_token=TOKEN, newsletter_days=tuple(range(7)))
    assert "The top Guam stories by email every day." in signup.get("/").text
    _set(monkeypatch, sendfox_api_token=TOKEN, newsletter_days=())
    assert "The top Guam stories by email." in signup.get("/").text


def test_signup_adds_the_address_to_the_sendfox_list(signup, fake_sendfox):
    sent = signup.post(
        "/newsletter",
        data={"email": " reader@example.com ", "website": ""},
        headers=FROM_SITE,
        follow_redirects=False,
    )
    assert sent.status_code == 303 and sent.headers["location"] == "/newsletter?signup=ok"
    assert fake_sendfox.contacts == [{"email": "reader@example.com", "lists": [11]}]
    contact = fake_sendfox.requests[-1]
    assert contact.headers["authorization"] == f"Bearer {TOKEN}"
    assert str(contact.url) == "https://api.sendfox.com/contacts"
    page = signup.get(sent.headers["location"]).text
    assert "check your inbox" in page and "confirm your address" in page
    assert "the newsletter starts with the next email on Tuesday or Friday." in page
    # The list is looked up once, not for every sign-up.
    signup.post("/newsletter", data={"email": "two@example.com"}, headers=FROM_SITE)
    assert [r.url.path for r in fake_sendfox.requests].count("/lists") == 1


def test_signup_refuses_robots_and_bad_addresses(signup, fake_sendfox, monkeypatch):
    from guheadlines.web import app as web

    def post(data, headers=FROM_SITE, **kw):
        return signup.post("/newsletter", data=data, headers=headers, follow_redirects=False, **kw)

    def result(response) -> str:
        return response.headers["location"].split("signup=", 1)[1]

    ok = {"email": "reader@example.com"}
    # Only from a page of this site.
    assert post(ok, headers={}).status_code == 403
    assert post(ok, headers={"origin": "https://evil.example"}).status_code == 403
    assert post(ok, headers={"origin": "null"}).status_code == 403
    assert post(ok, headers={"referer": "http://testserver/jobs"}).status_code == 303
    fake_sendfox.requests.clear()
    # A robot that fills in the hidden field is told it worked, and nothing is sent.
    assert result(post({**ok, "website": "http://spam.example"})) == "ok"
    bad_addresses = (
        "",
        "reader",
        "reader@",
        "@example.com",
        "a b@x.com",
        "a@b",
        "x" * 250 + "@a.co",
    )
    for bad in bad_addresses:
        assert result(post({"email": bad})) == "invalid", bad
    assert fake_sendfox.requests == []
    assert post({"email": "x" * 5000}).status_code == 413
    # SendFox's own answers.
    fake_sendfox.contact_status = 422
    assert result(post(ok)) == "invalid"
    fake_sendfox.contact_status = 500
    assert result(post(ok)) == "error"
    assert "didn&#39;t work this time" in signup.get("/newsletter?signup=error").text
    fake_sendfox.contact_status = 200
    # At most a few sign-ups an hour from one address.
    monkeypatch.setattr(web, "signup_limit", web.SignupLimit(per_address=2, total=100))
    assert [result(post(ok)) for _ in range(3)] == ["ok", "ok", "busy"]
    monkeypatch.setattr(web, "signup_limit", web.SignupLimit(per_address=5, total=1))
    assert [result(post(ok)) for _ in range(2)] == ["ok", "busy"]


def test_signup_limit_forgets_after_an_hour(monkeypatch):
    from guheadlines.web import app as web

    clock = [1000.0]
    monkeypatch.setattr(web.time, "monotonic", lambda: clock[0])
    limit = web.SignupLimit(per_address=1, total=10, window=3600)
    assert limit.allow("1.2.3.4") and not limit.allow("1.2.3.4") and limit.allow("5.6.7.8")
    clock[0] += 3601
    assert limit.allow("1.2.3.4")
    assert list(limit._by_address) == ["1.2.3.4"]  # the old address is forgotten


def test_signup_list_is_found_by_name_or_number():
    lists = [sendfox.SendFoxList(11, "Guam Headlines"), sendfox.SendFoxList(12, "Other")]
    assert sendfox.pick_list("guam  headlines", lists).id == 11
    assert sendfox.pick_list(" 12 ", lists).id == 12
    assert sendfox.pick_list("", lists[:1]).id == 11  # the only list
    assert sendfox.pick_list("", []) is None  # no list yet: contacts only
    with pytest.raises(sendfox.SignupError, match="several lists"):
        sendfox.pick_list("", lists)
    with pytest.raises(sendfox.SignupError, match='no list "Readers"'):
        sendfox.pick_list("Readers", lists)


def test_sendfox_lists_come_a_page_at_a_time(fake_sendfox):
    fake_sendfox.pages = 3
    settings = dataclasses.replace(load_settings(), sendfox_api_token=TOKEN)
    names = [x.name for x in sendfox.lists(settings)]
    assert names == ["Guam Headlines", "Page 2", "Page 3"]


def test_sendfox_check_command(fake_sendfox, monkeypatch, capsys):
    from guheadlines.cli import cmd_sendfox_check

    settings = dataclasses.replace(load_settings(), sendfox_api_token=TOKEN, sendfox_list="")
    cmd_sendfox_check(None, settings)
    out = capsys.readouterr().out
    assert "SendFox account: owner@example.com" in out
    assert 'join the list "Guam Headlines" (11)' in out
    with pytest.raises(SystemExit, match='no list "Readers"'):
        cmd_sendfox_check(None, dataclasses.replace(settings, sendfox_list="Readers"))
    with pytest.raises(SystemExit, match="SENDFOX_API_TOKEN is empty"):
        cmd_sendfox_check(None, dataclasses.replace(settings, sendfox_api_token=""))
    fake_sendfox.handler = lambda request: httpx.Response(401)
    with pytest.raises(SystemExit, match="refused SENDFOX_API_TOKEN"):
        cmd_sendfox_check(None, settings)


def test_sendfox_settings(monkeypatch):
    monkeypatch.setenv("SENDFOX_API_TOKEN", "  token  ")
    monkeypatch.setenv("SENDFOX_LIST", " Guam Headlines ")
    settings = load_settings()
    assert settings.sendfox_api_token == "token" and settings.sendfox_list == "Guam Headlines"
    monkeypatch.delenv("SENDFOX_API_TOKEN")
    monkeypatch.delenv("SENDFOX_LIST")
    assert load_settings().sendfox_api_token == ""


# ---------------------------------------------------------------- sponsor


def _picture(width=1200, height=1000, color=(200, 16, 46, 255), fmt="PNG") -> bytes:
    image = Image.new("RGBA" if fmt == "PNG" else "RGB", (width, height), color[:3] + (0,))
    image.paste(color[: 4 if fmt == "PNG" else 3], (0, 0, width, height // 2))
    out = io.BytesIO()
    image.save(out, fmt)
    return out.getvalue()


@pytest.fixture()
def sponsor_dir(tmp_path, monkeypatch):
    from guheadlines.web import sponsor

    monkeypatch.setattr(sponsor, "sponsor_dir", lambda settings: tmp_path)
    return tmp_path


@pytest.fixture()
def with_sponsor(client, sponsor_dir, monkeypatch):
    (sponsor_dir / "ad.png").write_bytes(_picture())
    _set(
        monkeypatch,
        sponsor_image="ad.png",
        sponsor_link="https://shop.example.com/menu/",
        sponsor_alt="Island Coffee: two drinks for $5",
    )
    return client


def test_no_sponsor_spot_until_a_picture_is_set(client, sponsor_dir, monkeypatch):
    for path in PAGES:
        assert 'class="panel sponsor"' not in client.get(path).text, path
    assert client.get("/sponsor.webp").status_code == 404
    assert client.get("/go/sponsor", follow_redirects=False).status_code == 404
    # A file name with no file, or a file that is not a picture: still no spot.
    _set(monkeypatch, sponsor_image="missing.jpg", sponsor_link="https://shop.example.com/")
    assert 'class="panel sponsor"' not in client.get("/").text
    (sponsor_dir / "notes.jpg").write_text("not a picture")
    _set(monkeypatch, sponsor_image="notes.jpg")
    assert 'class="panel sponsor"' not in client.get("/").text
    assert client.get("/sponsor.webp").status_code == 404


def test_sponsor_spot_on_every_page(with_sponsor, monkeypatch):
    from guheadlines.web import app as web

    versions = set()
    for path in PAGES:
        page = with_sponsor.get(path)
        sidebar = _sidebar(page.text)
        spot = sidebar.split('<section class="panel sponsor" aria-label="Sponsor">', 1)[1]
        spot = spot.split("</section>", 1)[0]
        assert "<h2>Sponsor</h2>" in spot
        assert (
            '<a href="https://shop.example.com/menu/" target="_blank" rel="sponsored noopener">'
        ) in spot
        # A copy at most 600 pixels wide (twice the column), with its size
        # given, so the column does not jump when the picture comes in.
        src = re.search(r'<img src="(/sponsor\.webp\?v=\w+)" width="600" height="500"', spot)
        assert src, spot
        versions.add(src[1])
        assert 'alt="Island Coffee: two drinks for $5"' in spot
        # Served from this site: the strict security rules are unchanged.
        assert "'nonce-" not in page.headers["content-security-policy"]
    assert len(versions) == 1
    picture = with_sponsor.get(versions.pop())
    assert picture.headers["content-type"] == "image/webp"
    assert "immutable" in picture.headers["cache-control"]
    image = Image.open(io.BytesIO(picture.content))
    assert (image.format, image.size, image.mode) == ("WEBP", (600, 500), "RGBA")
    assert "immutable" not in with_sponsor.get("/sponsor.webp?v=old").headers["cache-control"]
    assert "a paid ad from a local business" in with_sponsor.get("/status").text

    # Above the AdSense block, on the pages that have one.
    _set(
        monkeypatch,
        sponsor_image="ad.png",
        sponsor_link="https://shop.example.com/menu/",
        sponsor_alt="",
        adsense_client="ca-pub-1234567890123456",
        adsense_slot="9876543210",
    )
    sidebar = _sidebar(with_sponsor.get("/").text)
    assert sidebar.index("panel sponsor") < sidebar.index("ad-slot")
    # Without words of its own, the picture is named after the sponsor's site.
    assert 'alt="Sponsor: shop.example.com"' in sidebar
    # Without a link, the picture is shown without one.
    _set(monkeypatch, sponsor_image="ad.png", sponsor_link="")
    spot = _sidebar(with_sponsor.get("/").text).split("panel sponsor", 1)[1]
    assert "<a href" not in spot.split("</section>", 1)[0]
    assert web.settings.sponsor_link == ""


def test_a_new_sponsor_picture_shows_without_a_restart(with_sponsor, sponsor_dir):
    first = re.search(r"/sponsor\.webp\?v=\w+", with_sponsor.get("/").text)[0]
    path = sponsor_dir / "ad.png"
    path.write_bytes(_picture(300, 250, (20, 90, 160, 255)))
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
    html = with_sponsor.get("/").text
    second = re.search(r"/sponsor\.webp\?v=\w+", html)[0]
    assert second != first
    assert 'width="300" height="250"' in html  # a small picture keeps its size


def test_sponsor_picture_copies():
    from guheadlines.web.sponsor import make_copy

    data, width, height = make_copy(_picture(1456, 180, fmt="JPEG"))  # a wide banner
    assert (width, height) == (600, 74)
    assert Image.open(io.BytesIO(data)).mode == "RGB"
    with pytest.raises(ValueError):
        make_copy(b"<svg></svg>")


def test_sponsor_clicks_are_counted_on_the_stats_page(with_sponsor, monkeypatch):
    import base64

    from guheadlines.db import session_scope
    from guheadlines.models import SPONSOR_CLICKS, PageViews
    from guheadlines.web import stats

    _set(
        monkeypatch,
        sponsor_image="ad.png",
        sponsor_link="https://shop.example.com/menu/",
        stats_password="a long stats password",
    )
    monkeypatch.setattr(stats, "counts", stats.Counts())
    client = with_sponsor
    client.base_url = "https://testserver"
    html = client.get("/", headers=BROWSER).text
    assert '<a href="/go/sponsor" target="_blank" rel="nofollow sponsored noopener">' in html
    go = client.get("/go/sponsor", headers=BROWSER, follow_redirects=False)
    assert go.status_code == 302 and go.headers["location"] == "https://shop.example.com/menu/"
    assert go.headers["cache-control"] == "no-store"
    client.get("/go/sponsor", headers={"user-agent": "Googlebot/2.1"}, follow_redirects=False)
    stats.counts.flush()
    with session_scope() as session:
        views = {row.path: row.views for row in session.query(PageViews)}
    assert views == {"/": 1, SPONSOR_CLICKS: 1}
    login = base64.b64encode(b"me:a long stats password").decode()
    page = client.get("/stats", headers={"authorization": f"Basic {login}"}).text
    spot = page.split("Sponsor spot</h2>", 1)[1].split("</dl>", 1)[0]
    assert re.search(r"<dt>Sponsor clicks</dt><dd>1</dd>", spot)
    assert re.search(r"<dt>Page views</dt><dd>1</dd>", spot)  # the click is not a page view
    assert SPONSOR_CLICKS not in page
    assert "Disallow: /go/\n" in client.get("/robots.txt").text


def test_sponsor_settings(monkeypatch):
    from guheadlines.settings import _file_name, _web_address

    assert _file_name("ad.jpg") == "ad.jpg"
    assert _file_name(" config/sponsor/Island Coffee.PNG ") == "Island Coffee.PNG"
    assert _file_name("C:\\Users\\me\\ad.webp") == "ad.webp"
    for bad in ("", None, "ad", "ad.exe", ".ad.jpg", "../", "ad.jpg/", "ad.svg"):
        assert _file_name(bad) == "", bad
    assert _web_address(" https://shop.example.com/menu/ ") == "https://shop.example.com/menu/"
    for bad in (
        "",
        None,
        "shop.example.com",
        "javascript:alert(1)",
        "https://a b.com",
        "https://[",
    ):
        assert _web_address(bad) == "", bad
    monkeypatch.setenv("SPONSOR_IMAGE", "ad.jpg")
    monkeypatch.setenv("SPONSOR_LINK", "https://shop.example.com/")
    monkeypatch.setenv("SPONSOR_ALT", "  Island   Coffee  ")
    settings = load_settings()
    assert (settings.sponsor_image, settings.sponsor_link, settings.sponsor_alt) == (
        "ad.jpg",
        "https://shop.example.com/",
        "Island Coffee",
    )


# ------------------------------------------------------------ coffee box

DONATE = "https://www.paypal.com/donate/?hosted_button_id=ABC123XYZ"


def test_no_coffee_box_until_a_paypal_link_is_set(client):
    for path in PAGES:
        assert "panel donate" not in client.get(path).text, path
    assert "Coffee money" not in client.get("/status").text


def test_coffee_box_on_every_page(client, monkeypatch):
    _set(monkeypatch, paypal_donate_link=DONATE, sendfox_api_token=TOKEN)
    for path in (*PAGES, "/weather", "/newsletter"):
        page = client.get(path)
        sidebar = _sidebar(page.text)
        box = sidebar.split('<section class="panel donate" aria-labelledby="donate-title">', 1)[1]
        box = box.split("</section>", 1)[0]
        assert '<h2 id="donate-title">Support this site</h2>' in box
        assert (
            f'<a class="btn donate-link" href="{DONATE}" target="_blank" rel="noopener">'
            '<span aria-hidden="true">☕</span> Buy me a coffee</a>'
        ) in box
        assert "Paid through PayPal." in box
        # A plain link: no PayPal script, and the strict security rules stay.
        assert "paypal" not in page.headers["content-security-policy"]
        assert "'nonce-" not in page.headers["content-security-policy"]
        # Under the newsletter box, where there is one.
        if "panel signup" in sidebar:
            assert sidebar.index("panel signup") < sidebar.index("panel donate")
    assert "Coffee money." in client.get("/status").text


def test_paypal_link_setting(monkeypatch):
    from guheadlines.settings import _paypal_link

    for good, link in (
        (" https://paypal.me/GuamHeadlines ", "https://paypal.me/GuamHeadlines"),
        ("paypal.me/GuamHeadlines/5", "https://paypal.me/GuamHeadlines/5"),
        ("www.paypal.com/paypalme/GuamHeadlines", "https://www.paypal.com/paypalme/GuamHeadlines"),
        (DONATE, DONATE),
        (
            "https://www.paypal.com/donate/?business=ABCDEFG1234&currency_code=USD",
            "https://www.paypal.com/donate/?business=ABCDEFG1234&currency_code=USD",
        ),
    ):
        assert _paypal_link(good) == link, good
    for bad in (
        "",
        None,
        "http://paypal.me/GuamHeadlines",
        "https://paypal.me.example.com/x",
        "https://notpaypal.com/donate",
        "https://ko-fi.com/guamheadlines",
        "javascript:alert(1)",
        # A donate link with an email address in it: the site never shows one.
        "https://www.paypal.com/donate/?business=me@example.com&currency_code=USD",
        "https://www.paypal.com/donate/?business=me%40example.com",
        '<form action="https://www.paypal.com/donate" method="post">',
    ):
        assert _paypal_link(bad) == "", bad

    monkeypatch.setenv("PAYPAL_DONATE_LINK", "paypal.me/GuamHeadlines")
    monkeypatch.setenv("ADSENSE_STORIES_SLOT", " 1122334455 ")
    monkeypatch.setenv("AFFILIATE_HOTELS_LINK", "https://www.example.com/guam-hotels?aid=1")
    monkeypatch.setenv("AFFILIATE_TOURS_LINK", "not a link")
    settings = load_settings()
    assert settings.paypal_donate_link == "https://paypal.me/GuamHeadlines"
    assert settings.adsense_stories_slot == "1122334455"
    assert settings.affiliate_hotels_link == "https://www.example.com/guam-hotels?aid=1"
    assert (settings.affiliate_tours_link, settings.affiliate_flights_link) == ("", "")
