"""Paid listings (listings.py): the forms, the PayPal payment, the review page,
and where approved listings show."""

from __future__ import annotations

import base64
import dataclasses
import json
import re
from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest
from conftest import make_jpeg
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from guheadlines import listings, mail, paypal, renewals
from guheadlines.db import session_scope
from guheadlines.models import FEATURED_CALENDAR, Event, EventCalendar, Listing, ListingRenewal
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import load_settings

FROM_SITE = {"origin": "http://testserver"}
PASSWORD = "review-password"
ADMIN = {"authorization": "Basic " + base64.b64encode(f"me:{PASSWORD}".encode()).decode()}


class FakePayPal:
    """PayPal's API for several orders: each new order gets the next number.
    `paid` sets what each capture reports."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.paid_value: str | None = None  # None: the order's own amount
        self.capture_status = "COMPLETED"
        self.refund_fails = False
        self.refunds: list[str] = []
        self.captures = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/v2/checkout/orders":
            body = json.loads(request.content)
            order_id = f"ORDER-{len(self.orders) + 1}"
            self.orders[order_id] = {"status": "PAYER_ACTION_REQUIRED", "body": body}
            link = f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
            return httpx.Response(
                201, json={"id": order_id, "links": [{"rel": "payer-action", "href": link}]}
            )
        order_id = path.split("/")[4] if path.startswith("/v2/checkout/orders/") else None
        if order_id and order_id in self.orders:
            order = self.orders[order_id]
            if path.endswith("/capture"):
                self.captures += 1
                order["status"] = "COMPLETED"
            return httpx.Response(200, json=self.describe(order_id))
        if path.endswith("/refund"):
            if self.refund_fails:
                return httpx.Response(
                    422, json={"details": [{"issue": "REFUND_TIME_LIMIT_EXCEEDED"}]}
                )
            self.refunds.append(path.split("/")[4])
            return httpx.Response(201, json={"id": "R-1", "status": "COMPLETED"})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})

    def describe(self, order_id: str) -> dict:
        order = self.orders[order_id]
        unit = order["body"]["purchase_units"][0]
        # PayPal shows the order's custom_id, as create_order gave it.
        answer = {
            "id": order_id,
            "status": order["status"],
            "purchase_units": [{"custom_id": unit["custom_id"], "amount": unit["amount"]}],
        }
        if order["status"] == "COMPLETED":
            amount = unit["amount"]
            value = self.paid_value or amount["value"]
            capture = {
                "id": f"CAP-{order_id}",
                "status": self.capture_status,
                "amount": {"currency_code": "USD", "value": value},
            }
            answer["purchase_units"][0]["payments"] = {"captures": [capture]}
            answer["payer"] = {"email_address": "buyer@example.com"}
        return answer

    def approve(self, order_id: str) -> None:
        """The buyer approves the order on PayPal's page."""
        self.orders[order_id]["status"] = "APPROVED"


def _set(monkeypatch, **values):
    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, **values)
    monkeypatch.setattr(web, "settings", site)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)
    return site


@pytest.fixture()
def fake_paypal(monkeypatch):
    fake = FakePayPal()
    real = paypal._client
    monkeypatch.setattr(
        paypal, "_client", lambda transport: real(httpx.MockTransport(fake.handler))
    )
    paypal._tokens.clear()
    yield fake
    paypal._tokens.clear()


@pytest.fixture()
def client(clean_db, monkeypatch, fake_paypal):
    from guheadlines.web.app import app, featured_cache, listing_limit, sidebar_cache

    sidebar_cache._data.clear()
    featured_cache._data.clear()
    listing_limit._by_address.clear()
    listing_limit._all.clear()
    _set(
        monkeypatch,
        paypal_client_id="id",
        paypal_client_secret="secret",
        stats_password=PASSWORD,
        discord_webhook_url="",
    )
    return TestClient(app)


def _post(client, kind, photo=None, **fields):
    # Browsers send the photo field empty when no photo is chosen.
    files = {
        "photo": ("photo.jpg", photo, "image/jpeg")
        if photo
        else ("", b"", "application/octet-stream")
    }
    return client.post(
        f"/post/{kind}", data=fields, files=files, headers=FROM_SITE, follow_redirects=False
    )


CLASSIFIED = {
    "title": "2015 Toyota Corolla",
    "category": "Cars and trucks",
    "price": "$4,500",
    "location": "Dededo",
    "description": "Runs well, new tires, cold AC.",
    "contact": "671-555-0100",
}


def _new(client, kind, photo=None, **fields) -> Listing:
    response = _post(client, kind, photo, **fields)
    assert response.status_code == 303, response.text
    found = re.fullmatch(r"/listing/(\d+)/([\w-]+)", response.headers["location"])
    with session_scope() as session:
        return session.get(Listing, int(found[1]))


def _pay(client, fake, listing: Listing) -> str:
    """Pay for a listing as a buyer does; returns the result of the payment."""
    page = f"/listing/{listing.id}/{listing.key}"
    to_paypal = client.get(f"{page}/pay", follow_redirects=False)
    assert to_paypal.status_code == 303
    order_id = to_paypal.headers["location"].split("token=")[1]
    fake.approve(order_id)
    back = client.get(
        f"{page}/paid", params={"token": order_id, "PayerID": "X"}, follow_redirects=False
    )
    assert back.status_code == 303
    return back.headers["location"].split("paid=")[1]


def _review(client, listing_id, action, **extra):
    return client.post(
        f"/admin/listings/{listing_id}",
        data={"action": action, **extra},
        headers={**ADMIN, **FROM_SITE},
        follow_redirects=False,
    )


def _get(listing_id) -> Listing:
    with session_scope() as session:
        return session.get(Listing, listing_id)


def test_listings_are_off_without_paypal_and_a_password(clean_db, monkeypatch):
    from guheadlines.web.app import app, sidebar_cache

    sidebar_cache._data.clear()
    _set(monkeypatch, paypal_client_id="", stats_password=PASSWORD)
    client = TestClient(app)
    for path in ("/classifieds", "/directory", "/post/classified", "/post/job"):
        assert client.get(path).status_code == 404
    assert "/classifieds" not in client.get("/").text
    assert "Post a job" not in client.get("/jobs").text
    _set(monkeypatch, paypal_client_id="id", paypal_client_secret="s", stats_password="")
    assert client.get("/post/classified").status_code == 404
    assert client.get("/admin/listings").status_code == 404


def test_a_classified_ad_from_form_to_page(client, fake_paypal):
    from guheadlines.web.app import CONTENT_SECURITY_POLICY

    home = client.get("/")
    assert 'href="/classifieds"' in home.text and 'href="/directory"' in home.text
    form = client.get("/post/classified")
    assert form.status_code == 200
    assert 'enctype="multipart/form-data"' in form.text
    assert form.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "noindex" in form.text

    listing = _new(client, "classified", make_jpeg(900, 600), **CLASSIFIED)
    assert (listing.status, listing.amount_cents) == ("unpaid", 500)
    assert listing.photo.startswith("listings/") and listing.photo_width == 720
    page = client.get(f"/listing/{listing.id}/{listing.key}")
    assert page.status_code == 200
    assert "Pay $5 with PayPal" in page.text and "2015 Toyota Corolla" in page.text
    assert page.headers["cache-control"] == "private, no-store"
    assert client.get(f"/listing/{listing.id}/wrong-key").status_code == 404

    assert _pay(client, fake_paypal, listing) == "ok"
    order = fake_paypal.orders["ORDER-1"]["body"]
    assert order["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "5.00"}
    assert order["purchase_units"][0]["custom_id"] == f"listing-{listing.id}"
    context = order["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == f"http://testserver/listing/{listing.id}/{listing.key}/paid"
    paid = _get(listing.id)
    assert (paid.status, paid.capture_id, paid.order_id) == ("review", "CAP-ORDER-1", "ORDER-1")
    assert (
        "Thank you, your payment went through"
        in client.get(f"/listing/{listing.id}/{listing.key}?paid=ok").text
    )
    # Back again (a reload): nothing changes, and PayPal is not asked to capture twice.
    client.get(f"/listing/{listing.id}/{listing.key}/paid?token=ORDER-1")
    assert fake_paypal.captures == 1

    assert "Corolla" not in client.get("/classifieds").text  # not reviewed yet
    assert client.get("/admin/listings").status_code == 401
    admin = client.get("/admin/listings", headers=ADMIN)
    assert admin.status_code == 200 and "2015 Toyota Corolla" in admin.text
    assert "Paid in the last 30 days: <strong>$5</strong>" in admin.text
    assert _review(client, listing.id, "approve").headers["location"].endswith("done=approved")
    live = _get(listing.id)
    assert live.status == "live"
    assert live.live_until - live.reviewed_at == timedelta(days=30)

    shown = client.get("/classifieds")
    assert "2015 Toyota Corolla" in shown.text and f"/media/{live.photo}" in shown.text
    # The section choices must not replace the topic links in the menu.
    assert 'href="/category/military"' in shown.text
    directory = client.get("/directory").text
    assert 'href="/category/military"' in directory
    # The directory is not the Business news topic.
    assert 'href="/category/business" aria-current' not in directory
    assert "671-555-0100" in shown.text
    assert "Corolla" not in client.get("/classifieds?category=Housing").text
    assert "Corolla" in client.get("/classifieds?q=corolla").text
    assert "Corolla" not in client.get("/classifieds?q=boat").text
    assert client.get(f"/media/{live.photo}").status_code == 200


def test_form_problems_are_shown_beside_the_fields(client):
    response = _post(client, "classified", title="ab", category="Boats", description="x")
    assert response.status_code == 400
    assert "Please give a title of at least 3 letters." in response.text
    assert "Please pick a section." in response.text
    assert "Please give a phone number or an email address." in response.text
    assert 'aria-invalid="true"' in response.text
    with session_scope() as session:
        assert session.scalar(select(Listing)) is None


def test_muted_words_and_robots_are_refused(client, monkeypatch):
    from guheadlines.web import app as web

    config = web.config_cache.get()
    monkeypatch.setattr(
        web.config_cache, "get", lambda: dataclasses.replace(config, muted=("forbidden club",))
    )
    response = _post(
        client, "classified", **{**CLASSIFIED, "description": "From the Forbidden Club."}
    )
    assert response.status_code == 400
    assert "words that the site does not accept" in response.text
    robot = _post(client, "classified", **CLASSIFIED, website="spam.example")
    assert robot.status_code == 303 and robot.headers["location"] == "/post/classified"
    elsewhere = client.post(
        "/post/classified",
        data=CLASSIFIED,
        files={"x": ("a", b"b")},
        headers={"origin": "https://evil.example"},
    )
    assert elsewhere.status_code == 403
    with session_scope() as session:
        assert session.scalar(select(Listing)) is None


def test_a_bad_photo_is_refused(client):
    response = _post(client, "classified", b"not a picture", **CLASSIFIED)
    assert response.status_code == 400
    assert "The photo could not be used" in response.text


def test_a_wrong_or_reused_payment_is_not_used(client, fake_paypal):
    first = _new(client, "classified", **CLASSIFIED)
    fake_paypal.paid_value = "1.00"
    assert _pay(client, fake_paypal, first) == "mismatch"
    assert _get(first.id).status == "unpaid"
    fake_paypal.paid_value = None
    assert _pay(client, fake_paypal, first) == "ok"
    # The first listing's payment, brought back to another listing's page.
    second = _new(client, "classified", **CLASSIFIED)
    reused = client.get(
        f"/listing/{second.id}/{second.key}/paid?token=ORDER-2", follow_redirects=False
    )
    assert reused.headers["location"].endswith("paid=mismatch")
    assert _get(second.id).status == "unpaid"


def test_an_unfinished_and_a_pending_payment(client, fake_paypal):
    listing = _new(client, "classified", **CLASSIFIED)
    page = f"/listing/{listing.id}/{listing.key}"
    client.get(f"{page}/pay", follow_redirects=False)
    # Back without approving (PayPal's cancel link leads to the page itself).
    back = client.get(f"{page}/paid?token=ORDER-1", follow_redirects=False)
    assert back.headers["location"].endswith("paid=unfinished")
    fake_paypal.capture_status = "PENDING"
    assert _pay(client, fake_paypal, listing) == "pending"
    assert _get(listing.id).status == "unpaid"
    assert "Check with PayPal" in client.get("/admin/listings", headers=ADMIN).text
    fake_paypal.capture_status = "COMPLETED"
    assert _review(client, listing.id, "check").headers["location"].endswith("done=paid")
    assert _get(listing.id).status == "review"


def test_rejecting_gives_the_money_back(client, fake_paypal):
    listing = _new(client, "classified", make_jpeg(), **CLASSIFIED)
    _pay(client, fake_paypal, listing)
    fake_paypal.refund_fails = True
    assert _review(client, listing.id, "reject").headers["location"].endswith("refund-failed")
    assert _get(listing.id).status == "review"
    fake_paypal.refund_fails = False
    response = _review(client, listing.id, "reject", note="No car ads this week.")
    assert response.headers["location"].endswith("done=rejected")
    assert fake_paypal.refunds == ["CAP-ORDER-1"]
    rejected = _get(listing.id)
    assert rejected.status == "rejected" and rejected.refunded_at is not None
    page = client.get(f"/listing/{listing.id}/{listing.key}").text
    assert "No car ads this week." in page and "given back to you through PayPal" in page
    from guheadlines.web.app import settings as site

    assert not (site.media_dir / listing.photo).exists()
    assert "Corolla" not in client.get("/classifieds").text


def test_review_buttons_need_the_password_and_this_site(client):
    listing = _new(
        client,
        "business",
        title="Island Plumbing",
        category="Home and building",
        location="Yigo",
        description="Pipes, leaks and water heaters.",
        plan="basic",
    )
    no_password = client.post(
        f"/admin/listings/{listing.id}", data={"action": "approve"}, headers=FROM_SITE
    )
    assert no_password.status_code == 401
    elsewhere = client.post(
        f"/admin/listings/{listing.id}",
        data={"action": "approve"},
        headers={**ADMIN, "origin": "https://evil.example"},
    )
    assert elsewhere.status_code == 403
    assert _get(listing.id).status == "review"


def test_the_directory_free_and_featured(client, fake_paypal):
    basic = _new(
        client,
        "business",
        make_jpeg(),
        title="Island Plumbing",
        category="Home and building",
        location="Yigo",
        description="Pipes, leaks and water heaters.",
        link="islandplumbing.example",
        plan="basic",
    )
    assert (basic.status, basic.amount_cents, basic.featured) == ("review", 0, False)
    assert basic.link == "https://islandplumbing.example"
    page = client.get(f"/listing/{basic.id}/{basic.key}").text
    assert "Waiting for review" in page and "/pay" not in page
    top = _new(
        client,
        "business",
        make_jpeg(),
        title="Zeta Pizza",
        category="Restaurants and food",
        location="Tamuning",
        description="Wood-fired pizza, open late.",
        link="https://zeta.example",
        plan="featured",
    )
    assert (top.status, top.amount_cents) == ("unpaid", 5000)
    assert _pay(client, fake_paypal, top) == "ok"
    _review(client, basic.id, "approve")
    _review(client, top.id, "approve")
    assert _get(top.id).live_until - _get(top.id).reviewed_at == timedelta(days=365)
    assert _get(basic.id).live_until is None

    html = client.get("/directory").text
    assert html.index("Zeta Pizza") < html.index("Island Plumbing")  # featured first
    assert 'href="https://zeta.example" target="_blank" rel="sponsored noopener"' in html
    assert "islandplumbing.example" not in html  # a basic entry has no link
    assert f"/media/{_get(top.id).photo}" in html and f"/media/{basic.photo}" not in html
    assert "Zeta" not in client.get("/directory?category=Shopping").text
    sitemap = client.get("/sitemap.xml").text
    assert "/directory</loc>" in sitemap and "/classifieds</loc>" in sitemap
    robots = client.get("/robots.txt").text
    assert "Disallow: /listing/" in robots and "Disallow: /admin/" in robots


def test_a_featured_job_is_pinned_on_the_jobs_page(client, fake_paypal):
    job = _new(
        client,
        "job",
        title="Line cook",
        name="Tumon Grill",
        location="Tumon",
        price="$15 an hour",
        description="Evenings and weekends. Experience helps.",
        contact="jobs@tumongrill.example",
    )
    assert job.amount_cents == 2500
    _pay(client, fake_paypal, job)
    _review(client, job.id, "approve")
    assert _get(job.id).live_until - _get(job.id).reviewed_at == timedelta(days=7)
    html = client.get("/jobs").text
    assert "Line cook" in html and "Tumon Grill" in html and "tag-featured" in html
    assert "Line cook" in client.get("/jobs?q=cook").text
    assert "Line cook" not in client.get("/jobs?q=nurse").text
    # Taken down: gone from the page.
    assert _review(client, job.id, "remove").headers["location"].endswith("done=removed")
    assert "Line cook" not in client.get("/jobs").text


def test_a_job_needs_a_way_to_apply(client):
    response = _post(
        client,
        "job",
        title="Line cook",
        name="Tumon Grill",
        description="Evenings and weekends.",
        link="not a link",
    )
    assert response.status_code == 400
    assert "look like a web address." in response.text
    assert "Please say how to apply" in response.text


def test_a_featured_event_is_on_the_calendar_and_the_home_page(client, fake_paypal):
    from guheadlines.events.pipeline import sync_calendars

    day = datetime.now(GUAM_TZ).date() + timedelta(days=3)
    event = _new(
        client,
        "event",
        title="Hafa Adai Night Market",
        name="Village Council",
        date=day.isoformat(),
        start_time="17:00",
        end_time="21:00",
        location="Chamorro Village, Hagåtña",
        description="Food and music.",
        link="https://market.example",
    )
    assert event.starts_at == datetime.combine(day, time(17), tzinfo=GUAM_TZ)
    _pay(client, fake_paypal, event)
    _review(client, event.id, "approve")
    approved = _get(event.id)
    with session_scope() as session:
        copy = session.get(Event, approved.event_id)
        calendar = session.get(EventCalendar, copy.calendar_id)
        assert (calendar.slug, copy.title, copy.organizer) == (
            FEATURED_CALENDAR,
            "Hafa Adai Night Market",
            "Village Council",
        )
        # Reading events.yaml leaves the featured calendar on.
        sync_calendars(session, [])
        assert calendar.enabled
    assert approved.live_until == datetime.combine(day, time(21), tzinfo=GUAM_TZ)

    home = client.get("/").text
    assert "Featured events" in home and f'href="/events/{copy.id}"' in home
    events = client.get("/events").text
    assert "Featured events" in events and "cal-gold" in events and "tag-featured" in events
    page = client.get(f"/events/{copy.id}").text
    assert 'href="https://market.example" target="_blank" rel="sponsored noopener"' in page
    assert "Hafa Adai Night Market" in client.get("/events.ics").text

    _review(client, event.id, "remove")
    from guheadlines.web.app import featured_cache

    featured_cache._data.clear()
    assert client.get(f"/events/{copy.id}").status_code == 404
    assert "Hafa Adai" not in client.get("/").text


def test_event_dates_are_checked():
    today = date(2026, 12, 1)

    def times(**fields):
        values, errors = listings.check(
            "event", {"title": "Fiesta", "name": "Parish", "location": "Agat", **fields}, today
        )
        return values, errors

    values, errors = times(date="2026-12-05")
    assert not errors and values["all_day"]
    assert values["ends_at"] - values["starts_at"] == timedelta(days=1)
    values, errors = times(date="2026-12-05", end_date="2026-12-07")
    assert values["ends_at"] == datetime(2026, 12, 8, tzinfo=GUAM_TZ)
    assert "date" in times(date="2026-11-30")[1]
    assert "date" in times(date="2027-06-01")[1]
    assert "end_date" in times(date="2026-12-05", end_date="2026-12-30")[1]
    assert "end_time" in times(date="2026-12-05", start_time="18:00", end_time="17:00")[1]


@pytest.mark.parametrize(
    "raw, link",
    [
        ("example.com", "https://example.com"),
        ("http://example.com/a?b=1", "http://example.com/a?b=1"),
        ("javascript:alert(1)", None),
        ("not a link", None),
        ("https://user@evil.example", None),
    ],
)
def test_web_links(raw, link):
    assert listings._web_link(raw) == link


def test_parse_form_keeps_the_photo_exact():
    photo = bytes(range(256)) * 50 + b"\r\n--x\r\n\r\n"
    request = httpx.Request(
        "POST", "http://x/", data={"title": "Café ☕"}, files={"photo": ("p.jpg", photo)}
    )
    fields, found = listings.parse_form(request.read(), request.headers["content-type"])
    assert fields == {"title": "Café ☕"} and found == photo
    with pytest.raises(listings.FormError):
        listings.parse_form(b"title=x", "application/x-www-form-urlencoded")


def test_unpaid_listings_are_deleted_after_a_week(client):
    listing = _new(client, "classified", make_jpeg(), **CLASSIFIED)
    with session_scope() as session:
        session.get(Listing, listing.id).created_at = datetime.now(timezone.utc) - timedelta(days=8)
    _new(client, "classified", **CLASSIFIED)
    assert _get(listing.id) is None
    from guheadlines.web.app import settings as site

    assert not (site.media_dir / listing.photo).exists()


def test_stats_page_links_to_the_review(client):
    _new(
        client,
        "business",
        title="Island Plumbing",
        category="Home and building",
        location="Yigo",
        description="Pipes, leaks and water heaters.",
        plan="basic",
    )
    stats = client.get("/stats", headers=ADMIN).text
    assert 'href="/admin/listings"' in stats and "<strong>1</strong> waiting for review" in stats


def test_price_settings(monkeypatch):
    monkeypatch.setenv("CLASSIFIED_PRICE", "$7.50")
    monkeypatch.setenv("FEATURED_JOB_PRICE", "0")
    monkeypatch.setenv("BUSINESS_UPGRADE_DAYS", "30")
    site = dataclasses.replace(
        load_settings(), paypal_client_id="id", paypal_client_secret="s", stats_password="p"
    )
    assert site.classified_price_cents == 750 and site.business_upgrade_days == 30
    assert listings.is_on(site, "classified") and not listings.is_on(site, "job")
    assert listings.money(750) == "$7.50" and listings.money(5000) == "$50"


# ---------------------------------------------------------------- renewals


def _live(client, fake, kind="classified", **fields) -> Listing:
    listing = _new(client, kind, **(fields or CLASSIFIED))
    assert _pay(client, fake, listing) == "ok"
    _review(client, listing.id, "approve")
    return _get(listing.id)


def _renew(client, fake, listing: Listing, approve: bool = True) -> str:
    """Press "Renew", approve on PayPal, and come back; returns the result."""
    page = f"/listing/{listing.id}/{listing.key}"
    to_paypal = client.get(f"{page}/renew", follow_redirects=False)
    assert to_paypal.status_code == 303 and "paypal.com" in to_paypal.headers["location"]
    order_id = to_paypal.headers["location"].split("token=")[1]
    if approve:
        fake.approve(order_id)
    back = client.get(f"{page}/renewed", params={"token": order_id}, follow_redirects=False)
    assert back.status_code == 303
    return back.headers["location"].split("renewed=")[1]


def _end(listing_id: int, when: datetime) -> None:
    with session_scope() as session:
        session.execute(update(Listing).where(Listing.id == listing_id).values(live_until=when))


def test_a_classified_ad_is_renewed_through_paypal(client, fake_paypal):
    waiting = _new(client, "classified", **CLASSIFIED)
    _pay(client, fake_paypal, waiting)
    page = f"/listing/{waiting.id}/{waiting.key}"
    assert "/renew" not in client.get(page).text  # not on the site yet
    assert client.get(f"{page}/renew", follow_redirects=False).headers["location"] == page
    assert _get(waiting.id).email == "buyer@example.com"  # from PayPal
    _review(client, waiting.id, "approve")
    live = _get(waiting.id)
    text = client.get(page).text
    assert "Renew for $5 with PayPal" in text and "30 days more" in text

    # Back from PayPal without paying: nothing changes.
    assert _renew(client, fake_paypal, live, approve=False) == "unfinished"
    assert _get(live.id).live_until == live.live_until
    assert _renew(client, fake_paypal, live) == "ok"
    order = fake_paypal.orders["ORDER-3"]["body"]
    unit = order["purchase_units"][0]
    assert unit["amount"]["value"] == "5.00" and unit["custom_id"] == f"listing-{live.id}-renewal"
    context = order["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == f"http://testserver{page}/renewed"
    renewed = _get(live.id)
    assert renewed.live_until - live.live_until == timedelta(days=30)
    assert renewed.status == "live" and renewed.reviewed_at == live.reviewed_at
    assert "See the new end date below" in client.get(f"{page}?renewed=ok").text
    # A reload takes no more money and adds no more days.
    captures = fake_paypal.captures
    client.get(f"{page}/renewed?token=ORDER-3")
    assert fake_paypal.captures == captures
    assert _get(live.id).live_until == renewed.live_until
    # An order that is not a renewal of this listing is not even sent to PayPal.
    other = client.get(f"{page}/renewed?token=ORDER-1", follow_redirects=False)
    assert other.headers["location"].endswith("renewed=unfinished")
    assert fake_paypal.captures == captures
    admin = client.get("/admin/listings", headers=ADMIN).text
    assert "Paid in the last 30 days: <strong>$10</strong> from 2 payments" in admin

    # Ended: off the Classifieds page, and a renewal counts from today.
    _end(live.id, datetime.now(timezone.utc) - timedelta(days=3))
    assert "Corolla" not in client.get("/classifieds").text
    assert "It ended on" in client.get(page).text
    assert _renew(client, fake_paypal, live) == "ok"
    back = _get(live.id).live_until - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < back <= timedelta(days=30)
    assert "Corolla" in client.get("/classifieds").text


def test_one_payment_never_pays_for_two_things(client, fake_paypal):
    """The order's ID comes back from the buyer's browser, so a buyer could
    send the ID of another order: here, a renewal of the same price."""
    live = _live(client, fake_paypal)
    page = f"/listing/{live.id}/{live.key}"
    to_paypal = client.get(f"{page}/renew", follow_redirects=False)
    order_id = to_paypal.headers["location"].split("token=")[1]
    fake_paypal.approve(order_id)
    other = _new(client, "classified", **CLASSIFIED)
    other_page = f"/listing/{other.id}/{other.key}"
    captures = fake_paypal.captures
    back = client.get(f"{other_page}/paid", params={"token": order_id}, follow_redirects=False)
    assert back.headers["location"].endswith("paid=mismatch")
    assert _get(other.id).status == "unpaid"
    assert fake_paypal.captures == captures  # not even taken from the buyer
    # The renewal that it was made for still goes through.
    back = client.get(f"{page}/renewed", params={"token": order_id}, follow_redirects=False)
    assert back.headers["location"].endswith("renewed=ok")
    # Once paid, it still can't pay for the new listing.
    back = client.get(f"{other_page}/paid", params={"token": order_id}, follow_redirects=False)
    assert back.headers["location"].endswith("paid=mismatch")
    assert _get(other.id).status == "unpaid"
    # Nor can the new listing's own order renew the live one.
    to_paypal = client.get(f"{other_page}/pay", follow_redirects=False)
    own = to_paypal.headers["location"].split("token=")[1]
    fake_paypal.approve(own)
    assert (
        client.get(f"{page}/renewed?token={own}", follow_redirects=False)
        .headers["location"]
        .endswith("renewed=unfinished")
    )
    assert _pay_back(client, other_page, own) == "ok"


def _pay_back(client, page: str, order_id: str) -> str:
    back = client.get(f"{page}/paid", params={"token": order_id}, follow_redirects=False)
    return back.headers["location"].split("paid=")[1]


def test_a_listing_taken_down_while_it_is_renewed_gets_its_money_back(client, fake_paypal):
    live = _live(client, fake_paypal)
    page = f"/listing/{live.id}/{live.key}"
    to_paypal = client.get(f"{page}/renew", follow_redirects=False)
    order_id = to_paypal.headers["location"].split("token=")[1]
    fake_paypal.approve(order_id)
    _review(client, live.id, "remove")
    back = client.get(f"{page}/renewed?token={order_id}", follow_redirects=False)
    assert back.headers["location"].endswith("renewed=gone")
    assert fake_paypal.refunds == [f"CAP-{order_id}"]
    with session_scope() as session:
        assert session.scalars(select(ListingRenewal)).all() == []
    assert "/renew" not in client.get(page).text


def test_a_business_upgrade_can_be_renewed_but_an_event_cannot(client, fake_paypal):
    business = _live(
        client,
        fake_paypal,
        "business",
        title="Zeta Pizza",
        category="Restaurants and food",
        location="Tamuning",
        description="Wood-fired pizza, open late.",
        plan="featured",
    )
    page = client.get(f"/listing/{business.id}/{business.key}").text
    assert "Renew for $50 with PayPal" in page and "of the featured upgrade" in page
    _end(business.id, datetime.now(timezone.utc) - timedelta(days=1))
    page = client.get(f"/listing/{business.id}/{business.key}").text
    assert "Its featured upgrade ended on" in page
    assert _renew(client, fake_paypal, business) == "ok"
    assert listings.upgraded(_get(business.id), datetime.now(timezone.utc))

    day = datetime.now(GUAM_TZ).date() + timedelta(days=3)
    event = _live(
        client,
        fake_paypal,
        "event",
        title="Hafa Adai Night Market",
        name="Village Council",
        date=day.isoformat(),
        location="Chamorro Village, Hagåtña",
    )
    assert "/renew" not in client.get(f"/listing/{event.id}/{event.key}").text
    orders = len(fake_paypal.orders)
    client.get(f"/listing/{event.id}/{event.key}/renew")
    assert len(fake_paypal.orders) == orders


def _mail_settings(**values):
    from guheadlines.web.app import settings as site

    return dataclasses.replace(
        site,
        smtp_host="mail.example",
        mail_from="GU Headlines <alerts@guamheadlines.example>",
        site_url="https://guamheadlines.example",
        **values,
    )


def test_a_reminder_email_goes_out_before_a_listing_ends(client, fake_paypal, monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send", lambda settings, to, *email, **kw: sent.append((to, *email)))
    live = _live(client, fake_paypal)
    site = _mail_settings()
    assert renewals.send_reminders(site) == 0  # 30 days to go
    soon = live.live_until - timedelta(days=2)
    assert renewals.send_reminders(dataclasses.replace(site, renewal_reminder_days=0), soon) == 0
    assert renewals.send_reminders(dataclasses.replace(site, smtp_host=""), soon) == 0
    assert renewals.send_reminders(site, soon) == 1
    (to, subject, text, html) = sent[0]
    assert to == "buyer@example.com"
    assert subject.startswith("Your classified ad ends on ")
    link = f"https://guamheadlines.example/listing/{live.id}/{live.key}"
    assert f"Renew it here: {link}" in text and "renew it for $5" in text
    assert f'href="{link}"' in html and "Renew for $5" in html
    assert renewals.send_reminders(site, soon) == 0  # one email only
    # After a renewal, the next end gets its own email.
    _renew(client, fake_paypal, live)
    assert _get(live.id).reminded_at is None
    assert renewals.send_reminders(site, _get(live.id).live_until - timedelta(days=1)) == 1
    # No email after the end.
    _end(live.id, soon)
    with session_scope() as session:
        session.execute(update(Listing).values(reminded_at=None))
    assert renewals.send_reminders(site, soon + timedelta(hours=1)) == 0


def test_a_failed_reminder_is_tried_again(client, fake_paypal, monkeypatch):
    def broken(*args, **kwargs):
        raise mail.MailError("the email service answered 451")

    monkeypatch.setattr(mail, "send", broken)
    live = _live(client, fake_paypal)
    soon = live.live_until - timedelta(days=1)
    assert renewals.send_reminders(_mail_settings(), soon) == 0
    assert _get(live.id).reminded_at is None
    monkeypatch.setattr(mail, "send", lambda *args, **kwargs: None)
    assert renewals.send_reminders(_mail_settings(), soon) == 1
    assert _get(live.id).reminded_at == soon
