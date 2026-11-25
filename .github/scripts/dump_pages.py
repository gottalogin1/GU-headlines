"""TEMP (bids research): fetch pages from GitHub's network and report what
each holds. Arguments are URLs; a leading '+' also dumps the page body
(gzip + base64) so it can be saved as a test fixture; a leading '>' also
follows the page's procurement links one level. Removed before the PR."""

import base64
import gzip
import re
import sys
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from guheadlines.scraper.http import FetchError, HttpClient
from guheadlines.settings import load_settings

TERMS = re.compile(r"\b(bids?|rfps?|ifbs?|procure\w*|solicit\w*|tenders?|purchas\w*|"
                   r"invitation|proposals?|quotations?|rfq)\b", re.I)
client = HttpClient(load_settings().user_agent, per_host_delay=1.0, timeout=25)
robots_seen = set()


def robots(url):
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin in robots_seen:
        return
    robots_seen.add(origin)
    try:
        r = client.get(origin + "/robots.txt", check_robots=False)
        body = r.content.decode("utf-8", "replace")
        print(f"--- robots.txt {origin}: {r.status} {len(body)} bytes")
        if r.status == 200:
            lines = [l for l in body.splitlines() if l.strip() and not l.startswith("#")]
            for l in lines[:40]:
                print("    " + l)
    except FetchError as exc:
        print(f"--- robots.txt {origin}: error {exc}")


def report(url, dump=False, follow=False, depth=0):
    pad = "  " * depth
    robots(url)
    print(f"{pad}=== {url}  (robots permits: {client.robots_permits(url)})")
    try:
        r = client.get(url, check_robots=False)
    except FetchError as exc:
        print(f"{pad}    ERROR {exc} status={exc.status}")
        return
    print(f"{pad}    {r.status} {r.content_type} {len(r.content)} bytes final={r.url}")
    if "html" not in r.content_type and "xml" not in r.content_type and "json" not in r.content_type:
        return
    text = r.content.decode("utf-8", "replace")
    if "json" in r.content_type:
        print(pad + "    " + text[:4000].replace("\n", " "))
    soup = BeautifulSoup(r.content, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    print(f"{pad}    title: {title[:150]}")
    gen = soup.find("meta", attrs={"name": "generator"})
    if gen:
        print(f"{pad}    generator: {gen.get('content')}")
    for sel in ("select option",):
        opts = [(o.get("value"), o.get_text(" ", strip=True)) for o in soup.select(sel)][:60]
        if opts:
            print(f"{pad}    options: {opts}")
    links = []
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        href = urljoin(r.url, a["href"])
        if TERMS.search(t) or TERMS.search(a["href"]) or "type_id" in href:
            links.append((t[:90], href))
    seen = set()
    uniq = [l for l in links if not (l[1] in seen or seen.add(l[1]))]
    print(f"{pad}    {len(uniq)} matching links:")
    for t, h in uniq[:70]:
        print(f"{pad}      - {t!r} -> {h}")
    if dump:
        data = base64.b64encode(gzip.compress(r.content)).decode()
        print(f"BEGIN-DUMP {url}")
        for i in range(0, len(data), 900):
            print("D " + data[i:i + 900])
        print(f"END-DUMP {url}")
    if follow and depth == 0:
        strong = [h for t, h in uniq if re.search(r"procure|bid|solicit|rfp|tender", t + h, re.I)
                  and urlsplit(h).netloc.split(":")[0] and not h.lower().endswith(".pdf")]
        for h in strong[:6]:
            report(h, follow=False, depth=1)


for arg in sys.argv[1:]:
    dump = follow = False
    while arg[:1] in "+>":
        dump |= arg[0] == "+"
        follow |= arg[0] == ">"
        arg = arg[1:]
    report(arg, dump=dump, follow=follow)
