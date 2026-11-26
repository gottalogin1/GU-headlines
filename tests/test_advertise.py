"""The Advertise page: ads that businesses book and pay for through PayPal
(ads.py), the owner's review page, and booked ads in the sponsor spot and the
newsletter feed."""

from __future__ import annotations

import dataclasses
import io
import json
import re
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import make_jpeg
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, update

from guheadlines import ads, paypal
from guheadlines.db import session_scope
from guheadlines.models import AdBooking, AdSlot, Article, NewsletterEdition, PageViews, Source
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import _price_cents

FROM_SITE = {"origin": "http://testserver"}
PASSWORD = "stats-secret"
LOGIN = ("owner", PASSWORD)
BROWSER = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "sec-fetch-mode": "navigate",
}
ON = {
    "stats_password": PASSWORD,
    "paypal_client_id": "client-id",
    "paypal_client_secret": "secret",
    "sponsor_week_price_cents": 2500,
    "newsletter_sponsor_price_cents": 1500,
}


# ------------------------------------------------------------ unit tests


def test_prices_from_env():
    assert _price_cents("25") == 2500
    assert _price_cents(" $25.5 ") == 2550
    assert _price_cents("1,200.00") == 120000
    assert _price_cents("10000") == 1_000_000
    for value in (None, "", "free", "-5", "25.555", "10000.01", "0"):
        assert _price_cents(value) == 0, value
    assert ads.money(2500) == "$25" and ads.money(2550) == "$25.50"
    assert ads.money(120000) == "$1,200"


def test_weeks_and_editions_that_can_be_booked():
    # Thursday, Nov 26, 2026, 9:00 AM in Guam.
    now = datetime(2026, 11, 26, 9, 0, tzinfo=GUAM_TZ)
    weeks = ads.week_choices(now)
    assert weeks[0] == date(2026, 11, 30) and len(weeks) == ads.WEEKS_AHEAD
    assert all(w.weekday() == 0 for w in weeks)
    assert ads.week_start(date(2026, 11, 29)) == date(2026, 11, 23)
    # Tuesday and Friday at 10:00: Friday's edition is only 25 hours away.
    days = ads.edition_choices(now, (1, 4), datetime.min.time().replace(hour=10))
    assert days[:3] == [date(2026, 11, 27), date(2026, 12, 1), date(2026, 12, 4)]
    later = datetime(2026, 11, 26, 11, 0, tzinfo=GUAM_TZ)  # less than a day before Friday's
    assert ads.edition_choices(later, (1, 4), datetime.min.time().replace(hour=10))[0] == date(
        2026, 12, 1
    )
    assert len(days) == ads.EDITIONS_AHEAD
    assert ads.edition_choices(now, (), datetime.min.time()) == []


def test_ads_that_name_a_muted_phrase_are_refused():
    muted = ("chamber of commerce", "guam chamber")
    assert ads.refused(["Guam Chamber of Commerce mixer"], muted)
    assert ads.refused(["Island Bakery", "https://www.guamchamber.com.gu/"], muted)
    assert not ads.refused(["Island Bakery", "https://islandbakery.example/"], muted)


def _picture(width, height, kind="PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 16, 46)).save(out, kind)
    return out.getvalue()


def test_sponsor_pictures_are_checked_and_made_small():
    webp, width, height = ads.picture_copy(make_jpeg(1200, 1000))
    assert (width, height) == (600, 500)
    assert Image.open(io.BytesIO(webp)).format == "WEBP"
    with pytest.raises(ValueError, match="at least 300 pixels"):
        ads.picture_copy(_picture(200, 200))
    with pytest.raises(ValueError, match="too tall"):
        ads.picture_copy(_picture(300, 700))
    with pytest.raises(ValueError, match="long and thin"):
        ads.picture_copy(_picture(1200, 200))
    with pytest.raises(ValueError, match="isn't a picture"):
        ads.picture_copy(b"<svg></svg>")


# ------------------------------------------------------------- the site


class FakePayPal:
    """PayPal's API, as far as the Advertise page uses it."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.orders: dict[str, dict] = {}
        self.value: str | None = None  # what the capture says was paid; None: the order's
        self.capture_status = "COMPLETED"
        self.refund_fails = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 32400})
        if path == "/v2/checkout/orders" and request.method == "POST":
            body = json.loads(request.content)
            order_id = f"ORDER-{len(self.orders) + 1}"
            self.orders[order_id] = {"status": "APPROVED", "body": body}
            return httpx.Response(
                201,
                json={
                    "id": order_id,
                    "status": "PAYER_ACTION_REQUIRED",
                    "links": [
                        {
                            "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                            "rel": "payer-action",
                        }
                    ],
                },
            )
        if path.startswith("/v2/checkout/orders/"):
            order_id = path.split("/")[4]
            order = self.orders[order_id]
            if path.endswith("/capture"):
                order["status"] = "COMPLETED"
            answer = {"id": order_id, "status": order["status"]}
            if order["status"] == "COMPLETED":
                amount = order["body"]["purchase_units"][0]["amount"]
                answer["purchase_units"] = [
                    {
                        "payments": {
                            "captures": [
                                {
                                    "id": f"CAP-{order_id}",
                                    "status": self.capture_status,
                                    "amount": {**amount, "value": self.value or amount["value"]},
                                }
                            ]
                        }
                    }
                ]
            return httpx.Response(201 if path.endswith("/capture") else 200, json=answer)
        if path.endswith("/refund"):
            if self.refund_fails:
                return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
            return httpx.Response(201, json={"id": "REFUND-1", "status": "COMPLETED"})
        return httpx.Response(404)

    def calls(self, end: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(end)]


@pytest.fixture()
def fake_paypal(monkeypatch):
    fake = FakePayPal()
    monkeypatch.setattr(
        paypal,
        "_client",
        lambda transport: httpx.Client(transport=httpx.MockTransport(fake.handler)),
    )
    paypal._tokens.clear()
    yield fake
    paypal._tokens.clear()


@pytest.fixture()
def client(clean_db):
    from guheadlines.web.app import ad_cache, app, audience_cache, sidebar_cache

    for cache in (sidebar_cache, ad_cache, audience_cache):
        cache._data.clear()
    return TestClient(app)


def _set(monkeypatch, **values):
    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, **values)
    monkeypatch.setattr(web, "settings", site)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)


@pytest.fixture()
def site(client, monkeypatch, fake_paypal):
    """The Advertise page on, with fresh limits and no real Discord alerts."""
    from guheadlines.web import app as web

    _set(monkeypatch, **ON)
    monkeypatch.setattr(web, "booking_limit", web.SignupLimit(per_address=10, total=60))
    sent = []
    monkeypatch.setattr(web.alerts, "send", lambda settings, alert: sent.append(alert))
    client.alerts = sent
    client.paypal = fake_paypal
    return client


def _week(n=0) -> date:
    return ads.week_choices(datetime.now(timezone.utc))[n]


def _book(client, kind="site", days=None, **fields):
    data = {
        "kind": kind,
        "day": [d.isoformat() for d in (days or [_week()])],
        "business": "Island Bakery",
        "email": "owner@islandbakery.example",
        "link": "https://islandbakery.example/",
        "words": "Island Bakery: fresh pandesal every morning in Tamuning",
        **fields,
    }
    files = {"picture": ("ad.jpg", make_jpeg(900, 750), "image/jpeg")} if kind == "site" else None
    return client.post(
        "/advertise", data=data, files=files, headers=FROM_SITE, follow_redirects=False
    )


def _code(response) -> str:
    assert response.status_code == 303, response.text[:500]
    found = re.fullmatch(r"/advertise/booking/([\w-]+)", response.headers["location"])
    assert found, response.headers["location"]
    return found[1]


def _pay(client, code: str) -> httpx.Response:
    """Press the PayPal button, approve on PayPal, and come back."""
    to_paypal = client.get(f"/advertise/booking/{code}/pay", follow_redirects=False)
    assert to_paypal.status_code == 303
    order = to_paypal.headers["location"].split("token=")[1]
    return client.get(
        f"/advertise/booking/{code}/paid?token={order}&PayerID=P1", follow_redirects=False
    )


def _booking(code: str) -> AdBooking:
    with session_scope() as session:
        return session.scalars(select(AdBooking).where(AdBooking.code == code)).one()


def _decide(client, booking_id: int, action: str, note: str = "") -> httpx.Response:
    return client.post(
        f"/advertise/review/{booking_id}",
        data={"action": action, "note": note},
        auth=LOGIN,
        headers=FROM_SITE,
        follow_redirects=False,
    )


def _move_to_this_week(booking_id: int) -> None:
    """Make a booked week this week (bookings start next week at the earliest)."""
    monday = ads.week_start(datetime.now(GUAM_TZ).date())
    with session_scope() as session:
        session.execute(update(AdSlot).where(AdSlot.booking_id == booking_id).values(day=monday))
        session.execute(update(AdBooking).where(AdBooking.id == booking_id).values(days=[monday]))


def test_advertise_page_is_off_until_it_can_take_bookings(client, monkeypatch):
    pages = ("/", "/status", "/jobs")
    for values in ({}, {**ON, "paypal_client_id": ""}, {**ON, "stats_password": ""}):
        _set(monkeypatch, **{**{k: type(v)() for k, v in ON.items()}, **values})
        assert client.get("/advertise").status_code == 404
        for path in pages:
            assert 'href="/advertise"' not in client.get(path).text, path
        assert "/advertise<" not in client.get("/sitemap.xml").text
    _set(monkeypatch, **{**ON, "sponsor_week_price_cents": 0, "newsletter_sponsor_price_cents": 0})
    assert client.get("/advertise").status_code == 404
    response = client.post("/advertise", data={"kind": "site"}, headers=FROM_SITE)
    assert response.status_code == 404


def test_advertise_page_shows_real_numbers_and_the_open_weeks(site):
    today = datetime.now(GUAM_TZ).date()
    with session_scope() as session:
        session.add_all(
            [
                PageViews(day=today, path="/", views=300),
                PageViews(day=today - timedelta(days=3), path="/jobs", views=120),
                PageViews(day=today - timedelta(days=40), path="/", views=999),  # too old
                PageViews(day=today, path="sponsor-click", views=7),
                PageViews(day=today, path="newsletter-click", views=11),
            ]
        )
    taken = _code(_book(site, days=[_week(1)]))
    assert taken
    page = site.get("/advertise")
    assert page.status_code == 200
    html = page.text
    assert "<h1>Advertise on GU Headlines</h1>" in html
    assert "<dt>Page views</dt><dd>420</dd>" in html
    assert "<dt>A day, on average</dt><dd>14</dd>" in html
    assert "<dt>Clicks on the sponsor spot</dt><dd>7</dd>" in html
    assert "<dt>Story clicks from the newsletter</dt><dd>11</dd>" in html
    assert "Sponsor spot · $25 a week" in html and "Newsletter sponsor · $15 an edition" in html
    assert "a week had about\n    98 page views" in html
    # The week another business holds can't be picked.
    week = _week(1)
    assert re.search(rf'value="{week.isoformat()}" disabled', html)
    assert re.search(rf'value="{_week(0).isoformat()}">', html)
    assert 'enctype="multipart/form-data"' in html
    # A plain form to this site: no PayPal script, and the security rules are unchanged.
    csp = page.headers["content-security-policy"]
    assert "form-action 'self';" in csp and "paypal" not in csp.lower()
    assert "paypal.com" not in html and "github" not in html.lower()
    assert '<link rel="canonical"' in html and "noindex" not in html
    # Linked from the footer, the sitemap, and the empty sponsor spot.
    home = site.get("/").text
    assert '<a href="/advertise">Advertise</a>' in home.split('<footer class="footer">')[1]
    assert "See the numbers and book a week" in home
    assert "/advertise</loc>" in site.get("/sitemap.xml").text
    assert "Disallow: /advertise/\n" in site.get("/robots.txt").text
    assert "Booking an ad." in site.get("/status").text


def test_a_business_books_pays_and_the_owner_approves(site):
    code = _code(_book(site, link="islandbakery.example/menu"))
    booking = _booking(code)
    assert (booking.status, booking.amount_cents, booking.link) == (
        "new",
        2500,
        "https://islandbakery.example/menu",
    )
    assert (booking.picture_width, booking.picture_height) == (600, 500)
    page = site.get(f"/advertise/booking/{code}")
    assert page.status_code == 200
    assert "Pay $25 with PayPal" in page.text
    assert f'href="/advertise/booking/{code}/pay"' in page.text
    assert "noindex" in page.headers["x-robots-tag"] and "no-store" in page.headers["cache-control"]
    assert page.headers["referrer-policy"] == "no-referrer"
    picture = site.get(f"/advertise/booking/{code}/picture.webp")
    assert picture.headers["content-type"] == "image/webp"

    back = _pay(site, code)
    assert back.headers["location"] == f"/advertise/booking/{code}?note=paid"
    order = json.loads(site.paypal.calls("/v2/checkout/orders")[0].content)
    unit = order["purchase_units"][0]
    assert unit["amount"] == {"currency_code": "USD", "value": "25.00"}
    assert unit["custom_id"] == f"ad-booking-{booking.id}"
    context = order["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == f"http://testserver/advertise/booking/{code}/paid"
    assert context["cancel_url"] == f"http://testserver/advertise/booking/{code}"
    booking = _booking(code)
    assert (booking.status, booking.paypal_capture_id) == ("paid", "CAP-ORDER-1")
    assert "Thank you, your payment went through" in site.get(back.headers["location"]).text
    (alert,) = site.alerts
    assert "Island Bakery paid $25 for the sponsor spot" in alert.message
    assert "http://testserver/advertise/review" in alert.message
    # Coming back again (a reload) captures nothing more.
    again = site.get(f"/advertise/booking/{code}/paid?token=ORDER-1", follow_redirects=False)
    assert again.headers["location"] == f"/advertise/booking/{code}"
    assert len(site.paypal.calls("/capture")) == 1

    # Not shown before the owner approves it.
    _move_to_this_week(booking.id)
    assert "/go/ad/" not in site.get("/").text

    # The review page needs the stats password, and its buttons this site.
    assert site.get("/advertise/review").status_code == 401
    review = site.get("/advertise/review", auth=LOGIN)
    assert "owner@islandbakery.example" in review.text and "Waiting for you" in review.text
    assert "no-store" in review.headers["cache-control"]
    blocked = site.post(f"/advertise/review/{booking.id}", data={"action": "approve"}, auth=LOGIN)
    assert blocked.status_code == 403
    assert (
        site.post(
            f"/advertise/review/{booking.id}", data={"action": "approve"}, headers=FROM_SITE
        ).status_code
        == 401
    )
    done = _decide(site, booking.id, "approve")
    assert done.headers["location"].startswith("/advertise/review?done=approved")
    assert _booking(code).status == "approved"

    # This week's sponsor spot shows it, and counts its clicks.
    home = site.get("/").text
    sidebar = home.split('<aside class="sidebar">')[1]
    assert f'src="/sponsor.webp?v=b{booking.id}"' in sidebar
    assert (
        f'href="/go/ad/{booking.id}"' in sidebar and 'rel="nofollow sponsored noopener"' in sidebar
    )
    assert 'alt="Island Bakery: fresh pandesal every morning in Tamuning"' in sidebar
    assert "Advertise here" in sidebar
    picture = site.get(f"/sponsor.webp?v=b{booking.id}")
    assert picture.headers["content-type"] == "image/webp"
    assert "immutable" in picture.headers["cache-control"]
    click = site.get(f"/go/ad/{booking.id}", headers=BROWSER, follow_redirects=False)
    assert click.status_code == 302
    assert click.headers["location"] == "https://islandbakery.example/menu"
    site.get(f"/advertise/booking/{code}", headers=BROWSER)  # a secret address: not counted
    from guheadlines.web import stats

    stats.counts.flush()
    with session_scope() as session:
        counted = dict(session.execute(select(PageViews.path, PageViews.views)).all())
    assert counted[f"ad-click:{booking.id}"] == 1 and counted["sponsor-click"] == 1
    assert not any("booking" in path for path in counted)
    # The business sees how its ad is doing.
    with session_scope() as session:
        session.add(PageViews(day=datetime.now(GUAM_TZ).date(), path="/", views=40))
    mine = site.get(f"/advertise/booking/{code}").text
    assert "How your ad is doing" in mine
    assert "<dt>Clicks</dt><dd>1</dd>" in mine and "<dt>Page views</dt><dd>40</dd>" in mine


def test_two_businesses_cannot_book_the_same_week(site):
    _code(_book(site, days=[_week(0), _week(2)]))
    second = _book(site, days=[_week(1), _week(2)], business="Reef Divers")
    assert second.status_code == 409
    assert "Sorry, someone else has just booked" in second.text
    assert "noindex" in second.text
    with session_scope() as session:
        assert (
            session.scalar(select(AdBooking.id).where(AdBooking.business == "Reef Divers")) is None
        )
        assert len(session.scalars(select(AdSlot)).all()) == 2


def test_unpaid_bookings_free_their_weeks_after_the_hold(site):
    code = _code(_book(site))
    with session_scope() as session:
        session.execute(
            update(AdBooking).values(created_at=datetime.now(timezone.utc) - timedelta(hours=2))
        )
    page = site.get(f"/advertise/booking/{code}").text
    assert "This booking is cancelled (not paid in time), and nothing was" in page
    assert "Pay $25" not in page
    # Someone else can now book the week; a late payment of the first one is not taken.
    order_before = _booking(code)
    other = _code(_book(site, business="Reef Divers"))
    assert other
    with session_scope() as session:
        session.execute(
            update(AdBooking).where(AdBooking.id == order_before.id).values(paypal_order_id="X-1")
        )
    late = site.get(f"/advertise/booking/{code}/paid?token=X-1", follow_redirects=False)
    assert late.headers["location"].endswith("?note=taken")
    assert not site.paypal.calls("/capture")


def test_a_late_payment_keeps_the_week_when_it_is_still_free(site):
    code = _code(_book(site))
    to_paypal = site.get(f"/advertise/booking/{code}/pay", follow_redirects=False)
    order = to_paypal.headers["location"].split("token=")[1]
    with session_scope() as session:
        session.execute(
            update(AdBooking).values(created_at=datetime.now(timezone.utc) - timedelta(hours=2))
        )
    site.get("/advertise")  # frees the weeks of unpaid bookings
    assert _booking(code).status == "cancelled"
    back = site.get(f"/advertise/booking/{code}/paid?token={order}", follow_redirects=False)
    assert back.headers["location"].endswith("?note=paid")
    assert _booking(code).status == "paid"
    with session_scope() as session:
        assert session.scalar(select(AdSlot.booking_id)) == _booking(code).id


def test_payments_that_do_not_match_are_not_taken(site):
    code = _code(_book(site))
    stale = site.get(f"/advertise/booking/{code}/paid?token=SOMETHING", follow_redirects=False)
    assert stale.headers["location"].endswith("?note=stale")
    site.paypal.value = "1.00"  # PayPal says it took another amount: given back
    back = _pay(site, code)
    assert back.headers["location"].endswith("?note=paypal")
    assert _booking(code).status == "new"
    assert len(site.paypal.calls("/refund")) == 1
    assert not site.alerts


def test_pending_payments_wait_with_a_note(site):
    site.paypal.capture_status = "PENDING"
    code = _code(_book(site))
    _pay(site, code)
    booking = _booking(code)
    assert booking.status == "paid" and "PENDING" in booking.note
    assert "PayPal is still checking the payment" in site.alerts[0].message


def test_the_owner_turns_an_ad_down_and_paypal_refunds_it(site):
    code = _code(_book(site))
    _pay(site, code)
    booking = _booking(code)
    site.paypal.refund_fails = True
    failed = _decide(site, booking.id, "reject", "Picture is too dark")
    assert "done=refund-failed" in failed.headers["location"]
    assert _booking(code).status == "paid"
    review = site.get(failed.headers["location"], auth=LOGIN).text
    assert "Turn down, refunded in PayPal" in review
    site.paypal.refund_fails = False
    done = _decide(site, booking.id, "reject", "Picture is too dark")
    assert "done=rejected" in done.headers["location"]
    refund = site.paypal.calls("/refund")[-1]
    assert refund.url.path == "/v2/payments/captures/CAP-ORDER-1/refund"
    booking = _booking(code)
    assert (booking.status, booking.note) == ("rejected", "Picture is too dark")
    page = site.get(f"/advertise/booking/{code}").text
    assert "this ad was not accepted: Picture is too dark" in page
    assert "refunded in full" in page
    # Its week is free again.
    assert _code(_book(site, business="Reef Divers"))


def test_the_owner_can_turn_down_an_ad_refunded_in_paypal(site):
    code = _code(_book(site))
    _pay(site, code)
    booking = _booking(code)
    done = _decide(site, booking.id, "reject-kept")
    assert "done=rejected-kept" in done.headers["location"]
    assert _booking(code).status == "rejected" and not site.paypal.calls("/refund")
    # Approving it now changes nothing.
    assert "done=unchanged" in _decide(site, booking.id, "approve").headers["location"]


def test_a_business_cancels_an_unpaid_booking(site):
    code = _code(_book(site))
    assert site.post(f"/advertise/booking/{code}/cancel").status_code == 403
    done = site.post(f"/advertise/booking/{code}/cancel", headers=FROM_SITE, follow_redirects=False)
    assert done.headers["location"].endswith("?note=cancelled")
    assert _booking(code).status == "cancelled"
    assert (
        site.get(f"/advertise/booking/{code}/pay", follow_redirects=False).headers["location"]
        == f"/advertise/booking/{code}"
    )
    assert not site.paypal.calls("/v2/checkout/orders")
    assert site.get("/advertise/booking/not-a-code").status_code == 404


def test_the_booking_form_checks_what_it_is_given(site):
    blocked = site.post("/advertise", data={"kind": "site"}, follow_redirects=False)
    assert blocked.status_code == 403
    response = _book(
        site,
        business="",
        email="not an email",
        link="javascript:alert(1)",
        words="",
        day=["2001-01-01"],
    )
    assert response.status_code == 400
    for words in (
        "Pick at least one week.",
        "Give the business&#39;s name",
        "That doesn&#39;t look like an email address.",
        "Give the full address of the website",
        "Say what the picture shows",
    ):
        assert words in response.text, words
    no_picture = site.post(
        "/advertise",
        data={
            "kind": "site",
            "day": [_week().isoformat()],
            "business": "Island Bakery",
            "email": "a@b.example",
            "link": "https://b.example",
            "words": "Bread",
        },
        headers=FROM_SITE,
    )
    assert "Choose the picture for the sponsor spot." in no_picture.text
    small = site.post(
        "/advertise",
        data={
            "kind": "site",
            "day": [_week().isoformat()],
            "business": "Island Bakery",
            "email": "a@b.example",
            "link": "https://b.example",
            "words": "Bread",
        },
        files={"picture": ("a.png", _picture(100, 100), "image/png")},
        headers=FROM_SITE,
    )
    assert "only 100 pixels wide" in small.text
    # What the site never shows can't be bought either.
    refused = _book(site, business="Guam Chamber of Commerce")
    assert "Sorry, this ad can&#39;t be booked on this site." in refused.text
    # The field that only robots fill in.
    robot = _book(site, website="https://spam.example")
    assert robot.headers["location"] == "/advertise"
    too_big = site.post(
        "/advertise",
        content=b"x",
        headers={**FROM_SITE, "content-length": str(ads.MAX_PICTURE_BYTES + 10**6)},
    )
    assert too_big.status_code == 413
    with session_scope() as session:
        assert session.scalars(select(AdBooking)).all() == []


def _story(session, n=1) -> int:
    source = session.scalar(select(Source).where(Source.slug == "kuam"))
    if source is None:
        source = Source(slug="kuam", name="KUAM News", homepage="https://www.kuam.com/")
        session.add(source)
        session.flush()
    article = Article(
        source_id=source.id,
        url=f"https://www.kuam.com/story/{n}",
        title=f"Story {n}",
        intro="A story.",
        categories=["local"],
        published_at=datetime.now(timezone.utc),
    )
    session.add(article)
    session.flush()
    return article.id


def test_newsletter_sponsor_line_in_the_feed(site):
    edition_day = ads.edition_choices(
        datetime.now(timezone.utc), (1, 4), datetime.min.time().replace(hour=10)
    )[0]
    response = _book(
        site,
        kind="newsletter",
        days=[edition_day],
        words="Fresh pandesal every morning in Tamuning. Order ahead online!",
    )
    code = _code(response)
    booking = _booking(code)
    assert booking.picture is None and booking.amount_cents == 1500
    _pay(site, code)
    _decide(site, booking.id, "approve")
    formed_at = datetime.combine(edition_day, datetime.min.time().replace(hour=10), tzinfo=GUAM_TZ)
    with session_scope() as session:
        story = _story(session)
        session.add(NewsletterEdition(formed_at=formed_at, article_ids=[story]))
    feed = site.get("/feed.xml").text
    first = feed.split("<item>")[1]
    assert "<title>Sponsor: Island Bakery</title>" in first
    assert f"<link>http://testserver/go/ad/{booking.id}</link>" in first
    assert "Fresh pandesal every morning in Tamuning. Order ahead online!" in first
    assert f"#ad-{booking.id}-{edition_day.isoformat()}</guid>" in first
    # The stories' links say they came from the newsletter, and are counted so.
    assert f"<link>http://testserver/go/{story}?from=newsletter</link>" in feed
    site.get(f"/go/{story}?from=newsletter", headers=BROWSER, follow_redirects=False)
    site.get(f"/go/ad/{booking.id}", headers=BROWSER, follow_redirects=False)
    from guheadlines.web import stats

    stats.counts.flush()
    with session_scope() as session:
        counted = dict(session.execute(select(PageViews.path, PageViews.views)).all())
    assert counted["newsletter-click"] == 1 and counted[f"ad-click:{booking.id}"] == 1
    assert "sponsor-click" not in counted  # not the sponsor spot
    # Not in other editions.
    with session_scope() as session:
        session.add(NewsletterEdition(formed_at=formed_at + timedelta(days=3), article_ids=[story]))
    assert "Sponsor:" not in site.get("/feed.xml").text
