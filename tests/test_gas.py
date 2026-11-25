"""Guam gas prices read from the local news (gas.py) and the sidebar panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from guheadlines import gas
from guheadlines.gas import read_prices

# Sentences the way Guam's newsrooms write them, and what is read from each.
CASES = [
    ("Guam gas prices rise to $5.49 for a gallon of regular grade fuel", {"regular": 5.49}),
    ("Gas prices jump 20 cents to $5.69 per gallon of regular grade", {"regular": 5.69}),
    ("Gas prices up again - $5.94 a gallon of regular grade fuel", {"regular": 5.94}),
    (
        "Record-high gas prices; regular grade fuel $6.04 a gallon, diesel 4 cents below $7",
        {"regular": 6.04},
    ),
    (
        "On Guam the price per gallon of regular, extra, and diesel fuel rose to $5.07, $5.46, "
        "and $5.74, respectively.",
        {"regular": 5.07, "premium": 5.46, "diesel": 5.74},
    ),
    (
        "Drivers were paying about $5.67 for regular and $7.34 for diesel, versus $4.59 on the "
        "U.S. mainland average.",
        {"regular": 5.67, "diesel": 7.34},
    ),
    (
        "Regular rose to $6.079 a gallon, up from $4.879 before the war. Diesel reached $8.49.",
        {"regular": 6.079, "diesel": 8.49},
    ),
    (
        "Premium is now $5.79 and diesel $6.49 at all three companies.",
        {"premium": 5.79, "diesel": 6.49},
    ),
    (
        "Shell, Mobil and 76 raised regular to $5.39, premium to $5.79 and diesel to $6.49.",
        {"regular": 5.39, "premium": 5.79, "diesel": 6.49},
    ),
    (
        "Prices at the pump: regular $5.39, premium $5.79, diesel $6.49.",
        {"regular": 5.39, "premium": 5.79, "diesel": 6.49},
    ),
    ("Regular now costs $5.39, down from $5.49 last week.", {"regular": 5.39}),
    ("Regular is now $5.39, a 10-cent drop from last week.", {"regular": 5.39}),
    ("Regular was $5.29 a year ago and is $5.59 today.", {"regular": 5.59}),
    ("Oil hits $95 a barrel. Regular unleaded on Guam stays at $5.39.", {"regular": 5.39}),
    # Other places, other times, and dollars that are not pump prices.
    ("Saipan drivers pay $5.20 for regular.", {}),
    ("Last week regular cost $5.29.", {}),
    ("A man paid an extra $5.00 at the pump.", {}),
    ("Gas station robbery in Dededo nets $500", {}),
    ("Regular hits $45.00 at one station", {}),
    ("", {}),
]


@pytest.mark.parametrize(("text", "prices"), CASES)
def test_read_prices(text, prices):
    assert read_prices(text) == prices


def test_price_text():
    story = _story(1, "x", 0)
    assert gas.Price("regular", 5.39, story).text == "$5.39"
    assert gas.Price("regular", 5.4, story).text == "$5.40"
    assert gas.Price("regular", 6.079, story).text == "$6.079"


NOW = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)


def _story(id: int, title: str, days_ago: int, intro: str | None = None) -> gas.Story:
    return gas.Story(
        id=id,
        url=f"https://www.postguam.com/news/local/{id}.html",
        title=title,
        intro=intro,
        published_at=NOW - timedelta(days=days_ago),
        source="The Guam Daily Post",
    )


def test_latest_prices_takes_the_newest_price_of_each_grade():
    stories = [
        _story(4, "Gas prices up again: $5.59 a gallon of regular", 1, "Diesel is $6.69."),
        _story(3, "Saipan regular hits $6.10", 2),  # not Guam
        _story(2, "Gas station robbery nets $500", 3),
        _story(1, "Regular $5.39, premium $5.79, diesel $6.49", 10),
    ]
    prices = gas.latest_prices(stories)
    assert [(p.grade, p.price, p.story.id) for p in prices] == [
        ("regular", 5.59, 4),
        ("premium", 5.79, 1),
        ("diesel", 6.69, 4),
    ]


def test_latest_prices_reads_the_headline_before_the_intro():
    story = _story(1, "Regular now $5.59", 0, "Regular was $5.39 for months; regular $5.49 in May.")
    assert [(p.grade, p.price) for p in gas.latest_prices([story])] == [("regular", 5.59)]
    assert gas.latest_prices([]) == []


def test_a_story_about_guam_and_saipan_counts():
    story = _story(1, "Guam and Saipan prices climb", 0, "On Guam, regular is $5.59.")
    assert gas.is_about_guam(story)
    assert not gas.is_about_guam(_story(2, "CNMI fuel prices climb", 0))


def test_gas_price_days_setting(monkeypatch):
    from guheadlines.settings import load_settings

    monkeypatch.delenv("GAS_PRICE_DAYS", raising=False)
    assert load_settings().gas_price_days == 45
    for value, days in (("0", 0), ("10", 10), ("-3", 0), ("9999", 365), ("x", 45)):
        monkeypatch.setenv("GAS_PRICE_DAYS", value)
        assert load_settings().gas_price_days == days, value


# ------------------------------------------------------------ with a database


@pytest.fixture()
def client(clean_db):
    from fastapi.testclient import TestClient

    from guheadlines.db import session_scope
    from guheadlines.models import Article, Source
    from guheadlines.web.app import app, sidebar_cache

    sidebar_cache._data.clear()
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        post = Source(
            slug="postguam", name="The Guam Daily Post", homepage="https://www.postguam.com/"
        )
        kuam = Source(slug="kuam", name="KUAM News", homepage="https://www.kuam.com/")
        session.add_all([post, kuam])
        session.flush()

        def story(source, slug, title, intro, days_ago, categories=("business",)):
            return Article(
                source_id=source.id,
                url=f"https://example.com/{slug}",
                title=title,
                intro=intro,
                categories=list(categories),
                published_at=now - timedelta(days=days_ago),
            )

        session.add_all(
            [
                story(kuam, "new", "Gas prices up again: $5.59 a gallon of regular", None, 1),
                story(post, "old", "Regular $5.39, premium $5.79, diesel $6.49", "Fuel <b>.", 5),
                # Muted (sources.yaml), too old, and not about gas: never read.
                story(post, "muted", "Guam Chamber says diesel is $9.99", None, 0),
                story(post, "ancient", "Premium $4.10 a gallon", None, 90),
                story(kuam, "other", "Village fiesta raises $5.00 a plate", None, 0),
            ]
        )
    yield TestClient(app)
    sidebar_cache._data.clear()


def test_sidebar_shows_gas_prices_with_their_stories(client):
    html = client.get("/").text
    panel = html.split('id="gas-title"', 1)[1].split("</section>", 1)[0]
    assert '<strong class="gas-price">$5.59</strong>' in panel
    assert '<strong class="gas-price">$5.79</strong>' in panel
    assert '<strong class="gas-price">$6.49</strong>' in panel
    assert "$9.99" not in panel and "$4.10" not in panel
    # Regular first, then premium and diesel; each linked to its story.
    assert panel.index(">Regular<") < panel.index(">Premium<") < panel.index(">Diesel<")
    assert 'href="https://example.com/new" target="_blank" rel="noopener">KUAM News ↗</a>' in panel
    assert 'href="https://example.com/old"' in panel
    # Every page has the sidebar.
    assert 'id="gas-title"' in client.get("/search").text


def test_one_story_gets_one_link(client):
    from guheadlines.db import session_scope
    from guheadlines.models import Article

    with session_scope() as session:
        session.query(Article).filter(Article.url == "https://example.com/new").delete()
    html = client.get("/").text
    panel = html.split('id="gas-title"', 1)[1].split("</section>", 1)[0]
    assert panel.count("<a ") == 1
    assert "Reported " in panel and "The Guam Daily Post ↗" in panel


def test_no_gas_prices_when_turned_off_or_none_found(client, monkeypatch):
    from test_web import _set

    from guheadlines.db import session_scope
    from guheadlines.models import Article
    from guheadlines.web.app import sidebar_cache

    _set(monkeypatch, gas_price_days=0)
    assert 'id="gas-title"' not in client.get("/").text
    monkeypatch.undo()
    with session_scope() as session:
        session.query(Article).delete()
    sidebar_cache._data.clear()
    assert 'id="gas-title"' not in client.get("/").text
