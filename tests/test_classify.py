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
    assert not mentions_guam(app_config, "Saipan council meets")
