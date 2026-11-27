"""Paid listings that people post on the site themselves: classified ads,
featured job posts, featured events, businesses in the directory, press
releases and deals.

There are no user accounts. A listing goes so:

1. The poster fills in the form of its kind (/post/<kind>). The site saves
   the listing as "unpaid" and opens the poster's own page for it,
   /listing/<id>/<key>, where <key> is a secret. That page shows how the
   listing will look, and the PayPal link (see paypal.py).
2. After the payment, the listing waits for review ("review"). A basic
   business entry costs nothing and goes straight to review. The owner of
   the site gets a Discord alert (alerts.py), if those are on.
3. The owner approves it or rejects it on /admin/listings (behind
   STATS_PASSWORD). A rejected listing gets its money back. An approved one
   is "live": classified ads and job posts for so many days, a featured
   event until it is over (it is copied to the events calendar
   FEATURED_CALENDAR, so it is on the Events page, its month calendar, the
   event's own page and the calendar feed), and a business without end (its
   upgrade for so many days). A press release stays on the Press releases
   page, with a page of its own, and goes once into the newsletter (the
   next edition after it is approved). A deal shows on the Deals page and on
   the home page until the last day that its business picked; it costs
   DEAL_WEEK_PRICE for each week (or part of a week) from the day it is sent.
4. Before a classified ad, a job post, a business's upgrade or a deal ends,
   the poster gets an email (renewals.py). On the poster's own page, the
   poster can pay again through PayPal for as many days more (a deal: one
   week more). A renewal needs no new review: the listing does not change.

Listings are on while PayPal (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET) and
STATS_PASSWORD are set. A kind whose price is 0 is off.
"""

from __future__ import annotations

import email.parser
import email.policy
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import paypal
from .alerts import fold
from .models import FEATURED_CALENDAR, Event, EventCalendar, Listing, ListingRenewal
from .scraper.images import ImageRejected, process_image
from .scraper.text import GUAM_TZ
from .settings import Settings

log = logging.getLogger(__name__)

UNPAID, REVIEW, LIVE, REJECTED, REMOVED = "unpaid", "review", "live", "rejected", "removed"

CLASSIFIED_SECTIONS = ("For sale", "Cars and trucks", "Housing", "Services", "Wanted")
BUSINESS_CATEGORIES = (
    "Restaurants and food",
    "Shopping",
    "Cars and repair",
    "Health and beauty",
    "Home and building",
    "Tours and fun",
    "Professional services",
    "Other services",
)

# An unpaid listing older than this is deleted (the poster left before paying).
UNPAID_DAYS = 7
# A featured event must start within this many days, and last at most
# MAX_EVENT_DAYS (the Events page shows the next 90 days).
EVENT_DAYS_AHEAD = 90
MAX_EVENT_DAYS = 14
# A deal can run for at most this many weeks from the day it is sent; a
# renewal adds DEAL_RENEW_DAYS.
MAX_DEAL_WEEKS = 8
DEAL_RENEW_DAYS = 7
# The longest press release, in characters (about 1,000 words).
MAX_PRESS_TEXT = 6000
# The largest form (with its photo) that the site accepts.
MAX_BODY = 8 * 1024 * 1024
MAX_PHOTO = 6 * 1024 * 1024
PHOTO_DIR = "listings"


@dataclass(frozen=True)
class Kind:
    slug: str
    # "a classified ad": what the poster buys.
    name: str
    # The public page that shows it.
    page: str
    # Has a photo field.
    photo: bool = False


KINDS = {
    "classified": Kind("classified", "Classified ad", "/classifieds", photo=True),
    "job": Kind("job", "Featured job post", "/jobs"),
    "event": Kind("event", "Featured event", "/events"),
    "business": Kind("business", "Business directory listing", "/directory", photo=True),
    "press": Kind("press", "Press release", "/press", photo=True),
    "deal": Kind("deal", "Deal", "/deals", photo=True),
}


def price_cents(settings: Settings, kind: str, featured: bool = False) -> int:
    """What a listing costs, in US cents. A business costs nothing without the
    upgrade. For a deal: the price of one week (see deal_price_cents)."""
    if kind == "classified":
        return settings.classified_price_cents
    if kind == "job":
        return settings.featured_job_price_cents
    if kind == "event":
        return settings.featured_event_price_cents
    if kind == "press":
        return settings.press_release_price_cents
    if kind == "deal":
        return settings.deal_week_price_cents
    return settings.business_upgrade_price_cents if featured else 0


def deal_weeks(last_day: date, today: date) -> int:
    """The weeks a deal is paid for: each week, or part of a week, from today
    to its last day. To the same weekday next week is one week."""
    return max(1, -(-(last_day - today).days // 7))


def paid_weeks(listing: Listing) -> int:
    """The weeks that a deal was (or is to be) paid for when it was sent."""
    sent = (listing.created_at or datetime.now(GUAM_TZ)).astimezone(GUAM_TZ).date()
    last = deal_last_day(listing)
    return deal_weeks(last, sent) if last else 1


def deal_last_day(listing: Listing) -> date | None:
    """The last day that a deal shows, in Guam time."""
    end = listing.live_until or listing.ends_at
    if end is None:
        return None
    return (end.astimezone(GUAM_TZ) - timedelta(microseconds=1)).date()


def days(settings: Settings, kind: str) -> int | None:
    """How many days a listing (or a business's upgrade) shows once approved,
    and what a renewal adds. None for a featured event, which shows until it
    is over, and for a press release, which stays. A deal shows until its
    last day; a renewal adds a week."""
    return {
        "classified": settings.classified_days,
        "job": settings.featured_job_days,
        "business": settings.business_upgrade_days,
        "deal": DEAL_RENEW_DAYS,
    }.get(kind)


def is_on(settings: Settings, kind: str | None = None) -> bool:
    """Listings need PayPal, and STATS_PASSWORD for the review page. A kind
    is on when its price is more than 0 (for a business: the upgrade)."""
    if not (paypal.enabled(settings) and settings.stats_password):
        return False
    if kind is None:
        return any(is_on(settings, k) for k in KINDS)
    return kind in KINDS and price_cents(settings, kind, featured=True) > 0


def money(cents: int) -> str:
    """500 -> "$5", 1250 -> "$12.50"."""
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents // 100:,}.{cents % 100:02d}"


# ------------------------------------------------------------------ forms


class FormError(Exception):
    """The form could not be read: too large, or not a form."""


def parse_form(body: bytes, content_type: str) -> tuple[dict[str, str], bytes | None]:
    """The text fields of a form sent as multipart/form-data, and the photo
    (the file of the field "photo"), if one was chosen. The standard
    library's email parser reads the parts."""
    if not content_type.lower().startswith("multipart/form-data") or len(body) > MAX_BODY:
        raise FormError("not a form, or too large")
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "replace")
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    if not message.is_multipart():
        raise FormError("not a multipart form")
    fields: dict[str, str] = {}
    photo = None
    for count, part in enumerate(message.iter_parts()):
        if count >= 30:
            raise FormError("too many fields")
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        data = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            if name == "photo" and data:
                photo = data
            continue
        fields[name] = data.decode("utf-8", "replace")
    return fields, photo


def _text(value: str | None, limit: int) -> str:
    """One line: spaces in a row become one, and the text is cut at `limit`."""
    return " ".join((value or "").split())[:limit]


def _block(value: str | None, limit: int) -> str:
    """Text with its line breaks (at most one empty line in a row), cut at `limit`."""
    lines = [" ".join(line.split()) for line in (value or "").replace("\r", "").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:limit]


def _web_link(value: str) -> str | None:
    """https://example.com/page, with https:// added when the poster left it
    out. None when it is not a web address."""
    value = value.strip()
    if not value:
        return None
    if not re.match(r"https?://", value, re.I):
        value = "https://" + value
    host = re.match(r"https?://([^/?#\s]+)", value, re.I)
    if not host or re.search(r"\s", value) or "." not in host.group(1) or "@" in host.group(1):
        return None
    return value[:300]


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _clock(value: str) -> time | None:
    try:
        return time.fromisoformat(value.strip()) if value.strip() else None
    except ValueError:
        return None


def check(
    kind: str, fields: dict[str, str], today: date, muted: tuple[str, ...] = ()
) -> tuple[dict, dict[str, str]]:
    """The listing's values from its form, and the problems to show beside
    the fields ({field: message}); no problems means it can be saved."""
    errors: dict[str, str] = {}
    values: dict = {"title": _text(fields.get("title"), 140 if kind == "press" else 100)}
    if len(values["title"]) < 3:
        errors["title"] = "Please give a title of at least 3 letters."

    if kind == "classified":
        values["category"] = fields.get("category", "")
        values["price"] = _text(fields.get("price"), 40) or None
        values["location"] = _text(fields.get("location"), 80) or None
        values["description"] = _block(fields.get("description"), 1500)
        values["contact"] = _text(fields.get("contact"), 120)
        if values["category"] not in CLASSIFIED_SECTIONS:
            errors["category"] = "Please pick a section."
    elif kind == "job":
        values["name"] = _text(fields.get("name"), 100)
        values["location"] = _text(fields.get("location"), 80) or None
        values["price"] = _text(fields.get("price"), 60) or None
        values["description"] = _block(fields.get("description"), 3000)
        values["contact"] = _text(fields.get("contact"), 200) or None
        if not values["name"]:
            errors["name"] = "Please give the employer's name."
    elif kind == "event":
        values["name"] = _text(fields.get("name"), 100)
        values["location"] = _text(fields.get("location"), 120)
        values["description"] = _block(fields.get("description"), 1500) or None
        if not values["name"]:
            errors["name"] = "Please give the organizer's name."
        if not values["location"]:
            errors["location"] = "Please say where the event is."
        values.update(_event_times(fields, today, errors))
    elif kind == "business":
        values["category"] = fields.get("category", "")
        values["location"] = _text(fields.get("location"), 120)
        values["contact"] = _text(fields.get("contact"), 60) or None
        values["description"] = _block(fields.get("description"), 600)
        values["featured"] = fields.get("plan") == "featured"
        if values["category"] not in BUSINESS_CATEGORIES:
            errors["category"] = "Please pick a category."
        if not values["location"]:
            errors["location"] = "Please give the village or the address."
    elif kind == "press":
        values["name"] = _text(fields.get("name"), 100)
        values["description"] = _block(fields.get("description"), MAX_PRESS_TEXT)
        values["contact"] = _text(fields.get("contact"), 200) or None
        if not values["name"]:
            errors["name"] = "Please give the name of the business or group."
        if len(values["description"]) < 50:
            errors["description"] = "Please write the release (at least 50 letters)."
    elif kind == "deal":
        values["name"] = _text(fields.get("name"), 100)
        values["category"] = fields.get("category", "")
        # The coupon code, if the deal has one, is kept as its "price".
        values["price"] = _text(fields.get("price"), 40) or None
        values["location"] = _text(fields.get("location"), 120) or None
        values["description"] = _block(fields.get("description"), 600)
        values["contact"] = _text(fields.get("contact"), 60) or None
        if not values["name"]:
            errors["name"] = "Please give the business's name."
        if values["category"] not in BUSINESS_CATEGORIES:
            errors["category"] = "Please pick a category."
        last = _day(fields.get("end_date", ""))
        if last is None or not today <= last <= today + timedelta(weeks=MAX_DEAL_WEEKS):
            errors["end_date"] = f"Please pick a day from today to {MAX_DEAL_WEEKS} weeks from now."
        else:
            # It shows until the end of its last day, Guam time.
            values["ends_at"] = datetime.combine(last + timedelta(days=1), time(0), tzinfo=GUAM_TZ)

    if (
        kind in ("classified", "job", "business", "deal")
        and len(values.get("description") or "") < 10
    ):
        errors["description"] = "Please write a few words about it (at least 10 letters)."
    if kind == "classified" and len(values["contact"]) < 5:
        errors["contact"] = "Please give a phone number or an email address."
    if kind in ("job", "event", "business", "press", "deal"):
        raw = fields.get("link", "")
        values["link"] = _web_link(raw)
        if raw.strip() and values["link"] is None:
            errors["link"] = "That doesn't look like a web address."
    if kind == "job" and not values.get("link") and not values.get("contact"):
        errors["contact"] = "Please say how to apply, or give the page to apply on."

    # The words that the site never shows (`muted` in sources.yaml) are not
    # named here, so the page does not show them either.
    text = fold(" ".join(str(v) for v in values.values() if isinstance(v, str)))
    if any(fold(phrase) in text for phrase in muted if phrase.strip()):
        errors["form"] = "This listing has words that the site does not accept. Please change it."
    return values, errors


def _event_times(fields: dict[str, str], today: date, errors: dict[str, str]) -> dict:
    """starts_at, ends_at and all_day from the date and time fields."""
    first = _day(fields.get("date", ""))
    last = _day(fields.get("end_date", "")) if fields.get("end_date", "").strip() else first
    start, end = _clock(fields.get("start_time", "")), _clock(fields.get("end_time", ""))
    if first is None or not today <= first <= today + timedelta(days=EVENT_DAYS_AHEAD):
        errors["date"] = f"Please pick a date from today to {EVENT_DAYS_AHEAD} days from now."
        return {}
    if last is None or not first <= last <= first + timedelta(days=MAX_EVENT_DAYS - 1):
        errors["end_date"] = (
            f"The last day must be on or after the first, within {MAX_EVENT_DAYS} days."
        )
        return {}
    if fields.get("start_time", "").strip() and start is None:
        errors["start_time"] = "Please give the time as on a clock, such as 18:30."
        return {}
    if start is None:
        # An all-day event ends at the start of the day after its last day.
        return {
            "starts_at": datetime.combine(first, time(0), tzinfo=GUAM_TZ),
            "ends_at": datetime.combine(last + timedelta(days=1), time(0), tzinfo=GUAM_TZ),
            "all_day": True,
        }
    starts_at = datetime.combine(first, start, tzinfo=GUAM_TZ)
    ends_at = datetime.combine(last, end, tzinfo=GUAM_TZ) if end else None
    if ends_at is not None and ends_at <= starts_at:
        errors["end_time"] = "The event must end after it starts."
        return {}
    return {"starts_at": starts_at, "ends_at": ends_at, "all_day": False}


# ----------------------------------------------------------------- photos


def save_photo(media_dir: Path, data: bytes, max_width: int) -> tuple[str, int, int]:
    """Store the photo of a listing as a WebP copy (see scraper/images.py),
    under a random name. Returns its path under the media folder, its width
    and its height. Raises ImageRejected."""
    if len(data) > MAX_PHOTO:
        raise ImageRejected("larger than 6 MB")
    webp, width, height = process_image(data, max_width)
    path = f"{PHOTO_DIR}/{secrets.token_hex(12)}.webp"
    target = media_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(webp)
    return path, width, height


def delete_photo(media_dir: Path, path: str | None) -> None:
    if path and path.startswith(f"{PHOTO_DIR}/") and ".." not in path:
        (media_dir / path).unlink(missing_ok=True)


# --------------------------------------------------------------- database


def create(
    session: Session, settings: Settings, kind: str, values: dict, photo: tuple | None
) -> Listing:
    """Save a new listing: unpaid, or waiting for review when it is free."""
    amount = price_cents(settings, kind, values.get("featured", False))
    if kind == "deal":
        last = (values["ends_at"] - timedelta(days=1)).date()
        amount *= deal_weeks(last, datetime.now(GUAM_TZ).date())
    listing = Listing(
        kind=kind,
        status=UNPAID if amount else REVIEW,
        key=secrets.token_urlsafe(12),
        amount_cents=amount,
        **values,
    )
    if photo:
        listing.photo, listing.photo_width, listing.photo_height = photo
    session.add(listing)
    session.flush()
    return listing


def delete_stale(session: Session, now: datetime, media_dir: Path) -> int:
    """Delete the listings that were never paid for, UNPAID_DAYS after they
    were made, and the renewals that were never paid for."""
    cutoff = now - timedelta(days=UNPAID_DAYS)
    session.execute(
        delete(ListingRenewal).where(
            ListingRenewal.paid_at.is_(None), ListingRenewal.created_at < cutoff
        )
    )
    old = session.execute(
        delete(Listing)
        .where(Listing.status == UNPAID, Listing.created_at < cutoff)
        .returning(Listing.photo)
    ).all()
    for (photo,) in old:
        delete_photo(media_dir, photo)
    return len(old)


def find(session: Session, listing_id: int, key: str) -> Listing | None:
    """The listing of a poster's own page, when the key is right."""
    if not 1 <= listing_id < 2**63:
        return None
    listing = session.get(Listing, listing_id)
    if listing is None or not secrets.compare_digest(listing.key.encode(), key.encode()):
        return None
    return listing


def set_order(session: Session, listing_id: int, order_id: str) -> None:
    """Remember the PayPal order of the latest "Pay" click."""
    session.execute(
        update(Listing)
        .where(Listing.id == listing_id, Listing.status == UNPAID)
        .values(order_id=order_id)
    )


class PaymentMismatch(Exception):
    """The payment is not the price of this listing, or it paid for another listing."""


def mark_paid(
    session: Session, listing_id: int, order_id: str, capture: paypal.Capture, now: datetime
) -> bool:
    """Record a completed PayPal payment, and send the listing to review.
    True when this call recorded it; False when it was recorded already.
    Raises PaymentMismatch when the amount is wrong, or when the payment
    belongs to another listing (each payment pays for one listing)."""
    listing = session.get(Listing, listing_id)
    if listing is None or not capture.paid or not capture.capture_id:
        raise PaymentMismatch("not a completed payment")
    if listing.capture_id == capture.capture_id:
        return False
    if capture.amount_cents != listing.amount_cents or capture.currency != paypal.CURRENCY:
        raise PaymentMismatch(
            f"paid {capture.amount_cents} {capture.currency}, the price is"
            f" {listing.amount_cents} {paypal.CURRENCY}"
        )
    try:
        with session.begin_nested():
            done = session.execute(
                update(Listing)
                .where(Listing.id == listing_id, Listing.status == UNPAID)
                .values(
                    status=REVIEW,
                    order_id=order_id,
                    capture_id=capture.capture_id,
                    paid_at=now,
                    email=capture.payer_email,
                )
            ).rowcount
    except IntegrityError as exc:
        raise PaymentMismatch("this payment was recorded for another listing") from exc
    return bool(done)


def _featured_calendar(session: Session) -> int:
    calendar = session.scalar(select(EventCalendar).where(EventCalendar.slug == FEATURED_CALENDAR))
    if calendar is None:
        calendar = EventCalendar(slug=FEATURED_CALENDAR, name="Featured events", enabled=True)
        session.add(calendar)
        session.flush()
    calendar.enabled = True
    return calendar.id


def approve(session: Session, settings: Settings, listing_id: int, now: datetime) -> bool:
    """Put a listing that waits for review on the site. False when it was not waiting."""
    listing = session.get(Listing, listing_id, with_for_update=True)
    if listing is None or listing.status != REVIEW:
        return False
    listing.status = LIVE
    listing.reviewed_at = now
    if listing.kind == "event":
        event = Event(
            calendar_id=_featured_calendar(session),
            uid=f"listing-{listing.id}",
            title=listing.title,
            starts_at=listing.starts_at,
            ends_at=listing.ends_at,
            all_day=listing.all_day,
            location=listing.location,
            organizer=listing.name,
            description=listing.description,
            url=listing.link,
        )
        session.add(event)
        session.flush()
        listing.event_id = event.id
        listing.live_until = listing.ends_at or listing.starts_at + timedelta(days=1)
    elif listing.kind == "deal":
        listing.live_until = listing.ends_at
    elif listing.kind == "press":
        listing.live_until = None  # it stays on the site
    elif listing.kind != "business" or listing.featured:
        length = days(settings, listing.kind)
        listing.live_until = now + timedelta(days=length)
    return True


def reject(session: Session, listing_id: int, now: datetime, note: str, refunded: bool) -> bool:
    """Turn down a listing that waits for review. `refunded`: its payment was
    given back (see the review page). False when it was not waiting."""
    done = session.execute(
        update(Listing)
        .where(Listing.id == listing_id, Listing.status == REVIEW)
        .values(
            status=REJECTED,
            reviewed_at=now,
            note=_text(note, 300) or None,
            refunded_at=now if refunded else None,
        )
    ).rowcount
    return bool(done)


def take_down(session: Session, listing_id: int, now: datetime) -> bool:
    """Take a live listing off the site (no refund). False when it was not live."""
    listing = session.get(Listing, listing_id, with_for_update=True)
    if listing is None or listing.status != LIVE:
        return False
    listing.status = REMOVED
    if listing.event_id:
        session.execute(
            update(Event)
            .where(Event.id == listing.event_id, Event.removed_at.is_(None))
            .values(removed_at=now)
        )
    return True


def upgraded(listing: Listing, now: datetime) -> bool:
    """A business whose upgrade (photo, link and top spot) has not ended."""
    return bool(listing.featured and listing.live_until and listing.live_until > now)


# --------------------------------------------------------------- renewals


def renewable(settings: Settings, listing: Listing) -> bool:
    """A live listing that its poster can pay to keep on the site for more
    days, also after it has ended: a classified ad, a job post, a business's
    upgrade or a deal. (A featured event shows until it is over, and a press
    release stays.) Only while that kind is for sale."""
    if listing.status != LIVE or days(settings, listing.kind) is None:
        return False
    return is_on(settings, listing.kind) and (listing.kind != "business" or listing.featured)


def start_renewal(
    session: Session, listing_id: int, order_id: str, amount_cents: int, length: int
) -> None:
    """Remember the PayPal order of a "Renew" click: its price and its days."""
    session.add(
        ListingRenewal(
            listing_id=listing_id, order_id=order_id, amount_cents=amount_cents, days=length
        )
    )


def record_renewal(
    session: Session, listing_id: int, order_id: str, capture: paypal.Capture, now: datetime
) -> str:
    """Record the payment of a renewal, and add its days to the listing: from
    its end, or from now when it has ended. Returns "ok"; "done" when it was
    recorded already; "gone" when the listing is not live any more (the
    caller gives the money back). Raises PaymentMismatch when the payment is
    not the renewal's price, or not of this listing's renewal."""
    renewal = session.scalars(
        select(ListingRenewal)
        .where(ListingRenewal.order_id == order_id, ListingRenewal.listing_id == listing_id)
        .with_for_update()
    ).first()
    if renewal is None or not capture.capture_id:
        raise PaymentMismatch("not a payment of a renewal of this listing")
    if renewal.paid_at is not None:
        return "done"
    if capture.amount_cents != renewal.amount_cents or capture.currency != paypal.CURRENCY:
        raise PaymentMismatch(
            f"paid {capture.amount_cents} {capture.currency}, the price is"
            f" {renewal.amount_cents} {paypal.CURRENCY}"
        )
    listing = session.get(Listing, listing_id, with_for_update=True)
    if listing is None or listing.status != LIVE:
        session.delete(renewal)
        return "gone"
    start = max(listing.live_until or now, now)
    listing.live_until = start + timedelta(days=renewal.days)
    if listing.kind == "deal":
        listing.ends_at = listing.live_until  # the last day that the page shows
    listing.reminded_at = None
    listing.email = capture.payer_email or listing.email
    renewal.capture_id, renewal.paid_at = capture.capture_id, now
    try:
        session.flush()
    except IntegrityError as exc:
        raise PaymentMismatch("this payment was recorded for another renewal") from exc
    return "ok"
