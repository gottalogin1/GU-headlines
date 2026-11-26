"""SQLAlchemy models. The schema itself is owned by Alembic migrations."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Text search configuration created in the initial migration: English stemming
# plus unaccent, so "Hagatna" matches "Hagåtña" and "Guahan" matches "Guåhan".
SEARCH_CONFIG = "guam_english"

SEARCH_VECTOR_SQL = (
    f"setweight(to_tsvector('{SEARCH_CONFIG}'::regconfig, coalesce(title, '')), 'A') || "
    f"setweight(to_tsvector('{SEARCH_CONFIG}'::regconfig, coalesce(intro, '')), 'B') || "
    f"setweight(to_tsvector('{SEARCH_CONFIG}'::regconfig, "
    f"coalesce(keywords, '') || ' ' || coalesce(section, '') || ' ' || coalesce(author, '')), 'C')"
)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    articles: Mapped[list[Article]] = relationship(back_populates="source")


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    # Canonical, normalized URL; the unique key that prevents re-fetching.
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # The URL as it was discovered (feed/listing), when different from the canonical one.
    discovered_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    intro: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    image_url: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(Text)
    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Only used inside search queries; not loaded with each story.
    search_vector = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True), deferred=True
    )

    source: Mapped[Source] = relationship(back_populates="articles")


class SeenUrl(Base):
    """URLs that were fetched but not stored (failed or rejected), so they are
    not fetched again every hour. Failed URLs are retried with backoff."""

    __tablename__ = "seen_urls"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text, nullable=False)  # 'failed' | 'rejected'
    reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IgnoredImage(Base):
    """Images a source reuses on many stories (logos, placeholders): never shown."""

    __tablename__ = "ignored_images"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HttpCache(Base):
    """Validators for conditional GETs of feeds and listing pages."""

    __tablename__ = "http_cache"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ScrapeRun.error / Source.last_error messages that start with this report a
# normal pause (the site asked us to slow down), not a problem.
CATCH_UP_NOTE = "catching up:"


class ScrapeRun(Base):
    """One row per source per scheduled run; powers the /status page."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_articles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class JobSource(Base):
    """One row per job board in jobs.yaml; powers the source list on /jobs."""

    __tablename__ = "job_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Postings found on the last successful check.
    last_count: Mapped[int | None] = mapped_column(Integer)

    jobs: Mapped[list[Job]] = relationship(back_populates="source")


class Job(Base):
    """A job posting. Fields a posting does not give are left empty."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("job_sources.id", ondelete="CASCADE"), nullable=False
    )
    # The posting's page (or its PDF when that is all there is); the unique key.
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    employer: Mapped[str | None] = mapped_column(Text)
    pay: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    open_date: Mapped[date | None] = mapped_column(Date)
    close_date: Mapped[date | None] = mapped_column(Date)
    # A closing condition that is not a date, e.g. "Open until filled".
    close_note: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # The scrape date: when the posting was last found on its board.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # When the posting's own page was last read (None: only the listing so far).
    detail_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the posting is no longer listed on its board.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[JobSource] = relationship(back_populates="jobs")


class EventCalendar(Base):
    """One row per calendar in events.yaml; powers the calendar list on /events."""

    __tablename__ = "event_calendars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Upcoming events found on the last successful check.
    last_count: Mapped[int | None] = mapped_column(Integer)

    events: Mapped[list[Event]] = relationship(back_populates="calendar")


class Event(Base):
    """An event on a calendar. Fields a calendar does not give are left empty."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    calendar_id: Mapped[int] = mapped_column(
        ForeignKey("event_calendars.id", ondelete="CASCADE"), nullable=False
    )
    # The calendar's own ID for the event (one per date of a repeating event),
    # so an edited event is updated rather than added again.
    uid: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A date without a time (all day, or the time isn't given).
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    location: Mapped[str | None] = mapped_column(Text)
    organizer: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # The event's own page, and its agenda (PDF) when there is one.
    url: Mapped[str | None] = mapped_column(Text)
    agenda_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set when the calendar no longer lists the event (cancelled or removed).
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    calendar: Mapped[EventCalendar] = relationship(back_populates="events")


class BidSource(Base):
    """One row per source in bids.yaml; powers the source list on /bids."""

    __tablename__ = "bid_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Open bids found on the last successful check.
    last_count: Mapped[int | None] = mapped_column(Integer)

    bids: Mapped[list[Bid]] = relationship(back_populates="source")


class Bid(Base):
    """A government bid, request for proposals or other solicitation. Fields a
    source does not give are left empty."""

    __tablename__ = "bids"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("bid_sources.id", ondelete="CASCADE"), nullable=False
    )
    # The source's own ID for the solicitation, so an edited one is updated
    # rather than added again.
    uid: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # The solicitation's number, such as "GSA-031-26" or "RFP-BIB-2027-001".
    number: Mapped[str | None] = mapped_column(Text)
    # What kind of solicitation it is: "Invitation for Bid", "Request for Proposals"...
    kind: Mapped[str | None] = mapped_column(Text)
    # The government agency asking for the bids.
    agency: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # The solicitation's own page, and its main document (PDF) when there is one.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    document_url: Mapped[str | None] = mapped_column(Text)
    posted_on: Mapped[date | None] = mapped_column(Date)
    # When bids or proposals are due. due_time is False when only the day is known.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_time: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set when the source no longer lists the solicitation.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[BidSource] = relationship(back_populates="bids")


class AlertState(Base):
    """A news site, job board or event calendar whose latest check failed, or
    a news site that went quiet (see alerts.py). The row goes away when it
    works again; `notified_at` is set once its alert was sent."""

    __tablename__ = "alerts"

    # "news:<slug>", "jobs:<slug>" or "events:<slug>" (failing), or
    # "quiet:<slug>" (a news site that went quiet)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    # When a failed check was first seen (used when it never worked at all),
    # or since when the site has been quiet.
    failing_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KeywordAlert(Base):
    """A story that was in a keyword alert (ALERT_KEYWORDS, see alerts.py), so
    it is never sent again. Rows older than a day are deleted."""

    __tablename__ = "keyword_alerts"

    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WeatherReport(Base):
    """The last good copy of one weather source (see weather.py): alerts,
    conditions, forecast or storms."""

    __tablename__ = "weather"

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    data = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Clicks on the sponsor spot (web/sponsor.py) are kept in page_views, under
# this name. Page addresses start with "/", so it is never a page.
SPONSOR_CLICKS = "sponsor-click"
# Clicks on the stories in the newsletter emails (links that end in
# ?from=newsletter), kept in page_views under this name.
NEWSLETTER_CLICKS = "newsletter-click"
# Sign-ups in the site's newsletter box (sendfox.py), kept in page_views
# under this name, for the weekly report (reports.py).
NEWSLETTER_SIGNUPS = "newsletter-signup"


class PageViews(Base):
    """How many times readers opened each page on one Guam day (see web/stats.py),
    and the clicks on the sponsor spot (path SPONSOR_CLICKS)."""

    __tablename__ = "page_views"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    # The page's address without its query: every page of a topic counts as one.
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False)


class StoryClicks(Base):
    """How many times readers followed a story's link to its news site on one
    Guam day (see web/stats.py)."""

    __tablename__ = "story_clicks"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    clicks: Mapped[int] = mapped_column(Integer, nullable=False)


class Referrers(Base):
    """How many visits came from each other website on one Guam day (see
    web/stats.py). Only the site's name is kept, such as google.com; "" is a
    visit that came from no site (typed, a bookmark, or an app)."""

    __tablename__ = "referrers"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    site: Mapped[str] = mapped_column(Text, primary_key=True)
    visits: Mapped[int] = mapped_column(Integer, nullable=False)


class NewsletterEdition(Base):
    """One edition of the newsletter feed (see newsletter.py): the stories
    that /feed.xml shows from `formed_at` until the next edition, in order."""

    __tablename__ = "newsletter_editions"

    # The scheduled time of the edition (NEWSLETTER_DAYS at NEWSLETTER_TIME).
    formed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    article_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)


# The event calendar that holds the featured events that organizers pay for
# (see listings.py). It is not in events.yaml, and the events pipeline leaves
# it on.
FEATURED_CALENDAR = "featured"


class Listing(Base):
    """A listing that someone posts on the site and pays for (see listings.py):
    a classified ad, a featured job post, a featured event, or a business in
    the directory (a basic entry is free). Fields a kind does not use are
    left empty."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # "classified", "job", "event" or "business"
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # "unpaid", "review" (paid, or free, and waiting for the owner of the
    # site), "live", "rejected" or "removed" (taken down after it was live)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # The secret part of the poster's own page (/listing/<id>/<key>).
    key: Mapped[str] = mapped_column(Text, nullable=False)
    # What is sold, the job title, the event's name or the business's name.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # The section of a classified ad, or the category of a business.
    category: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # The village or address, or the event's place.
    location: Mapped[str | None] = mapped_column(Text)
    # The asking price of a classified ad, or the pay of a job.
    price: Mapped[str | None] = mapped_column(Text)
    # The employer of a job, or the organizer of an event.
    name: Mapped[str | None] = mapped_column(Text)
    # A phone number, an email address or "how to apply", shown as text.
    contact: Mapped[str | None] = mapped_column(Text)
    # A website, the page to apply on, or the event's own page.
    link: Mapped[str | None] = mapped_column(Text)
    # The photo, under the media folder (listings/<random>.webp).
    photo: Mapped[str | None] = mapped_column(Text)
    photo_width: Mapped[int | None] = mapped_column(Integer)
    photo_height: Mapped[int | None] = mapped_column(Integer)
    # An event's time; all_day when only the date is given.
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # A business that paid for the upgrade (photo, link and top spot).
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The price, in US cents (0: free), and the PayPal order and payment.
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[str | None] = mapped_column(Text, unique=True)
    capture_id: Mapped[str | None] = mapped_column(Text, unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Until when a live listing shows (classified ads and job posts), or until
    # when a business's upgrade lasts. Empty: no end.
    live_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The featured event's copy on the events calendar, once approved.
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="SET NULL")
    )
    # Why it was not accepted, for the poster's own page.
    note: Mapped[str | None] = mapped_column(Text)
    # The email address that the poster paid with on PayPal, and when the
    # email that says the listing ends soon was sent (see renewals.py).
    email: Mapped[str | None] = mapped_column(Text)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ListingRenewal(Base):
    """A payment that keeps a live listing on the site for more days (see
    renewals.py): a classified ad, a job post or a business's upgrade."""

    __tablename__ = "listing_renewals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The PayPal order, and its payment once it is paid.
    order_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    capture_id: Mapped[str | None] = mapped_column(Text, unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # The days that the payment adds.
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Clicks on a booked ad (ads.py) are kept in page_views too, under this name
# and the booking's number, such as "ad-click:12".
AD_CLICKS = "ad-click:"


class AdBooking(Base):
    """An ad that a business booked on the Advertise page (see ads.py): the
    sponsor spot for some weeks, or a sponsor line in some newsletter
    editions. It shows only once it is paid through PayPal and the site's
    owner has approved it."""

    __tablename__ = "ad_bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The secret part of the booking's own page (/advertise/booking/<code>),
    # which only the business that booked it is given.
    code: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # "site" (the sponsor spot) or "newsletter" (a line in the newsletter).
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # new (not paid yet), paid (waiting for the owner), approved, rejected
    # (and refunded) or cancelled (not paid in time, or cancelled by the business).
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    business: Mapped[str] = mapped_column(Text, nullable=False)
    # So the site's owner can reach the business about its ad. Shown only on
    # the owner's review page.
    email: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str] = mapped_column(Text, nullable=False)
    # The words of the picture (for people who cannot see it), or the
    # sponsor line in the newsletter.
    words: Mapped[str] = mapped_column(Text, nullable=False)
    # The sponsor spot's picture: a WebP copy at most 600 pixels wide.
    picture: Mapped[bytes | None] = mapped_column(LargeBinary)
    picture_width: Mapped[int | None] = mapped_column(Integer)
    picture_height: Mapped[int | None] = mapped_column(Integer)
    # The Monday of each week (the sponsor spot), or the day of each edition
    # (the newsletter), in Guam time.
    days: Mapped[list[date]] = mapped_column(ARRAY(Date), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    paypal_order_id: Mapped[str | None] = mapped_column(Text)
    paypal_capture_id: Mapped[str | None] = mapped_column(Text)
    # Why the owner turned the ad down, shown to the business.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the email that says the ad ends soon was sent (see renewals.py).
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdSlot(Base):
    """A week of the sponsor spot, or a newsletter edition, that a booking
    holds. One booking per slot: a slot that is taken cannot be booked again,
    even by two businesses at the same moment."""

    __tablename__ = "ad_slots"

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("ad_bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )


class BidAlertPlan(Base):
    """The PayPal plan (a monthly price) that new bid alert subscriptions use,
    made by bids/alerts.py the first time it is needed. One for each PayPal
    app (client ID) and price, so a new BID_ALERTS_PRICE or a move from the
    sandbox to live makes a new plan; subscribers keep the plan they joined."""

    __tablename__ = "bid_alert_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # PayPal's ID of the plan, such as "P-5ML4271244454362WXNWU5NQ".
    plan_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BidAlertSubscriber(Base):
    """A business that pays by PayPal for an email when a new bid matches its
    trades or words (see bids/alerts.py). There is no login: the secret
    `token` in the links of every email opens the subscriber's own page."""

    __tablename__ = "bid_alert_subscribers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    # Keys of bids/alerts.py TRADES, and the subscriber's own words.
    trades: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # "pending" (not paid yet), "active", "suspended" (PayPal could not take a
    # payment) or "cancelled" (alerts go on until paid_until).
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # PayPal's ID of the subscription ("I-..."), and the PayPal page where the
    # subscriber approves it.
    paypal_id: Mapped[str | None] = mapped_column(Text, unique=True)
    approve_url: Mapped[str | None] = mapped_column(Text)
    paypal_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The next PayPal payment: the paid time runs until then.
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The newest bid (bids.id) already looked at for this subscriber.
    last_bid_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When PayPal was last asked about the subscription.
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
