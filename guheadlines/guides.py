"""Visitor guides: pages about visiting Guam that the site's owner writes and
keeps up to date (/guides).

The guides are kept in the database, not in files of the code, so their text
stays the owner's own: the code is public, and a guide may one day be for
paying readers only. The owner writes them on /admin/guides (behind
STATS_PASSWORD), in Markdown, the plain-text way of marking headings, lists
and links that is explained on that page (docs/guides.md has more). The
nightly backup keeps them with the rest of the database, and their photos
with the other photos.

A guide is a "draft" (only the owner sees it) until it is "published". Its
`access` is "free" for now; "paid" is there for guides that only paying
readers may read in full, once that is built (docs/guides.md#selling-guides).

The site comes with starter guides (guide_starters/), which the owner can
add as drafts, then read, change and publish.
"""

from __future__ import annotations

import email.parser
import email.policy
import math
import re
import secrets
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token
from markupsafe import Markup, escape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import GUIDE_SPONSOR_CLICKS, Guide, PageViews
from .scraper.images import ImageRejected, process_image

DRAFT = "draft"
PUBLISHED = "published"
STATUSES = (DRAFT, PUBLISHED)
FREE = "free"
PAID = "paid"

STARTER_DIR = Path(__file__).resolve().parent / "guide_starters"

# The form's limits.
MAX_TITLE = 120
MAX_SLUG = 60
MAX_SECTION = 60
MAX_SUMMARY = 300
MAX_BODY_TEXT = 60_000
MAX_ALT = 200
MAX_SPONSOR_NAME = 80
MAX_SPONSOR_TEXT = 150
# The form with two photos, and room for the text.
MAX_PHOTO = 8 * 1024 * 1024
MAX_FORM = 2 * MAX_PHOTO + 256 * 1024

# Photos are kept under the media folder, in guides/, named
# <guide id>-<random>-<width>x<height>.webp, so a page knows a photo's size
# from its name (no jump when it loads). PHOTO_WIDTH is about twice the
# width of a guide's text column, for sharp phone and laptop screens; a
# SMALL_PHOTO copy (name-small.webp) is for phones.
PHOTO_DIR = "guides"
PHOTO_WIDTH = 1400
SMALL_PHOTO = 700
_PHOTO_NAME = re.compile(r"^/media/guides/(\d+)-[0-9a-f]{12}-(\d{2,5})x(\d{2,5})\.webp$")
# What a guide's text column is wide, for the browser's choice of photo.
PHOTO_SIZES = "(max-width: 700px) calc(100vw - 32px), 720px"

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
WORDS_A_MINUTE = 220


# -------------------------------------------------------------------- text


def slugify(text: str) -> str:
    """ "Håfa Adai: Two Lovers' Point!" -> "hafa-adai-two-lovers-point"."""
    plain = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    plain = re.sub(r"['’‘`]", "", plain.lower())
    plain = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    return plain[:MAX_SLUG].rstrip("-")


def reading_minutes(text: str) -> int:
    return max(1, math.ceil(len(text.split()) / WORDS_A_MINUTE))


def photo_size(path: str) -> tuple[int, int] | None:
    """The width and height of a guide photo, from its address
    (/media/guides/<id>-<random>-<width>x<height>.webp)."""
    match = _PHOTO_NAME.match(path)
    return (int(match.group(2)), int(match.group(3))) if match else None


def small_photo(path: str) -> str:
    return path.removesuffix(".webp") + "-small.webp"


def photo_srcset(path: str) -> str | None:
    """The srcset of a guide photo that has a small copy, else None."""
    size = photo_size(path)
    if size is None or size[0] <= SMALL_PHOTO:
        return None
    return f"{small_photo(path)} {SMALL_PHOTO}w, {path} {size[0]}w"


@dataclass
class Rendered:
    """A guide's text as HTML, cut before each main heading (## ...): parts[0]
    is the opening, before the first one. `contents` lists the main headings,
    as (id, text), for the "On this page" list."""

    parts: list[Markup] = field(default_factory=list)
    contents: list[tuple[str, str]] = field(default_factory=list)


def _allowed_link(url: str) -> bool:
    """Links may go to web pages, to this site's own pages (/weather, #id)
    and to phone numbers (tel:). Never javascript: or data: addresses."""
    url = url.strip().lower()
    if url.startswith("//"):
        return False
    return url.startswith(("https://", "http://", "/", "#", "tel:"))


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False})
    md.enable(["table", "strikethrough"])
    md.validateLink = _allowed_link
    rules = md.renderer.rules
    default_image = rules["image"]

    def image(tokens, idx, options, env):
        token = tokens[idx]
        src = token.attrGet("src") or ""
        size = photo_size(src)
        if size is None:
            # Only photos uploaded on the guide's page: a photo from another
            # site may be someone else's, and would load from their server.
            # (str(): the renderer adds plain strings to what this returns,
            # and adding them to Markup would escape them.)
            return str(escape(token.content))
        token.attrSet("width", str(size[0]))
        token.attrSet("height", str(size[1]))
        srcset = photo_srcset(src)
        if srcset:
            token.attrSet("srcset", srcset)
            token.attrSet("sizes", PHOTO_SIZES)
        token.attrSet("loading", "lazy")
        token.attrSet("decoding", "async")
        caption = token.attrGet("title")
        if caption:
            del token.attrs["title"]
        html = default_image(tokens, idx, options, env)
        if caption:
            html += f'<span class="caption">{str(escape(caption))}</span>'
        return html

    def table_open(tokens, idx, options, env):
        return '<div class="table-wrap"><table>\n'

    def table_close(tokens, idx, options, env):
        return "</table></div>\n"

    rules["image"] = image
    rules["table_open"] = table_open
    rules["table_close"] = table_close
    return md


_MD = _markdown()
_ALIGN = {"text-align:right": "num", "text-align:center": "center"}


def _inline_text(token: Token) -> str:
    return "".join(t.content for t in token.children or [] if t.type in ("text", "code_inline"))


def _prepare(tokens: list[Token]) -> list[tuple[str, str]]:
    """Adjust the tokens in place; returns the main headings.

    - "#" and "##" are main headings (<h2>, with an id for the "On this page"
      list); "###" and smaller are <h3>. The page's <h1> is the title.
    - Links to other sites open in a new tab. rel="nofollow" tells search
      engines that the site does not vouch for them, as Google asks for
      paid (affiliate) links.
    - Table columns lined up with ":" get a class: the page's security rules
      do not allow style="" attributes.
    """
    contents: list[tuple[str, str]] = []
    used: set[str] = set()
    for i, token in enumerate(tokens):
        if token.type in ("heading_open", "heading_close"):
            main = token.tag in ("h1", "h2")
            token.tag = "h2" if main else "h3"
            if main and token.type == "heading_open":
                text = _inline_text(tokens[i + 1])
                base = slugify(text) or "section"
                anchor, n = base, 2
                while anchor in used:
                    anchor, n = f"{base}-{n}", n + 1
                used.add(anchor)
                token.attrSet("id", anchor)
                contents.append((anchor, text))
        elif token.type in ("th_open", "td_open"):
            style = token.attrGet("style")
            if style:
                del token.attrs["style"]
                if style in _ALIGN:
                    token.attrSet("class", _ALIGN[style])
        elif token.type == "inline":
            for child in token.children or []:
                if child.type == "link_open":
                    href = (child.attrGet("href") or "").lower()
                    if href.startswith(("http://", "https://")):
                        child.attrSet("target", "_blank")
                        child.attrSet("rel", "nofollow noopener")
    return contents


# A link to another guide, as render() writes it.
_GUIDE_LINK = re.compile(r'<a href="/guides/([a-z0-9-]+)(?:#[^"]*)?"[^>]*>(.*?)</a>', re.S)


def unlink_unpublished(text: Rendered, published_slugs: set[str]) -> Rendered:
    """`text` with each link to a guide that is not published (a draft, or
    no guide at all) turned into its plain words, so readers never land on a
    missing page. The link comes back once that guide is published."""

    def keep_or_unlink(found: re.Match) -> str:
        return found[0] if found[1] in published_slugs else found[2]

    parts = [Markup(_GUIDE_LINK.sub(keep_or_unlink, str(part))) for part in text.parts]
    return Rendered(parts, text.contents)


@lru_cache(maxsize=128)
def render(text: str) -> Rendered:
    """A guide's Markdown text as HTML. Kept for the next visit: the same
    text always gives the same page."""
    tokens = _MD.parse(text)
    contents = _prepare(tokens)
    chunks: list[list[Token]] = [[]]
    for token in tokens:
        if token.type == "heading_open" and token.tag == "h2" and token.level == 0:
            chunks.append([])
        chunks[-1].append(token)
    parts = [Markup(_MD.renderer.render(chunk, _MD.options, {})) for chunk in chunks]
    if len(parts) > 1 and not parts[0].strip():
        parts = parts[1:]  # no opening text: the guide starts with a heading
    return Rendered(parts=parts, contents=contents)


# --------------------------------------------------------------------- form


class FormError(Exception):
    """The form could not be read: too large, or not a form."""


def read_form(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """The text fields of a form sent as multipart/form-data, and its chosen
    files ({field name: data}). The standard library's email parser reads
    the parts."""
    if not content_type.lower().startswith("multipart/form-data") or len(body) > MAX_FORM:
        raise FormError("not a form, or too large")
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "replace")
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    if not message.is_multipart():
        raise FormError("not a multipart form")
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for count, part in enumerate(message.iter_parts()):
        if count >= 40:
            raise FormError("too many fields")
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        data = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            if data:
                files[name] = data
            continue
        fields[name] = data.decode("utf-8", "replace")
    return fields, files


def _line(value: str | None, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def _web_link(value: str) -> str | None:
    """A website address, with https:// added when it was left out; None for
    anything else."""
    value = value.strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    if len(value) > 500 or re.search(r"[\s<>\"]", value):
        return None
    host = re.match(r"^https?://([^/?#]+)", value, re.I)
    if not host or "." not in host.group(1) or "@" in host.group(1):
        return None
    return value


def check(fields: dict[str, str], has_photo: bool) -> tuple[dict, dict[str, str]]:
    """The guide's values from the editor's form, and the problems found
    ({field: message}). `has_photo`: the guide will have a cover photo."""
    errors: dict[str, str] = {}
    values: dict = {}
    values["title"] = _line(fields.get("title"), MAX_TITLE + 1)
    if not values["title"]:
        errors["title"] = "Please give the guide a title."
    elif len(values["title"]) > MAX_TITLE:
        errors["title"] = f"At most {MAX_TITLE} characters, please."
    slug = (fields.get("slug") or "").strip().lower() or slugify(values["title"])
    values["slug"] = slug
    if slug and (len(slug) > MAX_SLUG or not SLUG.match(slug)):
        errors["slug"] = (
            "Use only small letters a to z, numbers and dashes, such as getting-around"
            f" (at most {MAX_SLUG})."
        )
    elif not slug and "title" not in errors:
        errors["slug"] = "Please type the guide's address, such as getting-around."
    values["section"] = _line(fields.get("section"), MAX_SECTION)
    values["summary"] = _line(fields.get("summary"), MAX_SUMMARY + 1)
    if not values["summary"]:
        errors["summary"] = "Please write one or two sentences about the guide."
    elif len(values["summary"]) > MAX_SUMMARY:
        errors["summary"] = f"At most {MAX_SUMMARY} characters, please."
    body = (fields.get("body") or "").replace("\r\n", "\n").strip()
    values["body"] = body
    if not body:
        errors["body"] = "The guide has no text yet."
    elif len(body) > MAX_BODY_TEXT:
        errors["body"] = f"At most {MAX_BODY_TEXT:,} characters, please."
    try:
        values["position"] = int((fields.get("position") or "100").strip())
        if not 0 <= values["position"] <= 9999:
            raise ValueError
    except ValueError:
        values["position"] = 100
        errors["position"] = "A whole number from 0 to 9999, please."
    values["status"] = PUBLISHED if fields.get("status") == PUBLISHED else DRAFT
    values["photo_alt"] = _line(fields.get("photo_alt"), MAX_ALT)
    if has_photo and not values["photo_alt"]:
        errors["photo_alt"] = "Please say what the photo shows, for readers who can't see it."
    values["picture_alt"] = _line(fields.get("picture_alt"), MAX_ALT)

    values["sponsor_name"] = _line(fields.get("sponsor_name"), MAX_SPONSOR_NAME)
    values["sponsor_text"] = _line(fields.get("sponsor_text"), MAX_SPONSOR_TEXT)
    link = _web_link(fields.get("sponsor_link") or "")
    if link is None:
        errors["sponsor_link"] = "Please type the sponsor's website, such as islandtours.com."
    values["sponsor_link"] = link or ""
    until = (fields.get("sponsor_until") or "").strip()
    values["sponsor_until"] = None
    if until:
        try:
            values["sponsor_until"] = date.fromisoformat(until)
        except ValueError:
            errors["sponsor_until"] = "Please pick a day."
    if (values["sponsor_link"] or values["sponsor_text"]) and not values["sponsor_name"]:
        errors["sponsor_name"] = "Please give the sponsor's name too."
    return values, errors


# ------------------------------------------------------------------- photos


def save_photo(media_dir: Path, guide_id: int, data: bytes) -> str:
    """Store a photo for a guide as a WebP copy at most PHOTO_WIDTH wide, and
    a SMALL_PHOTO copy when it is wider. Returns its address
    (/media/guides/...). Raises ImageRejected."""
    if len(data) > MAX_PHOTO:
        raise ImageRejected("larger than 8 MB")
    webp, width, height = process_image(data, PHOTO_WIDTH)
    name = f"{guide_id}-{secrets.token_hex(6)}-{width}x{height}.webp"
    target = media_dir / PHOTO_DIR / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(webp)
    if width > SMALL_PHOTO:
        small, _, _ = process_image(webp, SMALL_PHOTO)
        (media_dir / PHOTO_DIR / small_photo(name)).write_bytes(small)
    return f"/media/{PHOTO_DIR}/{name}"


def delete_photo(media_dir: Path, path: str | None) -> None:
    """Delete one photo of a guide (its address, /media/guides/...) and its
    small copy. Anything that is not a guide photo's address is left alone."""
    if path and photo_size(path) is not None:
        name = path.removeprefix(f"/media/{PHOTO_DIR}/")
        (media_dir / PHOTO_DIR / name).unlink(missing_ok=True)
        (media_dir / PHOTO_DIR / small_photo(name)).unlink(missing_ok=True)


def delete_photos(media_dir: Path, guide_id: int) -> int:
    """Delete every photo of a guide. Returns how many files were deleted."""
    folder = media_dir / PHOTO_DIR
    if not folder.is_dir():
        return 0
    deleted = 0
    for path in folder.glob(f"{guide_id}-*.webp"):
        path.unlink(missing_ok=True)
        deleted += 1
    return deleted


# ----------------------------------------------------------------- database


def published(session: Session) -> list[Guide]:
    """The published guides, in the owner's order."""
    return list(
        session.scalars(
            select(Guide)
            .where(Guide.status == PUBLISHED)
            .order_by(Guide.position, func.lower(Guide.title))
        )
    )


def everything(session: Session) -> list[Guide]:
    return list(session.scalars(select(Guide).order_by(Guide.position, func.lower(Guide.title))))


def by_slug(session: Session, slug: str) -> Guide | None:
    return session.scalar(select(Guide).where(Guide.slug == slug))


def slug_taken(session: Session, slug: str, guide_id: int | None) -> bool:
    found = session.scalar(select(Guide.id).where(Guide.slug == slug))
    return found is not None and found != guide_id


def apply(guide: Guide, values: dict, now: datetime) -> None:
    """Put the form's values on a guide."""
    for name in (
        "title",
        "slug",
        "section",
        "summary",
        "body",
        "position",
        "status",
        "photo_alt",
        "sponsor_name",
        "sponsor_text",
        "sponsor_link",
        "sponsor_until",
    ):
        setattr(guide, name, values[name])
    guide.updated_at = now
    if guide.status == PUBLISHED and guide.published_at is None:
        guide.published_at = now


def sponsor_showing(guide: Guide, today: date) -> bool:
    """The guide's sponsor box shows: a sponsor is named, and its last day
    (sponsor_until, Guam time) has not passed."""
    return bool(guide.sponsor_name) and (
        guide.sponsor_until is None or guide.sponsor_until >= today
    )


def numbers(session: Session, guides: list[Guide], since: date) -> dict[int, dict[str, int]]:
    """The page views of each guide and the clicks on its sponsor since
    `since` (counted while the visitor stats are on)."""
    paths = {f"/guides/{g.slug}": g.id for g in guides}
    paths.update({f"{GUIDE_SPONSOR_CLICKS}{g.id}": g.id for g in guides})
    found = {g.id: {"views": 0, "clicks": 0} for g in guides}
    if not guides:
        return found
    rows = session.execute(
        select(PageViews.path, func.sum(PageViews.views))
        .where(PageViews.day >= since, PageViews.path.in_(list(paths)))
        .group_by(PageViews.path)
    )
    for path, total in rows:
        key = "clicks" if path.startswith(GUIDE_SPONSOR_CLICKS) else "views"
        found[paths[path]][key] += int(total or 0)
    return found


# ------------------------------------------------------------ starter guides


@dataclass(frozen=True)
class Starter:
    slug: str
    title: str
    section: str
    summary: str
    position: int
    body: str


def starters() -> list[Starter]:
    """The starter guides that come with the site (guide_starters/*.md), in
    their order: each file is a block of settings between --- lines, then
    the text."""
    found = []
    for path in sorted(STARTER_DIR.glob("*.md")):
        _, head, body = path.read_text(encoding="utf-8").split("---\n", 2)
        meta = yaml.safe_load(head)
        found.append(
            Starter(
                slug=path.stem,
                title=meta["title"],
                section=meta.get("section", ""),
                summary=" ".join(meta["summary"].split()),
                position=int(meta.get("position", 100)),
                body=body.strip(),
            )
        )
    return sorted(found, key=lambda s: (s.position, s.title))


def missing_starters(session: Session) -> list[Starter]:
    """The starter guides whose address no guide has yet."""
    taken = set(session.scalars(select(Guide.slug)))
    return [s for s in starters() if s.slug not in taken]


def add_starters(session: Session, now: datetime, slugs: Iterable[str]) -> int:
    """Add the missing starter guides whose address is in `slugs` (the ones
    the owner ticked), as drafts. Returns how many."""
    wanted = set(slugs)
    added = [s for s in missing_starters(session) if s.slug in wanted]
    for s in added:
        session.add(
            Guide(
                slug=s.slug,
                title=s.title,
                section=s.section,
                summary=s.summary,
                body=s.body,
                position=s.position,
                status=DRAFT,
                access=FREE,
                created_at=now,
                updated_at=now,
            )
        )
    return len(added)
