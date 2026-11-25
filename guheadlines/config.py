"""Loading and validating config/sources.yaml and config/categories.yaml."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


def fold(text: str) -> str:
    """Lowercase and strip accents, for accent-insensitive matching."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def keyword_regex(keyword: str) -> str:
    """Turn a config keyword into a whole-word regex over folded text.

    'employ*' -> \\bemploy\\w*   'service member*' -> \\bservice[\\s-]+member\\w*
    """
    word = fold(keyword.strip())
    wildcard = word.endswith("*")
    word = word.rstrip("*")
    parts = [re.escape(p) for p in re.split(r"[\s]+", word) if p]
    body = r"[\s\-]+".join(parts)
    # \b only works next to word characters; keywords like "B-52" are fine,
    # but guard against leading/trailing punctuation.
    start = r"\b" if re.match(r"\w", word) else ""
    end = r"\w*" if wildcard else (r"\b" if re.search(r"\w$", word) else "")
    return start + body + end


def compile_keywords(keywords: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(kw, re.compile(keyword_regex(kw))) for kw in keywords if kw and kw.strip()]


@dataclass
class SourceConfig:
    slug: str
    name: str
    homepage: str | None = None
    enabled: bool = True
    feeds: list[str] = field(default_factory=list)
    listing_pages: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    autodiscover_feeds: bool = False
    article_patterns: list[re.Pattern[str]] = field(default_factory=list)
    exclude_patterns: list[re.Pattern[str]] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    require_guam: bool = False
    body_selector: str | None = None
    # Seconds between requests to this source's site (for sites that rate-limit).
    request_delay: float | None = None
    # Links found on listing pages that are older than this are not stored:
    # they are usually evergreen/promo pages rather than news.
    max_age_days: int = 30
    # Skip stories whose site section or feed category/tag is one of these.
    exclude_sections: list[str] = field(default_factory=list)
    # Fetch this site even where its robots.txt disallows crawlers.
    ignore_robots: bool = False
    # Build stories from the feed entries alone, without downloading the
    # article pages (for sites whose pages refuse bots).
    feed_only: bool = False
    # Put this source's stories in this one topic only, and show them only on
    # that topic's page (not on the front page, other topics or the archive).
    only_in_topic: str | None = None
    # The site's pages carry no date: accept pages that match article_pattern
    # without one, dated when first found.
    undated: bool = False
    # Why this site can't be collected (e.g. its firewall blocks us). Such a
    # site is never fetched; the website lists it separately with a link.
    cannot_scrape: str | None = None

    @property
    def collected(self) -> bool:
        """Whether the scraper reads this source."""
        return self.enabled and not self.cannot_scrape

    def is_excluded(self, url: str) -> bool:
        return any(p.search(url) for p in self.exclude_patterns)

    def excluded_section(self, *labels: str | None) -> str | None:
        """The first label (section, tag, feed category) this source excludes."""
        wanted = {fold(x).strip() for x in self.exclude_sections}
        for label in labels:
            if label and fold(label).strip() in wanted:
                return label
        return None

    def matches_article_pattern(self, url: str) -> bool:
        if not self.article_patterns:
            return True
        return any(p.search(url) for p in self.article_patterns)


@dataclass
class CategoryConfig:
    slug: str
    name: str
    description: str = ""
    # The topic page's title in browser tabs and search results
    # (page_title: in categories.yaml, else "<name> news").
    page_title: str = ""
    fallback: bool = False
    min_score: int = 3
    keywords: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    # A story in this topic gets only this topic, and is shown only on the
    # topic's page (not under Latest, the other topics or the archive).
    separate: bool = False
    # The topic's page shows the ad block, once AdSense is set up.
    show_ad: bool = False


@dataclass
class AppConfig:
    sources: list[SourceConfig]
    categories: list[CategoryConfig]
    guam_keywords: list[tuple[str, re.Pattern[str]]]
    # Words and phrases whose stories are never shown on the site or in the
    # newsletter feed (they are still collected): `muted` in sources.yaml.
    muted: tuple[str, ...] = ()

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    @property
    def uncollectable_sources(self) -> list[SourceConfig]:
        """Sites kept on the list that can't be collected (cannot_scrape)."""
        return [s for s in self.sources if s.enabled and s.cannot_scrape]

    def hidden_sources(self, topic: str | None = None) -> list[str]:
        """Slugs of sources whose stories belong only on another topic's page
        (only_in_topic), so a listing for `topic` (None: front page, archive,
        search without a topic) should leave them out."""
        return [s.slug for s in self.sources if s.only_in_topic and s.only_in_topic != topic]

    def separate_topics(self, topic: str | None = None) -> list[str]:
        """Slugs of separate topics (Obituaries), whose stories a listing for
        `topic` (None: front page, archive) should leave out."""
        return [c.slug for c in self.categories if c.separate and c.slug != topic]

    def source(self, slug: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.slug == slug), None)

    def category(self, slug: str) -> CategoryConfig | None:
        return next((c for c in self.categories if c.slug == slug), None)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _reason(value) -> str | None:
    """cannot_scrape: a reason, or just `true`."""
    if value is None or value is False:
        return None
    if value is True or not str(value).strip():
        return "This site turns away automated visitors."
    return str(value).strip()


def _compile(patterns: list[str], where: str) -> list[re.Pattern[str]]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error as exc:
            raise ConfigError(f"{where}: invalid regex {pattern!r}: {exc}") from exc
    return compiled


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def parse_config(sources_data: dict, categories_data: dict) -> AppConfig:
    sources_data = sources_data or {}
    categories_data = categories_data or {}

    categories: list[CategoryConfig] = []
    for raw in _as_list(categories_data.get("categories")):
        slug = str(raw.get("slug", "")).strip()
        if not _SLUG_RE.match(slug):
            raise ConfigError(f"categories.yaml: invalid category slug {slug!r}")
        name = str(raw.get("name") or slug.title())
        categories.append(
            CategoryConfig(
                slug=slug,
                name=name,
                description=str(raw.get("description") or ""),
                page_title=str(raw.get("page_title") or f"{name} news"),
                fallback=bool(raw.get("fallback", False)),
                min_score=int(raw.get("min_score", 3)),
                keywords=compile_keywords([str(k) for k in _as_list(raw.get("keywords"))]),
                separate=bool(raw.get("separate", False)),
                show_ad=bool(raw.get("show_ad", False)),
            )
        )
    category_slugs = {c.slug for c in categories}

    defaults = sources_data.get("defaults") or {}
    default_excludes = [str(p) for p in _as_list(defaults.get("exclude_url_patterns"))]
    default_sections = [str(x) for x in _as_list(defaults.get("exclude_sections"))]

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for raw in _as_list(sources_data.get("sources")):
        slug = str(raw.get("slug", "")).strip()
        where = f"sources.yaml [{slug or '?'}]"
        if not _SLUG_RE.match(slug):
            raise ConfigError(f"{where}: invalid or missing slug")
        if slug in seen:
            raise ConfigError(f"{where}: duplicate slug")
        seen.add(slug)
        if not raw.get("name"):
            raise ConfigError(f"{where}: missing name")
        forced = [str(c) for c in _as_list(raw.get("categories"))]
        unknown = [c for c in forced if c not in category_slugs]
        if unknown:
            raise ConfigError(f"{where}: unknown categories {unknown}")
        # allow_sections: collect sections the defaults skip (e.g. Sports). The
        # section names and the default URL patterns for /<section>/ are lifted.
        allowed = {fold(str(x)).strip() for x in _as_list(raw.get("allow_sections"))}
        url_excludes = [
            p
            for p in default_excludes
            if not any(re.search(p, f"/{a}/", re.IGNORECASE) for a in allowed)
        ]
        sections = [x for x in default_sections if fold(x).strip() not in allowed]
        source = SourceConfig(
            slug=slug,
            name=str(raw["name"]),
            homepage=raw.get("homepage"),
            enabled=bool(raw.get("enabled", True)),
            feeds=[str(u) for u in _as_list(raw.get("feeds"))],
            listing_pages=[str(u) for u in _as_list(raw.get("listing_pages"))],
            sitemaps=[str(u) for u in _as_list(raw.get("sitemaps"))],
            autodiscover_feeds=bool(raw.get("autodiscover_feeds", False)),
            article_patterns=_compile(
                [str(p) for p in _as_list(raw.get("article_pattern"))], where
            ),
            exclude_patterns=_compile(
                url_excludes + [str(p) for p in _as_list(raw.get("exclude_url_patterns"))],
                where,
            ),
            categories=forced,
            require_guam=bool(raw.get("require_guam", False)),
            body_selector=raw.get("body_selector"),
            request_delay=float(raw["request_delay"]) if raw.get("request_delay") else None,
            ignore_robots=bool(raw.get("ignore_robots", False)),
            feed_only=bool(raw.get("feed_only", False)),
            undated=bool(raw.get("undated", False)),
            only_in_topic=str(raw["only_in_topic"]) if raw.get("only_in_topic") else None,
            cannot_scrape=_reason(raw.get("cannot_scrape")),
            max_age_days=int(raw.get("max_age_days", defaults.get("max_age_days", 30))),
            exclude_sections=sections + [str(x) for x in _as_list(raw.get("exclude_sections"))],
        )
        if source.only_in_topic and source.only_in_topic not in category_slugs:
            raise ConfigError(f"{where}: unknown only_in_topic {source.only_in_topic!r}")
        if source.collected and not (source.feeds or source.listing_pages):
            raise ConfigError(f"{where}: needs at least one feed or listing page")
        if source.undated and not source.article_patterns:
            raise ConfigError(f"{where}: undated needs an article_pattern")
        if source.feed_only and not source.feeds:
            raise ConfigError(f"{where}: feed_only needs at least one feed")
        sources.append(source)

    guam_keywords = compile_keywords([str(k) for k in _as_list(sources_data.get("guam_keywords"))])
    muted = tuple(
        dict.fromkeys(str(m).strip() for m in _as_list(sources_data.get("muted")) if str(m).strip())
    )
    return AppConfig(
        sources=sources, categories=categories, guam_keywords=guam_keywords, muted=muted
    )


def load_config(config_dir: Path) -> AppConfig:
    sources_file = config_dir / "sources.yaml"
    categories_file = config_dir / "categories.yaml"
    try:
        sources_data = yaml.safe_load(sources_file.read_text(encoding="utf-8"))
        categories_data = yaml.safe_load(categories_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {exc.filename}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    return parse_config(sources_data, categories_data)


# ------------------------------------------------------------------- jobs


@dataclass
class JobSourceConfig:
    slug: str
    name: str
    # How the board is read: a parser name from guheadlines.jobs.parsers.
    parser: str
    homepage: str | None = None
    # The board's listing pages (the parser may follow its own "next" links).
    pages: list[str] = field(default_factory=list)
    # Used when a posting does not say who is hiring or where.
    employer: str | None = None
    location: str | None = None
    enabled: bool = True
    request_delay: float | None = None
    ignore_robots: bool = False
    # Why this board can't be collected; it is listed on the Jobs page with a link.
    cannot_scrape: str | None = None

    @property
    def collected(self) -> bool:
        return self.enabled and not self.cannot_scrape


def parse_jobs_config(data: dict, parsers: set[str] | frozenset[str] = frozenset()) -> list:
    """The job boards in jobs.yaml. `parsers`, when given, are the known parser names."""
    boards: list[JobSourceConfig] = []
    seen: set[str] = set()
    for raw in _as_list((data or {}).get("sources")):
        slug = str(raw.get("slug", "")).strip()
        where = f"jobs.yaml [{slug or '?'}]"
        if not _SLUG_RE.match(slug):
            raise ConfigError(f"{where}: invalid or missing slug")
        if slug in seen:
            raise ConfigError(f"{where}: duplicate slug")
        seen.add(slug)
        if not raw.get("name"):
            raise ConfigError(f"{where}: missing name")
        parser = str(raw.get("parser") or "").strip()
        if parsers and parser not in parsers:
            raise ConfigError(f"{where}: unknown parser {parser!r} (one of {sorted(parsers)})")
        board = JobSourceConfig(
            slug=slug,
            name=str(raw["name"]),
            parser=parser,
            homepage=raw.get("homepage"),
            pages=[str(u) for u in _as_list(raw.get("pages"))],
            employer=raw.get("employer"),
            location=raw.get("location"),
            enabled=bool(raw.get("enabled", True)),
            request_delay=float(raw["request_delay"]) if raw.get("request_delay") else None,
            ignore_robots=bool(raw.get("ignore_robots", False)),
            cannot_scrape=_reason(raw.get("cannot_scrape")),
        )
        if board.collected and not board.pages:
            raise ConfigError(f"{where}: needs at least one page")
        boards.append(board)
    return boards


def load_jobs_config(config_dir: Path) -> list[JobSourceConfig]:
    """config/jobs.yaml; no file means no job boards."""
    from .jobs.parsers import PARSERS

    path = config_dir / "jobs.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"jobs.yaml: invalid YAML: {exc}") from exc
    return parse_jobs_config(data, set(PARSERS))


# ----------------------------------------------------------------- events


@dataclass
class CalendarConfig:
    """One calendar in events.yaml: a place that lists Guam events.

    A calendar is read from one or more inputs, each a different kind of
    address (see config/events.yaml and docs/events.md)."""

    slug: str
    name: str
    homepage: str | None = None
    # A public Google Calendar's ID, read through Google's Calendar API
    # (needs GOOGLE_CALENDAR_API_KEY).
    google_calendar: list[str] = field(default_factory=list)
    # iCal (.ics) addresses, the format calendar apps share calendars in.
    ical: list[str] = field(default_factory=list)
    # RSS feeds of calendars built with Modern Campus (the University of Guam's).
    moderncampus: list[str] = field(default_factory=list)
    # Pages that list meeting agendas as PDF links under a dated heading
    # (the Guam Legislature's session agendas).
    agendas: list[str] = field(default_factory=list)
    # notices.guam.gov listing pages: the government's public meeting notices.
    notices: list[str] = field(default_factory=list)
    # Web pages that list events, read with the CSS selectors below.
    pages: list[str] = field(default_factory=list)
    item: str | None = None  # each event on a page
    title: str | None = None  # the event's name (and link) inside an item
    when: str | None = None  # where an item's date and time are written
    description: str | None = None  # an item's short description
    # Used when an event does not say where it is or who holds it.
    location: str | None = None
    organizer: str | None = None
    # Leave out events whose title or organizer matches one of these (regexes, any case).
    exclude: list[re.Pattern[str]] = field(default_factory=list)
    # Leave out events whose title, organizer, place or description matches one
    # of these: the file's own `exclude`, which applies to every calendar.
    exclude_everywhere: list[re.Pattern[str]] = field(default_factory=list)
    # Which events are shown on the month calendar at the top of the Events
    # page: True (all), False (none), those whose title matches one of
    # `main_events`, or all but those whose title matches one of `not_main`.
    main_events: bool | list[re.Pattern[str]] = True
    not_main: list[re.Pattern[str]] = field(default_factory=list)
    # The colour of its events on the month calendar (one of CALENDAR_COLORS).
    color: str | None = None
    enabled: bool = True
    request_delay: float | None = None
    ignore_robots: bool = False
    # Why this calendar can't be collected. It is never read, and not shown on the site.
    cannot_scrape: str | None = None

    @property
    def collected(self) -> bool:
        return self.enabled and not self.cannot_scrape

    @property
    def addresses(self) -> list[str]:
        """Every web address this calendar reads (for robots.txt and delays)."""
        return self.ical + self.moderncampus + self.agendas + self.notices + self.pages

    def excluded(self, *labels: str | None) -> bool:
        return any(p.search(label) for p in self.exclude for label in labels if label)

    def excluded_everywhere(self, *texts: str | None) -> bool:
        return any(p.search(text) for p in self.exclude_everywhere for text in texts if text)

    def is_main(self, title: str) -> bool:
        """Whether an event with this title goes on the month calendar."""
        if any(p.search(title) for p in self.not_main):
            return False
        if isinstance(self.main_events, bool):
            return self.main_events
        return any(p.search(title) for p in self.main_events)


# Colours a calendar's events can have on the month calendar (style.css has
# a muted shade of each for the light and the dark look).
CALENDAR_COLORS = ("green", "red", "blue")
_INPUTS = ("google_calendar", "ical", "moderncampus", "agendas", "notices", "pages")


def _main_events(value, where: str) -> tuple[bool | list[re.Pattern[str]], list]:
    """(main_events, not_main) from `main_events`: all, none, a list of
    patterns, or {except: [patterns]}."""
    if isinstance(value, dict):
        if set(value) != {"except"}:
            raise ConfigError(f"{where}: main_events can only have an except list")
        return True, _compile([str(p) for p in _as_list(value["except"])], where)
    if value is None or value is True or str(value).strip().lower() == "all":
        return True, []
    if value is False or str(value).strip().lower() == "none":
        return False, []
    patterns = [str(p) for p in _as_list(value) if str(p).strip()]
    if not patterns:
        raise ConfigError(f"{where}: main_events must be all, none or a list of patterns")
    return _compile(patterns, where), []


def parse_events_config(data: dict) -> list[CalendarConfig]:
    calendars: list[CalendarConfig] = []
    seen: set[str] = set()
    everywhere = _compile([str(p) for p in _as_list((data or {}).get("exclude"))], "events.yaml")
    for raw in _as_list((data or {}).get("calendars")):
        slug = str(raw.get("slug", "")).strip()
        where = f"events.yaml [{slug or '?'}]"
        if not _SLUG_RE.match(slug):
            raise ConfigError(f"{where}: invalid or missing slug")
        if slug in seen:
            raise ConfigError(f"{where}: duplicate slug")
        seen.add(slug)
        if not raw.get("name"):
            raise ConfigError(f"{where}: missing name")
        urls = {
            key: [str(u).strip() for u in _as_list(raw.get(key)) if str(u).strip()]
            for key in _INPUTS
        }
        main_events, not_main = _main_events(raw.get("main_events"), where)
        color = str(raw["color"]).strip().lower() if raw.get("color") else None
        if color and color not in CALENDAR_COLORS:
            raise ConfigError(f"{where}: color must be one of {', '.join(CALENDAR_COLORS)}")
        calendar = CalendarConfig(
            slug=slug,
            name=str(raw["name"]),
            homepage=raw.get("homepage"),
            **urls,
            item=raw.get("item"),
            title=raw.get("title"),
            when=raw.get("when"),
            description=raw.get("description"),
            location=raw.get("location"),
            organizer=raw.get("organizer"),
            exclude=_compile([str(p) for p in _as_list(raw.get("exclude"))], where),
            exclude_everywhere=everywhere,
            main_events=main_events,
            not_main=not_main,
            color=color,
            enabled=bool(raw.get("enabled", True)),
            request_delay=float(raw["request_delay"]) if raw.get("request_delay") else None,
            ignore_robots=bool(raw.get("ignore_robots", False)),
            cannot_scrape=_reason(raw.get("cannot_scrape")),
        )
        if calendar.collected and not any(urls.values()):
            raise ConfigError(
                f"{where}: needs at least one of {', '.join(_INPUTS[:-1])} or {_INPUTS[-1]}"
            )
        if calendar.pages and not calendar.item:
            raise ConfigError(f"{where}: pages needs an item selector")
        calendars.append(calendar)
    return calendars


def load_events_config(config_dir: Path) -> list[CalendarConfig]:
    """config/events.yaml; no file means no calendars."""
    path = config_dir / "events.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"events.yaml: invalid YAML: {exc}") from exc
    return parse_events_config(data)


@dataclass
class BidSourceConfig:
    """One source in bids.yaml: a government page that lists open bids and
    requests for proposals (see config/bids.yaml and docs/bids.md)."""

    slug: str
    name: str
    homepage: str | None = None
    # notices.guam.gov listing pages, narrowed to the procurement topics.
    notices: list[str] = field(default_factory=list)
    # Pages with a table of bids, one row each.
    table: list[str] = field(default_factory=list)
    # Read each open bid's own page for its due date when the table has none.
    details: bool = False
    # The agency asking for the bids, when a bid doesn't say (a table's rows never do).
    agency: str | None = None
    # How many days after it was posted a bid without a due date stays listed.
    undated_days: int = 30
    # Leave out bids whose title, number or agency matches one of these (regexes, any case).
    exclude: list[re.Pattern[str]] = field(default_factory=list)
    # The file's own `exclude`, which applies to every source (and to descriptions too).
    exclude_everywhere: list[re.Pattern[str]] = field(default_factory=list)
    enabled: bool = True
    request_delay: float | None = None
    ignore_robots: bool = False
    # Why this source can't be collected. It is never read, and not shown on the site.
    cannot_scrape: str | None = None

    @property
    def collected(self) -> bool:
        return self.enabled and not self.cannot_scrape

    @property
    def addresses(self) -> list[str]:
        return self.notices + self.table

    def excluded(self, *labels: str | None) -> bool:
        return any(p.search(label) for p in self.exclude for label in labels if label)

    def excluded_everywhere(self, *texts: str | None) -> bool:
        return any(p.search(text) for p in self.exclude_everywhere for text in texts if text)


_BID_INPUTS = ("notices", "table")


def parse_bids_config(data: dict) -> list[BidSourceConfig]:
    sources: list[BidSourceConfig] = []
    seen: set[str] = set()
    everywhere = _compile([str(p) for p in _as_list((data or {}).get("exclude"))], "bids.yaml")
    for raw in _as_list((data or {}).get("sources")):
        slug = str(raw.get("slug", "")).strip()
        where = f"bids.yaml [{slug or '?'}]"
        if not _SLUG_RE.match(slug):
            raise ConfigError(f"{where}: invalid or missing slug")
        if slug in seen:
            raise ConfigError(f"{where}: duplicate slug")
        seen.add(slug)
        if not raw.get("name"):
            raise ConfigError(f"{where}: missing name")
        urls = {
            key: [str(u).strip() for u in _as_list(raw.get(key)) if str(u).strip()]
            for key in _BID_INPUTS
        }
        try:
            undated_days = int(raw.get("undated_days", 30))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: undated_days must be a whole number") from exc
        if undated_days < 1:
            raise ConfigError(f"{where}: undated_days must be at least 1")
        source = BidSourceConfig(
            slug=slug,
            name=str(raw["name"]),
            homepage=raw.get("homepage"),
            **urls,
            details=bool(raw.get("details", False)),
            agency=str(raw["agency"]).strip() if raw.get("agency") else None,
            undated_days=undated_days,
            exclude=_compile([str(p) for p in _as_list(raw.get("exclude"))], where),
            exclude_everywhere=everywhere,
            enabled=bool(raw.get("enabled", True)),
            request_delay=float(raw["request_delay"]) if raw.get("request_delay") else None,
            ignore_robots=bool(raw.get("ignore_robots", False)),
            cannot_scrape=_reason(raw.get("cannot_scrape")),
        )
        if source.collected and not any(urls.values()):
            raise ConfigError(f"{where}: needs notices or table")
        sources.append(source)
    return sources


def load_bids_config(config_dir: Path) -> list[BidSourceConfig]:
    """config/bids.yaml; no file means no sources."""
    path = config_dir / "bids.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"bids.yaml: invalid YAML: {exc}") from exc
    return parse_bids_config(data)
