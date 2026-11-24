"""TEMPORARY: dry-run the Andersen (DVIDS 36th Wing) source without a database,
and look for press releases on visitguam.com. Removed before the pull request."""

import re
from datetime import datetime, timezone
from pathlib import Path

from guheadlines.config import load_config
from guheadlines.scraper.images import store_image
from guheadlines.scraper.pipeline import Scraper, Skip, SourceResult
from guheadlines.settings import load_settings

settings = load_settings()
config = load_config(settings.config_dir)
source = config.source("andersen")
print("andersen collected:", source.collected, source.listing_pages, source.article_patterns)
scraper = Scraper(settings, config)
scraper.prepare(source)
result = SourceResult(slug=source.slug)
candidates, _ = scraper.discover(source, result, use_cache=False)
print("errors:", result.errors)
print("discovered", len(candidates))
for cand in candidates:
    print("  -", cand.via, cand.url)
stored = 0
for cand in candidates[:10]:
    try:
        draft = scraper.build_draft(source, None, cand)
    except Skip as exc:
        print(f"\nSKIP {cand.url}: {exc}")
        continue
    image = None
    if draft.image_url:
        image = store_image(
            scraper.client,
            draft.image_url,
            referer=draft.url,
            media_dir=Path("/tmp/media"),
            when=datetime.now(timezone.utc),
            max_width=1200,
        )
    stored += 1
    print(f"\nWOULD STORE {draft.url}")
    print(f"  title: {draft.title}")
    print(f"  date: {draft.published_at}  categories: {draft.categories}  author: {draft.author}")
    print(f"  intro: {(draft.intro or '')[:200]}")
    print(f"  image: {draft.image_url} -> {image}")
print(f"\n{stored} of {min(10, len(candidates))} would be stored")

print("\n######## visitguam.com/media links about news or press")
r = scraper.client.get("https://www.visitguam.com/media/")
for href, text in re.findall(rb'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.content, re.S):
    t = re.sub(rb"<[^>]+>|\s+", b" ", text).strip()
    if re.search(rb"press|release|news|guamvisitorsbureau|media", href + t, re.I):
        print("  ", href.decode(errors="replace")[:160], "|", t.decode(errors="replace")[:100])
