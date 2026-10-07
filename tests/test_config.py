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


def test_shipped_sources(app_config):
    assert app_config.source("pnc") is None  # PNC News First has closed
    assert [s.slug for s in app_config.uncollectable_sources] == ["mbj", "andersen", "gbm"]
    assert all(not s.collected for s in app_config.uncollectable_sources)
    reddit = app_config.source("reddit-guam")
    assert reddit.only_in_topic == "community" and reddit.collected
    assert app_config.hidden_sources() == ["reddit-guam"]  # front page, archive
    assert app_config.hidden_sources("community") == []


def test_only_in_topic_and_cannot_scrape_options():
    categories = {"categories": [{"slug": "community", "name": "Community"}]}
    config = parse_config(
        {
            "sources": [
                # A site that can't be scraped needs no feeds; `true` gets a reason.
                {"slug": "blocked", "name": "Blocked", "cannot_scrape": True},
                {
                    "slug": "forum",
                    "name": "Forum",
                    "feeds": ["https://f.example/rss"],
                    "only_in_topic": "community",
                },
            ]
        },
        categories,
    )
    blocked = config.source("blocked")
    assert blocked.cannot_scrape and not blocked.collected
    assert config.hidden_sources("labor") == ["forum"]
    with pytest.raises(ConfigError, match="only_in_topic"):
        parse_config(
            {
                "sources": [
                    {"slug": "a", "name": "A", "feeds": ["https://a.b"], "only_in_topic": "x"}
                ]
            },
            categories,
        )
