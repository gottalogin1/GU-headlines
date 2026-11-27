"""The money summary and the weekly report in Discord (reports.py)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from guheadlines import paypal, reports
from guheadlines.alerts import check_sources
from guheadlines.db import session_scope
from guheadlines.models import (
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
from guheadlines.scraper.text import GUAM_TZ
from guheadlines.settings import get_settings, load_settings

WEBHOOK = "https://discord.com/api/webhooks/1234567890/abc-DEF_123"
# Thu Nov 26, 2026, 8:00 AM in Guam: the morning summary of Wed Nov 25 is due.
MORNING = datetime(2026, 11, 26, 8, 0, tzinfo=GUAM_TZ)
YESTERDAY = datetime(2026, 11, 25, 12, 0, tzinfo=GUAM_TZ)
# Mon Nov 30, 2026, 8:00 AM in Guam: the report of Nov 23 to 29 is due.
MONDAY = datetime(2026, 11, 30, 8, 0, tzinfo=GUAM_TZ)


@pytest.fixture(autouse=True)
def no_saved_tokens():
    paypal._tokens.clear()
    yield
    paypal._tokens.clear()


def _settings(**changes):
    base = replace(
        get_settings(),
        discord_webhook_url=WEBHOOK,
        paypal_client_id="client-id",
        paypal_client_secret="secret",
        site_url="https://guamheadlines.com",
        stats_password="pw",
    )
    return replace(base, **changes)


class Fake:
    """Discord and PayPal, as far as the reports use them."""

    def __init__(self, search=None, search_status=200, subscription=None, discord_status=204):
        self.search = search or []
        self.search_status = search_status
        self.subscription = subscription or {}
        self.discord_status = discord_status
        self.discord: list[dict] = []
        self.searched: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "discord.com":
            self.discord.append(json.loads(request.content))
            return httpx.Response(self.discord_status)
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 32400})
        if path == "/v1/reporting/transactions":
            self.searched.append({k: v[0] for k, v in parse_qs(request.url.query.decode()).items()})
            if self.search_status != 200:
                return httpx.Response(
                    self.search_status,
                    json={"name": "NOT_AUTHORIZED", "message": "Insufficient permissions"},
                )
            return httpx.Response(
                200, json={"transaction_details": self.search, "total_pages": 1, "page": 1}
            )
        if path.startswith("/v1/billing/subscriptions/") and path.endswith("/transactions"):
            sub_id = path.split("/")[4]
            return httpx.Response(200, json={"transactions": self.subscription.get(sub_id, [])})
        return httpx.Response(404, json={"name": "NOT_FOUND"})

    def texts(self) -> list[str]:
        return [m["content"] for m in self.discord]


def _found(transaction_id, value, code="T0000", status="S", custom=""):
    info = {
        "transaction_id": transaction_id,
        "transaction_event_code": code,
        "transaction_status": status,
        "transaction_amount": {"currency_code": "USD", "value": value},
    }
    if custom:
        info["custom_field"] = custom
    return {"transaction_info": info}


def _sub_payment(payment_id, value, status="COMPLETED"):
    return {
        "id": payment_id,
        "status": status,
        "amount_with_breakdown": {"gross_amount": {"currency_code": "USD", "value": value}},
        "time": "2026-11-25T02:00:00Z",
    }


def _listing(session, kind, cents, status="live", paid_at=YESTERDAY, capture=None, refunded=None):
    listing = Listing(
        kind=kind,
        status=status,
        key=f"k{kind}{cents}{status}{capture}",
        title=f"A {kind}",
        amount_cents=cents,
        paid_at=paid_at,
        capture_id=capture,
        refunded_at=refunded,
    )
    session.add(listing)
    session.flush()
    return listing


def _booking(session, kind, cents, status="approved", paid_at=YESTERDAY, capture=None):
    booking = AdBooking(
        code=f"code-{kind}-{cents}-{status}-{capture}",
        kind=kind,
        status=status,
        business="Shop",
        email="shop@example.com",
        link="https://shop.example.com",
        words="Shop",
        days=[date(2026, 11, 30)],
        amount_cents=cents,
        paid_at=paid_at,
        paypal_capture_id=capture,
    )
    session.add(booking)
    session.flush()
    return booking


def _subscriber(session, paypal_id, activated_at=None, status="active"):
    sub = BidAlertSubscriber(
        email="buyer@example.com",
        trades=["construction"],
        keywords=[],
        token=f"t-{paypal_id}",
        status=status,
        paypal_id=paypal_id,
        activated_at=activated_at,
    )
    session.add(sub)
    session.flush()


def _sales(session):
    """Yesterday: two classified ads, a featured job, a sponsor week, a new bid
    alert subscriber; a refunded classified ad; and a sale the day before."""
    _listing(session, "classified", 500, capture="CAP-A")
    _listing(session, "classified", 500, capture="CAP-B")
    _listing(session, "job", 2500, capture="CAP-C")
    _listing(session, "classified", 500, status="rejected", capture="CAP-D", refunded=YESTERDAY)
    _listing(session, "event", 1500, paid_at=YESTERDAY - timedelta(days=1), capture="CAP-E")
    _booking(session, "site", 3000, capture="CAP-F")
    _subscriber(session, "I-NEW", activated_at=YESTERDAY)
    _subscriber(session, "I-OLD", activated_at=YESTERDAY - timedelta(days=40))


def test_report_settings(monkeypatch):
    for name in ("REPORT_TIME", "DAILY_REPORT", "WEEKLY_REPORT_DAY"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.report_time == time(8, 0)
    assert settings.daily_report is True
    assert settings.weekly_report_day == 0  # Monday
    monkeypatch.setenv("REPORT_TIME", "6:30 am")
    monkeypatch.setenv("DAILY_REPORT", "false")
    monkeypatch.setenv("WEEKLY_REPORT_DAY", "Friday")
    settings = load_settings()
    assert settings.report_time == time(6, 30)
    assert settings.daily_report is False
    assert settings.weekly_report_day == 4
    monkeypatch.setenv("WEEKLY_REPORT_DAY", "off")
    assert load_settings().weekly_report_day is None
    monkeypatch.setenv("WEEKLY_REPORT_DAY", "someday")
    assert load_settings().weekly_report_day == 0


def test_reports_need_discord_and_the_money_summary_needs_paypal():
    assert not reports.is_on(_settings(discord_webhook_url=""))
    no_paypal = _settings(paypal_client_id="", paypal_client_secret="")
    assert reports._jobs(no_paypal) == [(reports.WEEKLY_KEY, (0,))]
    assert reports._jobs(_settings(weekly_report_day=None)) == [
        (reports.DAILY_KEY, reports.EVERY_DAY)
    ]
    assert not reports.is_on(_settings(weekly_report_day=None, daily_report=False))
    assert reports.describe(_settings()) == (
        "a money summary every day and a weekly report on Mondays, at 8:00 AM (Guam time)"
    )


def test_change_words():
    assert reports.change(108, 100) == "up 8%"
    assert reports.change(97, 100) == "down 3%"
    assert reports.change(100, 100) == "the same"
    assert reports.change(5, 0) == ""
    assert reports.money_text(2550) == "$25.50"
    assert reports.money_text(-500) == "-$5"
    assert reports.money_text(120000) == "$1,200"


def test_money_of_a_day(clean_db):
    with session_scope() as session:
        _sales(session)
    fake = Fake(
        search=[
            # Site sales PayPal also lists: counted once, from the site's records.
            _found("CAP-A", "5.00", "T0006", custom="listing-1"),
            _found("CAP-F", "30.00", "T0006"),  # no custom_field: known by its ID
            _found("SUB-1", "15.00", "T0002"),  # a subscription payment
            # Donations through the PayPal.Me link.
            _found("DON-1", "5.00", "T0000"),
            _found("DON-2", "3.50", "T0013"),
            _found("DON-3", "9.00", "T0000", status="P"),  # pending: not yet
            _found("REF-1", "-5.00", "T1107"),  # the refunded classified ad
            _found("FEE-1", "-0.50", "T0106"),  # a fee: neither
            _found("BANK-1", "-100.00", "T0400"),  # money moved to the bank: neither
        ],
        subscription={
            "I-NEW": [_sub_payment("SUB-1", "15.00")],
            "I-OLD": [_sub_payment("SUB-2", "15.00", status="DECLINED")],
        },
    )
    start = datetime(2026, 11, 25, tzinfo=GUAM_TZ)
    found = reports.money(_settings(), start, start + timedelta(days=1), fake.transport())
    assert found.text() == [
        "• Classified ads: 3, $15",
        "• Featured job posts: 1, $25",
        "• Sponsor spot: 1, $30",
        "• Bid alerts: 1, $15 (1 new subscriber)",
        "• Donations and other PayPal payments: 2, $8.50",
        "• Given back: 1, -$5",
    ]
    # The refunded ad came in too, and went back.
    assert found.count == 8
    assert found.total_cents == 1500 + 2500 + 3000 + 1500 + 850 - 500
    assert found.notes == []
    # Guam midnight to midnight, in UTC.
    assert fake.searched[0]["start_date"] == "2026-11-24T14:00:00Z"
    assert fake.searched[0]["end_date"] == "2026-11-25T14:00:00Z"


def test_renewals_count_with_their_kind(clean_db):
    with session_scope() as session:
        event = _listing(session, "event", 1500, paid_at=YESTERDAY - timedelta(days=9))
        job = _listing(session, "job", 2500, capture="CAP-C")
        for order, listing, paid_at, capture in (
            ("O-1", event, YESTERDAY, "CAP-R1"),
            ("O-2", job, YESTERDAY, "CAP-R2"),
            ("O-3", job, None, None),  # not paid
            ("O-4", event, YESTERDAY - timedelta(days=1), "CAP-R4"),  # the day before
        ):
            session.add(
                ListingRenewal(
                    listing_id=listing.id,
                    order_id=order,
                    capture_id=capture,
                    amount_cents=listing.amount_cents,
                    days=30,
                    paid_at=paid_at,
                )
            )
    # PayPal lists the renewals too, with no custom_id: known by their IDs.
    fake = Fake(search=[_found("CAP-R1", "15.00", "T0006"), _found("CAP-R2", "25.00", "T0006")])
    start = datetime(2026, 11, 25, tzinfo=GUAM_TZ)
    found = reports.money(_settings(), start, start + timedelta(days=1), fake.transport())
    assert found.text() == ["• Featured job posts: 2, $50", "• Featured events: 1, $15"]
    assert found.total_cents == 6500


def test_money_without_transaction_search(clean_db):
    with session_scope() as session:
        _sales(session)
    fake = Fake(search_status=403)
    start = datetime(2026, 11, 25, tzinfo=GUAM_TZ)
    end = start + timedelta(days=1)
    found = reports.money(_settings(), start, end, fake.transport())
    # The refund comes from the site's own records then.
    assert found.refunds.count == 1 and found.refunds.cents == 500
    assert found.notes == []  # no donate link: nothing to point out
    found = reports.money(
        _settings(paypal_donate_link="https://paypal.me/gu"), start, end, fake.transport()
    )
    assert found.notes == [reports.NO_SEARCH]


def test_daily_summary_text(clean_db):
    with session_scope() as session:
        _sales(session)
        _listing(session, "classified", 500, status="review", paid_at=MORNING, capture="CAP-W")
        _booking(session, "newsletter", 1000, status="paid", paid_at=MORNING, capture="CAP-X")
    fake = Fake(search_status=403, subscription={"I-NEW": [_sub_payment("SUB-1", "15.00")]})
    alert = reports.daily_alert(_settings(), MORNING, fake.transport())
    assert alert.title == "Money for Wed Nov 25: $80"
    assert alert.message.split("\n\n") == [
        "6 payments:\n"
        "• Classified ads: 3, $15\n"
        "• Featured job posts: 1, $25\n"
        "• Sponsor spot: 1, $30\n"
        "• Bid alerts: 1, $15 (1 new subscriber)\n"
        "• Given back: 1, -$5",
        "**Waiting for you**\n"
        "• 1 listing to approve: https://guamheadlines.com/admin/listings\n"
        "• 1 ad to approve: https://guamheadlines.com/advertise/review",
    ]
    assert not alert.quiet  # something waits: with a phone notification


def test_daily_summary_of_a_quiet_day(clean_db):
    alert = reports.daily_alert(_settings(), MORNING, Fake(search_status=403).transport())
    assert alert.title == "Money for Wed Nov 25: no payments"
    assert alert.message == "Nothing waits for you."
    assert alert.quiet


def test_send_due_posts_each_report_once(clean_db):
    settings = _settings()
    fake = Fake(search_status=403)
    # 7:59 AM: not yet; the summary of the day before was due 24 hours ago,
    # too late to send now.
    assert reports.send_due(settings, MORNING - timedelta(minutes=1), fake.transport()) == []
    assert reports.send_due(settings, MORNING, fake.transport()) == [reports.DAILY_KEY]
    assert reports.send_due(settings, MORNING + timedelta(hours=1), fake.transport()) == []
    assert len(fake.discord) == 1
    assert fake.texts()[0].startswith("💰 **Money for Wed Nov 25")
    # Other alert checks keep the report's row.
    check_sources(settings, [], [], now=MORNING, transport=fake.transport())
    with session_scope() as session:
        assert session.get(AlertState, reports.DAILY_KEY) is not None
    assert reports.send_due(settings, MORNING + timedelta(hours=2), fake.transport()) == []


def test_send_due_tries_again_and_gives_up_after_12_hours(clean_db):
    settings = _settings()
    down = Fake(search_status=403, discord_status=500)
    assert reports.send_due(settings, MORNING, down.transport()) == []
    up = Fake(search_status=403)
    late = MORNING + timedelta(hours=3)
    assert reports.send_due(settings, late, up.transport()) == [reports.DAILY_KEY]
    with session_scope() as session:
        state = session.get(AlertState, reports.DAILY_KEY)
        assert state.failing_since == MORNING
        assert state.notified_at == late
    # The worker was off all day: the summary of Fri Nov 27 is not sent at 9 PM.
    evening = MORNING + timedelta(days=1, hours=13)
    assert reports.send_due(settings, evening, up.transport()) == []


def test_monday_brings_both_reports(clean_db):
    fake = Fake(search_status=403)
    assert reports.send_due(_settings(), MONDAY, fake.transport()) == [
        reports.DAILY_KEY,
        reports.WEEKLY_KEY,
    ]
    assert fake.texts()[1].startswith("📊 **Weekly report: Mon Nov 23 to Sun Nov 29**")


def _readers(session):
    source = Source(slug="kuam", name="KUAM")
    session.add(source)
    session.flush()
    story = Article(
        source_id=source.id,
        url="https://kuam.com/story",
        title="Typhoon *warning* lifted",
        published_at=MONDAY - timedelta(days=3),
    )
    session.add(story)
    session.flush()
    this_week = date(2026, 11, 25)
    last_week = date(2026, 11, 18)
    session.add_all(
        [
            PageViews(day=this_week, path="/", views=900),
            PageViews(day=this_week + timedelta(days=1), path="/", views=300),
            PageViews(day=this_week, path="/weather", views=100),
            PageViews(day=last_week, path="/", views=1000),
            PageViews(day=this_week, path=SPONSOR_CLICKS, views=7),
            PageViews(day=this_week, path=f"{AD_CLICKS}3", views=4),
            PageViews(day=this_week, path=NEWSLETTER_CLICKS, views=12),
            PageViews(day=this_week, path=NEWSLETTER_SIGNUPS, views=5),
            PageViews(day=last_week, path=NEWSLETTER_SIGNUPS, views=4),
            # Today (Monday) belongs to next week's report.
            PageViews(day=date(2026, 11, 30), path="/", views=5000),
            StoryClicks(day=this_week, article_id=story.id, clicks=40),
            Referrers(day=this_week, site="google.com", visits=60),
            Referrers(day=this_week, site="facebook.com", visits=30),
            Referrers(day=this_week, site="", visits=500),
        ]
    )


def test_weekly_report_text(clean_db):
    with session_scope() as session:
        _readers(session)
        _listing(session, "job", 2500, paid_at=MONDAY - timedelta(days=2), capture="CAP-1")
        _listing(session, "job", 2500, paid_at=MONDAY - timedelta(days=9), capture="CAP-2")
        _listing(session, "job", 2500, paid_at=MONDAY - timedelta(days=10), capture="CAP-3")
    alert = reports.weekly_alert(_settings(), MONDAY, Fake(search_status=403).transport())
    assert alert.title == "Weekly report: Mon Nov 23 to Sun Nov 29"
    assert alert.quiet
    assert alert.message.split("\n\n") == [
        "**Readers**\n"
        "• Page views: 1,300 (up 30% on the week before)\n"
        "• Story clicks: 40\n"
        "• Busiest day: Wed Nov 25, 1,000 page views\n"
        "• Most clicked story: Typhoon \\*warning\\* lifted (KUAM), 40 clicks\n"
        "• Readers came from: google.com 60, facebook.com 30",
        "**Ads and sign-ups**\n"
        "• Sponsor spot clicks: 7\n"
        "• Ad clicks (booked ads): 4\n"
        "• Newsletter story clicks: 12\n"
        "• Newsletter sign-ups on the site: 5 (up 25% on the week before)",
        "**Money: $25** ($50 the week before)\n"
        "• Featured job posts: 1, $25\n"
        "AdSense and affiliate earnings are on their own sites.",
    ]


def test_weekly_report_without_visitor_stats(clean_db):
    alert = reports.weekly_alert(
        _settings(stats_password="", paypal_client_id=""), MONDAY, Fake().transport()
    )
    readers, money = alert.message.split("\n\n")
    assert "Visitor counts are off" in readers and "STATS_PASSWORD" in readers
    assert money.startswith("**Money: $0**\n")


def test_newsletter_signups_are_counted_but_not_as_a_page(clean_db):
    from guheadlines.web import queries
    from guheadlines.web.stats import Counts

    counts = Counts()
    counts.signup()
    counts.flush()
    with session_scope() as session:
        rows = session.execute(select(PageViews.path, PageViews.views)).all()
        assert rows == [(NEWSLETTER_SIGNUPS, 1)]
        stats = queries.visitor_stats(session, date(2020, 1, 1))
    assert stats["pages"] == []


def test_paypal_transactions_reads_every_page():
    pages = {
        "1": {"transaction_details": [_found("A", "1.00")], "total_pages": 2},
        "2": {"transaction_details": [_found("B", "2.00", status="V")], "total_pages": 2},
    }

    def handle(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        page = parse_qs(request.url.query.decode())["page"][0]
        return httpx.Response(200, json=pages[page])

    start = datetime(2026, 11, 25, tzinfo=timezone.utc)
    found = paypal.transactions(
        _settings(), start, start + timedelta(days=1), httpx.MockTransport(handle)
    )
    assert [(p.id, p.done, p.amount_cents) for p in found] == [("A", True, 100), ("B", False, 200)]


def test_section_sponsors_are_one_line_in_the_money_summary(clean_db):
    # Each section has its own kind ("section:weather"); the summary adds them up.
    with session_scope() as session:
        _booking(session, "site", 3000, capture="CAP-F")
        _booking(session, "section:weather", 1500, capture="CAP-W")
        _booking(session, "section:sports", 1500, capture="CAP-S")
    start = datetime(2026, 11, 25, tzinfo=GUAM_TZ)
    found = reports.money(_settings(), start, start + timedelta(days=1), Fake().transport())
    assert found.text()[:2] == ["• Sponsor spot: 1, $30", "• Section sponsors: 2, $30"]
