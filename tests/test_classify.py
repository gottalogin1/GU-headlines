from guheadlines.config import parse_config
from guheadlines.scraper.classify import classify, mentions_guam


def test_military_story(app_config):
    cats = classify(
        app_config,
        title="Andersen to host Cope North exercise with Japan, Australia",
        intro="Hundreds of airmen will arrive next week.",
    )
    assert cats == ["military"]


def test_labor_and_business(app_config):
    cats = classify(
        app_config,
        title="H-2B worker shortage could delay construction contracts",
        intro="Contractors say the labor shortage is raising prices.",
    )
    assert "labor" in cats and "business" in cats
    assert "local" not in cats


def test_fallback_is_local(app_config):
    assert classify(app_config, title="A quiet Sunday", intro="Nothing much happened.") == ["local"]


def test_site_section_counts(app_config):
    cats = classify(
        app_config,
        title="Quarterly numbers released",
        url="https://www.postguam.com/business/local/quarterly-numbers/article_1.html",
    )
    assert "business" in cats


def test_topic_without_keywords_is_only_given_on_purpose():
    # A topic without keywords is only given by a site's categories: or
    # only_in_topic:. A news site's own section with its name, or the name in
    # a story's address, doesn't put news there.
    config = parse_config(
        {"sources": [{"slug": "a", "name": "A", "feeds": ["https://a.example/feed"]}]},
        {"categories": [{"slug": "community", "name": "Community"}]},
    )
    cats = classify(
        config,
        title="Humatak family loses home in fire",
        section="Community",
        url="https://guamnewsnetwork.com/news/community-rallies-for-humatak-family",
    )
    assert "community" not in cats
    assert classify(config, title="Best kelaguen?", only="community") == ["community"]
    assert "community" in classify(config, title="Ceremony held", forced=["community"])


def test_forced_categories(app_config):
    assert classify(app_config, title="Ceremony held", forced=["military"]) == ["military"]


def test_single_intro_mention_is_not_enough(app_config):
    cats = classify(
        app_config, title="Village fiesta draws crowds", intro="A bank sponsored the event."
    )
    assert cats == ["local"]


def test_mentions_guam(app_config):
    assert mentions_guam(app_config, "Governor visits Hagatna")
    assert mentions_guam(app_config, "Storm nears Guåhan")
    # Saipan-based (CNMI) stories are wanted too.
    assert mentions_guam(app_config, "Saipan council meets")
    assert not mentions_guam(app_config, "Palau council meets")
    # Naval Station Rota in Spain is not the island of Rota.
    assert not mentions_guam(app_config, "Sailors arrive at Naval Station Rota")


def test_cnmi_story_is_not_local(app_config):
    cats = classify(
        app_config,
        title="Lawmakers pass budget",
        intro="The House in Saipan passed the spending plan on Friday.",
    )
    assert cats == ["cnmi"]


def test_new_topics(app_config):
    assert "sports" in classify(app_config, title="Matao fall to Puerto Rico in FIFA Series")
    assert "sports" in classify(
        app_config,
        title="Sharks win in overtime",
        url="https://www.postguam.com/sports/local/sharks-win/article_1.html",
    )
    assert "food" in classify(app_config, title="New ramen restaurant opens in Tamuning")
    tourism = classify(app_config, title="Visitor arrivals climb as GVB courts Korea")
    assert "tourism" in tourism and "business" not in tourism


def test_obituaries_get_only_their_own_topic(app_config):
    # The site's own obituaries section, tag or address is enough.
    for found in (
        {"section": "Obituaries"},
        {"section": "Rosaries and Funerals"},
        {"keywords": ["Obituary"]},
        {"url": "https://www.guampdn.com/obituaries/juan-cruz/article_1.html"},
        {"url": "https://kanditnews.com/obituary-juan-cruz-santos/"},
    ):
        assert classify(app_config, title="Juan Cruz Santos", **found) == ["obituaries"], found
    # So is a headline that says so. The story gets no other topic, so it
    # stays off the other topics' pages.
    assert classify(app_config, title="Obituary: Navy veteran Jose Taitano") == ["obituaries"]
    assert classify(app_config, title="In Memoriam: Maria Cruz") == ["obituaries"]


def test_news_about_a_death_stays_news(app_config):
    cats = classify(
        app_config,
        title="Driver dies in Route 1 crash",
        intro="Police said the driver passed away at the scene on Tuesday.",
    )
    assert "obituaries" not in cats and "local" in cats
    # Sanjay's rule: news stories about a politician's death stay in the news,
    # even when they give the funeral details.
    for title, intro in (
        ("Former senator Bob Klitzkie passes away at 86", "He served six terms."),
        (
            "Veteran, educator, former senator Robert Klitzkie laid to rest",
            "Last respects were held at the cathedral, followed by a Mass of Christian Burial "
            "and interment at Guam Veterans Cemetery. He is survived by his wife.",
        ),
        ("Guam mourns the passing of former senator Carmen A. Kasperbauer", None),
    ):
        cats = classify(app_config, title=title, intro=intro)
        assert "obituaries" not in cats and "local" in cats, title
