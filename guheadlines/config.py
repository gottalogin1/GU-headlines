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
    fallback: bool = False
    min_score: int = 3
    keywords: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)


@dataclass
class AppConfig:
    sources: list[SourceConfig]
    categories: list[CategoryConfig]
    guam_keywords: list[tuple[str, re.Pattern[str]]]

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

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
        categories.append(
            CategoryConfig(
                slug=slug,
                name=str(raw.get("name") or slug.title()),
                description=str(raw.get("description") or ""),
                fallback=bool(raw.get("fallback", False)),
                min_score=int(raw.get("min_score", 3)),
                keywords=compile_keywords([str(k) for k in _as_list(raw.get("keywords"))]),
            )
        )
    category_slugs = {c.slug for c in categories}

    defaults = sources_data.get("defaults") or {}
    default_excludes = [str(p) for p in _as_list(defaults.get("exclude_url_patterns"))]

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
                default_excludes + [str(p) for p in _as_list(raw.get("exclude_url_patterns"))],
                where,
            ),
            categories=forced,
            require_guam=bool(raw.get("require_guam", False)),
            body_selector=raw.get("body_selector"),
            request_delay=float(raw["request_delay"]) if raw.get("request_delay") else None,
            ignore_robots=bool(raw.get("ignore_robots", False)),
            max_age_days=int(raw.get("max_age_days", defaults.get("max_age_days", 30))),
            exclude_sections=[
                str(x)
                for x in _as_list(defaults.get("exclude_sections"))
                + _as_list(raw.get("exclude_sections"))
            ],
        )
        if source.enabled and not (source.feeds or source.listing_pages):
            raise ConfigError(f"{where}: needs at least one feed or listing page")
        sources.append(source)

    guam_keywords = compile_keywords([str(k) for k in _as_list(sources_data.get("guam_keywords"))])
    return AppConfig(sources=sources, categories=categories, guam_keywords=guam_keywords)


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
