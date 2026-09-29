import re

import pytest

from guheadlines.config import ConfigError, keyword_regex, parse_config


def test_shipped_config_is_valid(app_config):
    slugs = [s.slug for s in app_config.sources]
    assert len(slugs) == len(set(slugs))
    assert {c.slug for c in app_config.categories} >= {"local", "military", "business", "labor"}
    assert [c.slug for c in app_config.categories if c.fallback] == ["local"]


def test_keyword_regex_whole_words_and_wildcards():
    assert re.search(keyword_regex("army"), "the army band")
    assert not re.search(keyword_regex("army"), "armyworm outbreak")
    assert re.search(keyword_regex("employ*"), "employment rose")
    assert re.search(keyword_regex("service member*"), "two service-members were")
    assert re.search(keyword_regex("Hagåtña"), "hagatna mayor")
    assert re.search(keyword_regex("H-2B"), "new h-2b rules")


def test_default_excludes_apply(app_config):
    post = app_config.source("postguam")
    assert post.is_excluded("https://www.postguam.com/sports/football/article_1.html")
    assert post.is_excluded("https://www.postguam.com/news/world/article_1.html")
    assert not post.is_excluded("https://www.postguam.com/news/local/x/article_1.html")


def test_invalid_config_is_reported():
    with pytest.raises(ConfigError):
        parse_config({"sources": [{"slug": "Bad Slug", "name": "x", "feeds": ["https://a.b"]}]}, {})
    with pytest.raises(ConfigError):
        parse_config(
            {
                "sources": [
                    {"slug": "a", "name": "A", "feeds": ["https://a.b"], "categories": ["nope"]}
                ]
            },
            {"categories": []},
        )
    with pytest.raises(ConfigError):
        parse_config({"sources": [{"slug": "a", "name": "A"}]}, {})
    with pytest.raises(ConfigError, match="feed_only"):
        parse_config(
            {
                "sources": [
                    {"slug": "a", "name": "A", "listing_pages": ["https://a.b"], "feed_only": True}
                ]
            },
            {},
        )
