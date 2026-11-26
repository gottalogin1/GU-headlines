"""Reports in Discord: a money summary every morning, and a report of the
week (REPORT_TIME, DAILY_REPORT and WEEKLY_REPORT_DAY in .env).

The morning summary covers yesterday (a Guam day) and comes only while
PayPal is set up (PAYPAL_CLIENT_ID). It lists:
  - paid listings (listings.py), their renewals (renewals.py) and booked
    ads (ads.py), from the site's own records,
  - bid alert payments (bids/alerts.py), from PayPal's list of each
    subscription's payments,
  - donations ("Buy me a coffee") and any other payment to the PayPal
    account, and money given back, from PayPal's Transaction Search. That
    needs "Transaction search" turned on for the app in the PayPal developer
    dashboard (see .env.example); without it, the summary says so and
    shows the rest,
  - what waits for the owner: listings on /admin/listings, ads on
    /advertise/review.
It comes without a phone notification, unless something waits.

The weekly report covers the 7 days before WEEKLY_REPORT_DAY (Monday to
Sunday by default) and compares each number with the week before: page
views and story clicks (while the visitor stats are on), the busiest day,
the most clicked story, the sites readers came from, clicks on the sponsor
spot and the ads, newsletter sign-ups, new bid alert subscribers, and the
money of the week. AdSense and affiliate earnings are not in it: those are
only on AdSense's and each partner's own site.

The worker posts them (cli._reports_loop). Rows "report:daily" and
"report:weekly" in the alerts table remember when each was last sent, so a
restart of the worker neither skips nor repeats one. A report that can't be
sent is tried again every 10 minutes, for up to 12 hours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import httpx
from sqlalchemy import func, or_, select

from . import ads, listings, paypal
from .alerts import Alert, _plain, send
from .db import session_scope
from .models import (
    AD_CLICKS,
    NEWSLETTER_CLICKS,
    NEWSLETTER_SIGNUPS,
    SPONSOR_CLICKS,
    AdBooking,
    AlertState,
    Article,
    BidAlertSubscriber,
    Listing,
    ListingRenewal,
    PageViews,
    Referrers,
    Source,
    StoryClicks,
)
from .newsletter import edition_time, next_edition_time
from .scraper.text import GUAM_TZ
from .settings import DAY_NAMES, Settings

log = logging.getLogger(__name__)

DAILY_KEY = "report:daily"
WEEKLY_KEY = "report:weekly"
EVERY_DAY = tuple(range(7))
# A report more than this late (the worker was off at the time) is skipped.
LATE = timedelta(hours=12)

LISTING_LABELS = {
    "classified": "Classified ads",
    "job": "Featured job posts",
    "event": "Featured events",
    "business": "Business directory upgrades",
}
AD_LABELS = {ads.SITE: "Sponsor spot", ads.NEWSLETTER: "Newsletter sponsor line"}
# Our own names of the things paid for through the site (custom_id).
SITE_ORDERS = ("listing-", "ad-booking-", "bid-alerts-")
# PayPal's kinds of payment: T00.. money that someone paid to the account
# (T0002: a subscription payment), T11.. money given back.
PAID_IN = "T00"
SUBSCRIPTION = "T0002"
GIVEN_BACK = "T11"
CURRENCY = paypal.CURRENCY

NO_SEARCH = (
    "Donations are not shown: PayPal's Transaction search is not turned on for your "
    'PayPal app. See "How to turn on Transaction search" in .env.example.'
)


def money_text(cents: int) -> str:
    """2500 -> "$25", 2550 -> "$25.50", -500 -> "-$5"."""
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}" + (f".{rest:02d}" if rest else "")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def day_text(day: date) -> str:
    """Wed Nov 25"""
    return f"{day:%a %b} {day.day}"


def _guam_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=GUAM_TZ)


def _page(settings: Settings, path: str) -> str:
    return f"{settings.site_url}{path}" if settings.site_url else path


# ------------------------------------------------------------------ money


@dataclass
class Line:
    label: str
    count: int
    cents: int
    # Such as "1 new subscriber".
    extra: str = ""

    def text(self) -> str:
        if not self.count:  # such as a new subscriber whose payment PayPal didn't list
            return f"• {self.label}: {self.extra}"
        extra = f" ({self.extra})" if self.extra else ""
        return f"• {self.label}: {self.count:,}, {money_text(self.cents)}{extra}"


@dataclass
class Money:
    # Money that came in, one line for each kind of thing paid for.
    lines: list[Line] = field(default_factory=list)
    # Money given back (cents more than 0).
    refunds: Line | None = None
    # What could not be read, for the end of the report.
    notes: list[str] = field(default_factory=list)

    @property
    def total_cents(self) -> int:
        return sum(line.cents for line in self.lines) - (self.refunds.cents if self.refunds else 0)

    @property
    def count(self) -> int:
        return sum(line.count for line in self.lines)

    def text(self) -> list[str]:
        out = [line.text() for line in self.lines if line.count or line.extra]
        if self.refunds and self.refunds.count:
            out.append(
                f"• {self.refunds.label}: {self.refunds.count:,}, {money_text(-self.refunds.cents)}"
            )
        return out


def _site_sales(session, start: datetime, end: datetime) -> tuple[list[Line], set[str]]:
    """Paid listings and booked ads, and their PayPal payment IDs."""
    lines: list[Line] = []
    rows = session.execute(
        select(Listing.kind, func.count(), func.sum(Listing.amount_cents))
        .where(Listing.paid_at >= start, Listing.paid_at < end, Listing.amount_cents > 0)
        .group_by(Listing.kind)
    ).all()
    found = {kind: (n, int(cents)) for kind, n, cents in rows}
    # A renewal (renewals.py) is a payment of its own, counted with its kind.
    rows = session.execute(
        select(Listing.kind, func.count(), func.sum(ListingRenewal.amount_cents))
        .join(Listing, Listing.id == ListingRenewal.listing_id)
        .where(ListingRenewal.paid_at >= start, ListingRenewal.paid_at < end)
        .group_by(Listing.kind)
    ).all()
    for kind, n, cents in rows:
        before = found.get(kind, (0, 0))
        found[kind] = (before[0] + n, before[1] + int(cents))
    for kind, label in LISTING_LABELS.items():
        if kind in found:
            lines.append(Line(label, *found[kind]))
    rows = session.execute(
        select(AdBooking.kind, func.count(), func.sum(AdBooking.amount_cents))
        .where(AdBooking.paid_at >= start, AdBooking.paid_at < end)
        .group_by(AdBooking.kind)
    ).all()
    found = {kind: (n, cents) for kind, n, cents in rows}
    for kind, label in AD_LABELS.items():
        if kind in found:
            lines.append(Line(label, found[kind][0], int(found[kind][1])))
    since = start - timedelta(days=1)
    ids: set[str] = set()
    for column, paid_at in (
        (Listing.capture_id, Listing.paid_at),
        (ListingRenewal.capture_id, ListingRenewal.paid_at),
        (AdBooking.paypal_capture_id, AdBooking.paid_at),
    ):
        ids.update(session.scalars(select(column).where(paid_at >= since, column.is_not(None))))
    return lines, ids


def _listing_refunds(session, start: datetime, end: datetime) -> Line | None:
    n, cents = session.execute(
        select(func.count(), func.coalesce(func.sum(Listing.amount_cents), 0)).where(
            Listing.refunded_at >= start, Listing.refunded_at < end
        )
    ).one()
    return Line("Given back", n, int(cents)) if n else None


def _bid_alerts(
    settings: Settings, session, start: datetime, end: datetime, transport
) -> tuple[Line | None, set[str], str | None]:
    """(the line, the payment IDs, a note when PayPal could not be asked)."""
    sub = BidAlertSubscriber
    new = session.scalar(
        select(func.count())
        .select_from(sub)
        .where(sub.activated_at >= start, sub.activated_at < end)
    )
    paying = session.scalars(
        select(sub.paypal_id).where(
            sub.paypal_id.is_not(None),
            or_(sub.cancelled_at.is_(None), sub.cancelled_at >= start - timedelta(days=1)),
        )
    ).all()
    count = cents = 0
    ids: set[str] = set()
    failed = 0
    for subscription_id in paying:
        try:
            payments = paypal.subscription_payments(
                settings, subscription_id, start, end, transport
            )
        except paypal.PayPalError as exc:
            log.warning("report: could not read bid alert payments of %s: %s", subscription_id, exc)
            failed += 1
            continue
        for payment in payments:
            ids.add(payment.id)
            if payment.done and payment.currency == CURRENCY and payment.amount_cents > 0:
                count += 1
                cents += payment.amount_cents
    note = (
        f"PayPal could not be asked about {_plural(failed, 'bid alert subscription')}; "
        "their payments may be missing."
        if failed
        else None
    )
    if not count and not new:
        return None, ids, note
    extra = _plural(new, "new subscriber") if new else ""
    return Line("Bid alerts", count, cents, extra), ids, note


def money(
    settings: Settings,
    start: datetime,
    end: datetime,
    transport: httpx.BaseTransport | None = None,
) -> Money:
    """The money that came in from `start` to `end`, and what was given back."""
    report = Money()
    with session_scope() as session:
        report.lines, known = _site_sales(session, start, end)
        refunds = _listing_refunds(session, start, end)
        bid_line, bid_ids, note = (
            _bid_alerts(settings, session, start, end, transport)
            if paypal.enabled(settings)
            else (None, set(), None)
        )
    if bid_line:
        report.lines.append(bid_line)
    if note:
        report.notes.append(note)
    known |= bid_ids
    if not paypal.enabled(settings):
        report.refunds = refunds
        return report
    try:
        payments = paypal.transactions(settings, start, end, transport)
    except paypal.PayPalError as exc:
        report.refunds = refunds
        if exc.status == 403:
            # Without a donate link, nothing is missing but refunds of ads.
            if settings.paypal_donate_link:
                report.notes.append(NO_SEARCH)
        else:
            log.warning("report: could not read the PayPal payments: %s", exc)
            report.notes.append(f"Donations could not be read from PayPal ({exc}).")
        return report
    other = given = 0
    other_cents = given_cents = 0
    for p in payments:
        if not p.done or p.currency != CURRENCY or p.id in known:
            continue
        if p.event_code.startswith(PAID_IN) and p.amount_cents > 0:
            if p.event_code == SUBSCRIPTION or p.custom_id.startswith(SITE_ORDERS):
                continue  # counted above
            other += 1
            other_cents += p.amount_cents
        elif p.event_code.startswith(GIVEN_BACK) and p.amount_cents < 0:
            given += 1
            given_cents -= p.amount_cents
    if other:
        report.lines.append(Line("Donations and other PayPal payments", other, other_cents))
    # PayPal's own list has every refund, also of ads and bid alerts.
    report.refunds = Line("Given back", given, given_cents) if given else None
    return report


def waiting(settings: Settings, session) -> list[str]:
    """What waits for the owner's approval, with the page to do it on."""
    lines = []
    n = session.scalar(
        select(func.count()).select_from(Listing).where(Listing.status == listings.REVIEW)
    )
    if n:
        lines.append(f"• {_plural(n, 'listing')} to approve: {_page(settings, '/admin/listings')}")
    n = session.scalar(
        select(func.count()).select_from(AdBooking).where(AdBooking.status == "paid")
    )
    if n:
        lines.append(f"• {_plural(n, 'ad')} to approve: {_page(settings, '/advertise/review')}")
    return lines


def daily_alert(
    settings: Settings, due: datetime, transport: httpx.BaseTransport | None = None
) -> Alert:
    """The money summary for the Guam day before `due`."""
    today = due.astimezone(GUAM_TZ).date()
    yesterday = today - timedelta(days=1)
    report = money(settings, _guam_midnight(yesterday), _guam_midnight(today), transport)
    with session_scope() as session:
        waits = waiting(settings, session)
    if report.count or (report.refunds and report.refunds.count):
        title = f"Money for {day_text(yesterday)}: {money_text(report.total_cents)}"
        parts = [f"{_plural(report.count, 'payment')}:\n" + "\n".join(report.text())]
    else:
        title = f"Money for {day_text(yesterday)}: no payments"
        parts = []
    parts.append("**Waiting for you**\n" + "\n".join(waits) if waits else "Nothing waits for you.")
    parts.extend(report.notes)
    return Alert(title=title, message="\n\n".join(parts), emoji="💰", quiet=not waits)


# ------------------------------------------------------------------ readers


@dataclass
class Readers:
    views: int = 0
    clicks: int = 0
    busiest: tuple[date, int] | None = None
    # (headline, news site, clicks)
    top_story: tuple[str, str, int] | None = None
    # (site, visits), most first; "" (no site) left out.
    came_from: list[tuple[str, int]] = field(default_factory=list)
    sponsor_clicks: int = 0
    ad_clicks: int = 0
    newsletter_clicks: int = 0
    signups: int = 0
    new_bid_subscribers: int = 0


def readers(session, first: date, end: date) -> Readers:
    """The counts of the Guam days from `first` up to (not with) `end`."""
    days = (PageViews.day >= first) & (PageViews.day < end)
    found = Readers()
    by_day = session.execute(
        select(PageViews.day, func.sum(PageViews.views))
        .where(days, PageViews.path.startswith("/"))
        .group_by(PageViews.day)
        .order_by(func.sum(PageViews.views).desc(), PageViews.day)
    ).all()
    found.views = sum(int(n) for _, n in by_day)
    if by_day:
        found.busiest = (by_day[0][0], int(by_day[0][1]))

    def special(*where) -> int:
        return int(
            session.scalar(select(func.coalesce(func.sum(PageViews.views), 0)).where(days, *where))
        )

    found.sponsor_clicks = special(PageViews.path == SPONSOR_CLICKS)
    found.ad_clicks = special(PageViews.path.startswith(AD_CLICKS))
    found.newsletter_clicks = special(PageViews.path == NEWSLETTER_CLICKS)
    found.signups = special(PageViews.path == NEWSLETTER_SIGNUPS)

    clicked = (StoryClicks.day >= first) & (StoryClicks.day < end)
    clicks = func.sum(StoryClicks.clicks)
    found.clicks = int(
        session.scalar(select(func.coalesce(func.sum(StoryClicks.clicks), 0)).where(clicked))
    )
    top = session.execute(
        select(Article.title, Source.name, clicks)
        .join(StoryClicks, StoryClicks.article_id == Article.id)
        .join(Source, Source.id == Article.source_id)
        .where(clicked)
        .group_by(Article.id, Source.name)
        .order_by(clicks.desc(), Article.id.desc())
        .limit(1)
    ).first()
    if top:
        found.top_story = (top[0], top[1], int(top[2]))
    visits = func.sum(Referrers.visits)
    found.came_from = [
        (site, int(n))
        for site, n in session.execute(
            select(Referrers.site, visits)
            .where(Referrers.day >= first, Referrers.day < end, Referrers.site != "")
            .group_by(Referrers.site)
            .order_by(visits.desc(), Referrers.site)
            .limit(3)
        )
    ]
    sub = BidAlertSubscriber
    found.new_bid_subscribers = session.scalar(
        select(func.count())
        .select_from(sub)
        .where(sub.activated_at >= _guam_midnight(first), sub.activated_at < _guam_midnight(end))
    )
    return found


def change(now: int, before: int) -> str:
    """ "up 8%", "down 3%", "the same" (than the week before), or "" when
    there is nothing to compare with."""
    if before <= 0:
        return ""
    percent = round((now - before) * 100 / before)
    if percent > 0:
        return f"up {percent}%"
    if percent < 0:
        return f"down {-percent}%"
    return "the same"


def _counted(label: str, now: int, before: int) -> str:
    moved = change(now, before)
    return f"• {label}: {now:,}" + (f" ({moved} on the week before)" if moved else "")


def weekly_alert(
    settings: Settings, due: datetime, transport: httpx.BaseTransport | None = None
) -> Alert:
    """The report of the 7 Guam days before the day of `due`."""
    end = due.astimezone(GUAM_TZ).date()
    first = end - timedelta(days=7)
    with session_scope() as session:
        week = readers(session, first, end)
        before = readers(session, first - timedelta(days=7), first)
    title = f"Weekly report: {day_text(first)} to {day_text(end - timedelta(days=1))}"

    seen: list[str] = []
    if settings.stats_password:
        seen.append(_counted("Page views", week.views, before.views))
        seen.append(_counted("Story clicks", week.clicks, before.clicks))
        if week.busiest:
            seen.append(
                f"• Busiest day: {day_text(week.busiest[0])}, "
                f"{_plural(week.busiest[1], 'page view')}"
            )
        if week.top_story:
            headline, source, n = week.top_story
            seen.append(f"• Most clicked story: {_plain(headline)} ({source}), {n:,} clicks")
        if week.came_from:
            sites = ", ".join(f"{_plain(site)} {n:,}" for site, n in week.came_from)
            seen.append(f"• Readers came from: {sites}")
    else:
        seen.append(
            "Visitor counts are off. To count page views and clicks, set STATS_PASSWORD "
            "in .env (see .env.example)."
        )

    # Only what happened in one of the two weeks.
    extras = [
        _counted(label, now_n, before_n)
        for label, now_n, before_n in (
            ("Sponsor spot clicks", week.sponsor_clicks, before.sponsor_clicks),
            ("Ad clicks (booked ads)", week.ad_clicks, before.ad_clicks),
            ("Newsletter story clicks", week.newsletter_clicks, before.newsletter_clicks),
            ("Newsletter sign-ups on the site", week.signups, before.signups),
            ("New bid alert subscribers", week.new_bid_subscribers, before.new_bid_subscribers),
        )
        if now_n or before_n
    ]

    start, stop = _guam_midnight(first), _guam_midnight(end)
    this = money(settings, start, stop, transport)
    last = money(settings, start - timedelta(days=7), start, transport)
    total = f"**Money: {money_text(this.total_cents)}**"
    if last.count or last.total_cents:
        total += f" ({money_text(last.total_cents)} the week before)"
    money_part = [total, *this.text(), *dict.fromkeys(this.notes)]
    money_part.append("AdSense and affiliate earnings are on their own sites.")

    parts = ["**Readers**\n" + "\n".join(seen)]
    if extras:
        parts.append("**Ads and sign-ups**\n" + "\n".join(extras))
    parts.append("\n".join(money_part))
    return Alert(title=title, message="\n\n".join(parts), emoji="📊", quiet=True)


# --------------------------------------------------------------- schedule


def _jobs(settings: Settings) -> list[tuple[str, tuple[int, ...]]]:
    """(row key, days of the week) of each report that is on."""
    if not settings.discord_webhook_url:
        return []
    jobs = []
    if settings.daily_report and paypal.enabled(settings):
        jobs.append((DAILY_KEY, EVERY_DAY))
    if settings.weekly_report_day is not None:
        jobs.append((WEEKLY_KEY, (settings.weekly_report_day,)))
    return jobs


def is_on(settings: Settings) -> bool:
    return bool(_jobs(settings))


def describe(settings: Settings) -> str:
    """For the worker's log and alert-test: "a money summary every day and a
    weekly report on Mondays, at 8:00 AM (Guam time)"."""
    parts = []
    for key, days in _jobs(settings):
        if key == DAILY_KEY:
            parts.append("a money summary every day")
        else:
            parts.append(f"a weekly report on {DAY_NAMES[days[0]].capitalize()}s")
    at = settings.report_time
    when = f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"
    return " and ".join(parts) + f", at {when} (Guam time)"


def next_report_time(settings: Settings, now: datetime) -> datetime | None:
    times = [next_edition_time(now, days, settings.report_time) for _, days in _jobs(settings)]
    return min((t for t in times if t), default=None)


def send_due(
    settings: Settings,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """Send each report that is due and was not sent yet. Returns the keys of
    the reports sent. A report that can't be sent is tried on the next call."""
    now = now or datetime.now(timezone.utc)
    sent = []
    for key, days in _jobs(settings):
        due = edition_time(now, days, settings.report_time)
        if due is None or now - due > LATE:
            continue
        with session_scope() as session:
            state = session.get(AlertState, key)
            if state is not None and state.notified_at and state.notified_at >= due:
                continue
        build = daily_alert if key == DAILY_KEY else weekly_alert
        if not send(settings, build(settings, due, transport), transport):
            continue
        with session_scope() as session:
            # failing_since holds the report's own time here.
            state = session.get(AlertState, key)
            if state is None:
                session.add(AlertState(key=key, failing_since=due, notified_at=now))
            else:
                state.failing_since, state.notified_at = due, now
        sent.append(key)
    return sent
