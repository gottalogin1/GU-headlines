"""TEMPORARY: try other ways to reach the sources that can't be collected.
Removed before the pull request."""

import json
import re
import subprocess
from urllib.parse import quote, urlencode

import feedparser
import trafilatura

from guheadlines.scraper.http import FetchError, HttpClient
from guheadlines.settings import load_settings

s = load_settings()
c = HttpClient(s.user_agent, per_host_delay=1.0, timeout=20)


def get(url, **kw):
    try:
        r = c.get(url, **kw)
        return r
    except FetchError as exc:
        print(f"  {url}\n    ERROR {exc}")
        return None


def status(url, show=0):
    r = get(url)
    if r is None:
        return None
    print(f"  {url}\n    {r.status} {r.content_type} {len(r.content):,}B final={r.url}")
    if r.content_type in ("application/rss+xml", "application/xml", "text/xml", "application/atom+xml") or r.content[:200].lstrip().startswith(b"<?xml"):
        f = feedparser.parse(r.content)
        if f.entries:
            print(f"    FEED {len(f.entries)} entries")
            for e in f.entries[:show or 4]:
                print(f"      - {e.get('published', e.get('updated', '?'))} | {e.get('title', '')[:90]} | {e.get('link', '')}")
        else:
            print("    xml head:", r.content[:600].decode("utf-8", "replace").replace("\n", " "))
    elif "json" in r.content_type:
        print("    json head:", r.content[:800].decode("utf-8", "replace"))
    return r


def text_of(url, limit=7000, use_curl=False):
    print(f"\n===== TEXT {url}")
    if use_curl:
        html = subprocess.run(["curl", "-sSL", "-m", "20", "-A", s.user_agent, url], capture_output=True).stdout
    else:
        r = get(url)
        if r is None:
            return
        html = r.content
    t = trafilatura.extract(html, include_links=False, favor_recall=True) or ""
    print(t[:limit])


print("\n######## DigitalNZ (RNZ metadata)")
base = "https://api.digitalnz.org/v3/records.json"
for params in (
    {"text": "guam", "per_page": 0, "facets": "content_partner,primary_collection,category", "facets_per_page": 15},
    {"text": "guam OR saipan OR tinian OR cnmi", "and[content_partner][]": "Radio New Zealand", "per_page": 15, "sort": "date", "direction": "desc"},
    {"and[content_partner][]": "Radio New Zealand", "per_page": 5, "sort": "date", "direction": "desc"},
):
    url = base + "?" + urlencode(params)
    r = get(url)
    if r is None:
        continue
    print(f"\n  {url}\n    {r.status} {len(r.content):,}B  ratelimit: " + ", ".join(f"{k}={v}" for k, v in r.headers.items() if "limit" in k.lower()))
    data = json.loads(r.content)
    srch = data.get("search", {})
    print("    result_count", srch.get("result_count"))
    if srch.get("facets"):
        print("    facets", json.dumps(srch["facets"])[:2500])
    for rec in srch.get("results", []):
        print("    ---")
        for k in ("id", "title", "date", "display_date", "created_at", "updated_at", "landing_url", "source_url", "object_url", "thumbnail_url", "large_thumbnail_url", "usage", "rights", "copyright", "category", "creator", "tag", "content_partner", "primary_collection"):
            v = rec.get(k)
            if v not in (None, [], ""):
                print(f"      {k}: {str(v)[:200]}")
        print(f"      description: {str(rec.get('description'))[:200]}")
        print(f"      fulltext len: {len(rec.get('fulltext') or '')}")
        print(f"      keys: {sorted(k for k, v in rec.items() if v not in (None, [], ''))}")

# thumbnails of the first RNZ records
r = get(base + "?" + urlencode({"text": "guam", "and[content_partner][]": "Radio New Zealand", "per_page": 3, "sort": "date", "direction": "desc"}))
if r is not None:
    for rec in json.loads(r.content)["search"]["results"]:
        for k in ("thumbnail_url", "large_thumbnail_url", "object_url"):
            if rec.get(k):
                print(f"\n  image {k}")
                status(rec[k])

text_of("https://digitalnz.org/about/terms-of-use/developer-api-terms-of-use", 12000)
text_of("https://digitalnz.org/about/terms-of-use", 5000)
text_of("https://digitalnz.org/developers/api-docs-v3/search-records-api-v3", 6000)
text_of("https://digitalnz.org/developers/getting-started", 4000)
text_of("https://digitalpasifik.org/terms-of-use", 5000)
text_of("https://www.rnz.co.nz/about/legal", 8000, use_curl=True)

print("\n######## RNZ other paths and partners")
for u in (
    "https://www.rnz.co.nz/robots.txt",
    "https://asiapacificreport.nz/feed/",
    "https://digitalpasifik.org/items/370396",
):
    status(u)

print("\n######## DVIDS: which unit pages carry Andersen stories")
r = get("https://www.dvidshub.net/news/563470/36th-wing-cat-supports-fema-led-typhoon-sinlaku-response-part-joint-task-force-micronesia")
if r is not None:
    print("   status", r.status)
    print("   unit links:", sorted(set(re.findall(rb'href="(/unit/[^"]+)"', r.content)))[:20])
for u in ("https://www.dvidshub.net/unit/36WG-PA", "https://www.dvidshub.net/unit/36thWing", "https://www.dvidshub.net/unit/36ABW", "https://www.dvidshub.net/unit/AAFB"):
    status(u)

print("\n######## Andersen (af.mil) other paths")
for u in (
    "https://www.andersen.af.mil/robots.txt",
    "https://www.andersen.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=43&max=10",
    "https://www.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=1&max=10",
    "https://www.pacaf.af.mil/News/",
):
    status(u)

print("\n######## Marianas Business Journal (Drupal) paths")
for u in (
    "https://mbjguam.com/sitemap.xml",
    "https://mbjguam.com/robots.txt",
    "https://mbjguam.com/taxonomy/term/1/feed",
    "https://mbjguam.com/taxonomy/term/2/feed",
    "https://mbjguam.com/taxonomy/term/3/feed",
    "https://mbjguam.com/guam/feed",
    "https://mbjguam.com/news/feed",
    "https://mbjguam.com/node/2149",
):
    status(u)

print("\n######## Guam Business Magazine (WordPress) paths")
for u in (
    "https://www.guambusinessmagazine.com/?feed=rss2",
    "https://www.guambusinessmagazine.com/feed/atom/",
    "https://www.guambusinessmagazine.com/wp-json/wp/v2/posts?per_page=2",
    "https://www.guambusinessmagazine.com/sitemap.xml",
    "https://www.guambusinessmagazine.com/wp-sitemap.xml",
    "https://guambusinessmagazine.com/feed/",
):
    status(u)

print("\n######## Guam Visitors Bureau paths")
for u in (
    "https://www.guamvisitorsbureau.com/sitemap.xml",
    "https://guamvisitorsbureau.com/news-releases",
    "https://www.guamvisitorsbureau.com/robots.txt",
    "https://www.visitguam.com/",
    "https://www.visitguam.com/feed/",
):
    status(u)
