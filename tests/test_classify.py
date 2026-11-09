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
