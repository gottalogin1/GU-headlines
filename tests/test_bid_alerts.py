"""Paid bid alerts (bids/alerts.py), their pages, and sending email (mail.py)."""

from __future__ import annotations

import dataclasses
import json
import smtplib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from guheadlines import mail, paypal
from guheadlines.bids import alerts
from guheadlines.db import session_scope
from guheadlines.models import Bid, BidAlertPlan, BidAlertSubscriber, BidSource
from guheadlines.settings import load_settings

FROM_SITE = {"origin": "http://testserver"}
ON = {
    "bid_alerts_price_cents": 1500,
    "paypal_client_id": "client-id",
    "paypal_client_secret": "secret",
    "smtp_host": "smtp.example.com",
    "mail_from": "GU Headlines <alerts@example.com>",
    "site_url": "https://news.example.com",
}


def _settings(**values):
    return dataclasses.replace(load_settings(), **{**ON, **values})


# ---------------------------------------------------------------- settings


def test_bid_alert_and_mail_settings(monkeypatch):
    for name in ("BID_ALERTS_PRICE", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "MAIL_FROM"):
        monkeypatch.delenv(name, raising=False)
    off = load_settings()
    assert off.bid_alerts_price_cents == 0 and off.smtp_port == 587 and not alerts.is_on(off)
    for typed, cents in (("15", 1500), ("15.00", 1500), ("$9.5", 950), ("$ 9.99", 999)):
        monkeypatch.setenv("BID_ALERTS_PRICE", typed)
        assert load_settings().bid_alerts_price_cents == cents
    for typed in ("", "free", "-5", "15 dollars", "1.234"):
        monkeypatch.setenv("BID_ALERTS_PRICE", typed)
        assert load_settings().bid_alerts_price_cents == 0
    monkeypatch.setenv("SMTP_HOST", " smtp.resend.com ")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("MAIL_FROM", "GU  Headlines   <alerts@guamheadlines.com>")
    site = load_settings()
    assert site.smtp_host == "smtp.resend.com" and site.smtp_port == 465
    assert site.mail_from == "GU Headlines <alerts@guamheadlines.com>"
    assert mail.sender_address(site) == "alerts@guamheadlines.com" and mail.enabled(site)
    assert not mail.enabled(dataclasses.replace(site, mail_from="GU Headlines"))
    # Bid alerts need all four: a price, PayPal, email and the site's address.
    assert alerts.is_on(_settings())
    for missing in ({"bid_alerts_price_cents": 0}, {"paypal_client_secret": ""}, {"site_url": ""}):
        assert not alerts.is_on(_settings(**missing))
    assert alerts.price_text(1500) == "$15" and alerts.price_text(950) == "$9.50"


# ----------------------------------------------------------------- matching


def _item(bid_id: int, title: str, **fields) -> dict:
    bid = Bid(id=bid_id, title=title, url=f"https://example.com/{bid_id}", **fields)
    return {"bid": bid, "ids": [bid_id], "source_name": "Test"}


def test_words_and_trades_find_bids():
    assert alerts.parse_words("roofing, Solar panels\nroofing ; ,, inspect*, ROOFING") == [
        "roofing",
        "Solar panels",
        "inspect*",
    ]
    assert len(alerts.parse_words(",".join(f"w{n}" for n in range(40)))) == alerts.MAX_WORDS
    assert alerts.parse_trades(["medical", "nonsense", "construction"]) == [
        "construction",
        "medical",
    ]
    items = [
        _item(1, "Re-Roofing of Building 3", agency="Department of Public Works"),
        _item(2, "Supply of Diesel Fuel", kind="Invitation for Bid"),
        _item(3, "Financial Audit Services", description="Annual audits of the agency"),
        _item(4, "Solar Panels for the Hagåtña Library"),
    ]
    found = alerts.matching(items, ["construction"], [])
    assert [i["bid"].id for i in found] == [1]
    assert found[0]["matches"] == ["Construction and repairs"]
    found = alerts.matching(items, ["utilities"], ["audit", "hagatna"])
    assert {i["bid"].id: i["matches"] for i in found} == {
        2: ["Power, water and fuel"],
        3: ["audit"],
        4: ["Power, water and fuel", "hagatna"],
    }
    # No trade and no word: every bid.
    assert len(alerts.matching(items, [], [])) == 4
    # "IT services" is a phrase: "it" alone is not a match.
    assert not alerts.matching([_item(5, "Make it shine: floor waxing")], ["it"], [])
    assert alerts.choices_text([], []) == "every new bid"
    assert alerts.choices_text(["food"], ["coffee"]) == "Food and catering; the words “coffee”"


# ----------------------------------------------------------------- sending


class FakeSMTP:
    """smtplib.SMTP and SMTP_SSL, recording what the site does."""

    sent: list = []
    calls: list = []
    refuse_login = False

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.calls.append(("connect", host, port, type(self).__name__))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSMTP.calls.append(("starttls",))

    def login(self, user, password):
        if FakeSMTP.refuse_login:
            raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")
        FakeSMTP.calls.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture()
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.calls, FakeSMTP.refuse_login = [], [], False
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


def test_mail_is_sent_encrypted_with_a_text_and_an_html_part(smtp):
    site = _settings(smtp_user="resend", smtp_password="re_123")
    mail.send(site, "buyer@example.com", "Hello", "Plain words\n", "<p>HTML words</p>")
    assert smtp.calls == [
        ("connect", "smtp.example.com", 587, "FakeSMTP"),
        ("starttls",),
        ("login", "resend", "re_123"),
    ]
    msg = smtp.sent[0]
    assert msg["From"] == "GU Headlines <alerts@example.com>"
    assert msg["To"] == "buyer@example.com" and msg["Subject"] == "Hello"
    assert msg["Message-ID"].endswith("@example.com>")
    assert msg.get_body("plain").get_content() == "Plain words\n"
    assert msg.get_body("html").get_content().strip() == "<p>HTML words</p>"
    smtp.calls.clear()
    mail.send(dataclasses.replace(site, smtp_port=465), "buyer@example.com", "Hi", "Words\n")
    assert smtp.calls[0] == ("connect", "smtp.example.com", 465, "FakeSMTPSSL")
    assert ("starttls",) not in smtp.calls
    smtp.refuse_login = True
    with pytest.raises(mail.MailError, match="SMTP_PASSWORD"):
        mail.send(site, "buyer@example.com", "Hi", "Words\n")
    with pytest.raises(mail.MailError, match="off"):
        mail.send(_settings(smtp_host=""), "buyer@example.com", "Hi", "Words\n")


def test_mail_test_command(smtp, monkeypatch, capsys):
    from guheadlines import cli

    monkeypatch.setattr(cli, "get_settings", lambda: _settings())
    cli.main(["mail-test", "me@example.com"])
    assert "Test email sent to me@example.com" in capsys.readouterr().out
    assert smtp.sent[0]["To"] == "me@example.com"
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(smtp_host=""))
    with pytest.raises(SystemExit, match="SMTP_HOST"):
        cli.main(["mail-test", "me@example.com"])


# ------------------------------------------------------------ PayPal stand-in


class FakePayPal:
    """PayPal's API for subscriptions, as far as the site uses it."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.subscriptions: dict[str, dict] = {}
        self.cancel_status = 204

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 32400})
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/catalogs/products":
            return httpx.Response(201, json={"id": "PROD-1", "name": body["name"]})
        if path == "/v1/billing/plans":
            return httpx.Response(201, json={"id": "P-1", "status": "ACTIVE"})
        if path == "/v1/billing/subscriptions" and request.method == "POST":
            number = f"I-{len(self.subscriptions) + 1}"
            self.subscriptions[number] = {
                "id": number,
                "status": "APPROVAL_PENDING",
                "plan_id": body["plan_id"],
                "custom_id": body["custom_id"],
            }
            approve = (
                f"https://www.sandbox.paypal.com/webapps/billing/subscriptions?ba_token=BA-{number}"
            )
            return httpx.Response(
                201,
                json={
                    "id": number,
                    "status": "APPROVAL_PENDING",
                    "links": [
                        {"href": approve, "rel": "approve", "method": "GET"},
                        {"href": f"https://api/{number}", "rel": "self", "method": "GET"},
                    ],
                },
            )
        if path.startswith("/v1/billing/subscriptions/"):
            number = path.split("/")[4]
            sub = self.subscriptions.get(number)
            if sub is None:
                return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
            if path.endswith("/cancel"):
                if self.cancel_status != 204:
                    return httpx.Response(self.cancel_status, json={"name": "UNPROCESSABLE_ENTITY"})
                sub["status"] = "CANCELLED"
                return httpx.Response(204)
            return httpx.Response(200, json=sub)
        return httpx.Response(404)

    def approve(self, number: str, next_billing: datetime) -> None:
        """The buyer approves the subscription on PayPal's page."""
        self.subscriptions[number]["status"] = "ACTIVE"
        self.subscriptions[number]["billing_info"] = {
            "next_billing_time": next_billing.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "failed_payments_count": 0,
        }


@pytest.fixture()
def fake_paypal(monkeypatch):
    fake = FakePayPal()
    paypal._tokens.clear()
    monkeypatch.setattr(
        paypal,
        "_client",
        lambda transport: httpx.Client(transport=httpx.MockTransport(fake.handler)),
    )
    yield fake
    paypal._tokens.clear()


@pytest.fixture()
def outbox(monkeypatch):
    """Emails the site sends, instead of sending them."""
    sent: list[dict] = []

    def send(settings, to, subject, text, html=None, headers=None):
        sent.append({"to": to, "subject": subject, "text": text, "html": html, **(headers or {})})

    monkeypatch.setattr(mail, "send", send)
    return sent


# -------------------------------------------------------------------- pages


NOW = datetime.now(timezone.utc)


def _add_bids(*bids: dict) -> list[int]:
    """Bids on the Bids page; each dict holds Bid fields (and "source")."""
    ids = []
    with session_scope() as session:
        sources = {s.slug: s for s in session.scalars(select(BidSource))}
        for fields in bids:
            fields = dict(fields)
            slug = fields.pop("source", "notices")
            if slug not in sources:
                sources[slug] = BidSource(slug=slug, name=slug.upper())
                session.add(sources[slug])
                session.flush()
            n = len(ids) + 1 + session.query(Bid).count()
            fields.setdefault("uid", f"uid-{n}")
            fields.setdefault("url", f"https://notices.guam.gov/notice_detail/{n}")
            fields.setdefault("due_at", NOW + timedelta(days=10))
            row = Bid(source_id=sources[slug].id, **fields)
            session.add(row)
            session.flush()
            ids.append(row.id)
    return ids


@pytest.fixture()
def web(clean_db, monkeypatch):
    from guheadlines.web import app as web_app

    site = _settings()
    monkeypatch.setattr(web_app, "settings", site)
    monkeypatch.setitem(web_app.templates.env.globals, "settings", site)
    monkeypatch.setattr(web_app, "bid_alert_limit", web_app.SignupLimit())
    web_app.sidebar_cache._data.clear()
    return TestClient(web_app.app, follow_redirects=False)


def _subscriber(token: str) -> BidAlertSubscriber:
    return alerts.by_token(token)


def _sign_up(web, email="buyer@example.com", trades=("construction",), words="") -> str:
    form = {"email": email, "words": words, "website": "", "trade": list(trades)}
    response = web.post("/bids/alerts", data=form, headers=FROM_SITE)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/bids/alerts/review?t=")
    return location.split("t=", 1)[1]


def test_bid_alert_pages_are_off_until_set_up(clean_db, monkeypatch):
    from guheadlines.web import app as web_app

    web_app.sidebar_cache._data.clear()
    client = TestClient(web_app.app, follow_redirects=False)
    assert client.get("/bids/alerts").status_code == 404
    assert client.get("/bids/alerts/manage?t=abc").status_code == 404
    assert (
        client.post("/bids/alerts", data={"email": "a@b.co"}, headers=FROM_SITE).status_code == 404
    )
    html = client.get("/bids").text
    assert "/bids/alerts" not in html
    assert "/bids/alerts" not in client.get("/sitemap.xml").text


def test_sign_up_page_and_links_to_it(web):
    page = web.get("/bids/alerts")
    assert page.status_code == 200
    html = page.text
    assert "Bid alerts by email" in html and "$15 a month through PayPal" in html
    for trade in alerts.TRADES:
        assert f'value="{trade.key}"' in html and trade.name in html
    assert 'action="/bids/alerts"' in html and 'action="/bids/alerts/link"' in html
    assert "github" not in html.lower() and "chamber" not in html.lower()
    assert "@example.com" not in html  # no email address of the site is shown
    # The strict policy stays: forms go only to this site, and no PayPal script loads.
    policy = page.headers["content-security-policy"]
    assert "form-action 'self'" in policy and "paypal" not in policy
    assert "paypal.com" not in html
    bids = web.get("/bids").text
    assert 'href="/bids/alerts"' in bids and "$15 a month" in bids
    assert 'href="/bids/alerts">Bid alerts</a>' in web.get("/").text  # the footer
    assert "<strong>Bid alerts.</strong>" in web.get("/status").text
    assert "/bids/alerts</loc>" in web.get("/sitemap.xml").text


def test_sign_up_refuses_robots_other_sites_and_bad_addresses(web):
    assert web.post("/bids/alerts", data={"email": "a@b.co"}).status_code == 403
    response = web.post(
        "/bids/alerts", data={"email": "a@b.co", "website": "spam"}, headers=FROM_SITE
    )
    assert response.headers["location"] == "/bids/alerts"
    response = web.post("/bids/alerts", data={"email": "not an address"}, headers=FROM_SITE)
    assert response.headers["location"] == "/bids/alerts?result=invalid"
    assert "doesn&#39;t look like an email address" in web.get(response.headers["location"]).text
    with session_scope() as session:
        assert session.query(BidAlertSubscriber).count() == 0


def test_sign_up_pay_and_start(web, fake_paypal, outbox):
    _add_bids(
        {"title": "Re-Roofing of Simon Sanchez High School", "agency": "Department of Education"},
        {"title": "Supply of Office Paper", "agency": "General Services Agency"},
    )
    token = _sign_up(web, words="Generator")
    sub = _subscriber(token)
    assert sub.status == "pending" and sub.trades == ["construction"]
    assert sub.keywords == ["Generator"] and sub.email == "buyer@example.com"

    review = web.get(f"/bids/alerts/review?t={token}")
    assert review.status_code == 200 and review.headers["cache-control"] == "no-store"
    assert "noindex" in review.text and "buyer@example.com" in review.text
    assert "Construction and repairs; the words “Generator”" in review.text
    assert f'href="/bids/alerts/pay?t={token}"' in review.text

    pay = web.get(f"/bids/alerts/pay?t={token}")
    assert pay.status_code == 303
    assert pay.headers["location"].startswith("https://www.sandbox.paypal.com/")
    created = json.loads(fake_paypal.calls("POST", "/v1/billing/subscriptions")[0].content)
    assert created["plan_id"] == "P-1" and created["custom_id"] == f"bid-alerts-{sub.id}"
    context = created["application_context"]
    assert context["return_url"] == f"https://news.example.com/bids/alerts/manage?t={token}"
    assert context["cancel_url"].endswith(f"/bids/alerts/review?t={token}&paypal=cancelled")
    plan = json.loads(fake_paypal.calls("POST", "/v1/billing/plans")[0].content)
    price = plan["billing_cycles"][0]["pricing_scheme"]["fixed_price"]
    assert price == {"value": "15.00", "currency_code": "USD"}
    assert plan["billing_cycles"][0]["frequency"] == {"interval_unit": "MONTH", "interval_count": 1}
    # A second click soon after opens the same PayPal page.
    assert web.get(f"/bids/alerts/pay?t={token}").headers["location"] == pay.headers["location"]
    assert len(fake_paypal.subscriptions) == 1

    # Back from PayPal without approving.
    left = web.get(f"/bids/alerts/review?t={token}&paypal=cancelled").text
    assert "nothing was paid" in left

    # The buyer approves; PayPal sends them back to their page.
    renews = NOW + timedelta(days=30)
    fake_paypal.approve("I-1", renews)
    page = web.get(f"/bids/alerts/manage?t={token}")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "the payment is done" in page.text and "Cancel my subscription" in page.text
    sub = _subscriber(token)
    assert sub.status == "active" and abs(sub.paid_until - renews) < timedelta(seconds=1)
    assert len(outbox) == 1
    welcome = outbox[0]
    assert welcome["to"] == "buyer@example.com"
    assert welcome["subject"].startswith("Your bid alerts are on")
    assert "Re-Roofing of Simon Sanchez High School" in welcome["text"]
    assert "Office Paper" not in welcome["text"]
    assert f"/bids/alerts/manage?t={token}" in welcome["text"]
    assert welcome["List-Unsubscribe"] == f"<https://news.example.com/bids/alerts/manage?t={token}>"
    assert "github" not in welcome["text"].lower() + welcome["html"].lower()
    # Opening the page again sends no second welcome.
    assert "the payment is done" not in web.get(f"/bids/alerts/manage?t={token}").text
    assert len(outbox) == 1
    # The review page of a paid subscription leads to the subscriber's page.
    review = web.get(f"/bids/alerts/review?t={token}")
    assert review.headers["location"] == f"/bids/alerts/manage?t={token}"
    # The plan is kept for the next subscriber.
    with session_scope() as session:
        assert session.scalars(select(BidAlertPlan.plan_id)).all() == ["P-1"]
    second = _sign_up(web, email="other@example.com")
    web.get(f"/bids/alerts/pay?t={second}")
    assert len(fake_paypal.calls("POST", "/v1/billing/plans")) == 1


def test_unknown_links_and_paypal_trouble(web, fake_paypal, monkeypatch):
    assert web.get("/bids/alerts/manage?t=nope").status_code == 404
    assert web.get("/bids/alerts/review?t=nope").status_code == 404
    assert web.get("/bids/alerts/pay?t=nope").status_code == 404
    token = _sign_up(web)

    def down(*args, **kwargs):
        raise paypal.PayPalError("PayPal could not be reached: ConnectError")

    monkeypatch.setattr(paypal, "call", down)
    page = web.get(f"/bids/alerts/pay?t={token}")
    assert page.status_code == 200 and "PayPal could not be reached just now" in page.text


def test_new_bids_are_sent_once(web, fake_paypal, outbox):
    old = _add_bids({"title": "Roof Repair at the Port", "number": "PAG-01", "agency": "Port"})
    token = _sign_up(web)
    web.get(f"/bids/alerts/pay?t={token}")
    fake_paypal.approve("I-1", NOW + timedelta(days=30))
    web.get(f"/bids/alerts/manage?t={token}")
    outbox.clear()
    site = _settings()
    assert alerts.send_alerts(site) == 0  # nothing new since the welcome

    _add_bids(
        {"title": "Construction of a Seawall", "number": "IFB-7", "agency": "DPW"},
        {"title": "Laptops for Schools", "number": "IFB-8", "agency": "DOE"},
        # The bid of the welcome email, now posted by a second source too.
        {
            "title": "Roof Repair at the Port",
            "number": "PAG-01",
            "agency": "Port",
            "source": "port",
        },
    )
    assert alerts.send_alerts(site) == 1
    email = outbox[0]
    assert email["subject"] == "New bid: Construction of a Seawall"
    assert "Laptops" not in email["text"] and "Roof Repair" not in email["text"]
    assert "Matches: Construction and repairs" in email["text"]
    assert "Due: " in email["text"] and "ChST" not in email["text"]  # a day without a time
    assert f"https://news.example.com/bids/alerts/manage?t={token}" in email["text"]
    assert "Construction of a Seawall" in email["html"]
    assert alerts.send_alerts(site) == 0  # never twice
    assert old  # the first bid stays open

    # A failed email is tried again with the next check.
    _add_bids({"title": "Roofing for the Hospital", "agency": "GMHA"})

    def broken(*args, **kwargs):
        raise mail.MailError("the email service answered 550")

    real = mail.send
    mail.send = broken
    try:
        assert alerts.send_alerts(site) == 0
    finally:
        mail.send = real
    assert alerts.send_alerts(site) == 1
    assert "Roofing for the Hospital" in outbox[-1]["text"]


def test_paypal_states_and_cancelling(web, fake_paypal, outbox):
    site = _settings()
    token = _sign_up(web)
    web.get(f"/bids/alerts/pay?t={token}")
    # The buyer approved but never came back: the worker finds it.
    fake_paypal.approve("I-1", NOW + timedelta(days=30))
    alerts.look_after(site)
    sub = _subscriber(token)
    assert sub.status == "active" and len(outbox) == 1
    # Checked again only after CHECK_EVERY.
    asked = len(fake_paypal.requests)
    alerts.look_after(site)
    assert len(fake_paypal.requests) == asked
    later = NOW + alerts.CHECK_EVERY + timedelta(minutes=1)
    fake_paypal.subscriptions["I-1"]["status"] = "SUSPENDED"
    alerts.look_after(site, later)
    sub = _subscriber(token)
    assert sub.status == "suspended" and not alerts.receiving(sub, later)
    assert "Paused" in web.get(f"/bids/alerts/manage?t={token}").text
    fake_paypal.subscriptions["I-1"]["status"] = "ACTIVE"
    alerts.look_after(site, later + alerts.CHECK_EVERY * 2)
    assert _subscriber(token).status == "active"

    # Cancelling on the subscriber's page.
    response = web.post("/bids/alerts/cancel", data={"t": token}, headers=FROM_SITE)
    assert response.headers["location"] == f"/bids/alerts/manage?t={token}&result=cancelled"
    cancel = fake_paypal.calls("POST", "/v1/billing/subscriptions/I-1/cancel")
    assert len(cancel) == 1 and json.loads(cancel[0].content)["reason"]
    sub = _subscriber(token)
    assert sub.status == "cancelled" and alerts.receiving(sub)  # until the paid time ends
    page = web.get(response.headers["location"]).text
    assert "Your subscription is cancelled" in page and "Cancel my subscription" not in page
    assert not alerts.receiving(sub, sub.paid_until + timedelta(minutes=1))
    # 30 days after the paid time ended, the address is deleted.
    alerts.look_after(site, sub.paid_until + alerts.KEEP_AFTER_END + timedelta(days=1))
    assert _subscriber(token) is None


def test_cancelled_in_paypal_and_unpaid_sign_ups(web, fake_paypal, outbox):
    site = _settings()
    paid = _sign_up(web)
    web.get(f"/bids/alerts/pay?t={paid}")
    fake_paypal.approve("I-1", NOW + timedelta(days=12))
    alerts.look_after(site)
    # Cancelled in PayPal itself: PayPal answers 422 to a second cancel.
    fake_paypal.subscriptions["I-1"]["status"] = "CANCELLED"
    fake_paypal.cancel_status = 422
    assert alerts.cancel(site, _subscriber(paid).id) is True
    assert _subscriber(paid).status == "cancelled"
    # A sign-up that is never paid is deleted after PENDING_DAYS.
    unpaid = _sign_up(web, email="later@example.com")
    alerts.look_after(site, NOW + alerts.PENDING_DAYS - timedelta(hours=1))
    assert _subscriber(unpaid) is not None
    alerts.look_after(site, NOW + alerts.PENDING_DAYS + timedelta(hours=1))
    assert _subscriber(unpaid) is None


def test_changing_alerts_and_lost_links(web, fake_paypal, outbox):
    token = _sign_up(web)
    web.get(f"/bids/alerts/pay?t={token}")
    fake_paypal.approve("I-1", NOW + timedelta(days=30))
    web.get(f"/bids/alerts/manage?t={token}")
    outbox.clear()
    form = {"t": token, "email": "new@example.com", "trade": ["it"], "words": "drones"}
    assert web.post("/bids/alerts/manage", data=form).status_code == 403  # another site
    response = web.post("/bids/alerts/manage", data=form, headers=FROM_SITE)
    assert response.headers["location"] == f"/bids/alerts/manage?t={token}&result=saved"
    sub = _subscriber(token)
    assert (sub.email, sub.trades, sub.keywords) == ("new@example.com", ["it"], ["drones"])
    bad = web.post("/bids/alerts/manage", data={"t": token, "email": "x"}, headers=FROM_SITE)
    assert bad.headers["location"].endswith("result=invalid")
    assert web.post("/bids/alerts/manage", data={"t": "nope"}, headers=FROM_SITE).status_code == 404

    # A lost link goes to the address; the page says the same for any address.
    response = web.post("/bids/alerts/link", data={"email": "NEW@example.com"}, headers=FROM_SITE)
    assert response.headers["location"] == "/bids/alerts?result=sent#lost"
    assert outbox[-1]["to"] == "new@example.com"
    assert f"/bids/alerts/manage?t={token}" in outbox[-1]["text"]
    response = web.post("/bids/alerts/link", data={"email": "who@example.com"}, headers=FROM_SITE)
    assert response.headers["location"] == "/bids/alerts?result=sent#lost"
    assert len(outbox) == 1


def test_bid_alerts_command(web, fake_paypal, monkeypatch, capsys):
    from guheadlines import cli

    token = _sign_up(web, words="radios")
    monkeypatch.setattr(cli, "get_settings", lambda: _settings())
    cli.main(["bid-alerts"])
    out = capsys.readouterr().out
    assert "Bid alerts are on: $15 a month, PayPal sandbox." in out
    assert "pending" in out and "buyer@example.com" in out and "“radios”" in out
    assert token not in out
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(smtp_host=""))
    cli.main(["bid-alerts"])
    assert "SMTP_HOST and MAIL_FROM" in capsys.readouterr().out
