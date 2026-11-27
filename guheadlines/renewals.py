"""Paid listings and booked ads end on their date. A few days before, the
buyer gets one email, so that they can pay for more time.

What ends, and how:

- A classified ad and a featured job post show for CLASSIFIED_DAYS and
  FEATURED_JOB_DAYS after the owner approves them, and a business's upgrade
  lasts BUSINESS_UPGRADE_DAYS (listings.py). Then the pages stop showing them
  by themselves: they show only the listings whose end (live_until) is still
  to come. (A featured event shows until the event is over.)
- A booked ad (ads.py) shows only in the weeks or editions that it booked.

RENEWAL_REMINDER_DAYS before the end, the worker (each hour, send_reminders)
emails the buyer the link to the buyer's own page:

- A listing's page has a "Renew" link. PayPal takes the price again, and
  the listing gets as many days more, from its end (settle). The email goes
  to the address that the poster paid with on PayPal.
- A booking's page has a form to book the same ad again, for the next free
  weeks or editions. The new booking goes the usual way: PayPal, then the
  owner approves it (ads.py). The email goes to the booking's address.

No email goes out for an ad whose business has booked later dates already,
or for a kind that is not for sale any more. Email needs SMTP_HOST and
MAIL_FROM (mail.py), and SITE_DOMAIN or SITE_URL for the links.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from html import escape

from sqlalchemy import any_, literal, select, update
from sqlalchemy.orm import Session, defer

from . import ads, alerts, listings, mail, paypal
from .db import session_scope
from .models import AdBooking, Listing, ListingRenewal
from .scraper.text import GUAM_TZ
from .settings import Settings

log = logging.getLogger(__name__)

# Bookings that hold a booked date (not cancelled or turned down).
HOLDING = ("new", "paid", "approved")


def is_on(settings: Settings) -> bool:
    return bool(settings.renewal_reminder_days > 0 and mail.enabled(settings) and settings.site_url)


def _ads_on_sale(settings: Settings) -> set[str]:
    """The kinds of ads that the Advertise page sells now (as in web/app.py)."""
    if not (settings.stats_password and paypal.enabled(settings)):
        return set()
    return {offer.kind for offer in ads.offers(settings)}


def booking_end(booking: AdBooking) -> datetime:
    """The moment a booked ad stops: the end of its last day, in Guam time."""
    return datetime.combine(ads.last_day(booking) + timedelta(days=1), time(0), tzinfo=GUAM_TZ)


def _day(value: date | datetime) -> str:
    """ "Monday, December 7" """
    if isinstance(value, datetime):
        value = value.astimezone(GUAM_TZ).date()
    return f"{value:%A}, {value:%B} {value.day}"


# ------------------------------------------------------- renewing a listing


def settle(settings: Settings, listing_id: int, order_id: str, now: datetime | None = None) -> str:
    """Take the money of a renewal's PayPal order (when the poster comes back
    from PayPal), and add the days to the listing. Returns ok, unfinished,
    declined, error, mismatch, or gone (the listing was taken down while its
    poster paid: the money goes back)."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as session:
        renewal = session.scalars(
            select(ListingRenewal).where(
                ListingRenewal.order_id == order_id, ListingRenewal.listing_id == listing_id
            )
        ).first()
    if renewal is None:
        return "unfinished"  # not an order of this listing: PayPal is not asked
    if renewal.paid_at is not None:
        return "ok"  # the page was loaded again
    try:
        capture = paypal.capture_order(
            settings, order_id, custom_id=f"listing-{listing_id}-renewal"
        )
    except paypal.PayPalError as exc:
        if exc.issue == paypal.WRONG_ORDER:
            log.error("PayPal payment not used for a renewal of listing %s: %s", listing_id, exc)
            return "mismatch"
        log.warning("PayPal capture for a renewal of listing %s failed: %s", listing_id, exc)
        return "declined" if exc.issue == "INSTRUMENT_DECLINED" else "error"
    # PENDING: PayPal still checks the payment. The days are given at once,
    # as for a booked ad (ads.py); the owner's alert says so.
    if capture.status not in ("COMPLETED", "PENDING"):
        return "unfinished"
    try:
        with session_scope() as session:
            result = listings.record_renewal(session, listing_id, order_id, capture, now)
            listing = session.get(Listing, listing_id)
    except listings.PaymentMismatch as exc:
        log.error(
            "PayPal payment %s not used for a renewal of listing %s: %s",
            capture.capture_id,
            listing_id,
            exc,
        )
        return "mismatch"
    if result == "gone":
        log.warning("listing %s was taken down while it was renewed: refunding", listing_id)
        try:
            paypal.refund_capture(settings, capture.capture_id)
        except paypal.PayPalError as exc:
            log.error("refund of PayPal payment %s failed: %s", capture.capture_id, exc)
        return "gone"
    if result == "ok":
        log.info("listing %s renewed (PayPal capture %s)", listing_id, capture.capture_id)
        if settings.discord_webhook_url:
            pending = " PayPal is still checking this payment." if not capture.paid else ""
            alerts.send(
                settings,
                alerts.Alert(
                    title=f"{listings.KINDS[listing.kind].name} renewed",
                    message=(
                        f"{listing.title}: {listings.money(renewal.amount_cents)} for"
                        f" {renewal.days} more days, now until"
                        f" {_day(listing.live_until)}.{pending}"
                    ),
                    emoji="💵",
                    quiet=True,
                ),
            )
    return "ok"


# ------------------------------------------------------------------ emails


def _html(title: str, paragraphs: list[str], button: str, link: str, footer: str) -> str:
    """A plain email in the colors of the site, with one button."""
    body = "".join(
        f'<p style="margin:0 0 12px;font:15px Arial,sans-serif">{escape(p)}</p>' for p in paragraphs
    )
    return (
        '<!doctype html><html><body style="margin:0;padding:16px;background:#f4f2ec;'
        'color:#1f1f1f;font-family:Georgia,serif">'
        '<div style="max-width:600px;margin:0 auto;background:#fff;padding:20px 24px;'
        'border:1px solid #e3e0d8">'
        f'<h1 style="margin:0 0 12px;font-size:21px">{escape(title)}</h1>{body}'
        f'<p style="margin:16px 0"><a href="{escape(link)}" style="display:inline-block;'
        "padding:10px 16px;background:#1f1f1f;color:#fff;text-decoration:none;"
        f'font:bold 15px Arial,sans-serif">{escape(button)}</a></p>'
        f'<p style="margin:12px 0 0;font:13px Arial,sans-serif;color:#555">{escape(footer)}</p>'
        "</div></body></html>"
    )


def listing_email(settings: Settings, listing: Listing) -> tuple[str, str, str]:
    """(subject, text, html) of the email that says a listing ends soon."""
    page = f"{settings.site_url}/listing/{listing.id}/{listing.key}"
    price = listings.money(listings.price_cents(settings, listing.kind, featured=True))
    length = listings.days(settings, listing.kind)
    ends = _day(listing.live_until)
    if listing.kind == "business":
        what = "The featured upgrade of your directory listing"
        title = f"Your featured upgrade ends on {ends}"
        after = (
            "If you don't renew, your business stays in the directory as a basic entry,"
            " without the photo and the link."
        )
    else:
        name = listings.KINDS[listing.kind].name.lower()
        what = f"Your {name}"
        title = f"Your {name} ends on {ends}"
        after = "If you don't renew, it leaves the site by itself. You don't need to do anything."
    paragraphs = [
        f'{what} "{listing.title}" on {settings.site_name} ends on {ends}.',
        f"To keep it {length} days more, renew it for {price} on its page. You pay with"
        " PayPal, as before. The days are added to the end date, and it needs no new review.",
        after,
    ]
    footer = (
        f"You get this one email because you paid for this listing on {settings.site_name}"
        " with this PayPal address."
    )
    text = "\n\n".join([title, *paragraphs, f"Renew it here: {page}", footer]) + "\n"
    return title, text, _html(title, paragraphs, f"Renew for {price}", page, footer)


def booking_email(
    settings: Settings, booking: AdBooking, results: dict | None
) -> tuple[str, str, str]:
    """(subject, text, html) of the email that says a booked ad ends soon."""
    page = f"{settings.site_url}/advertise/booking/{booking.code}#again"
    ends = _day(ads.last_day(booking))
    if booking.kind == ads.SITE:
        title = f"Your sponsor spot ends on {ends}"
        first = f"Your ad for {booking.business} in the sponsor spot on {settings.site_name}"
        first += f" shows for the last time on {ends}."
        unit = "weeks"
    elif slug := ads.section_slug(booking.kind):
        title = f"Your section sponsor line ends on {ends}"
        first = f"Your sponsor line for {booking.business} at the top of"
        first += f" {settings.site_url}{ads.section_path(slug)} shows for the last time on {ends}."
        unit = "weeks"
    else:
        title = f"Your last newsletter sponsor line goes out on {ends}"
        first = f"The last {settings.site_name} newsletter with your sponsor line for"
        first += f" {booking.business} goes out on {ends}."
        unit = "editions"
    paragraphs = [first]
    if results:
        seen = f"{results['views']:,} page views and " if "views" in results else ""
        paragraphs.append(f"So far, it had {seen}{results['clicks']:,} clicks.")
    paragraphs.append(
        f"To run the same ad again, pick the next {unit} on your booking's page and pay with"
        " PayPal. Dates are not held until you book them, so another business can take them"
        " first."
    )
    footer = f"You get this one email because you booked this ad on {settings.site_name}."
    text = "\n\n".join([title, *paragraphs, f"Book it again: {page}", footer]) + "\n"
    return title, text, _html(title, paragraphs, "Book it again", page, footer)


# --------------------------------------------------------- who is told when


def due_listings(session: Session, settings: Settings, now: datetime) -> list[Listing]:
    """Live listings that end within RENEWAL_REMINDER_DAYS, can be renewed,
    and have not been told yet."""
    soon = now + timedelta(days=settings.renewal_reminder_days)
    rows = session.scalars(
        select(Listing).where(
            Listing.status == listings.LIVE,
            Listing.reminded_at.is_(None),
            Listing.email.is_not(None),
            Listing.live_until > now,
            Listing.live_until <= soon,
        )
    ).all()
    return [listing for listing in rows if listings.renewable(settings, listing)]


def due_bookings(session: Session, settings: Settings, now: datetime) -> list[AdBooking]:
    """Approved ads that end within RENEWAL_REMINDER_DAYS, have not been told
    yet, and whose business has booked no later dates of the same kind."""
    on_sale = _ads_on_sale(settings)
    if not on_sale:
        return []
    today = now.astimezone(GUAM_TZ).date()
    rows = session.scalars(
        select(AdBooking)
        .options(defer(AdBooking.picture))
        .where(
            AdBooking.status.in_(HOLDING),
            literal(today - timedelta(days=7)) <= any_(AdBooking.days),
        )
    ).all()
    soon = now + timedelta(days=settings.renewal_reminder_days)
    due = []
    for booking in rows:
        if booking.status != "approved" or booking.reminded_at:
            continue
        if ads.offer_kind(booking.kind) not in on_sale:
            continue
        if not now < booking_end(booking) <= soon:
            continue
        later = any(
            other.kind == booking.kind
            and other.email.lower() == booking.email.lower()
            and max(other.days) > max(booking.days)
            for other in rows
        )
        if not later:
            due.append(booking)
    return due


def _deliver(settings: Settings, model, row_id: int, to: str, email: tuple, now: datetime) -> bool:
    """Send one reminder, once: the row is marked first, so a second worker
    (or the same one again) skips it. When the email fails, the mark is taken
    off, and the next hour tries again."""
    with session_scope() as session:
        claimed = session.execute(
            update(model)
            .where(model.id == row_id, model.reminded_at.is_(None))
            .values(reminded_at=now)
        ).rowcount
    if not claimed:
        return False
    subject, text, html = email
    try:
        mail.send(settings, to, subject, text, html)
    except mail.MailError as exc:
        log.warning("renewal reminder for %s %d failed: %s", model.__tablename__, row_id, exc)
        with session_scope() as session:
            session.execute(
                update(model)
                .where(model.id == row_id, model.reminded_at == now)
                .values(reminded_at=None)
            )
        return False
    return True


def send_reminders(settings: Settings, now: datetime | None = None) -> int:
    """The worker's hourly job: email each buyer whose listing or ad ends
    within RENEWAL_REMINDER_DAYS. Returns the number of emails sent."""
    if not is_on(settings):
        return 0
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(GUAM_TZ).date()
    with session_scope() as session:
        found = [
            (Listing, row.id, row.email, listing_email(settings, row))
            for row in due_listings(session, settings, now)
        ]
        found += [
            (
                AdBooking,
                row.id,
                row.email,
                booking_email(settings, row, ads.results(session, row, today)),
            )
            for row in due_bookings(session, settings, now)
        ]
    sent = sum(
        _deliver(settings, model, row_id, to, email, now) for model, row_id, to, email in found
    )
    if sent:
        log.info("renewal reminders: %d emails sent", sent)
    return sent
