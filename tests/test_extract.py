from datetime import datetime, timezone

from conftest import fixture_bytes

from guheadlines.scraper.extract import build_intro, extract_article
from guheadlines.scraper.text import parse_datetime, truncate


def test_wordpress_article():
    url = "https://www.pncguam.com/govguam-agencies-brace-for-h-2b-worker-shortage/"
    data = extract_article(
        fixture_bytes("wordpress_article.html"), url, source_name="PNC News First"
    )
    assert data.is_article
    assert data.title == "GovGuam agencies brace for H-2B worker shortage"
    assert data.canonical_url == url
    assert data.image_url == "https://www.pncguam.com/wp-content/uploads/2026/09/h2b-workers.jpg"
    assert data.published_at == datetime(2026, 9, 26, 21, 15, tzinfo=timezone.utc)
    assert data.author == "Jolene Toves"
    assert data.section == "Local News"
    assert "H-2B" in data.keywords
    # The photo credit and caption are skipped; the story's first paragraphs are kept.
    assert data.intro.startswith("Government of Guam agencies are bracing")
    assert "Labor Director" in data.intro
    assert "Share this" not in data.intro
    assert "Photo courtesy" not in data.intro


def test_blox_article_with_subscriber_split():
    url = (
        "https://www.postguam.com/news/local/andersen-to-host-cope-north-exercise-with-japan-australia/"
        "article_0f2c3a9e-1111-2222-3333-444455556666.html"
    )
    data = extract_article(
        fixture_bytes("blox_article.html"), url, source_name="The Guam Daily Post"
    )
    assert data.title == "Andersen to host Cope North exercise with Japan, Australia"
    assert data.image_url.startswith("https://bloximages.newyork1.vip.townnews.com/")
    # Guam local time (UTC+10) converted to UTC.
    assert data.published_at == datetime(2026, 9, 26, 22, 30, tzinfo=timezone.utc)
    assert data.author == "Kenneth Quinata"
    assert data.intro.startswith("HAGÅTÑA — Hundreds of airmen")
    assert "Cope North" in data.keywords


def test_plain_article_falls_back_to_trafilatura():
    data = extract_article(
        fixture_bytes("plain_article.html"), "https://news.example.com/farmers-market"
    )
    assert data.title == "Island farmers market returns to Hagåtña"
    assert data.intro and data.intro.startswith("The weekly farmers market")
    assert "Copyright" not in data.intro
    assert data.image_url is None
    assert not data.is_article  # no article metadata: rejected when found on a listing page


def test_title_suffix_removed():
    html = b"<html><head><title>Big news today | postguam.com</title></head><body></body></html>"
    assert extract_article(html, "https://www.postguam.com/a").title == "Big news today"


def test_logo_images_are_ignored():
    html = b"""<html><head><meta property="og:image" content="https://x.com/wp-content/uploads/site-logo.png">
    <meta property="twitter:image" content="https://x.com/photo.jpg"></head><body></body></html>"""
    assert extract_article(html, "https://x.com/a").image_url == "https://x.com/photo.jpg"


def test_build_intro_limits():
    long_para = "Guam " + "word " * 300
    intro = build_intro([long_para])
    assert len(intro) <= 701
    assert intro.endswith("…")
    assert build_intro(["Too short.", "By John Smith"]) is None
    two = build_intro(
        [
            "First paragraph is reasonably long, with enough words to count.",
            "Second paragraph is also long enough to be included in the intro.",
        ]
    )
    assert two.count("\n\n") == 1


def test_parse_datetime_rules():
    assert parse_datetime("2026-09-27 08:00") == datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)
    assert parse_datetime("not a date") is None
    assert parse_datetime("1970-01-01T00:00:00Z") is None
    assert parse_datetime("2999-01-01T00:00:00Z") is None


def test_truncate_prefers_sentence_boundary():
    text = "First sentence here. Second sentence is much longer and goes on and on."
    assert truncate(text, 30) == "First sentence here."
    assert truncate(text, 45) == "First sentence here. Second sentence is much…"


def test_story_text_from_embedded_page_state():
    # KUAM-style page: story text only exists in a JSON blob rendered by JavaScript.
    model = {
        "title": "Guam hosting TB conference",
        "blocks": [
            {"type": "header", "props": {"text": "Guam hosting TB conference"}},
            {
                "type": "richtext",
                "props": {
                    "content": "<p>Guam will host the 2026 Tuberculosis Controllers Association "
                    "Conference next month, bringing health officials from across the Pacific.</p>"
                    "<p>The Department of Public Health says registration is open.</p>"
                },
            },
        ],
        "padding": "x" * 600,
    }
    import json

    html = f"""<html><head><meta property="og:type" content="article">
    <meta property="og:title" content="Guam hosting TB conference - KUAM">
    <meta property="og:description" content="Guam hosting TB conference"></head>
    <body><main><h1>Guam hosting TB conference</h1></main>
    <script>window.__PAGE_MODEL__ = {json.dumps(model)};</script></body></html>"""
    data = extract_article(html.encode(), "https://www.kuam.com/story/1/tb", source_name="KUAM")
    assert data.title == "Guam hosting TB conference"
    assert data.intro.startswith("Guam will host the 2026 Tuberculosis Controllers")


def test_published_date_from_page_text():
    html = b"""<html><head><meta property="og:type" content="article"></head><body>
    <div class="info">Story by Sgt. Jane Doe | Marine Corps Base Camp Blaz</div>
    <div>Date: 08.12.2026 | Posted: 08.12.2026 21:55 | News ID: 573626</div>
    <div class="news-body"><p>MARINE CORPS BASE CAMP BLAZ, Guam - Marines greeted students at
    Finegayan Elementary School on the first day of the school year.</p></div></body></html>"""
    data = extract_article(html, "https://www.dvidshub.net/news/573626/x")
    assert data.published_at.date().isoformat() in ("2026-08-11", "2026-08-12")
    assert data.intro.startswith("MARINE CORPS BASE CAMP BLAZ")
