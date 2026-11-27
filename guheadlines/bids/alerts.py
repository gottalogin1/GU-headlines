"""Bid alerts: a business pays a monthly price through PayPal, and gets an
email when a new bid on the Bids page matches its trades or its own words.

How it goes:
1. On /bids/alerts the business types its email address and picks its trades
   or types its own words. The site keeps a "pending" subscriber with a
   secret token, and shows a page to check the details.
2. "Pay with PayPal" makes a PayPal subscription on the monthly plan
   (start_payment) and opens PayPal's page, where the business approves it.
3. PayPal sends the business back to its own page (/bids/alerts/manage?t=
   <token>). The site asks PayPal about the subscription (check), and once
   PayPal says it is ACTIVE, the alerts start: a welcome email brings the
   link to that page and the open bids that match now. The worker also asks
   PayPal every hour about pending subscriptions, in case the business never
   came back to the site.
4. After each bids check, the worker sends each paying subscriber one email
   with the new bids that match (send_alerts).
5. The worker asks PayPal about each subscription twice a day (look_after). A
   renewal moves paid_until on. PayPal suspends a subscription whose
   payments fail, and its alerts stop. A cancelled subscription (on the
   business's page, or in PayPal) gets alerts until the end of the time that
   it paid for.

There is no login. Every email links to the subscriber's own page, where the
trades and words can change and the subscription can be cancelled. A lost
link is sent again to the address (send_links).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from markupsafe import escape
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .. import mail, paypal
from ..alerts import Alert, fold, keyword_patterns
from ..alerts import send as send_discord
from ..db import BID_ALERTS_LOCK_KEY, advisory_lock, session_scope
from ..models import Bid, BidAlertPlan, BidAlertSubscriber
from ..scraper.text import GUAM_TZ
from ..settings import Settings
from ..web.queries import BidFilters, list_bids

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Trade:
    key: str
    name: str
    # Words as in ALERT_KEYWORDS (see alerts.keyword_patterns), with a comma
    # between them: a word finds itself with "s" or "es" after it, and
    # "roof*" finds every word that starts with "roof".
    words_text: str

    @property
    def words(self) -> list[str]:
        return [w.strip() for w in self.words_text.split(",") if w.strip()]


TRADES = (
    Trade(
        "construction",
        "Construction and repairs",
        (
            "construction, renovat*, repair*, roof*, paving, pavement, road, demolition, "
            "concrete, plumbing, electrical, hvac, air condition*, painting, contractor, "
            "building, rehabilitation, carpentry"
        ),
    ),
    Trade(
        "it",
        "Computers, IT and phones",
        (
            "computer, software, network, information technology, IT services, cyber*, "
            "laptop, server, telecommunication*, website, internet, data, radio, camera, "
            "cctv, phone, printer, copier"
        ),
    ),
    Trade(
        "vehicles",
        "Vehicles, boats and heavy equipment",
        (
            "vehicle, truck, bus, fleet, tire, heavy equipment, forklift, boat, vessel, "
            "automotive, car, van, excavator, backhoe"
        ),
    ),
    Trade(
        "medical",
        "Medical and health",
        (
            "medical, medicine, pharmac*, hospital, health, clinic, laboratory, dental, "
            "nurs*, drug, patient, surgical, x-ray, ambulance"
        ),
    ),
    Trade(
        "professional",
        "Consulting and professional services",
        (
            "consult*, audit*, legal, attorney, engineering, architect*, accounting, "
            "study, assessment, planning, training, actuar*, appraisal, survey, "
            "professional services"
        ),
    ),
    Trade(
        "facilities",
        "Cleaning, grounds and security",
        (
            "janitorial, custodial, cleaning, landscap*, grounds, grass, security guard, "
            "security services, pest control, maintenance, waste, trash, refuse, "
            "recycling"
        ),
    ),
    Trade(
        "supplies",
        "Supplies, furniture and printing",
        ("supplies, furniture, printing, paper, toner, uniform, stationery, equipment, appliance"),
    ),
    Trade(
        "food",
        "Food and catering",
        ("food, meal, catering, grocer*, beverage, kitchen, snack"),
    ),
    Trade(
        "utilities",
        "Power, water and fuel",
        (
            "generator, transformer, pipe*, water, wastewater, sewer*, meter, fuel, "
            "diesel, solar, power plant, substation, electric*"
        ),
    ),
    Trade(
        "shipping",
        "Shipping, travel and moving",
        (
            "shipping, freight, cargo, airfare, air travel, travel, moving, relocation, "
            "stevedor*, courier, delivery"
        ),
    ),
)
TRADE_NAMES = {t.key: t.name for t in TRADES}

MAX_WORDS = 20
MAX_WORD_LENGTH = 60
# PayPal retries a failed renewal for some days: alerts go on meanwhile.
GRACE = timedelta(days=3)
# PayPal is asked about each paying subscription this often.
CHECK_EVERY = timedelta(hours=12)
# A sign-up that is not paid within this time is deleted.
PENDING_DAYS = timedelta(days=3)
# A PayPal approval page is used again for this long (PayPal's links last
# about three hours).
APPROVE_REUSE = timedelta(hours=2)
# A cancelled subscriber's address is deleted this long after its paid time ended.
KEEP_AFTER_END = timedelta(days=30)
# The welcome email lists at most this many open bids.
WELCOME_BIDS = 25


class AlertsError(Exception):
    """Something the subscriber should hear about, in plain words."""


# --------------------------------------------------------------- settings


def is_on(settings: Settings) -> bool:
    """Bid alerts need a price, PayPal, email and the site's address (for the
    links in the emails and for PayPal's way back)."""
    return bool(
        settings.bid_alerts_price_cents > 0
        and paypal.enabled(settings)
        and mail.enabled(settings)
        and settings.site_url
    )


def price_text(cents: int) -> str:
    """1500 -> "$15", 950 -> "$9.50"."""
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents // 100}.{cents % 100:02d}"


def manage_url(settings: Settings, token: str) -> str:
    return f"{settings.site_url}/bids/alerts/manage?t={token}"


def review_url(settings: Settings, token: str) -> str:
    return f"{settings.site_url}/bids/alerts/review?t={token}"


def awards_url(settings: Settings, token: str) -> str:
    """The link that opens the whole award history in the subscriber's browser."""
    return f"{settings.site_url}/bids/awards?t={token}"


def awards_key(token: str) -> str:
    """What the award history page's cookie holds for a subscriber: made from
    the token, but not the token itself, so the cookie can't open the
    subscriber's own page (where alerts are changed or cancelled)."""
    return hashlib.sha256(f"bid-awards:{token}".encode()).hexdigest()


def by_awards_key(key: str, now: datetime | None = None) -> BidAlertSubscriber | None:
    """The subscriber getting alerts now whose awards_key is `key`."""
    if not key or len(key) != 64:
        return None
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        for sub in session.scalars(select(BidAlertSubscriber).where(_receiving(now))):
            if hmac.compare_digest(awards_key(sub.token), key):
                return sub
    return None


# ----------------------------------------------------------------- choices


def parse_words(text: str | None) -> list[str]:
    """The words a subscriber typed, one per comma or line: "roofing, solar
    panels" -> ["roofing", "solar panels"]. Repeats and words with no letter
    or digit are dropped, and at most MAX_WORDS are kept."""
    words: dict[str, str] = {}
    for part in re.split(r"[,;\n]", text or ""):
        word = " ".join(part.split()).strip("\"'")[:MAX_WORD_LENGTH].strip()
        if re.search(r"\w", word) and fold(word) not in words:
            words[fold(word)] = word
    return list(words.values())[:MAX_WORDS]


def parse_trades(keys: list[str]) -> list[str]:
    """The known trade keys among `keys`, in the order of TRADES."""
    return [t.key for t in TRADES if t.key in keys]


def choices_text(trades: list[str], keywords: list[str]) -> str:
    """What a subscriber gets, in words, for the emails and pages."""
    parts = [TRADE_NAMES[k] for k in trades if k in TRADE_NAMES]
    if keywords:
        parts.append("the words " + ", ".join(f"“{w}”" for w in keywords))
    return "; ".join(parts) if parts else "every new bid"


def _patterns(trades: list[str], keywords: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    found = []
    for trade in TRADES:
        if trade.key in trades:
            found += [(trade.name, pattern) for _, pattern in keyword_patterns(trade.words)]
    return found + keyword_patterns(keywords)


def _bid_text(bid) -> str:
    return fold(
        " ".join(p for p in (bid.title, bid.number, bid.kind, bid.agency, bid.description) if p)
    )


def matching(items: list[dict], trades: list[str], keywords: list[str]) -> list[dict]:
    """The bids of `items` (from queries.list_bids) that match the trades or
    words, each with "matches": the trades and words it matched. With no
    trades and no words, every bid matches."""
    patterns = _patterns(trades, keywords)
    found = []
    for item in items:
        text = _bid_text(item["bid"])
        labels = list(dict.fromkeys(label for label, p in patterns if p.search(text)))
        if labels or not patterns:
            found.append({**item, "matches": labels})
    return found


# ------------------------------------------------------------------ sign-up


def new_subscriber(email: str, trades: list[str], keywords: list[str]) -> str:
    """Keep a pending subscriber; returns its secret token."""
    token = secrets.token_urlsafe(24)
    with session_scope() as session:
        session.add(BidAlertSubscriber(email=email, trades=trades, keywords=keywords, token=token))
    return token


def by_token(token: str) -> BidAlertSubscriber | None:
    if not token or len(token) > 64:
        return None
    with session_scope() as session:
        return session.scalar(select(BidAlertSubscriber).where(BidAlertSubscriber.token == token))


def has_alerts(email: str, now: datetime | None = None) -> bool:
    """True when the address already gets bid alerts."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        return bool(
            session.scalar(
                select(func.count(BidAlertSubscriber.id)).where(
                    func.lower(BidAlertSubscriber.email) == email.lower(), _receiving(now)
                )
            )
        )


def update_choices(sub_id: int, email: str, trades: list[str], keywords: list[str]) -> None:
    with session_scope() as session:
        session.execute(
            update(BidAlertSubscriber)
            .where(BidAlertSubscriber.id == sub_id)
            .values(email=email, trades=trades, keywords=keywords)
        )


def _receiving(now: datetime):
    """Subscribers who get alerts now: paid and active (with some days'
    grace while PayPal retries a renewal), or cancelled with paid time left."""
    return or_(
        (BidAlertSubscriber.status == "active") & (BidAlertSubscriber.paid_until > now - GRACE),
        (BidAlertSubscriber.status == "cancelled") & (BidAlertSubscriber.paid_until > now),
    )


def receiving(sub: BidAlertSubscriber, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if sub.paid_until is None:
        return False
    if sub.status == "active":
        return sub.paid_until > now - GRACE
    return sub.status == "cancelled" and sub.paid_until > now


# ------------------------------------------------------------------- PayPal


def plan_id(settings: Settings, transport=None) -> str:
    """PayPal's ID of the monthly plan at BID_ALERTS_PRICE, made the first
    time it is needed (a product, then a plan on it)."""
    cents = settings.bid_alerts_price_cents
    client_id = settings.paypal_client_id
    wanted = (BidAlertPlan.client_id == client_id) & (BidAlertPlan.price_cents == cents)
    with session_scope() as session:
        found = session.scalar(select(BidAlertPlan.plan_id).where(wanted))
    if found:
        return found
    # The same request IDs make PayPal answer a second web worker that asks
    # at the same time with the same product and plan.
    key = f"bid-alerts-{settings.paypal_mode}-{cents}-{client_id[-16:]}"
    product = paypal.call(
        settings,
        "POST",
        "/v1/catalogs/products",
        json={
            "name": f"{settings.site_name} bid alerts"[: paypal.MAX_FIELD],
            "description": "Email alerts for new Government of Guam bids",
            "type": "SERVICE",
        },
        request_id=f"{key}-product",
        transport=transport,
    )
    if not isinstance(product.get("id"), str):
        raise paypal.PayPalError("PayPal made no product for the plan")
    plan = paypal.call(
        settings,
        "POST",
        "/v1/billing/plans",
        json={
            "product_id": product["id"],
            "name": f"Bid alerts, {price_text(cents)} a month",
            "description": "Email alerts for new Government of Guam bids",
            "status": "ACTIVE",
            "billing_cycles": [
                {
                    "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                    "tenure_type": "REGULAR",
                    "sequence": 1,
                    "total_cycles": 0,  # every month until cancelled
                    "pricing_scheme": {
                        "fixed_price": {
                            "value": paypal.amount_text(cents),
                            "currency_code": paypal.CURRENCY,
                        }
                    },
                }
            ],
            "payment_preferences": {
                "auto_bill_outstanding": True,
                # Two failed payments in a row suspend the subscription.
                "payment_failure_threshold": 2,
            },
        },
        request_id=f"{key}-plan",
        transport=transport,
    )
    if not isinstance(plan.get("id"), str):
        raise paypal.PayPalError("PayPal made no plan")
    with session_scope() as session:
        session.execute(
            insert(BidAlertPlan)
            .values(client_id=client_id, price_cents=cents, plan_id=plan["id"])
            .on_conflict_do_nothing(constraint="uq_bid_alert_plans_price")
        )
        found = session.scalar(select(BidAlertPlan.plan_id).where(wanted))
    log.info("PayPal plan for bid alerts at %s a month: %s", price_text(cents), found)
    return found


def start_payment(
    settings: Settings, token: str, now: datetime | None = None, transport=None
) -> str | None:
    """The PayPal page where the subscriber approves the monthly payment.
    None when there is nothing to pay: the subscription is paid already."""
    now = now or datetime.now(timezone.utc)
    sub = by_token(token)
    if sub is None:
        raise AlertsError("This link is not valid any more. Please sign up again.")
    if sub.status != "pending":
        return None
    if sub.paypal_id:
        # Approved in another tab? Then it is paid, and needs no new page.
        check(settings, sub.id, now, transport)
        if (by_token(token) or sub).status != "pending":
            return None
        if (
            sub.approve_url
            and sub.paypal_created_at
            and now - sub.paypal_created_at < APPROVE_REUSE
        ):
            return sub.approve_url
    data = paypal.call(
        settings,
        "POST",
        "/v1/billing/subscriptions",
        json={
            "plan_id": plan_id(settings, transport),
            "custom_id": f"bid-alerts-{sub.id}",
            "application_context": {
                "brand_name": settings.site_name[: paypal.MAX_FIELD],
                "shipping_preference": "NO_SHIPPING",
                "user_action": "SUBSCRIBE_NOW",
                "return_url": manage_url(settings, token),
                "cancel_url": review_url(settings, token) + "&paypal=cancelled",
            },
        },
        # Two clicks within ten minutes get the same subscription.
        request_id=f"bid-alerts-{sub.id}-{int(now.timestamp()) // 600}",
        transport=transport,
    )
    links = data.get("links") if isinstance(data.get("links"), list) else []
    approve = next(
        (x.get("href") for x in links if isinstance(x, dict) and x.get("rel") == "approve"), None
    )
    if not isinstance(data.get("id"), str) or not isinstance(approve, str):
        raise paypal.PayPalError("PayPal made no subscription, or gave no page to approve it on")
    if not _set(
        sub.id, ("pending",), paypal_id=data["id"], approve_url=approve, paypal_created_at=now
    ):
        return None  # paid or ended meanwhile: the subscriber's page says which
    return approve


def _paypal_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _set(sub_id: int, statuses: tuple[str, ...], **values) -> bool:
    """Change a subscriber whose status is one of `statuses`. False when it
    was not (another check changed it first), so each change happens once."""
    with session_scope() as session:
        changed = session.execute(
            update(BidAlertSubscriber)
            .where(BidAlertSubscriber.id == sub_id, BidAlertSubscriber.status.in_(statuses))
            .values(**values)
            .returning(BidAlertSubscriber.id)
        ).first()
    return changed is not None


def check(
    settings: Settings, sub_id: int, now: datetime | None = None, transport=None
) -> str | None:
    """Ask PayPal about one subscription and bring the subscriber up to date.
    Returns what changed: "activated", "resumed", "suspended", "cancelled",
    or None. Sends the welcome email and the Discord notes."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        sub = session.get(BidAlertSubscriber, sub_id)
        newest = session.scalar(select(func.max(Bid.id))) or 0
    if sub is None or not sub.paypal_id:
        return None
    data = paypal.call(
        settings, "GET", f"/v1/billing/subscriptions/{sub.paypal_id}", transport=transport
    )
    if data.get("custom_id") not in (None, f"bid-alerts-{sub.id}"):
        raise paypal.PayPalError(f"PayPal subscription {sub.paypal_id} is not for this subscriber")
    status = str(data.get("status") or "")
    billing = data.get("billing_info") if isinstance(data.get("billing_info"), dict) else {}
    next_billing = _paypal_time(billing.get("next_billing_time"))
    with session_scope() as session:
        session.execute(
            update(BidAlertSubscriber).where(BidAlertSubscriber.id == sub.id).values(checked_at=now)
        )
    event = None
    if status == "ACTIVE":
        paid_until = next_billing or max(sub.paid_until or now, now + timedelta(days=31))
        if _set(
            sub.id,
            ("pending",),
            status="active",
            activated_at=now,
            paid_until=paid_until,
            last_bid_id=newest,  # the welcome email lists the bids open now
        ):
            event = "activated"
        elif _set(sub.id, ("suspended",), status="active", paid_until=paid_until):
            event = "resumed"
        else:
            _set(sub.id, ("active",), paid_until=paid_until)
    elif status == "SUSPENDED":
        if _set(sub.id, ("active",), status="suspended"):
            event = "suspended"
    elif status in ("CANCELLED", "EXPIRED"):
        if _set(sub.id, ("active", "suspended"), status="cancelled", cancelled_at=now):
            event = "cancelled"
        else:
            # Never paid: nothing to keep.
            _set(sub.id, ("pending",), status="cancelled", cancelled_at=now)
    if event:
        _after(settings, sub.id, event, now)
    return event


def cancel(settings: Settings, sub_id: int, now: datetime | None = None, transport=None) -> bool:
    """Stop the PayPal payments of a subscription. The alerts go on until the
    end of the time already paid for. False when it was not active."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        sub = session.get(BidAlertSubscriber, sub_id)
    if sub is None or sub.status not in ("active", "suspended") or not sub.paypal_id:
        return False
    try:
        paypal.call(
            settings,
            "POST",
            f"/v1/billing/subscriptions/{sub.paypal_id}/cancel",
            json={"reason": "Cancelled on the bid alerts page"},
            transport=transport,
        )
    except paypal.PayPalError as exc:
        # 422: PayPal has it cancelled (or ended) already.
        if exc.status != 422:
            raise
    if not _set(sub.id, ("active", "suspended"), status="cancelled", cancelled_at=now):
        return False
    _after(settings, sub.id, "cancelled", now)
    return True


def look_after(settings: Settings, now: datetime | None = None, transport=None) -> None:
    """The worker's hourly care of the subscriptions: delete unpaid sign-ups
    and old cancelled ones, and ask PayPal about the pending subscriptions
    (every time) and the paying ones (every CHECK_EVERY)."""
    now = now or datetime.now(timezone.utc)
    sub = BidAlertSubscriber
    with session_scope() as session:
        session.execute(
            delete(sub).where(sub.status == "pending", sub.created_at < now - PENDING_DAYS)
        )
        session.execute(
            delete(sub).where(
                sub.status == "cancelled",
                sub.cancelled_at < now - KEEP_AFTER_END,
                or_(sub.paid_until.is_(None), sub.paid_until < now - KEEP_AFTER_END),
            )
        )
        due = session.scalars(
            select(sub.id).where(
                sub.paypal_id.is_not(None),
                or_(
                    sub.status == "pending",
                    sub.status.in_(("active", "suspended"))
                    & or_(sub.checked_at.is_(None), sub.checked_at < now - CHECK_EVERY),
                ),
            )
        ).all()
    for sub_id in due:
        try:
            check(settings, sub_id, now, transport)
        except paypal.PayPalError as exc:
            log.warning("bid alerts: could not check subscriber %d with PayPal: %s", sub_id, exc)


# ------------------------------------------------------------------- emails


def _due_text(bid) -> str:
    """ "Tue, Dec 1, 2026, 10:00 AM ChST", or the day alone without a time."""
    if bid.due_at is None:
        return "See the notice"
    local = bid.due_at.astimezone(GUAM_TZ)
    day = f"{local:%a}, {local:%b} {local.day}, {local.year}"
    if not bid.due_time:
        return day
    return f"{day}, {local.hour % 12 or 12}:{local:%M} {local:%p} ChST"


def _bid_lines(item: dict) -> list[str]:
    bid = item["bid"]
    lines = [bid.title]
    head = " · ".join(p for p in (bid.kind, bid.number) if p)
    if head:
        lines.append(head)
    if bid.agency:
        lines.append(bid.agency)
    lines.append(f"Due: {_due_text(bid)}")
    if item.get("matches"):
        lines.append("Matches: " + ", ".join(item["matches"]))
    lines.append(bid.url)
    return lines


def _bid_html(item: dict) -> str:
    bid = item["bid"]
    head = " · ".join(p for p in (bid.kind, bid.number) if p)
    rows = [
        f'<p style="margin:0;font:12px Arial,sans-serif;color:#666">{escape(head)}</p>'
        if head
        else "",
        f'<p style="margin:2px 0 4px;font-size:17px;line-height:1.3"><a href="{escape(bid.url)}"'
        f' style="color:#0b4f8a">{escape(bid.title)}</a></p>',
        '<p style="margin:0;font:14px Arial,sans-serif;color:#222">'
        + (f"<strong>{escape(bid.agency)}</strong> · " if bid.agency else "")
        + f"Due {escape(_due_text(bid))}</p>",
    ]
    if item.get("matches"):
        rows.append(
            '<p style="margin:2px 0 0;font:13px Arial,sans-serif;color:#666">Matches: '
            f"{escape(', '.join(item['matches']))}</p>"
        )
    return f'<div style="border-top:1px solid #e3e0d8;padding:12px 0">{"".join(rows)}</div>'


def _html(title: str, intro: str, items: list[dict], footer: list[str]) -> str:
    body = "".join(_bid_html(item) for item in items)
    notes = "".join(
        f'<p style="margin:8px 0;font:13px Arial,sans-serif;color:#555">{line}</p>'
        for line in footer
    )
    return (
        '<!doctype html><html><body style="margin:0;padding:16px;background:#f4f2ec;'
        'color:#1f1f1f;font-family:Georgia,serif">'
        '<div style="max-width:600px;margin:0 auto;background:#fff;padding:20px 24px;'
        'border:1px solid #e3e0d8">'
        f'<h1 style="margin:0 0 8px;font-size:21px">{escape(title)}</h1>'
        f'<p style="margin:0 0 12px;font:15px Arial,sans-serif">{intro}</p>'
        f"{body}"
        f'<div style="border-top:1px solid #e3e0d8;margin-top:4px;padding-top:4px">{notes}</div>'
        "</div></body></html>"
    )


def _footer(settings: Settings, sub: BidAlertSubscriber) -> tuple[list[str], list[str]]:
    """The end of every alert email: plain text lines, and HTML lines."""
    page = manage_url(settings, sub.token)
    bids = f"{settings.site_url}/bids"
    awards = awards_url(settings, sub.token)
    text = [
        f"All open bids: {bids}",
        f"Who won past bids, and for how much (your full award history): {awards}",
        f"Change your trades or words, or cancel: {page}",
        f"You get this email because you subscribed to bid alerts on {settings.site_name}."
        " Always check the details with the agency before you bid.",
    ]
    html = [
        f'<a href="{escape(bids)}">All open bids</a> · <a href="{escape(awards)}">Who won past'
        f' bids (your full award history)</a> · <a href="{escape(page)}">Change your'
        " trades or words, or cancel</a>",
        f"You get this email because you subscribed to bid alerts on"
        f" {escape(settings.site_name)}. Always check the details with the agency before you"
        " bid.",
    ]
    return text, html


def alert_email(
    settings: Settings, sub: BidAlertSubscriber, found: list[dict]
) -> tuple[str, str, str]:
    """(subject, text, html) of an email with new bids."""
    if len(found) == 1:
        subject = f"New bid: {found[0]['bid'].title}"
        title = "A new bid matches your alerts"
    else:
        subject = title = f"{len(found)} new bids match your alerts"
    if len(subject) > 120:
        subject = subject[:119] + "…"
    foot_text, foot_html = _footer(settings, sub)
    text = "\n\n".join(["\n".join(_bid_lines(item)) for item in found] + foot_text)
    intro = f"For {escape(choices_text(sub.trades, sub.keywords))}."
    return subject, f"{title}\n\n{text}\n", _html(title, intro, found, foot_html)


def welcome_email(
    settings: Settings, sub: BidAlertSubscriber, found: list[dict]
) -> tuple[str, str, str]:
    """(subject, text, html) of the email that starts the alerts."""
    page = manage_url(settings, sub.token)
    price = price_text(settings.bid_alerts_price_cents)
    title = "Your bid alerts are on"
    about = (
        f"You'll get an email when a new Government of Guam bid matches"
        f" {choices_text(sub.trades, sub.keywords)}. The bid lists are checked every"
        f" {settings.bids_interval_hours} hours."
    )
    keep = (
        f"Keep this email: its link opens your own page, where you can change your trades or"
        f" words, or cancel. PayPal takes {price} each month until you cancel."
    )
    awards = awards_url(settings, sub.token)
    history = (
        "Your subscription also opens the full award history: who won past Government of Guam"
        " bids, and for how much."
    )
    shown = found[:WELCOME_BIDS]
    if shown:
        now_text = f"{len(found)} open {'bid matches' if len(found) == 1 else 'bids match'} now:"
        if len(found) > len(shown):
            now_text = f"{len(found)} open bids match now. The {len(shown)} due soonest:"
    else:
        now_text = "No open bid matches right now. You'll hear from us when one does."
    text = "\n\n".join(
        [title, about, keep, f"Your page: {page}", f"{history} {awards}", now_text]
        + ["\n".join(_bid_lines(item)) for item in shown]
        + [f"All open bids: {settings.site_url}/bids"]
    )
    intro = (
        f'{escape(about)}</p><p style="margin:0 0 12px;font:15px Arial,sans-serif">'
        f'{escape(keep)} <a href="{escape(page)}">Open your page</a>.</p>'
        f'<p style="margin:0 0 12px;font:15px Arial,sans-serif">{escape(history)}'
        f' <a href="{escape(awards)}">Open the award history</a>.</p>'
        f'<p style="margin:0 0 12px;font:15px Arial,sans-serif"><strong>{escape(now_text)}'
        "</strong>"
    )
    footer = [
        f'<a href="{escape(settings.site_url)}/bids">All open bids</a> · Always check the details'
        " with the agency before you bid."
    ]
    return f"{title} ({settings.site_name})", text + "\n", _html(title, intro, shown, footer)


def links_email(settings: Settings, subs: list[BidAlertSubscriber]) -> tuple[str, str, str]:
    """(subject, text, html) of the email that sends lost page links again."""
    title = "The link to your bid alerts"
    intro = "Here is the link to your bid alerts page, where you can change or cancel them."
    lines = [
        f"{choices_text(s.trades, s.keywords)}:\n{manage_url(settings, s.token)}" for s in subs
    ]
    html_lines = "".join(
        '<p style="margin:12px 0;font:15px Arial,sans-serif">'
        f"{escape(choices_text(s.trades, s.keywords))}<br>"
        f'<a href="{escape(manage_url(settings, s.token))}">Open this bid alerts page</a></p>'
        for s in subs
    )
    note = (
        "Someone asked for this link on the bid alerts page. If it was not you, you can ignore it."
    )
    text = "\n\n".join([title, intro, *lines, note]) + "\n"
    html = (
        '<!doctype html><html><body style="margin:0;padding:16px;background:#f4f2ec;'
        'font-family:Georgia,serif"><div style="max-width:600px;margin:0 auto;background:#fff;'
        'padding:20px 24px;border:1px solid #e3e0d8">'
        f'<h1 style="margin:0 0 8px;font-size:21px">{escape(title)}</h1>'
        f'<p style="margin:0;font:15px Arial,sans-serif">{escape(intro)}</p>{html_lines}'
        f'<p style="font:13px Arial,sans-serif;color:#555">{escape(note)}</p></div></body></html>'
    )
    return f"{title} ({settings.site_name})", text, html


def _send(settings: Settings, sub: BidAlertSubscriber, email: tuple[str, str, str]) -> None:
    subject, text, html = email
    mail.send(
        settings,
        sub.email,
        subject,
        text,
        html,
        headers={"List-Unsubscribe": f"<{manage_url(settings, sub.token)}>"},
    )


def _after(settings: Settings, sub_id: int, event: str, now: datetime) -> None:
    """The welcome email of a new subscriber, and a note in Discord."""
    with session_scope() as session:
        sub = session.get(BidAlertSubscriber, sub_id)
        items = list_bids(session, BidFilters(), now).items if event == "activated" else []
    if sub is None:
        return
    if event == "activated":
        try:
            _send(
                settings,
                sub,
                welcome_email(settings, sub, matching(items, sub.trades, sub.keywords)),
            )
        except mail.MailError as exc:
            log.warning("bid alerts: the welcome email to subscriber %d failed: %s", sub_id, exc)
    if not settings.discord_webhook_url:
        return
    what = choices_text(sub.trades, sub.keywords)
    notes = {
        "activated": ("New bid alerts subscriber", f"For {what}.", "💵"),
        "resumed": ("Bid alerts payment works again", f"A subscriber for {what}.", "💵"),
        "suspended": (
            "Bid alerts payment failed",
            f"PayPal suspended the subscription of a subscriber for {what}. Their alerts stop.",
            "⚠️",
        ),
        "cancelled": (
            "Bid alerts subscription cancelled",
            f"A subscriber for {what}. Alerts go on until the paid time ends.",
            "👋",
        ),
    }
    title, message, emoji = notes[event]
    send_discord(settings, Alert(title=title, message=message, emoji=emoji, quiet=True))


def send_alerts(settings: Settings, now: datetime | None = None) -> int:
    """After a bids check: send each receiving subscriber one email with the
    new open bids that match. Returns the number of emails sent. A bid is
    never sent twice to a subscriber; when an email fails, its bids are tried
    again next time."""
    if not is_on(settings):
        return 0
    now = now or datetime.now(timezone.utc)
    sent = 0
    with advisory_lock(BID_ALERTS_LOCK_KEY) as acquired:
        if not acquired:
            log.info("bid alerts are being sent by another process; skipping")
            return 0
        with session_scope() as session:
            newest = session.scalar(select(func.max(Bid.id))) or 0
            items = list_bids(session, BidFilters(), now).items
            subs = session.scalars(
                select(BidAlertSubscriber).where(
                    _receiving(now), BidAlertSubscriber.last_bid_id < newest
                )
            ).all()
        for sub in subs:
            # A bid posted in two places is new when both listings are new.
            new = [i for i in items if sub.last_bid_id < min(i["ids"]) <= newest]
            found = matching(new, sub.trades, sub.keywords)
            if found:
                try:
                    _send(settings, sub, alert_email(settings, sub, found))
                except mail.MailError as exc:
                    log.warning("bid alerts: the email to subscriber %d failed: %s", sub.id, exc)
                    continue
                sent += 1
            with session_scope() as session:
                session.execute(
                    update(BidAlertSubscriber)
                    .where(BidAlertSubscriber.id == sub.id)
                    .values(last_bid_id=newest, **({"last_sent_at": now} if found else {}))
                )
    if sent:
        log.info("bid alerts: %d emails sent", sent)
    return sent


def send_links(settings: Settings, email: str, now: datetime | None = None) -> bool:
    """Email the page links of an address's bid alerts again. False when the
    address has none (the page says the same either way)."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        subs = session.scalars(
            select(BidAlertSubscriber)
            .where(
                func.lower(BidAlertSubscriber.email) == email.lower(),
                or_(_receiving(now), BidAlertSubscriber.status == "suspended"),
            )
            .order_by(BidAlertSubscriber.id)
        ).all()
    if not subs:
        return False
    subject, text, html = links_email(settings, subs)
    mail.send(settings, subs[0].email, subject, text, html)
    return True
