"""Guide packs: visitor guides (guides.py) sold together through PayPal, such
as a PCS move pack for military families or a first-week pack for visitors.

There are no accounts and no passwords. A sale goes so:

1. A guide whose `access` is "paid" shows readers its opening (the text
   before its first main heading) and the packs that have it. Each pack has
   its own page, /guides/packs/<slug>, with a "Buy" link.
2. The link (/guides/buy/<slug>) makes a PayPal order for the pack's price
   and sends the buyer to PayPal (see paypal.py). Nothing is stored yet: an
   order that is never paid costs nothing and leaves nothing behind.
3. PayPal sends the buyer back to /guides/buy/<slug>/paid. The site takes the
   money (capture_order) and makes a pass (GuidePass) with a secret token.
4. The buyer lands on the pass's page, /guides/pass/<token>: the private
   link that opens the guides. The site keeps the token in the buyer's
   browser (the cookie COOKIE, only for /guides pages), so the guides open
   in full from then on. On another phone or computer, the buyer opens the
   link once. With email set up (mail.py), the link is also emailed to the
   address the buyer paid with, and the pack pages have a "lost your link?"
   form that emails it again.

A pack holds the guides the owner ticked, or every paid guide (also ones
added later). A buyer reads what the pack holds now, so guides added to a
pack later are theirs too.

Packs are for sale while PayPal (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)
and STATS_PASSWORD (for the owner's pages) are set. The owner makes the packs
on /admin/guides, can turn a pass off (a link shared in public) and give the
money back.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml
from markupsafe import escape
from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import guides, paypal
from .models import Guide, GuidePack, GuidePackGuide, GuidePass
from .settings import Settings

log = logging.getLogger(__name__)

DRAFT = "draft"
ON_SALE = "on_sale"
STATUSES = (DRAFT, ON_SALE)

# A pass's states (GuidePass.status). PENDING: PayPal is still checking the
# payment; it opens the guides too, as with booked ads.
PAID, PENDING, OFF, REFUNDED = "paid", "pending", "off", "refunded"
READABLE = (PAID, PENDING)

# The cookie that keeps a buyer's passes in their browser: their tokens,
# joined by ".". Only sent to /guides pages.
COOKIE = "guide_pass"
COOKIE_PATH = "/guides"
# Browsers keep a cookie at most about 400 days; opening the link again
# starts the time again.
COOKIE_DAYS = 400
MAX_TOKENS = 8
TOKEN = re.compile(r"^[A-Za-z0-9_-]{24,64}$")

# Our own name of what a PayPal order pays for (create_order's custom_id):
# the pack and its price when the order was made, so a payment can only open
# the pack it was made for, at that price.
ORDER_PREFIX = "guide-pack-"
# After this long, the return address from PayPal no longer shows the
# buyer's link (it is in the email, and on the "lost your link?" form).
RETURN_WINDOW = timedelta(days=1)

# The form's limits.
MAX_TITLE = 100
MAX_SUMMARY = 300
MAX_ABOUT = 5000
MIN_PRICE_CENTS = 100
MAX_PRICE_CENTS = 100_000

STARTERS = Path(__file__).resolve().parent / "guide_starters" / "packs.yaml"


def on(settings: Settings) -> bool:
    """Packs can be sold: PayPal is set up, and STATS_PASSWORD for the
    owner's pages."""
    return paypal.enabled(settings) and bool(settings.stats_password)


def money(cents: int) -> str:
    """500 -> "$5", 1299 -> "$12.99"."""
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents // 100:,}.{cents % 100:02d}"


def parse_price(value: str | None) -> int | None:
    """ "12", "$12.99" or "12.5" -> 1200, 1299, 1250 (US cents); None when it
    is not a price."""
    text = (value or "").strip().removeprefix("$").replace(",", "").strip()
    found = re.fullmatch(r"(\d{1,6})(?:\.(\d{1,2}))?", text)
    if not found:
        return None
    return int(found[1]) * 100 + int((found[2] or "0").ljust(2, "0"))


def order_name(pack: GuidePack) -> str:
    return f"{ORDER_PREFIX}{pack.id}-{pack.price_cents}"


# ------------------------------------------------------------------ the form


def _line(value: str | None, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def check(fields: dict[str, list[str]]) -> tuple[dict, dict[str, str]]:
    """The pack's values from the owner's form, and the problems found
    ({field: message})."""

    def one(name: str) -> str:
        return (fields.get(name) or [""])[0]

    errors: dict[str, str] = {}
    values: dict = {"title": _line(one("title"), MAX_TITLE + 1)}
    if not values["title"]:
        errors["title"] = "Please give the pack a name."
    elif len(values["title"]) > MAX_TITLE:
        errors["title"] = f"At most {MAX_TITLE} characters, please."
    slug = one("slug").strip().lower() or guides.slugify(values["title"])
    values["slug"] = slug
    if not slug or len(slug) > guides.MAX_SLUG or not guides.SLUG.match(slug):
        errors["slug"] = (
            "Use only small letters a to z, numbers and dashes, such as pcs-move"
            f" (at most {guides.MAX_SLUG})."
        )
    values["summary"] = _line(one("summary"), MAX_SUMMARY + 1)
    if not values["summary"]:
        errors["summary"] = "Please write one or two sentences about the pack."
    elif len(values["summary"]) > MAX_SUMMARY:
        errors["summary"] = f"At most {MAX_SUMMARY} characters, please."
    values["about"] = one("about").replace("\r\n", "\n").strip()
    if len(values["about"]) > MAX_ABOUT:
        errors["about"] = f"At most {MAX_ABOUT:,} characters, please."
    price = parse_price(one("price"))
    values["price_cents"] = price or 0
    if price is None or not MIN_PRICE_CENTS <= price <= MAX_PRICE_CENTS:
        errors["price"] = (
            f"A price from {money(MIN_PRICE_CENTS)} to {money(MAX_PRICE_CENTS)}, such as 12 or"
            " 12.99."
        )
    try:
        values["position"] = int(one("position").strip() or "100")
        if not 0 <= values["position"] <= 9999:
            raise ValueError
    except ValueError:
        values["position"] = 100
        errors["position"] = "A whole number from 0 to 9999, please."
    values["status"] = ON_SALE if one("status") == ON_SALE else DRAFT
    values["every_paid"] = one("every_paid") == "yes"
    guide_ids = []
    for raw in fields.get("guide", []):
        if raw.isdigit() and 0 < int(raw) < 2**31:
            guide_ids.append(int(raw))
    values["guide_ids"] = sorted(set(guide_ids))
    if not values["every_paid"] and not values["guide_ids"]:
        errors["guides"] = "Please tick the guides in this pack, or tick every paid guide."
    return values, errors


# ----------------------------------------------------------------- database


def everything(session: Session) -> list[GuidePack]:
    return list(
        session.scalars(select(GuidePack).order_by(GuidePack.position, GuidePack.price_cents))
    )


def on_sale(session: Session) -> list[GuidePack]:
    return list(
        session.scalars(
            select(GuidePack)
            .where(GuidePack.status == ON_SALE)
            .order_by(GuidePack.position, GuidePack.price_cents)
        )
    )


def by_slug(session: Session, slug: str) -> GuidePack | None:
    return session.scalar(select(GuidePack).where(GuidePack.slug == slug))


def slug_taken(session: Session, slug: str, pack_id: int | None) -> bool:
    found = session.scalar(select(GuidePack.id).where(GuidePack.slug == slug))
    return found is not None and found != pack_id


def guide_ids(session: Session, pack_id: int) -> set[int]:
    """The guides the owner ticked for a pack."""
    return set(
        session.scalars(select(GuidePackGuide.guide_id).where(GuidePackGuide.pack_id == pack_id))
    )


def contents(session: Session, pack: GuidePack, published_only: bool = True) -> list[Guide]:
    """The guides in a pack, in the guides' order: the ticked ones, or every
    paid guide. Readers see only the published ones."""
    query = select(Guide).order_by(Guide.position, func.lower(Guide.title))
    if pack.every_paid:
        query = query.where(Guide.access == guides.PAID)
    else:
        query = query.join(GuidePackGuide, GuidePackGuide.guide_id == Guide.id).where(
            GuidePackGuide.pack_id == pack.id
        )
    if published_only:
        query = query.where(Guide.status == guides.PUBLISHED)
    return list(session.scalars(query))


def _holds(guide_id: int):
    """The packs that hold this guide (in a query)."""
    return or_(
        GuidePack.every_paid,
        exists().where(GuidePackGuide.pack_id == GuidePack.id, GuidePackGuide.guide_id == guide_id),
    )


def packs_for(session: Session, guide: Guide) -> list[GuidePack]:
    """The packs for sale that hold this guide, the cheapest first."""
    query = select(GuidePack).where(GuidePack.status == ON_SALE)
    if guide.access == guides.PAID:
        query = query.where(_holds(guide.id))
    else:  # a free guide is in no "every paid guide" pack
        query = query.where(
            exists().where(
                GuidePackGuide.pack_id == GuidePack.id, GuidePackGuide.guide_id == guide.id
            )
        )
    return list(session.scalars(query.order_by(GuidePack.price_cents, GuidePack.position)))


def apply(session: Session, pack: GuidePack, values: dict, now: datetime) -> None:
    """Put the form's values on a pack (with a number: flushed), and its guides."""
    for name in ("title", "slug", "summary", "about", "price_cents", "position", "status"):
        setattr(pack, name, values[name])
    pack.every_paid = values["every_paid"]
    pack.updated_at = now
    session.flush()
    session.execute(delete(GuidePackGuide).where(GuidePackGuide.pack_id == pack.id))
    known = set(session.scalars(select(Guide.id).where(Guide.id.in_(values["guide_ids"]))))
    for guide_id in values["guide_ids"]:
        if guide_id in known:
            session.add(GuidePackGuide(pack_id=pack.id, guide_id=guide_id))


def sold(session: Session, pack_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(GuidePass).where(GuidePass.pack_id == pack_id)
    )


def sales(session: Session, since: datetime) -> dict[int, tuple[int, int]]:
    """Each pack's passes and money since `since` ({pack id: (count, cents)}),
    without the ones given back."""
    rows = session.execute(
        select(GuidePass.pack_id, func.count(), func.sum(GuidePass.amount_cents))
        .where(GuidePass.created_at >= since, GuidePass.status != REFUNDED)
        .group_by(GuidePass.pack_id)
    )
    return {pack_id: (n, int(cents or 0)) for pack_id, n, cents in rows}


def recent_passes(session: Session, limit: int = 50) -> list[tuple[GuidePass, str]]:
    """The latest passes, with their pack's name."""
    rows = session.execute(
        select(GuidePass, GuidePack.title)
        .join(GuidePack, GuidePack.id == GuidePass.pack_id)
        .order_by(GuidePass.created_at.desc(), GuidePass.id.desc())
        .limit(limit)
    )
    return [(p, title) for p, title in rows]


# ------------------------------------------------------------------ passes


def cookie_tokens(value: str | None) -> list[str]:
    """The tokens in a buyer's cookie (at most MAX_TOKENS)."""
    found: list[str] = []
    for token in (value or "").split("."):
        if TOKEN.match(token) and token not in found:
            found.append(token)
    return found[:MAX_TOKENS]


def with_token(value: str | None, token: str) -> str:
    """The cookie's value with `token` added first (the oldest drop off)."""
    tokens = [token] + [t for t in cookie_tokens(value) if t != token]
    return ".".join(tokens[:MAX_TOKENS])


def by_token(session: Session, token: str) -> GuidePass | None:
    if not TOKEN.match(token or ""):
        return None
    return session.scalar(select(GuidePass).where(GuidePass.token == token))


def unlocking(session: Session, tokens: Iterable[str], guide: Guide) -> GuidePack | None:
    """The pack of one of these passes that opens this paid guide, if any."""
    tokens = list(tokens)
    if not tokens:
        return None
    return session.scalar(
        select(GuidePack)
        .join(GuidePass, GuidePass.pack_id == GuidePack.id)
        .where(
            GuidePass.token.in_(tokens),
            GuidePass.status.in_(READABLE),
            _holds(guide.id),
        )
        .order_by(GuidePack.price_cents.desc())
        .limit(1)
    )


def owned(session: Session, tokens: Iterable[str]) -> dict[int, str]:
    """The packs that these passes open ({pack id: token})."""
    tokens = list(tokens)
    if not tokens:
        return {}
    rows = session.execute(
        select(GuidePass.pack_id, GuidePass.token).where(
            GuidePass.token.in_(tokens), GuidePass.status.in_(READABLE)
        )
    )
    return {pack_id: token for pack_id, token in rows}


def by_order(session: Session, order_id: str) -> GuidePass | None:
    return session.scalar(select(GuidePass).where(GuidePass.order_id == order_id))


class PaymentMismatch(Exception):
    """The payment is not the pack's price, or is not for this pack."""


@dataclass
class Bought:
    """What came of a buyer's return from PayPal (settle)."""

    # "ok", "pending" (paid, PayPal still checks it), "used" (this payment
    # opened a pass long ago), "unfinished", "declined", "error", "mismatch".
    result: str
    guide_pass: GuidePass | None = None
    # True when this call made the pass (send the email, tell the owner).
    new: bool = False
    notes: list[str] = field(default_factory=list)


def settle(
    settings: Settings, session_factory, pack: GuidePack, order_id: str, now: datetime
) -> Bought:
    """Take the money of a PayPal order for `pack`, and make its pass. Safe
    to call again (the buyer reloads the page): a payment opens one pass."""
    with session_factory() as session:
        found = by_order(session, order_id)
    if found is not None:
        if found.pack_id == pack.id and now - found.created_at <= RETURN_WINDOW:
            return Bought("ok", found)
        return Bought("used")
    try:
        capture = paypal.capture_order(settings, order_id, custom_id=order_name(pack))
    except paypal.PayPalError as exc:
        if exc.issue == paypal.WRONG_ORDER:
            # Made for another pack, or before the price changed: not taken.
            log.warning("guide pack %s: PayPal order not used: %s", pack.id, exc)
            return Bought("mismatch")
        log.warning("guide pack %s: PayPal capture failed: %s", pack.id, exc)
        return Bought("declined" if exc.issue == "INSTRUMENT_DECLINED" else "error")
    if capture.status not in ("COMPLETED", "PENDING") or not capture.capture_id:
        return Bought("unfinished")
    if capture.amount_cents != pack.price_cents or capture.currency != paypal.CURRENCY:
        log.error(
            "guide pack %s: paid %s %s, the price is %s; giving it back",
            pack.id,
            capture.amount_cents,
            capture.currency,
            pack.price_cents,
        )
        try:
            paypal.refund_capture(settings, capture.capture_id)
        except paypal.PayPalError as exc:
            log.error("guide pack %s: refund of %s failed: %s", pack.id, capture.capture_id, exc)
        return Bought("mismatch")
    guide_pass = GuidePass(
        token=secrets.token_urlsafe(24),
        pack_id=pack.id,
        order_id=order_id,
        capture_id=capture.capture_id,
        amount_cents=capture.amount_cents,
        email=capture.payer_email,
        status=PAID if capture.paid else PENDING,
        created_at=now,
    )
    try:
        with session_factory() as session:
            session.add(guide_pass)
    except IntegrityError:
        # The same return twice at once: the other one made the pass.
        with session_factory() as session:
            found = by_order(session, order_id)
        return Bought("ok", found) if found else Bought("error")
    return Bought("pending" if guide_pass.status == PENDING else "ok", guide_pass, new=True)


def count_open(session: Session, pass_id: int) -> None:
    session.execute(
        update(GuidePass).where(GuidePass.id == pass_id).values(opened=GuidePass.opened + 1)
    )


def passes_of(session: Session, email: str) -> list[tuple[GuidePass, str]]:
    """The readable passes bought with this email address, with their pack's name."""
    rows = session.execute(
        select(GuidePass, GuidePack.title)
        .join(GuidePack, GuidePack.id == GuidePass.pack_id)
        .where(func.lower(GuidePass.email) == email.lower(), GuidePass.status.in_(READABLE))
        .order_by(GuidePass.created_at)
    )
    return [(p, title) for p, title in rows]


def set_status(session: Session, pass_id: int, status: str, now: datetime) -> bool:
    """Turn a pass off or on again, or record its refund. False when nothing changed."""
    allowed = {OFF: READABLE, PAID: (OFF,), REFUNDED: (PAID, PENDING, OFF)}[status]
    values: dict = {"status": status}
    if status == REFUNDED:
        values["refunded_at"] = now
    done = session.execute(
        update(GuidePass)
        .where(and_(GuidePass.id == pass_id, GuidePass.status.in_(allowed)))
        .values(**values)
    ).rowcount
    return bool(done)


# ------------------------------------------------------------------- email


def _html(title: str, paragraphs: list[str]) -> str:
    """A plain email page, like the bid alert emails."""
    body = "".join(
        f'<p style="margin:0 0 12px;font:15px Arial,sans-serif">{p}</p>' for p in paragraphs
    )
    return (
        '<!doctype html><html><body style="margin:0;padding:16px;background:#f4f2ec;'
        'font-family:Georgia,serif"><div style="max-width:600px;margin:0 auto;background:#fff;'
        'padding:20px 24px;border:1px solid #e3e0d8">'
        f'<h1 style="margin:0 0 12px;font-size:21px">{escape(title)}</h1>{body}</div></body></html>'
    )


def bought_email(
    settings: Settings, pack_title: str, link: str, guide_titles: list[str]
) -> tuple[str, str, str]:
    """(subject, text, html) of the email with a new buyer's link."""
    title = f"Your guide pack: {pack_title}"
    thanks = f"Thank you for buying the {pack_title}. This link opens your guides:"
    keep = (
        "Keep this email. The link is your key: open it once on each phone or computer you"
        " want to read on, and the guides open in full there. Please don't share it."
    )
    inside = "In the pack: " + "; ".join(guide_titles) + "." if guide_titles else ""
    text = "\n\n".join(x for x in [title, thanks, link, keep, inside] if x) + "\n"
    html = _html(
        title,
        [
            escape(thanks),
            f'<a href="{escape(link)}"><strong>Open your guides</strong></a>',
            escape(keep),
            *([escape(inside)] if inside else []),
        ],
    )
    return f"{title} ({settings.site_name})", text, html


def links_email(settings: Settings, found: list[tuple[str, str]]) -> tuple[str, str, str]:
    """(subject, text, html) of the email that sends lost links again;
    `found` is (pack name, link) for each pass."""
    title = "The link to your guide packs"
    intro = "Here is the link to each guide pack bought with this email address:"
    note = "Someone asked for this link on the guides pages. If it was not you, you can ignore it."
    lines = [f"{name}:\n{link}" for name, link in found]
    text = "\n\n".join([title, intro, *lines, note]) + "\n"
    html = _html(
        title,
        [
            escape(intro),
            *(
                f'{escape(name)}<br><a href="{escape(link)}">Open these guides</a>'
                for name, link in found
            ),
            f'<span style="color:#555;font-size:13px">{escape(note)}</span>',
        ],
    )
    return f"{title} ({settings.site_name})", text, html


# ----------------------------------------------------------- starter packs


@dataclass(frozen=True)
class Starter:
    slug: str
    title: str
    summary: str
    about: str
    price_cents: int
    position: int
    every_paid: bool
    # The starter guides it holds (guide_starters/<slug>.md).
    guides: tuple[str, ...]


@lru_cache(maxsize=1)
def starters() -> list[Starter]:
    """The starter packs that come with the site (guide_starters/packs.yaml)."""
    data = yaml.safe_load(STARTERS.read_text(encoding="utf-8")) or {}
    found = []
    for slug, meta in data.items():
        found.append(
            Starter(
                slug=slug,
                title=meta["title"],
                summary=" ".join(meta["summary"].split()),
                about=(meta.get("about") or "").strip(),
                price_cents=parse_price(str(meta["price"])) or 0,
                position=int(meta.get("position", 100)),
                every_paid=bool(meta.get("every_paid", False)),
                guides=tuple(meta.get("guides") or ()),
            )
        )
    return sorted(found, key=lambda s: (s.position, s.title))


def missing_starters(session: Session) -> list[Starter]:
    taken = set(session.scalars(select(GuidePack.slug)))
    return [s for s in starters() if s.slug not in taken]


def add_starters(session: Session, now: datetime, slugs: Iterable[str]) -> int:
    """Add the missing starter packs whose address is in `slugs`, not for
    sale yet, with their starter guides (added as drafts where missing).
    Returns how many packs."""
    wanted = set(slugs)
    added = [s for s in missing_starters(session) if s.slug in wanted]
    if not added:
        return 0
    guides.add_starters(session, now, {g for s in added for g in s.guides})
    session.flush()
    ids = {slug: guide_id for slug, guide_id in session.execute(select(Guide.slug, Guide.id))}
    for s in added:
        pack = GuidePack(created_at=now)
        session.add(pack)
        apply(
            session,
            pack,
            {
                "title": s.title,
                "slug": s.slug,
                "summary": s.summary,
                "about": s.about,
                "price_cents": s.price_cents,
                "position": s.position,
                "status": DRAFT,
                "every_paid": s.every_paid,
                "guide_ids": [ids[g] for g in s.guides if g in ids],
            },
            now,
        )
    return len(added)
