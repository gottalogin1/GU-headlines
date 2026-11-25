"""Read-side queries for the website: listings, archive and full-text search."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import (
    Select,
    Text,
    and_,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
    text,
    true,
)
from sqlalchemy.orm import Session

from ..models import (
    SEARCH_CONFIG,
    Article,
    Event,
    EventCalendar,
    Job,
    JobSource,
    NewsletterEdition,
    PageViews,
    Referrers,
    ScrapeRun,
    Source,
    StoryClicks,
    WeatherReport,
)
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
    # Sources left out unless asked for by name (only_in_topic sources whose
    # stories belong on another topic's page).
    hide_sources: tuple[str, ...] = ()
    # Topics left out (separate topics, such as Obituaries, whose stories
    # belong only on their own page).
    hide_topics: tuple[str, ...] = ()
    # Words and phrases (muted: in sources.yaml) whose stories are never shown.
    mute: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.q or self.source or self.category or self.date_from or self.date_to)


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC instants for the start and end of a calendar day in Guam."""
    start = datetime.combine(day, time.min, tzinfo=GUAM_TZ).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _article_query() -> Select:
    return select(
        Article,
        Source.name.label("source_name"),
        Source.slug.label("source_slug"),
        Source.homepage.label("source_homepage"),
    ).join(Source, Source.id == Article.source_id)


def _hidden(slugs: tuple[str, ...] | list[str]):
    """Condition leaving out the articles of these sources."""
    return Article.source_id.not_in(select(Source.id).where(Source.slug.in_(slugs)))


def _muted(phrases: tuple[str, ...] | list[str]):
    """Condition leaving out the articles whose headline or intro contains one
    of these words or phrases (any upper/lower case)."""
    patterns = [
        "%" + p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for p in phrases
    ]
    intro = func.coalesce(Article.intro, "")
    return not_(or_(*[c for p in patterns for c in (Article.title.ilike(p), intro.ilike(p))]))


def _left_out(
    hide_sources: list[str] | None,
    hide_topics: list[str] | None,
    mute: tuple[str, ...] | list[str] = (),
) -> list:
    """Conditions leaving out the articles of these sources and these topics,
    and the muted ones."""
    conditions = []
    if hide_sources:
        conditions.append(_hidden(hide_sources))
    if hide_topics:
        conditions.append(not_(Article.categories.overlap(list(hide_topics))))
    if mute:
        conditions.append(_muted(mute))
    return conditions


def _apply_filters(stmt: Select, f: Filters) -> Select:
    if f.source:
        stmt = stmt.where(Source.slug == f.source)
    elif f.hide_sources:
        stmt = stmt.where(_hidden(f.hide_sources))
    if f.category:
        stmt = stmt.where(Article.categories.contains([f.category]))
    if f.hide_topics:
        stmt = stmt.where(*_left_out(None, f.hide_topics))
    if f.date_from:
        stmt = stmt.where(Article.published_at >= local_day_bounds(f.date_from)[0])
    if f.date_to:
        stmt = stmt.where(Article.published_at < local_day_bounds(f.date_to)[1])
    if f.mute:
        stmt = stmt.where(_muted(f.mute))
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
                "source_homepage": mapping["source_homepage"],
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


def facet_counts(
    session: Session, since: datetime | None = None, mute: tuple[str, ...] | list[str] = ()
) -> dict:
    """Article counts per category and per source (optionally since a date),
    without muted stories."""
    cond = and_(Article.published_at >= since if since else true(), *_left_out(None, None, mute))
    category = func.unnest(Article.categories).label("category")
    cat_rows = session.execute(
        select(category, func.count()).select_from(Article).where(cond).group_by(category)
    ).all()
    src_rows = session.execute(
        select(Source.slug, Source.name, Source.homepage, func.count(Article.id))
        .select_from(Source)
        .outerjoin(Article, and_(Article.source_id == Source.id, cond))
        .where(Source.enabled.is_(True))
        .group_by(Source.slug, Source.name, Source.homepage)
        .order_by(func.count(Article.id).desc(), Source.name)
    ).all()
    return {
        "categories": {slug: count for slug, count in cat_rows},
        "sources": [{"slug": s, "name": n, "homepage": h, "count": c} for s, n, h, c in src_rows],
    }


def all_sources(session: Session, keep: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """Sources to offer as a search filter: those in `keep`, plus any other
    (paused or removed) whose old stories are still searchable."""
    rows = session.execute(
        select(Source.slug, Source.name, func.count(Article.id))
        .outerjoin(Article, Article.source_id == Source.id)
        .group_by(Source.slug, Source.name)
        .order_by(Source.name)
    ).all()
    return [{"slug": slug, "name": name} for slug, name, count in rows if slug in keep or count]


def last_updated(session: Session) -> datetime | None:
    return session.scalar(select(func.max(Source.last_run_at)))


def weather_reports(session: Session) -> dict[str, tuple]:
    """{kind: (data, fetched_at)} for each saved weather source."""
    rows = session.execute(select(WeatherReport.kind, WeatherReport.data, WeatherReport.fetched_at))
    return {kind: (data, fetched_at) for kind, data, fetched_at in rows}


def archive_months(
    session: Session,
    hide_sources: list[str] | None = None,
    hide_topics: list[str] | None = None,
    mute: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    """[{year, months: [{month, count}]}] in Guam local time, newest first."""
    local = func.timezone("Pacific/Guam", Article.published_at)
    year = func.extract("year", local).label("y")
    month = func.extract("month", local).label("m")
    stmt = select(year, month, func.count()).where(*_left_out(hide_sources, hide_topics, mute))
    stmt = stmt.group_by(year, month)
    rows = session.execute(stmt.order_by(year.desc(), month.desc())).all()
    years: dict[int, list[dict]] = {}
    for y, m, count in rows:
        years.setdefault(int(y), []).append({"month": int(m), "count": count})
    return [{"year": y, "months": months} for y, months in years.items()]


def archive_days(
    session: Session,
    year: int,
    month: int,
    hide_sources: list[str] | None = None,
    hide_topics: list[str] | None = None,
    mute: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1)
    local_day = func.date(func.timezone("Pacific/Guam", Article.published_at)).label("d")
    stmt = select(local_day, func.count()).where(
        Article.published_at >= local_day_bounds(start)[0],
        Article.published_at < local_day_bounds(end)[0],
        *_left_out(hide_sources, hide_topics, mute),
    )
    rows = session.execute(stmt.group_by(local_day).order_by(local_day.desc())).all()
    return [{"day": d, "count": c} for d, c in rows]


def neighbor_days(
    session: Session,
    day: date,
    hide_sources: list[str] | None = None,
    hide_topics: list[str] | None = None,
    mute: tuple[str, ...] | list[str] = (),
) -> tuple[date | None, date | None]:
    """The nearest earlier and later Guam days that have stories (None if none),
    so the archive's day-to-day links never lead to empty pages."""
    start, end = local_day_bounds(day)
    hidden = _left_out(hide_sources, hide_topics, mute)
    before = session.scalar(
        select(func.max(Article.published_at)).where(Article.published_at < start, *hidden)
    )
    after = session.scalar(
        select(func.min(Article.published_at)).where(Article.published_at >= end, *hidden)
    )
    return (
        before.astimezone(GUAM_TZ).date() if before else None,
        after.astimezone(GUAM_TZ).date() if after else None,
    )


def sitemap_data(
    session: Session,
    hide_sources: list[str] | None = None,
    hide_topics: list[str] | None = None,
    mute: tuple[str, ...] | list[str] = (),
) -> dict:
    """Newest story time overall, per topic, per source and per archive day,
    for the sitemap's last-modified dates."""
    hidden = _left_out(hide_sources, hide_topics, mute)
    muted = _left_out(None, None, mute)
    latest = session.scalar(select(func.max(Article.published_at)).where(*hidden))
    category = func.unnest(Article.categories).label("category")
    categories = session.execute(
        select(category, func.max(Article.published_at)).where(*muted).group_by(category)
    ).all()
    sources = session.execute(
        select(Source.slug, func.max(Article.published_at))
        .join(Article, Article.source_id == Source.id)
        .where(*muted)
        .group_by(Source.slug)
    ).all()
    local_day = func.date(func.timezone("Pacific/Guam", Article.published_at)).label("d")
    days = session.execute(
        select(local_day, func.max(Article.published_at))
        .where(*hidden)
        .group_by(local_day)
        .order_by(local_day.desc())
    ).all()
    return {
        "latest": latest,
        "categories": dict(categories),
        "sources": dict(sources),
        "days": [(d, newest) for d, newest in days],
    }


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


# ------------------------------------------------------------------- jobs


@dataclass
class JobFilters:
    q: str = ""
    source: str | None = None


def _open_jobs(today: date):
    """Postings still listed on their board and not past their closing date."""
    return and_(
        Job.removed_at.is_(None),
        (Job.close_date.is_(None)) | (Job.close_date >= today),
        JobSource.enabled.is_(True),
    )


def list_jobs(session: Session, f: JobFilters, page: int, per_page: int, today: date) -> Page:
    stmt = (
        select(Job, JobSource.name.label("source_name"), JobSource.homepage.label("homepage"))
        .join(JobSource, JobSource.id == Job.source_id)
        .where(_open_jobs(today))
    )
    if f.source:
        stmt = stmt.where(JobSource.slug == f.source)
    if f.q:
        pattern = "%" + f.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        words = func.concat_ws(" ", Job.title, Job.employer, Job.location, Job.description)
        stmt = stmt.where(func.unaccent(words).ilike(func.unaccent(pattern)))
    total = _count(session, stmt)
    # Newest openings first; a posting without an open date counts from when it was first seen.
    newest = func.coalesce(Job.open_date, func.date(Job.first_seen_at))
    stmt = stmt.order_by(newest.desc(), Job.id.desc()).limit(per_page).offset((page - 1) * per_page)
    items = [
        {"job": row[0], "source_name": row.source_name, "source_homepage": row.homepage}
        for row in session.execute(stmt)
    ]
    return Page(items, total, page, per_page)


def job_boards(session: Session, today: date) -> list[dict]:
    """Each job board with its open postings, for the Jobs page's source list."""
    rows = session.execute(
        select(JobSource, func.count(Job.id).filter(_open_jobs(today)).label("open"))
        .outerjoin(Job, Job.source_id == JobSource.id)
        .where(JobSource.enabled.is_(True))
        .group_by(JobSource.id)
        .order_by(JobSource.name)
    ).all()
    return [{"board": r[0], "open": r.open} for r in rows]


# ----------------------------------------------------------------- events


@dataclass
class EventFilters:
    q: str = ""
    calendar: str | None = None
    # One Guam day: the events on it (picked on the month calendar).
    day: date | None = None


def _upcoming_events(since: datetime):
    """Events still listed on their calendar that end at or after `since`
    (the start of today, so today's earlier events stay on the page)."""
    return and_(
        Event.removed_at.is_(None),
        func.coalesce(Event.ends_at, Event.starts_at) >= since,
        EventCalendar.enabled.is_(True),
    )


def list_events(
    session: Session, f: EventFilters, page: int, per_page: int, since: datetime
) -> Page:
    stmt = (
        select(
            Event,
            EventCalendar.slug.label("slug"),
            EventCalendar.name.label("calendar_name"),
            EventCalendar.homepage.label("homepage"),
        )
        .join(EventCalendar, EventCalendar.id == Event.calendar_id)
        .where(_upcoming_events(since))
    )
    stmt = _filter_events(stmt, f)
    if f.day:
        stmt = stmt.where(_during(*local_day_bounds(f.day)))
    total = _count(session, stmt)
    stmt = (
        stmt.order_by(Event.starts_at, Event.title, Event.id)
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    items = [
        {
            "event": row[0],
            "calendar_slug": row.slug,
            "calendar_name": row.calendar_name,
            "calendar_homepage": row.homepage,
        }
        for row in session.execute(stmt)
    ]
    return Page(items, total, page, per_page)


def _during(start: datetime, end: datetime):
    """Events on at some time from `start` to `end`."""
    return and_(
        Event.starts_at < end,
        or_(Event.ends_at > start, and_(Event.ends_at.is_(None), Event.starts_at >= start)),
    )


def _filter_events(stmt: Select, f: EventFilters) -> Select:
    """The calendar and word filters of the Events page."""
    if f.calendar:
        stmt = stmt.where(EventCalendar.slug == f.calendar)
    if f.q:
        pattern = "%" + f.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        words = func.concat_ws(" ", Event.title, Event.location, Event.organizer, Event.description)
        stmt = stmt.where(func.unaccent(words).ilike(func.unaccent(pattern)))
    return stmt


def month_events(
    session: Session, f: EventFilters, start: datetime, end: datetime, since: datetime
) -> list[dict]:
    """Upcoming events from `start` to `end`, for the month calendar."""
    stmt = (
        select(Event, EventCalendar.slug.label("slug"), EventCalendar.name.label("calendar_name"))
        .join(EventCalendar, EventCalendar.id == Event.calendar_id)
        .where(_upcoming_events(since), _during(start, end))
        .order_by(Event.starts_at, Event.title, Event.id)
    )
    return [
        {"event": row[0], "calendar_slug": row.slug, "calendar_name": row.calendar_name}
        for row in session.execute(_filter_events(stmt, f))
    ]


def event_calendars(session: Session, since: datetime) -> list[dict]:
    """Each calendar with its upcoming events, for the Events page's calendar list."""
    rows = session.execute(
        select(
            EventCalendar,
            func.count(Event.id).filter(_upcoming_events(since)).label("upcoming"),
        )
        .outerjoin(Event, Event.calendar_id == EventCalendar.id)
        .where(EventCalendar.enabled.is_(True))
        .group_by(EventCalendar.id)
        .order_by(EventCalendar.name)
    ).all()
    return [{"calendar": r[0], "upcoming": r.upcoming} for r in rows]


# ------------------------------------------------------------ visitor stats


def visitor_stats(session: Session, since: date, top: int = 20) -> dict:
    """Page views and story clicks from the Guam day `since` to today, for the
    stats page (see web/stats.py)."""
    viewed = PageViews.day >= since
    clicked = StoryClicks.day >= since
    views = func.sum(PageViews.views).label("views")
    clicks = func.sum(StoryClicks.clicks).label("clicks")
    visits = func.sum(Referrers.visits).label("visits")
    by_story = (
        select(Article, Source.name.label("source_name"), clicks)
        .join(StoryClicks, StoryClicks.article_id == Article.id)
        .join(Source, Source.id == Article.source_id)
        .where(clicked)
        .group_by(Article.id, Source.name)
        .order_by(clicks.desc(), Article.id.desc())
        .limit(top)
    )
    by_source = (
        select(Source.name, clicks)
        .join(Article, Article.source_id == Source.id)
        .join(StoryClicks, StoryClicks.article_id == Article.id)
        .where(clicked)
        .group_by(Source.id, Source.name)
        .order_by(clicks.desc(), Source.name)
    )
    # A story with two topics counts for both.
    tagged = (
        select(func.unnest(Article.categories).label("topic"), StoryClicks.clicks)
        .join(Article, Article.id == StoryClicks.article_id)
        .where(clicked)
        .subquery()
    )
    return {
        "views_by_day": dict(
            session.execute(
                select(PageViews.day, views).where(viewed).group_by(PageViews.day)
            ).all()
        ),
        "clicks_by_day": dict(
            session.execute(
                select(StoryClicks.day, clicks).where(clicked).group_by(StoryClicks.day)
            ).all()
        ),
        "pages": session.execute(
            select(PageViews.path, views)
            .where(viewed)
            .group_by(PageViews.path)
            .order_by(views.desc(), PageViews.path)
        ).all(),
        "referrers": session.execute(
            select(Referrers.site, visits)
            .where(Referrers.day >= since)
            .group_by(Referrers.site)
            .order_by(visits.desc(), Referrers.site)
        ).all(),
        "stories": session.execute(by_story).all(),
        "sources": session.execute(by_source).all(),
        "topics": dict(
            session.execute(
                select(tagged.c.topic, func.sum(tagged.c.clicks)).group_by(tagged.c.topic)
            ).all()
        ),
    }


# ------------------------------------------------------------- newsletter


def top_stories(
    session: Session,
    since: datetime,
    until: datetime,
    limit: int,
    per_source: int,
    hide_sources: list[str] | None = None,
    hide_topics: list[str] | None = None,
    mute: tuple[str, ...] | list[str] = (),
) -> list[int]:
    """The ids of the stories for a newsletter edition (see newsletter.py):
    stories first found from `since` to `until`, the ones readers clicked most
    first, then those with a photo, then the newest; at most `per_source` from
    one news source."""
    clicks = (
        select(StoryClicks.article_id, func.sum(StoryClicks.clicks).label("clicks"))
        .group_by(StoryClicks.article_id)
        .subquery()
    )
    has_photo = func.coalesce(Article.image_path, Article.image_url, "") != ""
    stmt = (
        select(Article.id, Article.source_id)
        .outerjoin(clicks, clicks.c.article_id == Article.id)
        .where(
            Article.first_seen_at >= since,
            Article.first_seen_at < until,
            # An old story that was found late (from a site's archive) is not news.
            Article.published_at >= since - timedelta(days=1),
            *_left_out(hide_sources, hide_topics, mute),
        )
        .order_by(
            func.coalesce(clicks.c.clicks, 0).desc(),
            has_photo.desc(),
            Article.published_at.desc(),
            Article.id.desc(),
        )
    )
    picked: list[int] = []
    taken: Counter[int] = Counter()
    for article_id, source_id in session.execute(stmt):
        if taken[source_id] < per_source:
            taken[source_id] += 1
            picked.append(article_id)
            if len(picked) == limit:
                break
    return picked


def newsletter_edition(
    session: Session, mute: tuple[str, ...] | list[str] = ()
) -> tuple[datetime, list[dict]] | None:
    """The newest newsletter edition: its time, and its stories in their
    order (a story deleted or muted since then is left out). None before the
    first."""
    edition = session.scalar(
        select(NewsletterEdition).order_by(NewsletterEdition.formed_at.desc()).limit(1)
    )
    if edition is None:
        return None
    stmt = _article_query().where(Article.id.in_(edition.article_ids), *_left_out(None, None, mute))
    rows = _rows(session, stmt)
    by_id = {row["article"].id: row for row in rows}
    return edition.formed_at, [by_id[i] for i in edition.article_ids if i in by_id]
