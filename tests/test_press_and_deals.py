"""Paid press releases and deals (listings.py): the forms, the price of a deal
by the week, where they show, the newsletter, and renewing a deal."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree

import pytest
from conftest import make_jpeg
from sqlalchemy import select
from test_listings import (
    ADMIN,
    _end,
    _get,
    _mail_settings,
    _new,
    _pay,
    _post,
    _renew,
    _review,
    _set,
)
from test_listings import client as listings_client  # noqa: F401 (a fixture)
from test_listings import fake_paypal as listings_paypal  # noqa: F401 (a fixture)

from guheadlines import listings, mail, renewals
from guheadlines.db import session_scope
from guheadlines.models import Article, Listing, NewsletterEdition, Source
from guheadlines.scraper.text import GUAM_TZ

PRESS = {
    "title": "Island Bakery opens its second shop in Dededo",
    "name": "Island Bakery",
    "description": "Island Bakery opens its second shop on Saturday.\n\n"
    "The new shop is on Route 3, next to the post office. It sells bread, pastries and"
    " coffee from 6 in the morning.",
    "contact": "Ana Cruz, 671-555-0123",
    "link": "islandbakery.example/news",
}


def _deal(days_ahead: int, **fields) -> dict:
    last = datetime.now(GUAM_TZ).date() + timedelta(days=days_ahead)
    return {
        "title": "20% off all plate lunches",
        "name": "Hafa Grill",
        "category": "Restaurants and food",
        "end_date": last.isoformat(),
        "price": "HAFA20",
        "location": "Tumon",
        "description": "Dine-in only, one per customer.",
        "contact": "671-555-0199",
        "link": "https://hafagrill.example/specials",
        **fields,
    }


# The paid listings tests' fixtures: a fake PayPal, and a site with PayPal and
# STATS_PASSWORD set.
@pytest.fixture()
def fake_paypal(listings_paypal):  # noqa: F811
    return listings_paypal


@pytest.fixture()
def client(listings_client):  # noqa: F811
    return listings_client


@pytest.fixture(autouse=True)
def fresh_caches():
    from guheadlines.web.app import deals_cache, sitemap_cache

    deals_cache._data.clear()
    sitemap_cache._data.clear()
    yield
    deals_cache._data.clear()


def _live(client, fake, kind, fields) -> Listing:
    listing = _new(client, kind, **fields)
    assert _pay(client, fake, listing) == "ok"
    assert _review(client, listing.id, "approve").headers["location"].endswith("done=approved")
    return _get(listing.id)


def _feed_titles(client) -> list[str]:
    channel = ElementTree.fromstring(client.get("/feed.xml").content).find("channel")
    return [item.findtext("title") for item in channel.findall("item")]


def _edition(formed_at: datetime, article_ids: list[int] | None = None) -> None:
    with session_scope() as session:
        session.add(NewsletterEdition(formed_at=formed_at, article_ids=article_ids or []))


# ---------------------------------------------------------- press releases


def test_a_press_release_from_form_to_its_own_page(client, fake_paypal):
    form = client.get("/post/press")
    assert form.status_code == 200 and "Post a press release" in form.text
    assert "$25" in form.text and "noindex" in form.text

    listing = _new(client, "press", make_jpeg(900, 600), **PRESS)
    assert (listing.status, listing.amount_cents) == ("unpaid", 2500)
    assert listing.photo.startswith("listings/")
    page = client.get(f"/listing/{listing.id}/{listing.key}").text
    assert "Pay $25 with PayPal" in page and PRESS["title"] in page
    assert client.get(f"/press/{listing.id}").status_code == 404  # not paid, not approved

    assert _pay(client, fake_paypal, listing) == "ok"
    order = fake_paypal.orders["ORDER-1"]["body"]["purchase_units"][0]
    assert order["amount"]["value"] == "25.00"
    assert order["description"] == f"Press release: {PRESS['title']}"
    assert client.get(f"/press/{listing.id}").status_code == 404  # waits for review
    _review(client, listing.id, "approve")
    live = _get(listing.id)
    assert live.status == "live" and live.live_until is None  # it stays

    shown = client.get("/press")
    assert shown.status_code == 200
    assert PRESS["title"] in shown.text and "Sponsored" in shown.text
    assert f'href="/press/{live.id}"' in shown.text
    assert "Island Bakery" not in client.get("/press?q=typhoon").text
    own = client.get(f"/press/{live.id}")
    assert own.status_code == 200
    assert "<p>Island Bakery opens its second shop on Saturday.</p>" in own.text
    assert "It sells bread, pastries and coffee" in own.text
    assert "Media contact" in own.text and "Ana Cruz, 671-555-0123" in own.text
    assert 'href="https://islandbakery.example/news" target="_blank" rel="sponsored noopener"' in (
        own.text
    )
    assert "did not write it" in own.text
    assert f'<link rel="canonical" href="http://testserver/press/{live.id}">' in own.text
    assert "Your listing is on" in client.get(f"/listing/{live.id}/{live.key}").text
    # Linked from every page, and in the sitemap with its own page.
    home = client.get("/").text
    assert 'href="/press"' in home and 'href="/deals"' in home
    sitemap = client.get("/sitemap.xml").text
    assert "http://testserver/press</loc>" in sitemap
    assert f"http://testserver/press/{live.id}</loc>" in sitemap
    assert "http://testserver/deals</loc>" in sitemap
    # A press release cannot be renewed: it does not end.
    assert not listings.renewable(_site(), live)

    # Taken down: its page is gone.
    _review(client, live.id, "remove")
    assert client.get(f"/press/{live.id}").status_code == 404
    assert PRESS["title"] not in client.get("/press").text


def _site():
    from guheadlines.web import app as web

    return web.settings


def test_press_release_form_checks(client):
    short = _post(client, "press", title="News", name="", description="Too short.")
    assert short.status_code == 400
    assert "Please give the name of the business or group." in short.text
    assert "Please write the release (at least 50 letters)." in short.text
    long = _post(client, "press", **{**PRESS, "description": "word " * 2000})
    listing_id = int(long.headers["location"].split("/")[2])
    assert len(_get(listing_id).description) == listings.MAX_PRESS_TEXT
    # A headline may be longer than other titles.
    headline = "A" * 150
    response = _post(client, "press", **{**PRESS, "title": headline})
    assert len(_get(int(response.headers["location"].split("/")[2])).title) == 140


def test_a_press_release_goes_into_one_newsletter_edition(client, fake_paypal, monkeypatch):
    _set(monkeypatch, stats_password="review-password")
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        source = Source(slug="kuam", name="KUAM News", homepage="https://www.kuam.com")
        session.add(source)
        session.flush()
        stories = [
            Article(
                source_id=source.id,
                url=f"https://www.kuam.com/story/{n}",
                title=f"Story {n}",
                intro=f"What happened in story {n}.",
                categories=["local"],
                published_at=now,
                first_seen_at=now,
            )
            for n in range(1, 6)
        ]
        session.add_all(stories)
        session.flush()
        story_ids = [story.id for story in stories]
    _edition(now - timedelta(days=3), story_ids)
    live = _live(client, fake_paypal, "press", PRESS)
    # Not in the edition made before it was approved.
    assert "Press release: " + PRESS["title"] not in _feed_titles(client)

    # The next edition has it, after the third story, marked as paid.
    _edition(now + timedelta(minutes=1), story_ids)
    response = client.get("/feed.xml")
    channel = ElementTree.fromstring(response.content).find("channel")
    items = channel.findall("item")
    titles = [item.findtext("title") for item in items]
    assert titles == [
        "Story 1",
        "Story 2",
        "Story 3",
        f"Press release: {PRESS['title']}",
        "Story 4",
        "Story 5",
    ]
    press = items[3]
    assert press.findtext("link") == f"http://testserver/press/{live.id}"
    assert press.findtext("description").startswith("Sponsored, from Island Bakery: ")
    content = press.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
    assert "Sponsored press release from Island Bakery" in content

    # And the edition after that does not.
    _edition(now + timedelta(days=3), story_ids)
    assert not any(title.startswith("Press release") for title in _feed_titles(client))


def test_a_press_release_taken_down_leaves_the_newsletter(client, fake_paypal):
    now = datetime.now(timezone.utc)
    _edition(now - timedelta(days=3))
    live = _live(client, fake_paypal, "press", PRESS)
    _edition(now + timedelta(minutes=1))
    assert _feed_titles(client) == [f"Press release: {PRESS['title']}"]
    _review(client, live.id, "remove")
    assert _feed_titles(client) == []


# ------------------------------------------------------------------ deals


def test_deal_weeks():
    today = date(2026, 11, 27)
    assert listings.deal_weeks(today, today) == 1
    assert listings.deal_weeks(today + timedelta(days=6), today) == 1
    assert listings.deal_weeks(today + timedelta(days=7), today) == 1
    assert listings.deal_weeks(today + timedelta(days=8), today) == 2
    assert listings.deal_weeks(today + timedelta(weeks=8), today) == 8


def test_a_deal_is_priced_by_the_week(client):
    one = _new(client, "deal", **_deal(7))
    assert one.amount_cents == 1000
    two = _new(client, "deal", **_deal(8))
    assert two.amount_cents == 2000
    page = client.get(f"/listing/{two.id}/{two.key}").text
    assert "Pay $20 with PayPal" in page and "(2 weeks, until" in page
    last = (two.ends_at.astimezone(GUAM_TZ) - timedelta(days=1)).date()
    assert last == datetime.now(GUAM_TZ).date() + timedelta(days=8)
    assert listings.deal_last_day(two) == last

    too_late = _post(client, "deal", **_deal(7 * listings.MAX_DEAL_WEEKS + 1))
    assert too_late.status_code == 400
    assert "Please pick a day from today to 8 weeks from now." in too_late.text
    past = _post(client, "deal", **_deal(-1))
    assert past.status_code == 400
    missing = _post(client, "deal", title="Sale", name="", category="Boats", description="x")
    assert "Please give the business&#39;s name." in missing.text
    assert "Please pick a category." in missing.text
    assert "Please write a few words about it" in missing.text


def test_a_deal_shows_on_the_deals_page_and_the_home_page(client, fake_paypal):
    form = client.get("/post/deal")
    assert form.status_code == 200 and "$10" in form.text and "Coupon code" in form.text
    live = _live(client, fake_paypal, "deal", _deal(10))
    assert live.live_until == live.ends_at
    assert live.amount_cents == 2000

    shown = client.get("/deals")
    assert shown.status_code == 200
    assert "20% off all plate lunches" in shown.text and "HAFA20" in shown.text
    assert 'rel="sponsored noopener"' in shown.text and f'id="deal-{live.id}"' in shown.text
    assert "20% off" not in client.get("/deals?category=Shopping").text
    assert "20% off" in client.get("/deals?q=lunch").text
    home = client.get("/").text
    assert 'id="deals-title"' in home and f'href="/deals#deal-{live.id}"' in home
    assert "code HAFA20" in home
    assert 'id="deals-title"' not in client.get("/?page=2").text

    # After its last day, it is off both pages.
    _end(live.id, datetime.now(timezone.utc) - timedelta(minutes=1))
    from guheadlines.web.app import deals_cache

    deals_cache._data.clear()
    assert "20% off" not in client.get("/deals").text
    assert 'id="deals-title"' not in client.get("/").text


def test_a_deal_is_renewed_by_the_week(client, fake_paypal, monkeypatch):
    live = _live(client, fake_paypal, "deal", _deal(3))
    page = f"/listing/{live.id}/{live.key}"
    text = client.get(page).text
    assert "Renew for $10 with PayPal" in text and "7 days more" in text
    assert _renew(client, fake_paypal, live) == "ok"
    renewed = _get(live.id)
    assert renewed.live_until - live.live_until == timedelta(days=7)
    assert renewed.ends_at == renewed.live_until
    assert listings.deal_last_day(renewed) == listings.deal_last_day(live) + timedelta(days=7)

    # The email before it ends says "deal".
    sent = []
    monkeypatch.setattr(mail, "send", lambda settings, to, *email, **kw: sent.append((to, *email)))
    assert renewals.send_reminders(_mail_settings(), renewed.live_until - timedelta(days=1)) == 1
    assert sent[0][1].startswith("Your deal ends on ")
    assert "renew it for $10" in sent[0][2]


def test_admin_page_shows_the_weeks_of_a_deal(client, fake_paypal):
    listing = _new(client, "deal", **_deal(8))
    _pay(client, fake_paypal, listing)
    admin = client.get("/admin/listings", headers=ADMIN).text
    assert "2 weeks, last day" in admin and "HAFA20" in admin


# ------------------------------------------------------------ turned off


def test_press_releases_and_deals_are_off_when_their_price_is_0(client, monkeypatch):
    from guheadlines.web.app import sidebar_cache

    _set(monkeypatch, press_release_price_cents=0, deal_week_price_cents=0)
    sidebar_cache._data.clear()
    for path in ("/press", "/deals", "/post/press", "/post/deal"):
        assert client.get(path).status_code == 404, path
    home = client.get("/").text
    assert 'href="/press"' not in home and 'href="/deals"' not in home
    assert 'href="/classifieds"' in home  # the other kinds are still on


def test_price_settings(monkeypatch):
    from guheadlines.settings import load_settings

    monkeypatch.setenv("PRESS_RELEASE_PRICE", "40")
    monkeypatch.setenv("DEAL_WEEK_PRICE", "0")
    site = dataclasses.replace(
        load_settings(), paypal_client_id="id", paypal_client_secret="s", stats_password="p"
    )
    assert site.press_release_price_cents == 4000 and site.deal_week_price_cents == 0
    assert listings.is_on(site, "press") and not listings.is_on(site, "deal")
    monkeypatch.delenv("PRESS_RELEASE_PRICE")
    monkeypatch.delenv("DEAL_WEEK_PRICE")
    defaults = load_settings()
    assert (defaults.press_release_price_cents, defaults.deal_week_price_cents) == (2500, 1000)


def test_muted_words_are_refused_in_a_deal(client, monkeypatch):
    from guheadlines.web import app as web

    config = web.config_cache.get()
    monkeypatch.setattr(
        web.config_cache, "get", lambda: dataclasses.replace(config, muted=("forbidden club",))
    )
    response = _post(client, "deal", **_deal(5, description="Members of the Forbidden Club."))
    assert response.status_code == 400
    assert "words that the site does not accept" in response.text
    with session_scope() as session:
        assert session.scalar(select(Listing)) is None  # nothing was saved
