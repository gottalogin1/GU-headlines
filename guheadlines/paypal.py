"""Payments through PayPal, with PayPal's REST API (Orders v2).

The site never shows PayPal's own buttons or scripts, so the pages stay as
fast and as strict (Content-Security-Policy) as before. A payment goes so:

1. The server makes an order for the amount (create_order). PayPal answers
   with the address of its own payment page (Order.approve_url).
2. The buyer opens that address from a plain link on one of our pages, signs
   in to PayPal (or pays by card as a guest) and approves the payment.
   (Not from a form: Chrome applies the page's form-action rule to the
   redirect after a form, and that rule allows only this site.)
3. PayPal sends the buyer back to return_url with ?token=<the order's ID>.
   The server then takes the money (capture_order). Only then is it paid: an
   order that the buyer approves but that is never captured costs nothing.

The money can go back to the buyer with refund_capture.

PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET come from an app in the PayPal
developer dashboard (see .env.example). PAYPAL_MODE "sandbox" uses PayPal's
test system with pretend money; "live" takes real payments.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

import httpx

from .settings import Settings

log = logging.getLogger(__name__)

API = {
    "live": "https://api-m.paypal.com",
    "sandbox": "https://api-m.sandbox.paypal.com",
}
TIMEOUT = 20.0
CURRENCY = "USD"
# A new access token is asked for this many seconds before the old one ends.
TOKEN_MARGIN = 60
# PayPal accepts at most this many characters in these order fields.
MAX_FIELD = 127


class PayPalError(Exception):
    """PayPal could not do what was asked. The message says why, for the log.
    `issue` is PayPal's own name for the problem when it gives one, such as
    INSTRUMENT_DECLINED (the bank or card said no)."""

    def __init__(self, message: str, issue: str | None = None, status: int | None = None):
        super().__init__(message)
        self.issue = issue
        self.status = status


@dataclass(frozen=True)
class Order:
    id: str
    # PayPal's page where the buyer approves the payment.
    approve_url: str


@dataclass(frozen=True)
class Capture:
    # COMPLETED: the money is ours. PENDING: PayPal is still checking the
    # payment (it can still fail). Any other value: not paid, such as
    # PAYER_ACTION_REQUIRED (the buyer did not approve), VOIDED or DECLINED.
    status: str
    capture_id: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    # The buyer's email address at PayPal, for an email about the purchase.
    payer_email: str | None = None

    @property
    def paid(self) -> bool:
        return self.status == "COMPLETED"


def enabled(settings: Settings) -> bool:
    return bool(settings.paypal_client_id and settings.paypal_client_secret)


def api_base(settings: Settings) -> str:
    return API["live" if settings.paypal_mode == "live" else "sandbox"]


def amount_text(cents: int) -> str:
    """500 -> "5.00", the form PayPal takes amounts in."""
    return f"{cents // 100}.{cents % 100:02d}"


def _cents(value: object) -> int | None:
    try:
        return int((Decimal(str(value)) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


_token_lock = threading.Lock()
_tokens: dict[tuple[str, str, str], tuple[float, str]] = {}


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(TIMEOUT), transport=transport)


def _token_key(settings: Settings) -> tuple[str, str, str]:
    return (api_base(settings), settings.paypal_client_id, settings.paypal_client_secret)


def _forget_token(settings: Settings) -> None:
    with _token_lock:
        _tokens.pop(_token_key(settings), None)


def access_token(settings: Settings, transport: httpx.BaseTransport | None = None) -> str:
    """A token for the other calls, kept until shortly before it ends (PayPal
    gives them for about nine hours)."""
    key = _token_key(settings)
    with _token_lock:
        cached = _tokens.get(key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    if not enabled(settings):
        raise PayPalError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are not both set")
    try:
        with _client(transport) as client:
            response = client.post(
                f"{key[0]}/v1/oauth2/token",
                auth=(settings.paypal_client_id, settings.paypal_client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json", "User-Agent": settings.user_agent},
            )
    except httpx.HTTPError as exc:
        raise PayPalError(f"PayPal could not be reached: {exc}") from exc
    if response.status_code == 401:
        raise PayPalError(
            "PayPal refused PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (401). Check that both"
            f" come from the same app, and that the app is a {settings.paypal_mode} app"
            " (PAYPAL_MODE)",
            status=401,
        )
    data = _json(response)
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise PayPalError("PayPal gave no access token")
    lifetime = data.get("expires_in")
    lifetime = lifetime if isinstance(lifetime, int) else 3600
    with _token_lock:
        _tokens[key] = (time.monotonic() + max(0, lifetime - TOKEN_MARGIN), token)
    return token


def _json(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        issue = None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            details = body.get("details")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                issue = details[0].get("issue")
            issue = issue or body.get("name") or body.get("error")
            why = body.get("message") or body.get("error_description") or ""
            debug = body.get("debug_id")
        else:
            why, debug = "", None
        message = f"PayPal answered {response.status_code}"
        if issue:
            message += f" {issue}"
        if why:
            message += f": {str(why)[:200]}"
        if debug:
            message += f" (debug_id {debug})"
        raise PayPalError(
            message, issue=issue if isinstance(issue, str) else None, status=response.status_code
        )
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise PayPalError("PayPal answered with something that is not JSON") from exc
    return data if isinstance(data, dict) else {}


def call(
    settings: Settings,
    method: str,
    path: str,
    json: dict | None = None,
    request_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """One call to the PayPal API, such as call(settings, "GET",
    "/v2/checkout/orders/<id>"). PayPal does a call with a request_id only
    once: the same call again gets the first answer, so it is safe to try
    again."""
    for attempt in (1, 2):
        headers = {
            "Authorization": f"Bearer {access_token(settings, transport)}",
            "Accept": "application/json",
            "User-Agent": settings.user_agent,
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            with _client(transport) as client:
                response = client.request(
                    method, api_base(settings) + path, json=json, headers=headers
                )
        except httpx.HTTPError as exc:
            raise PayPalError(f"PayPal could not be reached: {exc}") from exc
        if response.status_code == 401 and attempt == 1:
            # The token ended early or was withdrawn: get a new one once.
            _forget_token(settings)
            continue
        return _json(response)
    raise AssertionError("unreachable")


def create_order(
    settings: Settings,
    *,
    amount_cents: int,
    description: str,
    custom_id: str,
    return_url: str,
    cancel_url: str,
    request_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Order:
    """Make an order for amount_cents US cents. `description` is what the
    buyer sees on PayPal's page and receipt; `custom_id` is our own name for
    the thing paid for, kept with the payment in PayPal's records."""
    if amount_cents <= 0:
        raise PayPalError(f"the amount must be more than 0 (not {amount_cents} cents)")
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "custom_id": custom_id[:MAX_FIELD],
                "description": description[:MAX_FIELD],
                "amount": {"currency_code": CURRENCY, "value": amount_text(amount_cents)},
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "brand_name": settings.site_name[:MAX_FIELD],
                    "shipping_preference": "NO_SHIPPING",
                    # PayPal's button says "Pay Now": nothing more to confirm here.
                    "user_action": "PAY_NOW",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                }
            }
        },
    }
    data = call(
        settings,
        "POST",
        "/v2/checkout/orders",
        json=body,
        request_id=request_id,
        transport=transport,
    )
    order_id = data.get("id")
    links = data.get("links") if isinstance(data.get("links"), list) else []
    approve = next(
        (
            link.get("href")
            for link in links
            if isinstance(link, dict) and link.get("rel") in ("payer-action", "approve")
        ),
        None,
    )
    if not isinstance(order_id, str) or not order_id or not isinstance(approve, str):
        raise PayPalError("PayPal made no order, or gave no page to approve it on")
    return Order(id=order_id, approve_url=approve)


def _payer_email(order: dict) -> str | None:
    payer = order.get("payer") if isinstance(order.get("payer"), dict) else {}
    email = payer.get("email_address")
    return email if isinstance(email, str) and "@" in email else None


def _capture(order: dict) -> Capture:
    """The payment of an order as PayPal describes it."""
    units = order.get("purchase_units") if isinstance(order.get("purchase_units"), list) else []
    for unit in units:
        payments = unit.get("payments") if isinstance(unit, dict) else None
        captures = payments.get("captures") if isinstance(payments, dict) else None
        if isinstance(captures, list) and captures and isinstance(captures[0], dict):
            capture = captures[0]
            amount = capture.get("amount") if isinstance(capture.get("amount"), dict) else {}
            return Capture(
                status=str(capture.get("status") or ""),
                capture_id=capture.get("id") if isinstance(capture.get("id"), str) else None,
                amount_cents=_cents(amount.get("value")) if "value" in amount else None,
                currency=amount.get("currency_code"),
                payer_email=_payer_email(order),
            )
    return Capture(status=str(order.get("status") or "UNKNOWN"))


def capture_order(
    settings: Settings, order_id: str, transport: httpx.BaseTransport | None = None
) -> Capture:
    """Take the money of an order that the buyer approved. Safe to call more
    than once (the buyer reloads the page, or an earlier answer was lost): it
    reads the order first, and only captures an order that is approved and
    not captured yet."""
    path = f"/v2/checkout/orders/{order_id}"
    order = call(settings, "GET", path, transport=transport)
    if order.get("status") == "APPROVED":
        order = call(
            settings,
            "POST",
            f"{path}/capture",
            json={},
            request_id=f"capture-{order_id}",
            transport=transport,
        )
    return _capture(order)


def refund_capture(
    settings: Settings, capture_id: str, transport: httpx.BaseTransport | None = None
) -> str:
    """Give all the money of a payment back to the buyer. Returns PayPal's
    status of the refund: COMPLETED, or PENDING while PayPal still works on
    it."""
    try:
        data = call(
            settings,
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json={},
            request_id=f"refund-{capture_id}",
            transport=transport,
        )
    except PayPalError as exc:
        if exc.issue == "CAPTURE_FULLY_REFUNDED":
            return "COMPLETED"  # given back already
        raise
    return str(data.get("status") or "UNKNOWN")


# ------------------------------------------------- what came in (reports.py)


@dataclass(frozen=True)
class Payment:
    """One payment in or out of the PayPal account, as PayPal's records list it."""

    id: str
    # True when the money moved: not pending, refused or taken back.
    done: bool
    # Less than 0: money that went out, such as a refund.
    amount_cents: int
    currency: str
    # PayPal's kind of payment (Transaction Search only), such as T0006 (a
    # payment through the site, Orders v2), T0002 (a subscription payment),
    # T0000 or T0013 (money sent with a PayPal.Me or donate link), T1107 (a
    # refund). "" from a subscription's own list.
    event_code: str = ""
    # Our own name of the thing paid for (create_order's custom_id), when
    # PayPal shows it.
    custom_id: str = ""


def _when(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# The most pages of 500 payments read for one report.
MAX_PAGES = 20


def transactions(
    settings: Settings,
    start: datetime,
    end: datetime,
    transport: httpx.BaseTransport | None = None,
) -> list[Payment]:
    """Every payment in and out of the PayPal account from `start` to `end`
    (at most 31 days), from PayPal's Transaction Search. It needs "Transaction
    search" turned on for the app in the PayPal developer dashboard; without
    it, PayPal answers 403 and this raises PayPalError with status 403.
    PayPal lists a payment up to 3 hours after it was made."""
    found: list[Payment] = []
    for page in range(1, MAX_PAGES + 1):
        query = urlencode(
            {
                "start_date": _when(start),
                "end_date": _when(end),
                "fields": "transaction_info",
                "page_size": 500,
                "page": page,
            }
        )
        data = call(settings, "GET", f"/v1/reporting/transactions?{query}", transport=transport)
        details = data.get("transaction_details")
        for item in details if isinstance(details, list) else []:
            info = item.get("transaction_info") if isinstance(item, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get("transaction_id"), str):
                continue
            amount = info.get("transaction_amount")
            amount = amount if isinstance(amount, dict) else {}
            cents = _cents(amount.get("value"))
            if cents is None:
                continue
            found.append(
                Payment(
                    id=info["transaction_id"],
                    # S: done. P: pending, V: taken back, D: refused.
                    done=info.get("transaction_status") == "S",
                    amount_cents=cents,
                    currency=str(amount.get("currency_code") or ""),
                    event_code=str(info.get("transaction_event_code") or ""),
                    custom_id=str(info.get("custom_field") or ""),
                )
            )
        pages = data.get("total_pages")
        if not isinstance(pages, int) or page >= pages:
            break
    return found


def subscription_payments(
    settings: Settings,
    subscription_id: str,
    start: datetime,
    end: datetime,
    transport: httpx.BaseTransport | None = None,
) -> list[Payment]:
    """The payments of one subscription (such as a bid alert subscription)
    from `start` to `end`. Unlike transactions(), this needs nothing turned on."""
    query = urlencode({"start_time": _when(start), "end_time": _when(end)})
    data = call(
        settings,
        "GET",
        f"/v1/billing/subscriptions/{quote(subscription_id, safe='')}/transactions?{query}",
        transport=transport,
    )
    found: list[Payment] = []
    items = data.get("transactions")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        breakdown = item.get("amount_with_breakdown")
        gross = breakdown.get("gross_amount") if isinstance(breakdown, dict) else None
        gross = gross if isinstance(gross, dict) else {}
        cents = _cents(gross.get("value"))
        if cents is None:
            continue
        found.append(
            Payment(
                id=item["id"],
                # A payment later given back in part still came in.
                done=item.get("status") in ("COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED"),
                amount_cents=cents,
                currency=str(gross.get("currency_code") or ""),
            )
        )
    return found
