"""Ads that local businesses book and pay for themselves, on the Advertise
page (/advertise). No logins: a business fills in a form, pays through PayPal,
and the site's owner approves the ad before it shows.

Three things are for sale, each only while its price is set in .env:

- The sponsor spot (web/sponsor.py), by the week, Monday to Sunday in Guam
  time (SPONSOR_WEEK_PRICE). In a booked week the business's picture takes
  the place of SPONSOR_IMAGE.
- A section sponsor, by the week (SECTION_SPONSOR_PRICE): "Weather is
  brought to you by" the business, at the top of one section of the site (a
  news topic, or a page such as Weather or Jobs). One business a section a
  week.
- A sponsor line at the top of a newsletter edition (newsletter.py), by the
  edition (NEWSLETTER_SPONSOR_PRICE).

How a booking goes:

1. The business picks its weeks or editions and fills in its name, email
   address, website, and its picture or words. The weeks are held for it for
   HOLD_MINUTES; it can book only those that no one else holds ("new").
2. On its booking's own page (a secret address), it pays through PayPal
   (paypal.py). Paid bookings wait for the owner ("paid"), who gets a Discord
   alert.
3. The owner approves the ad on /advertise/review ("approved") and it shows
   in its weeks, or turns it down ("rejected") and PayPal refunds the payment.

A booking that is not paid in time is "cancelled", and its weeks are free again.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import AD_CLICKS, NEWSLETTER_CLICKS, SPONSOR_CLICKS, AdBooking, AdSlot, PageViews
from .newsletter import next_edition_time
from .scraper.text import GUAM_TZ
from .settings import Settings
from .web.sponsor import make_copy

SITE = "site"
NEWSLETTER = "newsletter"
# The bookings of a section sponsor have the kind "section:<the section's
# slug>", such as "section:weather", so that each section has its own weeks.
SECTION = "section"

# How long the weeks of a booking that is not paid yet are kept for it.
HOLD_MINUTES = 60
# How far ahead each thing can be booked.
WEEKS_AHEAD = 12
EDITIONS_AHEAD = 8
# A newsletter edition can be booked until a day before it is made, so that
# the owner has time to look at the ad.
EDITION_NOTICE = timedelta(hours=24)

# The sponsor's picture: a file that a phone or a computer makes, not too
# small for the column (300 pixels wide) and not a long strip either way.
MAX_PICTURE_BYTES = 5 * 1024 * 1024
MIN_PICTURE_WIDTH = 300
TALLEST = 2.0  # height / width, such as 300 x 600
WIDEST = 0.25  # such as 600 x 150

MAX_BUSINESS = 80
MAX_WORDS = {SITE: 150, NEWSLETTER: 200, SECTION: 100}

# The sections that are not news topics: (slug, name, address). Each news
# topic (/category/<slug>) is a section too.
PAGES = (
    ("jobs", "Jobs", "/jobs"),
    ("events", "Events", "/events"),
    ("bids", "Bids", "/bids"),
    ("weather", "Weather", "/weather"),
    ("outages", "Outages", "/outages"),
)


@dataclass(frozen=True)
class Offer:
    kind: str
    name: str
    price_cents: int
    unit: str  # "week" or "edition"


def offers(settings: Settings) -> list[Offer]:
    """What is for sale: the things whose price is set. The newsletter line
    only while the newsletter feed is on."""
    found = []
    if settings.sponsor_week_price_cents:
        found.append(Offer(SITE, "Sponsor spot", settings.sponsor_week_price_cents, "week"))
    if settings.section_sponsor_price_cents:
        found.append(
            Offer(SECTION, "Section sponsor", settings.section_sponsor_price_cents, "week")
        )
    if settings.newsletter_sponsor_price_cents and settings.newsletter_days:
        found.append(
            Offer(
                NEWSLETTER,
                "Newsletter sponsor",
                settings.newsletter_sponsor_price_cents,
                "edition",
            )
        )
    return found


def offer_kind(kind: str) -> str:
    """The thing that a booking's kind is: "section:weather" -> "section"."""
    return kind.partition(":")[0]


def weekly(kind: str) -> bool:
    """True for the things that are booked by the week: all but the newsletter."""
    return offer_kind(kind) != NEWSLETTER


def section_slug(kind: str) -> str:
    """ "section:weather" -> "weather"; "" for the other kinds."""
    return kind.partition(":")[2] if offer_kind(kind) == SECTION else ""


def section_path(slug: str) -> str:
    """The address of a section's page: "/weather", or "/category/sports"."""
    return next((path for page, _, path in PAGES if page == slug), f"/category/{slug}")


@dataclass(frozen=True)
class Section:
    slug: str
    name: str

    @property
    def kind(self) -> str:
        return f"{SECTION}:{self.slug}"

    @property
    def path(self) -> str:
        return section_path(self.slug)


def sections(settings: Settings, topics) -> list[Section]:
    """The sections that can have a sponsor: each news topic (from
    categories.yaml, with its slug and name), then the other pages, the
    Weather and Outages pages only while they are on."""
    off = set()
    if not settings.weather_interval_minutes:
        off.add("weather")
    if not settings.outages_interval_minutes:
        off.add("outages")
    found = [Section(topic.slug, topic.name) for topic in topics]
    return found + [Section(slug, name) for slug, name, _ in PAGES if slug not in off]


def money(cents: int) -> str:
    """2500 -> "$25", 2550 -> "$25.50", 120000 -> "$1,200"."""
    dollars, rest = divmod(cents, 100)
    return f"${dollars:,}" + (f".{rest:02d}" if rest else "")


# ------------------------------------------------------------ the dates


def week_start(day: date) -> date:
    """The Monday of the week that `day` is in."""
    return day - timedelta(days=day.weekday())


def week_choices(now: datetime) -> list[date]:
    """The Mondays of the weeks that can be booked: from next week on (this
    week has already begun)."""
    first = week_start(now.astimezone(GUAM_TZ).date()) + timedelta(days=7)
    return [first + timedelta(weeks=n) for n in range(WEEKS_AHEAD)]


def edition_choices(now: datetime, days: tuple[int, ...], at: time) -> list[date]:
    """The days of the newsletter editions that can be booked: those made
    more than EDITION_NOTICE from now."""
    found: list[date] = []
    moment = now + EDITION_NOTICE
    while days and len(found) < EDITIONS_AHEAD:
        moment = next_edition_time(moment, days, at)
        found.append(moment.date())
    return found


def choices(settings: Settings, kind: str, now: datetime) -> list[date]:
    if weekly(kind):
        return week_choices(now)
    return edition_choices(now, settings.newsletter_days, settings.newsletter_time)


def run_days(booking: AdBooking) -> list[date]:
    """Every day the ad shows: all seven days of each booked week, or the
    day of each edition."""
    if not weekly(booking.kind):
        return sorted(booking.days)
    return [monday + timedelta(days=n) for monday in sorted(booking.days) for n in range(7)]


def first_day(booking: AdBooking) -> date:
    return min(booking.days)


def last_day(booking: AdBooking) -> date:
    return run_days(booking)[-1]


# ------------------------------------------------------- the booking form


class NotAvailable(Exception):
    """Another booking holds some of the days (`days`)."""

    def __init__(self, days: list[date]) -> None:
        super().__init__(", ".join(d.isoformat() for d in days))
        self.days = days


def picture_copy(data: bytes) -> tuple[bytes, int, int]:
    """The small WebP copy of an uploaded picture (see web/sponsor.py). Raises
    ValueError with words for the business when the picture won't do."""
    if len(data) > MAX_PICTURE_BYTES:
        raise ValueError("The picture is over 5 MB. Please send a smaller file.")
    try:
        webp, width, height = make_copy(data)
    except ValueError:
        raise ValueError(
            "That file isn't a picture the site can read. Please send a JPEG, PNG, WebP or GIF."
        ) from None
    if width < MIN_PICTURE_WIDTH:
        raise ValueError(
            f"The picture is only {width} pixels wide. Please send one at least"
            f" {MIN_PICTURE_WIDTH} pixels wide (600 looks sharp on phones)."
        )
    if height / width > TALLEST:
        raise ValueError(
            "The picture is too tall for the column. It can be at most twice as tall as it is wide."
        )
    if height / width < WIDEST:
        raise ValueError(
            "The picture is too long and thin. It can be at most four times as wide as it is tall."
        )
    return webp, width, height


def refused(texts: list[str], muted: tuple[str, ...] | list[str]) -> bool:
    """True when the ad names something the site never shows (muted: in
    sources.yaml), also with the spaces left out, as in a web address."""
    joined = " ".join(texts).lower()
    squeezed = joined.replace(" ", "")
    for phrase in muted:
        phrase = phrase.lower().strip()
        if phrase and (phrase in joined or phrase.replace(" ", "") in squeezed):
            return True
    return False


def release_expired(session: Session, now: datetime) -> None:
    """Free the days of bookings that were not paid within HOLD_MINUTES."""
    cutoff = now - timedelta(minutes=HOLD_MINUTES)
    ids = session.scalars(
        update(AdBooking)
        .where(AdBooking.status == "new", AdBooking.created_at < cutoff)
        .values(status="cancelled", note="Not paid in time.")
        .returning(AdBooking.id)
    ).all()
    if ids:
        session.execute(delete(AdSlot).where(AdSlot.booking_id.in_(ids)))


def taken(session: Session, kind: str, days: list[date]) -> set[date]:
    """The days among `days` that a booking holds."""
    if not days:
        return set()
    return set(
        session.scalars(select(AdSlot.day).where(AdSlot.kind == kind, AdSlot.day.in_(days))).all()
    )


def _hold(session: Session, booking: AdBooking) -> None:
    """Take the booking's days, or raise NotAvailable naming those another
    booking holds. A unique key on (kind, day) makes this safe when two
    businesses book the same week at the same moment."""
    session.execute(
        insert(AdSlot)
        .values([{"kind": booking.kind, "day": d, "booking_id": booking.id} for d in booking.days])
        .on_conflict_do_nothing()
    )
    mine = set(session.scalars(select(AdSlot.day).where(AdSlot.booking_id == booking.id)).all())
    missing = sorted(set(booking.days) - mine)
    if missing:
        raise NotAvailable(missing)


def book(
    session: Session,
    *,
    kind: str,
    days: list[date],
    business: str,
    email: str,
    link: str,
    words: str,
    price_cents: int,
    picture: tuple[bytes, int, int] | None,
    now: datetime,
) -> AdBooking:
    """Make a new booking and hold its days for it. Raises NotAvailable."""
    release_expired(session, now)
    booking = AdBooking(
        code=secrets.token_urlsafe(18),
        kind=kind,
        status="new",
        business=business,
        email=email,
        link=link,
        words=words,
        days=sorted(set(days)),
        amount_cents=price_cents * len(set(days)),
        created_at=now,
    )
    if picture:
        booking.picture, booking.picture_width, booking.picture_height = picture
    session.add(booking)
    session.flush()
    _hold(session, booking)
    return booking


def by_code(session: Session, code: str, *, lock: bool = False) -> AdBooking | None:
    if not code or len(code) > 40:
        return None
    stmt = select(AdBooking).where(AdBooking.code == code)
    if lock:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def hold_again(session: Session, booking: AdBooking) -> bool:
    """For a booking that was not paid in time but whose business has just
    paid on PayPal: take its days again, if no other booking has taken them."""
    if booking.status == "new":
        return True
    if booking.status != "cancelled":
        return False
    try:
        with session.begin_nested():
            _hold(session, booking)
    except NotAvailable:
        return False
    booking.status, booking.note = "new", None
    return True


def mark_paid(session: Session, booking: AdBooking, capture_id: str | None, now: datetime) -> None:
    booking.status = "paid"
    booking.paypal_capture_id = capture_id
    booking.paid_at = now


def cancel(session: Session, booking: AdBooking, note: str) -> None:
    """Cancel a booking that is not paid, and free its days."""
    booking.status, booking.note = "cancelled", note
    session.execute(delete(AdSlot).where(AdSlot.booking_id == booking.id))


def approve(booking: AdBooking, now: datetime) -> None:
    booking.status, booking.decided_at = "approved", now


def reject(session: Session, booking: AdBooking, note: str, now: datetime) -> None:
    """Turn the ad down (its payment is refunded by the caller) and free its days."""
    booking.status, booking.decided_at = "rejected", now
    booking.note = note or None
    session.execute(delete(AdSlot).where(AdSlot.booking_id == booking.id))


# ---------------------------------------------------------- showing ads


def _showing(session: Session, kind: str, day: date) -> AdBooking | None:
    return session.scalars(
        select(AdBooking)
        .join(AdSlot, AdSlot.booking_id == AdBooking.id)
        .where(AdSlot.kind == kind, AdSlot.day == day, AdBooking.status == "approved")
    ).first()


def site_ad(session: Session, today: date) -> AdBooking | None:
    """The approved booking of the sponsor spot for the week of `today`."""
    return _showing(session, SITE, week_start(today))


def section_ad(session: Session, slug: str, today: date) -> AdBooking | None:
    """The approved section sponsor of the section `slug` for the week of `today`."""
    return _showing(session, f"{SECTION}:{slug}", week_start(today))


def newsletter_ad(session: Session, day: date) -> AdBooking | None:
    """The approved newsletter booking for the edition made on `day`."""
    return _showing(session, NEWSLETTER, day)


def approved(session: Session, booking_id: int) -> AdBooking | None:
    booking = session.get(AdBooking, booking_id)
    return booking if booking is not None and booking.status == "approved" else None


# ------------------------------------------------------------- numbers


def _sum_views(session: Session, *where) -> int:
    return session.scalar(select(func.coalesce(func.sum(PageViews.views), 0)).where(*where)) or 0


def _in_section(path: str):
    """The page views of a section: its page, and the pages under it (such as
    an event's own page under /events)."""
    return or_(PageViews.path == path, PageViews.path.startswith(path + "/", autoescape=True))


def results(session: Session, booking: AdBooking, today: date) -> dict | None:
    """How the ad has done so far: the page views while it showed (the
    sponsor spot is on every page, a section sponsor on its section's pages)
    and the clicks on it. None before it has started."""
    shown = [d for d in run_days(booking) if d <= today]
    if not shown or booking.status != "approved":
        return None
    clicks = _sum_views(session, PageViews.path == f"{AD_CLICKS}{booking.id}")
    found = {"days": len(shown), "clicks": clicks, "done": shown[-1] == run_days(booking)[-1]}
    if booking.kind == SITE:
        found["views"] = _sum_views(
            session, PageViews.path.startswith("/"), PageViews.day.in_(shown)
        )
    elif slug := section_slug(booking.kind):
        found["views"] = _sum_views(
            session, _in_section(section_path(slug)), PageViews.day.in_(shown)
        )
    return found


def audience(session: Session, today: date, days: int = 30) -> dict:
    """The site's readership in the last `days` Guam days, for the Advertise
    page: page views (a day each, for the chart), and the clicks on the
    sponsor spot and on the stories in the newsletter emails."""
    since = today - timedelta(days=days - 1)
    by_day = dict(
        session.execute(
            select(PageViews.day, func.sum(PageViews.views))
            .where(PageViews.day >= since, PageViews.path.startswith("/"))
            .group_by(PageViews.day)
        ).all()
    )
    daily = [
        {"day": day, "views": int(by_day.get(day, 0))}
        for day in (since + timedelta(days=n) for n in range(days))
    ]
    views = sum(d["views"] for d in daily)
    return {
        "days": days,
        "daily": daily,
        "views": views,
        "per_day": round(views / days),
        "per_week": round(views * 7 / days),
        "sponsor_clicks": _sum_views(
            session, PageViews.day >= since, PageViews.path == SPONSOR_CLICKS
        ),
        "newsletter_clicks": _sum_views(
            session, PageViews.day >= since, PageViews.path == NEWSLETTER_CLICKS
        ),
    }


def section_views(
    session: Session, found: list[Section], today: date, days: int = 30
) -> dict[str, int]:
    """The page views of each section (by slug) in the last `days` Guam days."""
    if not found:
        return {}
    since = today - timedelta(days=days - 1)
    rows = session.execute(
        select(PageViews.path, func.sum(PageViews.views))
        .where(PageViews.day >= since, or_(*(_in_section(s.path) for s in found)))
        .group_by(PageViews.path)
    ).all()
    return {
        s.slug: sum(
            int(views) for path, views in rows if path == s.path or path.startswith(s.path + "/")
        )
        for s in found
    }


def for_review(session: Session) -> list[AdBooking]:
    """Every booking of the last year, the ones that need the owner first."""
    order = {"paid": 0, "approved": 1, "new": 2, "rejected": 3, "cancelled": 4}
    rows = session.scalars(
        select(AdBooking).where(AdBooking.created_at >= func.now() - timedelta(days=365))
    ).all()
    return sorted(rows, key=lambda b: (order.get(b.status, 9), min(b.days), b.id))
