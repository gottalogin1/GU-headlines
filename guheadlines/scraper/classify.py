"""Keyword-based topic tagging driven by config/categories.yaml."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from ..config import AppConfig, fold

TITLE_WEIGHT = 3
SITE_WEIGHT = 2  # the site's own section / tags / URL path
INTRO_WEIGHT = 1


def _url_words(url: str | None) -> str:
    if not url:
        return ""
    path = unquote(urlsplit(url).path)
    return path.replace("/", " ").replace("-", " ").replace("_", " ")


def classify(
    config: AppConfig,
    *,
    title: str | None,
    intro: str | None = None,
    section: str | None = None,
    keywords: str | list[str] | None = None,
    url: str | None = None,
    forced: list[str] | None = None,
    only: str | None = None,
) -> list[str]:
    """Return category slugs in config order. `only` (a source's only_in_topic)
    puts the story in that one topic, whatever it mentions."""
    if only:
        return [only]
    if isinstance(keywords, list):
        keywords = " ".join(keywords)
    title_text = fold(title or "")
    intro_text = fold(intro or "")
    section_text = fold(" ".join(filter(None, [section, _url_words(url)])))
    site_text = fold(" ".join(filter(None, [section_text, keywords])))

    matched = set(forced or [])
    for category in config.categories:
        if category.slug in matched:
            continue
        # The site filed it under e.g. /business/ or section "Military".
        if not category.fallback and re.search(
            rf"\b({re.escape(category.slug)}|{re.escape(fold(category.name))})\b", section_text
        ):
            matched.add(category.slug)
            continue
        score = 0
        for _, pattern in category.keywords:
            if pattern.search(title_text):
                score += TITLE_WEIGHT
            elif pattern.search(site_text):
                score += SITE_WEIGHT
            elif pattern.search(intro_text):
                score += INTRO_WEIGHT
            if score >= category.min_score:
                matched.add(category.slug)
                break

    if not matched:
        matched.update(c.slug for c in config.categories if c.fallback)
    return [c.slug for c in config.categories if c.slug in matched]


def mentions_guam(config: AppConfig, *texts: str | None) -> bool:
    haystack = fold(" ".join(t for t in texts if t))
    return any(pattern.search(haystack) for _, pattern in config.guam_keywords)
