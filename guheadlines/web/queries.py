"""Read-side queries for the website: listings, archive and full-text search."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import Select, Text, and_, func, literal, literal_column, select, text, true
from sqlalchemy.orm import Session

from ..models import SEARCH_CONFIG, Article, ScrapeRun, Source
from ..scraper.text import GUAM_TZ

# ts_headline markers; replaced by <mark> after HTML-escaping the snippet.
MARK_START = "⟦"
MARK_END = "⟧"


@dataclass
class Page:
    items: list
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    def window(self, size: int = 2) -> list[int | None]:
        """Page numbers to show, with None for gaps: 1 … 4 5 [6] 7 8 … 20."""
        pages = self.pages
        shown = {1, pages} | set(range(max(1, self.page - size), min(pages, self.page + size) + 1))
        result: list[int | None] = []
        previous = 0
        for number in sorted(shown):
            if number - previous > 1:
                result.append(None)
            result.append(number)
            previous = number
        return result


@dataclass
class Filters:
    q: str = ""
    source: str | None = None
    category: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    sort: str = "relevance"  # relevance | newest | oldest

    @property
    def active(self) -> bool:
        return bool(self.q or self.source or self.category or self.date_from or self.date_to)


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC instants for the start and end of a calendar day in Guam."""
    start = datetime.combine(day, time.min, tzinfo=GUAM_TZ).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _article_query() -> Select:
    return select(Article, Source.name.label("source_name"), Source.slug.label("source_slug")).join(
        Source, Source.id == Article.source_id
    )


def _apply_filters(stmt: Select, f: Filters) -> Select:
    if f.source:
        stmt = stmt.where(Source.slug == f.source)
    if f.category:
        stmt = stmt.where(Article.categories.contains([f.category]))
    if f.date_from:
        stmt = stmt.where(Article.published_at >= local_day_bounds(f.date_from)[0])
    if f.date_to:
        stmt = stmt.where(Article.published_at < local_day_bounds(f.date_to)[1])
    return stmt


def _count(session: Session, stmt: Select) -> int:
    return session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0


def _rows(session: Session, stmt: Select) -> list[dict]:
    items = []
    for row in session.execute(stmt):
        mapping = row._mapping
        items.append(
            {
                "article": mapping[Article],
                "source_name": mapping["source_name"],
                "source_slug": mapping["source_slug"],
                "snippet": mapping.get("snippet"),
                "title_marked": mapping.get("title_marked"),
            }
        )
    return items


def list_articles(session: Session, f: Filters, page: int, per_page: int) -> Page:
    stmt = _apply_filters(_article_query(), f)
    total = _count(session, stmt)
    order = (
        [Article.published_at.asc(), Article.id.asc()]
        if f.sort == "oldest"
        else [Article.published_at.desc(), Article.id.desc()]
    )
    stmt = stmt.order_by(*order).limit(per_page).offset((page - 1) * per_page)
    return Page(_rows(session, stmt), total, page, per_page)


def search_articles(session: Session, f: Filters, page: int, per_page: int) -> tuple[Page, bool]:
    """Full-text search. Returns (page, fuzzy) where fuzzy=True means no exact
    matches were found and similar headlines are shown instead."""
    query = func.websearch_to_tsquery(literal_column(f"'{SEARCH_CONFIG}'::regconfig"), f.q)
    # Intros are short (<= ~700 chars), so show them whole with matches marked.
    marks = f'StartSel="{MARK_START}", StopSel="{MARK_END}", HighlightAll=true'
    regconfig = literal_column(f"'{SEARCH_CONFIG}'::regconfig")
    snippet = func.ts_headline(regconfig, Article.intro, query, marks).label("snippet")
    title_marked = func.ts_headline(regconfig, Article.title, query, marks).label("title_marked")
    stmt = _apply_filters(_article_query().add_columns(snippet, title_marked), f).where(
        Article.search_vector.op("@@")(query)
    )
    total = _count(session, stmt)
    if total:
        if f.sort == "newest":
            order = [Article.published_at.desc(), Article.id.desc()]
        elif f.sort == "oldest":
            order = [Article.published_at.asc(), Article.id.asc()]
        else:
            # Relevance, gently favouring recent stories: a year-old match
            # needs twice the text score to outrank today's.
            age_years = func.extract("epoch", func.now() - Article.published_at) / (86400 * 365.0)
            rank = func.ts_rank_cd(Article.search_vector, query, 32) / (1 + age_years)
            order = [rank.desc(), Article.published_at.desc()]
        stmt = stmt.order_by(*order).limit(per_page).offset((page - 1) * per_page)
        return Page(_rows(session, stmt), total, page, per_page), False

    # Nothing matched exactly: fall back to fuzzy headline matching (typos,
    # partial names). Uses the pg_trgm index on title.
    similarity = func.word_similarity(f.q, Article.title)
    stmt = _apply_filters(_article_query(), f).where(
        literal(f.q, type_=Text).op("<%")(Article.title)
    )
    stmt = stmt.order_by(similarity.desc(), Article.published_at.desc()).limit(per_page)
    rows = _rows(session, stmt)
    return Page(rows, len(rows), 1, per_page), bool(rows)


def facet_counts(session: Session, since: datetime | None = None) -> dict:
    """Article counts per category and per source (optionally since a date)."""
    cond = Article.published_at >= since if since else true()
    category = func.unnest(Article.categories).label("category")
    cat_rows = session.execute(
        select(category, func.count()).select_from(Article).where(cond).group_by(category)
    ).all()
    src_rows = session.execute(
        select(Source.slug, Source.name, func.count(Article.id))
        .select_from(Source)
        .outerjoin(Article, and_(Article.source_id == Source.id, cond))
        .where(Source.enabled.is_(True))
        .group_by(Source.slug, Source.name)
        .order_by(func.count(Article.id).desc(), Source.name)
    ).all()
    return {
        "categories": {slug: count for slug, count in cat_rows},
        "sources": [{"slug": s, "name": n, "count": c} for s, n, c in src_rows],
    }


def all_sources(session: Session) -> list[dict]:
    """Every source, including paused ones whose old stories are still searchable."""
    rows = session.execute(select(Source.slug, Source.name).order_by(Source.name)).all()
    return [{"slug": slug, "name": name} for slug, name in rows]


def last_updated(session: Session) -> datetime | None:
    return session.scalar(select(func.max(Source.last_run_at)))


def archive_months(session: Session) -> list[dict]:
    """[{year, months: [{month, count}]}] in Guam local time, newest first."""
    local = func.timezone("Pacific/Guam", Article.published_at)
    year = func.extract("year", local).label("y")
    month = func.extract("month", local).label("m")
    rows = session.execute(
        select(year, month, func.count()).group_by(year, month).order_by(year.desc(), month.desc())
    ).all()
    years: dict[int, list[dict]] = {}
    for y, m, count in rows:
        years.setdefault(int(y), []).append({"month": int(m), "count": count})
    return [{"year": y, "months": months} for y, months in years.items()]


def archive_days(session: Session, year: int, month: int) -> list[dict]:
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1)
    local_day = func.date(func.timezone("Pacific/Guam", Article.published_at)).label("d")
    rows = session.execute(
        select(local_day, func.count())
        .where(
            Article.published_at >= local_day_bounds(start)[0],
            Article.published_at < local_day_bounds(end)[0],
        )
        .group_by(local_day)
        .order_by(local_day.desc())
    ).all()
    return [{"day": d, "count": c} for d, c in rows]


def source_status(session: Session) -> list[dict]:
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    rows = session.execute(
        select(
            Source,
            func.count(Article.id).label("total"),
            func.count(Article.id).filter(Article.first_seen_at >= day_ago).label("day"),
            func.count(Article.id).filter(Article.first_seen_at >= week_ago).label("week"),
            func.max(Article.published_at).label("latest"),
        )
        .outerjoin(Article, Article.source_id == Source.id)
        .group_by(Source.id)
        .order_by(Source.enabled.desc(), Source.name)
    ).all()
    return [
        {"source": r[0], "total": r.total, "day": r.day, "week": r.week, "latest": r.latest}
        for r in rows
    ]


def recent_runs(session: Session, limit: int = 40) -> list[dict]:
    rows = session.execute(
        select(ScrapeRun, Source.name)
        .join(Source, Source.id == ScrapeRun.source_id)
        .order_by(ScrapeRun.started_at.desc())
        .limit(limit)
    ).all()
    return [{"run": run, "source_name": name} for run, name in rows]


def db_ok(session: Session) -> bool:
    return session.execute(text("SELECT 1")).scalar() == 1
