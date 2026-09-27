from guheadlines.scraper.urls import normalize_url, same_site, url_variants


def test_normalize_strips_tracking_and_fragment():
    url = (
        "HTTPS://WWW.PostGuam.com/news/local/story/article_1.html?utm_source=fb&fbclid=abc#comments"
    )
    assert normalize_url(url) == "https://www.postguam.com/news/local/story/article_1.html"


def test_normalize_keeps_meaningful_query_sorted():
    assert normalize_url("https://x.com/a?b=2&a=1") == "https://x.com/a?a=1&b=2"


def test_normalize_relative_and_amp():
    assert (
        normalize_url("/story/amp/", base="https://www.kuam.com/news")
        == "https://www.kuam.com/story/"
    )
    assert normalize_url("https://kuam.com:443/a") == "https://kuam.com/a"


def test_normalize_rejects_non_http():
    assert normalize_url("mailto:news@example.com") is None
    assert normalize_url("javascript:void(0)") is None
    assert normalize_url("") is None


def test_url_variants_cover_www_scheme_and_slash():
    variants = url_variants("https://www.pncguam.com/story/")
    assert "http://pncguam.com/story" in variants
    assert "https://www.pncguam.com/story/" in variants
    assert len(variants) == 8


def test_same_site():
    assert same_site("https://subscribe.stripes.com/rss", "https://www.stripes.com/a")
    assert not same_site("https://example.com/", "https://www.stripes.com/a")
