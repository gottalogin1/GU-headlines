"""Guide packs (guide_packs.py): paid visitor guides sold together through
PayPal and read with a private link, the owner's pages, and the starter packs."""

from __future__ import annotations

import base64
import dataclasses
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_listings import FakePayPal

from guheadlines import guide_packs, guides, mail, paypal
from guheadlines.db import session_scope
from guheadlines.models import Guide, GuidePack, GuidePass

FROM_SITE = {"origin": "http://testserver"}
PASSWORD = "packs-password"
ADMIN = {"authorization": "Basic " + base64.b64encode(f"me:{PASSWORD}".encode()).decode()}
OWNER = {**ADMIN, **FROM_SITE}

TEXT = """Moving to Guam is easier with a plan.

## Before you leave

Start early.

## When you arrive

Check in.

## Your first months

Explore.
"""


# ------------------------------------------------------------- the pieces


def test_prices_tokens_and_the_opening():
    assert guide_packs.parse_price("12") == 1200
    assert guide_packs.parse_price("$12.5") == 1250
    assert guide_packs.parse_price(" 1,200.99 ") == 120099
    assert guide_packs.parse_price("twelve") is None
    assert guide_packs.money(1200) == "$12" and guide_packs.money(1299) == "$12.99"

    a, b = "a" * 32, "b" * 32
    assert guide_packs.cookie_tokens(f"{a}.bad token.{b}.{a}") == [a, b]
    assert guide_packs.with_token(a, b) == f"{b}.{a}"
    many = ".".join(chr(ord("c") + i) * 32 for i in range(12))
    assert len(guide_packs.cookie_tokens(guide_packs.with_token(many, a))) == guide_packs.MAX_TOKENS
    assert guide_packs.cookie_tokens(guide_packs.with_token(many, a))[0] == a

    text = guides.render(TEXT)
    shown = guides.opening(text)
    assert len(shown.parts) == 1 and "easier with a plan" in str(shown.parts[0])
    assert "Start early" not in str(shown.parts[0]) and len(shown.contents) == 3
    # No opening text, or no main heading: readers without a pack see none of it.
    assert guides.opening(guides.render("## Only\n\nText.\n\n## Two\n\nMore.")).parts == []
    assert guides.opening(guides.render("All of it, no headings.")).parts == []


def test_the_pack_form_is_checked():
    form = {"title": ["PCS move pack"], "summary": ["Six guides."], "price": ["12"],
            "position": ["10"], "status": ["on_sale"], "guide": ["3", "1", "x", "3"]}  # fmt: skip
    values, errors = guide_packs.check(form)
    assert errors == {}
    assert values["slug"] == "pcs-move-pack" and values["price_cents"] == 1200
    assert values["guide_ids"] == [1, 3] and values["status"] == "on_sale"
    _, errors = guide_packs.check({"title": [""], "price": ["0.50"], "slug": ["Bad!"]})
    assert set(errors) == {"title", "slug", "summary", "price", "guides"}
    values, errors = guide_packs.check({**form, "guide": [], "every_paid": ["yes"]})
    assert errors == {} and values["every_paid"]


def test_the_starter_packs_hold_paid_starter_guides():
    starters = {s.slug: s for s in guides.starters()}
    packs = guide_packs.starters()
    assert {p.slug for p in packs} >= {"pcs-move", "first-week", "every-guide"}
    for pack in packs:
        assert pack.price_cents >= guide_packs.MIN_PRICE_CENTS and pack.summary
        assert pack.every_paid or pack.guides
        for slug in pack.guides:
            assert starters[slug].access == guides.PAID, slug
            # Readers without the pack read an opening that says what it is about.
            assert guides.opening(guides.render(starters[slug].body)).parts, slug
        text = (pack.title + pack.summary + pack.about).lower()
        assert "github" not in text and "chamber of commerce" not in text


# ------------------------------------------------------------ the site


def _set(monkeypatch, **values):
    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, **values)
    monkeypatch.setattr(web, "settings", site)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)
    return site


@pytest.fixture()
def fake_paypal(monkeypatch):
    fake = FakePayPal()
    real = paypal._client
    monkeypatch.setattr(
        paypal, "_client", lambda transport: real(httpx.MockTransport(fake.handler))
    )
    paypal._tokens.clear()
    yield fake
    paypal._tokens.clear()


@pytest.fixture()
def sent(monkeypatch):
    """The emails the site sends: (to, subject, text)."""
    found: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        mail,
        "send",
        lambda settings, to, subject, text, html=None, headers=None: found.append(
            (to, subject, text)
        ),
    )
    return found


@pytest.fixture()
def client(clean_db, monkeypatch, fake_paypal):
    from guheadlines.web.app import (
        app,
        guide_buy_limit,
        guide_cache,
        guide_link_limit,
        sidebar_cache,
        sitemap_cache,
    )

    for cache in (guide_cache, sidebar_cache, sitemap_cache):
        cache._data.clear()
    for limit in (guide_buy_limit, guide_link_limit):
        limit._by_address.clear()
        limit._all.clear()
    _set(
        monkeypatch,
        stats_password=PASSWORD,
        paypal_client_id="id",
        paypal_client_secret="secret",
        adsense_client="",
        adsense_slot="",
        discord_webhook_url="",
        smtp_host="",
    )
    yield TestClient(app)
    guide_cache._data.clear()


def _guide(slug: str, access: str = guides.PAID, status: str = guides.PUBLISHED) -> Guide:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        guide = Guide(
            slug=slug,
            title=slug.replace("-", " ").title(),
            summary="About it.",
            body=TEXT,
            status=status,
            access=access,
            created_at=now,
            updated_at=now,
        )
        session.add(guide)
    return guide


def _pack(client, guide_ids, **fields) -> GuidePack:
    data = {
        "title": "PCS move pack",
        "slug": "pcs-move",
        "summary": "Six guides for the move.",
        "price": "12",
        "position": "10",
        "status": "on_sale",
        "guide": [str(i) for i in guide_ids],
        **fields,
    }
    response = client.post(
        "/admin/guides/packs/new", data=data, headers=OWNER, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    found = re.fullmatch(r"/admin/guides/packs/(\d+)\?done=saved", response.headers["location"])
    with session_scope() as session:
        return session.get(GuidePack, int(found[1]))


def _buy(client, fake, slug="pcs-move") -> httpx.Response:
    """Buy a pack: the link to PayPal, the buyer approves, PayPal sends them back."""
    response = client.get(f"/guides/buy/{slug}", follow_redirects=False)
    assert response.status_code == 303
    order_id = re.search(r"token=([\w-]+)", response.headers["location"])[1]
    fake.approve(order_id)
    return client.get(f"/guides/buy/{slug}/paid?token={order_id}", follow_redirects=False)


def _passes() -> list[GuidePass]:
    with session_scope() as session:
        return list(session.scalars(select(GuidePass).order_by(GuidePass.id)))


def test_a_paid_guide_from_opening_to_full_text(client, fake_paypal, sent, monkeypatch):
    _set(
        monkeypatch,
        stats_password=PASSWORD,
        paypal_client_id="id",
        paypal_client_secret="secret",
        smtp_host="mail",
        mail_from="Site <guides@example.com>",
        adsense_client="ca-pub-1234567890123456",
        adsense_slot="9876543210",
        discord_webhook_url="",
    )
    paid = _guide("moving-to-guam")
    _guide("beaches", access=guides.FREE)
    pack = _pack(client, [paid.id])

    # Readers without the pack: the opening, the parts, and the pack.
    page = client.get("/guides/moving-to-guam")
    assert page.status_code == 200
    assert "easier with a plan" in page.text and "Start early" not in page.text
    assert "Read the rest of this guide" in page.text and "<li>When you arrive</li>" in page.text
    assert 'href="/guides/packs/pcs-move"' in page.text and "$12" in page.text
    assert page.headers["cache-control"] == "private, no-cache"
    assert "Cookie" in page.headers["vary"]
    assert '"isAccessibleForFree":false' in page.text
    assert "adsbygoogle" in page.text
    # A free guide is as before.
    free_page = client.get("/guides/beaches")
    assert "Start early" in free_page.text and "Read the rest" not in free_page.text
    assert "isAccessibleForFree" not in free_page.text

    index = client.get("/guides").text
    assert "Guide packs" in index and 'href="/guides/packs/pcs-move"' in index
    pack_page = client.get("/guides/packs/pcs-move")
    assert pack_page.status_code == 200
    assert 'href="/guides/buy/pcs-move" rel="nofollow">Buy with PayPal' in pack_page.text
    assert "Moving To Guam" in pack_page.text and 'id="lost"' in pack_page.text

    # The PayPal order: the pack's price, and our own name for it.
    response = client.get("/guides/buy/pcs-move", follow_redirects=False)
    assert response.headers["location"].startswith("https://www.sandbox.paypal.com/")
    order = fake_paypal.orders["ORDER-1"]["body"]["purchase_units"][0]
    assert order["amount"] == {"currency_code": "USD", "value": "12.00"}
    assert order["custom_id"] == f"guide-pack-{pack.id}-1200"
    assert _passes() == []  # nothing is stored before the payment

    # Back from PayPal: paid, the pass is made, the buyer lands on their page.
    fake_paypal.approve("ORDER-1")
    response = client.get("/guides/buy/pcs-move/paid?token=ORDER-1", follow_redirects=False)
    [guide_pass] = _passes()
    assert response.headers["location"] == f"/guides/pass/{guide_pass.token}?new=1"
    assert guide_pass.status == "paid" and guide_pass.amount_cents == 1200
    assert guide_pass.email == "buyer@example.com" and guide_pass.emailed_at is not None
    assert [s[0] for s in sent] == ["buyer@example.com"]
    assert f"http://testserver/guides/pass/{guide_pass.token}" in sent[0][2]
    assert "Moving To Guam" in sent[0][2]

    client.cookies.clear()
    mine = client.get(response.headers["location"])
    assert mine.status_code == 200 and "Thank you, your payment went through" in mine.text
    assert "no-store" in mine.headers["cache-control"]
    assert mine.headers["referrer-policy"] == "no-referrer"
    cookie = mine.headers["set-cookie"]
    assert cookie.startswith(f"guide_pass={guide_pass.token};")
    assert "HttpOnly" in cookie and "Path=/guides" in cookie and "SameSite=lax" in cookie
    assert _passes()[0].opened == 1

    # With the cookie, the guide opens in full, without ads.
    page = client.get("/guides/moving-to-guam")
    assert "Start early" in page.text and "Read the rest" not in page.text
    assert "reading this with your PCS move pack" in page.text
    assert "adsbygoogle" not in page.text
    assert "You have this pack" in client.get("/guides/packs/pcs-move").text

    # The return address again (a reload): the same pass, no second capture.
    captures = fake_paypal.captures
    again = client.get("/guides/buy/pcs-move/paid?token=ORDER-1", follow_redirects=False)
    assert again.headers["location"] == f"/guides/pass/{guide_pass.token}?new=1"
    assert fake_paypal.captures == captures and len(_passes()) == 1
    # Long after: the return address no longer shows the link.
    with session_scope() as session:
        session.execute(
            update(GuidePass).values(created_at=datetime.now(timezone.utc) - timedelta(days=2))
        )
    late = client.get("/guides/buy/pcs-move/paid?token=ORDER-1", follow_redirects=False)
    assert late.headers["location"] == "/guides/packs/pcs-move?result=used"

    # Another browser has no cookie: the opening only.
    client.cookies.clear()
    assert "Start early" not in client.get("/guides/moving-to-guam").text

    # Lost the link: the email again, the same answer for an unknown address.
    sent.clear()
    response = client.post(
        "/guides/pass/link",
        data={"email": "BUYER@example.com", "pack": "pcs-move"},
        headers=FROM_SITE,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/guides/packs/pcs-move?result=sent#lost"
    assert len(sent) == 1 and guide_pass.token in sent[0][2]
    response = client.post(
        "/guides/pass/link",
        data={"email": "nobody@example.com", "pack": "pcs-move"},
        headers=FROM_SITE,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/guides/packs/pcs-move?result=sent#lost"
    assert len(sent) == 1
    assert client.post("/guides/pass/link", data={"email": "a@b.co"}).status_code == 403


def test_payments_that_do_not_match_are_not_used(client, fake_paypal):
    guide = _guide("moving-to-guam")
    pack = _pack(client, [guide.id])
    _pack(client, [guide.id], title="Other", slug="other", price="20")

    # An order made for another pack can't open this one: not captured.
    client.get("/guides/buy/other", follow_redirects=False)
    fake_paypal.approve("ORDER-1")
    response = client.get("/guides/buy/pcs-move/paid?token=ORDER-1", follow_redirects=False)
    assert response.headers["location"] == "/guides/packs/pcs-move?result=mismatch"
    assert fake_paypal.captures == 0 and _passes() == []

    # The price changed while the buyer was on PayPal: not captured either.
    client.get("/guides/buy/pcs-move", follow_redirects=False)
    fake_paypal.approve("ORDER-2")
    with session_scope() as session:
        session.execute(update(GuidePack).where(GuidePack.id == pack.id).values(price_cents=1500))
    response = client.get("/guides/buy/pcs-move/paid?token=ORDER-2", follow_redirects=False)
    assert response.headers["location"] == "/guides/packs/pcs-move?result=mismatch"
    assert fake_paypal.captures == 0

    # Not approved on PayPal: nothing taken.
    client.get("/guides/buy/pcs-move", follow_redirects=False)
    response = client.get("/guides/buy/pcs-move/paid?token=ORDER-3", follow_redirects=False)
    assert response.headers["location"] == "/guides/packs/pcs-move?result=unfinished"
    assert _passes() == []

    # PayPal took a different amount: given back at once.
    client.get("/guides/buy/pcs-move", follow_redirects=False)
    fake_paypal.approve("ORDER-4")
    fake_paypal.paid_value = "1.00"
    response = client.get("/guides/buy/pcs-move/paid?token=ORDER-4", follow_redirects=False)
    assert response.headers["location"] == "/guides/packs/pcs-move?result=mismatch"
    assert fake_paypal.refunds == ["CAP-ORDER-4"] and _passes() == []

    assert client.get("/guides/pass/" + "x" * 32).status_code == 404


def test_packs_are_hidden_until_they_can_be_sold(client, monkeypatch):
    draft_guide = _guide("moving-to-guam", status=guides.DRAFT)
    _pack(client, [draft_guide.id])
    # Its only guide is a draft: no pack page, nothing to buy.
    assert client.get("/guides/packs/pcs-move").status_code == 404
    assert client.get("/guides/buy/pcs-move").status_code == 404
    with session_scope() as session:
        session.execute(update(Guide).values(status=guides.PUBLISHED))
    assert client.get("/guides/packs/pcs-move").status_code == 200
    # Not for sale.
    with session_scope() as session:
        session.execute(update(GuidePack).values(status="draft"))
    assert client.get("/guides/packs/pcs-move").status_code == 404
    from guheadlines.web.app import guide_cache

    guide_cache._data.clear()
    page = client.get("/guides/moving-to-guam").text
    assert "not on sale right now" in page and "Start early" not in page
    # Without PayPal, nothing is for sale.
    with session_scope() as session:
        session.execute(update(GuidePack).values(status="on_sale"))
    _set(monkeypatch, stats_password=PASSWORD, paypal_client_id="", paypal_client_secret="")
    assert client.get("/guides/packs/pcs-move").status_code == 404
    listing = client.get("/admin/guides", headers=ADMIN).text
    assert "packs can't be sold until PayPal is set up" in listing


def test_the_owner_turns_off_refunds_and_cannot_delete_a_sold_pack(client, fake_paypal):
    guide = _guide("moving-to-guam")
    pack = _pack(client, [guide.id])
    _buy(client, fake_paypal)
    [guide_pass] = _passes()
    client.get(f"/guides/pass/{guide_pass.token}")
    assert "Start early" in client.get("/guides/moving-to-guam").text

    listing = client.get("/admin/guides", headers=ADMIN).text
    assert "Guide pack sales" in listing and "buyer@example.com" in listing and "$12" in listing

    # Turned off (a link shared in public): the guide is locked again.
    response = client.post(
        f"/admin/guides/passes/{guide_pass.id}",
        data={"action": "off"},
        headers=OWNER,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin/guides?done=pass-off#sales"
    assert "Start early" not in client.get("/guides/moving-to-guam").text
    assert "This link is turned off" in client.get(f"/guides/pass/{guide_pass.token}").text
    client.post(f"/admin/guides/passes/{guide_pass.id}", data={"action": "on"}, headers=OWNER)
    assert "Start early" in client.get("/guides/moving-to-guam").text

    # A refund needs the box ticked, then gives the money back and locks the guide.
    response = client.post(
        f"/admin/guides/passes/{guide_pass.id}",
        data={"action": "refund"},
        headers=OWNER,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin/guides?done=refund-confirm#sales"
    assert fake_paypal.refunds == []
    client.post(
        f"/admin/guides/passes/{guide_pass.id}",
        data={"action": "refund", "confirm": "yes"},
        headers=OWNER,
    )
    assert fake_paypal.refunds == [guide_pass.capture_id]
    assert _passes()[0].status == "refunded" and _passes()[0].refunded_at is not None
    assert "Start early" not in client.get("/guides/moving-to-guam").text

    # A pack that was bought can't be deleted.
    response = client.post(
        f"/admin/guides/packs/{pack.id}/delete",
        data={"confirm": "yes"},
        headers=OWNER,
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/admin/guides/packs/{pack.id}?done=sold"
    with session_scope() as session:
        assert session.get(GuidePack, pack.id) is not None
    assert client.post(f"/admin/guides/passes/{guide_pass.id}", data={"action": "off"},
                       headers=ADMIN).status_code == 403  # fmt: skip


def test_an_every_paid_guide_pack_opens_guides_added_later(client, fake_paypal):
    first = _guide("moving-to-guam")
    _pack(client, [], title="Every guide pack", slug="every-guide", price="15", every_paid="yes")
    _buy(client, fake_paypal, "every-guide")
    client.get(f"/guides/pass/{_passes()[0].token}")
    assert "Start early" in client.get(f"/guides/{first.slug}").text
    later = _guide("pets-to-guam")
    from guheadlines.web.app import guide_cache

    guide_cache._data.clear()
    assert "Start early" in client.get(f"/guides/{later.slug}").text
    # A free guide is never in an "every paid guide" pack, and stays free.
    _guide("beaches", access=guides.FREE)
    guide_cache._data.clear()
    assert "Start early" in client.get("/guides/beaches").text


def test_the_owner_adds_the_starter_packs(client):
    listing = client.get("/admin/guides", headers=ADMIN).text
    assert "Starter guide packs" in listing and 'name="pack" value="pcs-move" checked' in listing
    response = client.post(
        "/admin/guides/packs/starters",
        data={"pack": ["pcs-move", "every-guide"]},
        headers=OWNER,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin/guides?done=packs#packs"
    with session_scope() as session:
        packs = {p.slug: p for p in guide_packs.everything(session)}
        assert set(packs) == {"pcs-move", "every-guide"}
        assert all(p.status == "draft" for p in packs.values())
        inside = guide_packs.contents(session, packs["pcs-move"], published_only=False)
        added = session.scalars(select(Guide)).all()
    starter = next(s for s in guide_packs.starters() if s.slug == "pcs-move")
    assert [g.slug for g in inside] == list(starter.guides)
    assert all(g.status == "draft" and g.access == "paid" for g in added)
    assert packs["every-guide"].every_paid
    # Pressed again: nothing more.
    client.post("/admin/guides/packs/starters", data={"pack": ["pcs-move"]}, headers=OWNER)
    with session_scope() as session:
        assert len(guide_packs.everything(session)) == 2
    editor = client.get(f"/admin/guides/packs/{packs['pcs-move'].id}", headers=ADMIN)
    assert editor.status_code == 200 and 'value="12"' in editor.text
    assert client.get("/admin/guides/packs/new").status_code == 401


def test_the_guide_editor_sets_free_or_paid(client):
    from test_guides import FORM

    empty = ("", b"", "application/octet-stream")
    response = client.post(
        "/admin/guides/new",
        data={**FORM, "access": "paid", "status": "published"},
        files={"cover": empty, "picture": empty},
        headers=OWNER,
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_scope() as session:
        guide = session.scalar(select(Guide))
    assert guide.access == "paid"
    preview = client.get(f"/admin/guides/{guide.id}/view", headers=ADMIN).text
    assert "Readers without a guide pack stop here" in preview
    assert "Getting there" in preview and "Where to stay" in preview  # the owner sees it all
    editor = client.get(f"/admin/guides/{guide.id}", headers=ADMIN).text
    assert 'name="access" value="paid" checked' in editor


def test_search_engines_and_the_privacy_note(client, fake_paypal):
    guide = _guide("moving-to-guam")
    _pack(client, [guide.id])
    robots = client.get("/robots.txt").text
    assert "Disallow: /guides/buy/\n" in robots and "Disallow: /guides/pass/\n" in robots
    assert "<loc>http://testserver/guides/packs/pcs-move</loc>" in client.get("/sitemap.xml").text
    assert "<strong>Guide packs.</strong>" in client.get("/status").text
