"""The PayPal client (paypal.py), against a stand-in for PayPal's API."""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from guheadlines import paypal
from guheadlines.settings import load_settings

SANDBOX = "api-m.sandbox.paypal.com"


@pytest.fixture(autouse=True)
def no_saved_tokens():
    paypal._tokens.clear()
    yield
    paypal._tokens.clear()


def _settings(**values):
    values = {"paypal_client_id": "client-id", "paypal_client_secret": "secret", **values}
    return dataclasses.replace(load_settings(), **values)


class FakePayPal:
    """PayPal's API, as far as the site uses it."""

    def __init__(self, order_status="APPROVED", capture_status="COMPLETED", value="5.00"):
        self.order_status = order_status
        self.capture_status = capture_status
        self.value = value
        self.requests: list[httpx.Request] = []
        self.tokens = 0
        self.refuse_token_once = False
        self.refund_issue = None

    def _order(self, order_id):
        body = {"id": order_id, "status": self.order_status}
        if self.order_status == "COMPLETED":
            capture = {
                "id": "CAP-1",
                "status": self.capture_status,
                "amount": {"currency_code": "USD", "value": self.value},
            }
            body["purchase_units"] = [{"payments": {"captures": [capture]}}]
            body["payer"] = {"email_address": "buyer@example.com", "payer_id": "P1"}
        return body

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens}", "expires_in": 32400}
            )
        if request.headers.get("authorization") == "Bearer token-1" and self.refuse_token_once:
            self.refuse_token_once = False
            return httpx.Response(401, json={"error": "invalid_token"})
        if path == "/v2/checkout/orders" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "ORDER-1",
                    "status": "PAYER_ACTION_REQUIRED",
                    "links": [
                        {
                            "href": "https://api.sandbox.paypal.com/v2/checkout/orders/ORDER-1",
                            "rel": "self",
                        },
                        {
                            "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1",
                            "rel": "payer-action",
                        },
                    ],
                },
            )
        if path.startswith("/v2/checkout/orders/") and path.endswith("/capture"):
            self.order_status = "COMPLETED"
            return httpx.Response(201, json=self._order(path.split("/")[4]))
        if path.startswith("/v2/checkout/orders/"):
            return httpx.Response(200, json=self._order(path.split("/")[-1]))
        if path.startswith("/v2/payments/captures/") and path.endswith("/refund"):
            if self.refund_issue:
                return httpx.Response(
                    422,
                    json={
                        "name": "UNPROCESSABLE_ENTITY",
                        "details": [{"issue": self.refund_issue}],
                        "debug_id": "abc123",
                    },
                )
            return httpx.Response(201, json={"id": "REFUND-1", "status": "COMPLETED"})
        return httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def calls(self):
        return [(r.method, r.url.path) for r in self.requests]


def test_settings(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "  id  ")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", " secret ")
    monkeypatch.setenv("PAYPAL_MODE", " Live ")
    site = load_settings()
    assert (site.paypal_client_id, site.paypal_client_secret) == ("id", "secret")
    assert paypal.api_base(site) == "https://api-m.paypal.com"
    assert paypal.enabled(site)
    for value in ("", "sandbox", "test", "nonsense"):
        monkeypatch.setenv("PAYPAL_MODE", value)
        assert paypal.api_base(load_settings()) == f"https://{SANDBOX}"
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET")
    assert not paypal.enabled(load_settings())


def test_create_order_sends_the_buyer_to_paypal():
    fake = FakePayPal()
    order = paypal.create_order(
        _settings(),
        amount_cents=2550,
        description="Classified ad, 30 days: " + "x" * 200,
        custom_id="listing-7",
        return_url="https://guamheadlines.com/pay/return",
        cancel_url="https://guamheadlines.com/pay/cancel",
        request_id="order-listing-7",
        transport=fake.transport,
    )
    assert order == paypal.Order(
        "ORDER-1", "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1"
    )
    token_call, order_call = fake.requests
    assert token_call.url.host == SANDBOX
    assert token_call.headers["authorization"].startswith("Basic ")
    assert order_call.headers["authorization"] == "Bearer token-1"
    assert order_call.headers["paypal-request-id"] == "order-listing-7"
    body = json.loads(order_call.content)
    unit = body["purchase_units"][0]
    assert body["intent"] == "CAPTURE"
    assert unit["amount"] == {"currency_code": "USD", "value": "25.50"}
    assert unit["custom_id"] == "listing-7"
    assert len(unit["description"]) == 127
    context = body["payment_source"]["paypal"]["experience_context"]
    assert context["shipping_preference"] == "NO_SHIPPING"
    assert context["user_action"] == "PAY_NOW"
    assert context["return_url"] == "https://guamheadlines.com/pay/return"
    assert context["cancel_url"] == "https://guamheadlines.com/pay/cancel"


def test_the_token_is_kept_and_renewed_when_refused():
    fake = FakePayPal(order_status="PAYER_ACTION_REQUIRED")
    site = _settings()
    paypal.capture_order(site, "ORDER-1", transport=fake.transport)
    paypal.capture_order(site, "ORDER-1", transport=fake.transport)
    assert fake.tokens == 1
    paypal._tokens.clear()
    fake.tokens = 0
    fake.refuse_token_once = True
    paypal.capture_order(site, "ORDER-1", transport=fake.transport)
    assert fake.tokens == 2  # asked again once after the 401


@pytest.mark.parametrize("cents, text", [(500, "5.00"), (1, "0.01"), (12345, "123.45")])
def test_amount_text(cents, text):
    assert paypal.amount_text(cents) == text


def test_capture_takes_the_money_once():
    fake = FakePayPal(order_status="APPROVED", value="25.50")
    capture = paypal.capture_order(_settings(), "ORDER-1", transport=fake.transport)
    assert capture == paypal.Capture("COMPLETED", "CAP-1", 2550, "USD", "buyer@example.com")
    assert capture.paid
    capture_call = fake.requests[-1]
    assert capture_call.url.path == "/v2/checkout/orders/ORDER-1/capture"
    assert capture_call.headers["paypal-request-id"] == "capture-ORDER-1"
    # Again (a reload): the order is read, not captured a second time.
    again = paypal.capture_order(_settings(), "ORDER-1", transport=fake.transport)
    assert again == capture
    assert fake.calls()[-1] == ("GET", "/v2/checkout/orders/ORDER-1")
    assert fake.calls().count(("POST", "/v2/checkout/orders/ORDER-1/capture")) == 1


def test_capture_of_an_unpaid_or_pending_order():
    fake = FakePayPal(order_status="PAYER_ACTION_REQUIRED")
    capture = paypal.capture_order(_settings(), "ORDER-1", transport=fake.transport)
    assert capture == paypal.Capture("PAYER_ACTION_REQUIRED")
    assert not capture.paid
    fake = FakePayPal(order_status="COMPLETED", capture_status="PENDING")
    capture = paypal.capture_order(_settings(), "ORDER-1", transport=fake.transport)
    assert (capture.status, capture.capture_id, capture.paid) == ("PENDING", "CAP-1", False)


def test_refund():
    fake = FakePayPal()
    assert paypal.refund_capture(_settings(), "CAP-1", transport=fake.transport) == "COMPLETED"
    assert fake.requests[-1].headers["paypal-request-id"] == "refund-CAP-1"
    fake.refund_issue = "CAPTURE_FULLY_REFUNDED"  # given back already
    assert paypal.refund_capture(_settings(), "CAP-1", transport=fake.transport) == "COMPLETED"
    fake.refund_issue = "REFUND_TIME_LIMIT_EXCEEDED"
    with pytest.raises(paypal.PayPalError) as caught:
        paypal.refund_capture(_settings(), "CAP-1", transport=fake.transport)
    assert caught.value.issue == "REFUND_TIME_LIMIT_EXCEEDED"
    assert caught.value.status == 422
    assert "debug_id abc123" in str(caught.value)


def test_errors_are_paypal_errors():
    def refuse(request):
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(paypal.PayPalError, match="PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET"):
        paypal.access_token(_settings(), transport=httpx.MockTransport(refuse))

    def down(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(paypal.PayPalError, match="could not be reached"):
        paypal.access_token(_settings(), transport=httpx.MockTransport(down))

    with pytest.raises(paypal.PayPalError, match="not both set"):
        paypal.access_token(dataclasses.replace(_settings(), paypal_client_secret=""))

    def declined(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 100})
        return httpx.Response(
            422,
            json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]},
        )

    with pytest.raises(paypal.PayPalError) as caught:
        paypal.call(
            _settings(),
            "POST",
            "/v2/checkout/orders/X/capture",
            json={},
            transport=httpx.MockTransport(declined),
        )
    assert caught.value.issue == "INSTRUMENT_DECLINED"
    with pytest.raises(paypal.PayPalError, match="more than 0"):
        paypal.create_order(
            _settings(), amount_cents=0, description="", custom_id="x", return_url="", cancel_url=""
        )


def test_paypal_check_command(monkeypatch, capsys):
    from guheadlines.cli import cmd_paypal_check

    fake = FakePayPal()
    real = paypal._client
    monkeypatch.setattr(paypal, "_client", lambda transport: real(fake.transport))
    cmd_paypal_check(None, _settings())
    out = capsys.readouterr().out
    assert "OK: PayPal accepts" in out and "Sandbox (test payments)" in out
    with pytest.raises(SystemExit, match="PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET is empty"):
        cmd_paypal_check(None, _settings(paypal_client_id=""))
    paypal._tokens.clear()
    fake.handler = lambda request: httpx.Response(401, json={"error": "invalid_client"})
    monkeypatch.setattr(
        paypal, "_client", lambda transport: real(httpx.MockTransport(fake.handler))
    )
    with pytest.raises(SystemExit, match="Problem: PayPal refused"):
        cmd_paypal_check(None, _settings(paypal_mode="live"))
