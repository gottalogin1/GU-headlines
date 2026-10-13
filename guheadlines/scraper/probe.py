"""`guheadlines probe <url>`: describe a page or feed to help configure a source.

Prints what the scraper sees: HTTP details, advertised feeds, the CMS, the
shapes of the links on a section page (to write an article_pattern), and what
the extractor gets from an article page.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

import feedparser
from bs4 import BeautifulSoup

from .discover import parse_feed
from .extract import BODY_SELECTORS, _jsonld_objects, _types, extract_article
from .http import FetchError, HttpClient
from .urls import normalize_url, same_site

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def path_shape(url: str) -> str:
    """Generalize a URL path: /news/local/some-long-slug/article_<uuid>.html ->
    /news/local/{slug}/article_{uuid}.html"""
    parts = urlsplit(url)
    shaped = []
    for segment in parts.path.split("/"):
        if not segment:
            continue
        segment = _UUID_RE.sub("{uuid}", segment)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", segment):
            segment = "{date}"
        elif re.fullmatch(r"\d+", segment):
            is_year = len(segment) == 4 and segment.startswith(("19", "20"))
            segment = "{year}" if is_year else "{n}"
        elif segment.count("-") >= 3 and "{uuid}" not in segment:
            segment = "{slug}" + (re.search(r"\.\w+$", segment).group(0) if "." in segment else "")
        elif re.search(r"\d{5,}", segment):
            segment = re.sub(r"\d{5,}", "{id}", segment)
        shaped.append(segment)
    return "/" + "/".join(shaped) + ("/" if parts.path.endswith("/") and shaped else "")


def _cms(soup: BeautifulSoup, html: str) -> str:
    generator = soup.find("meta", attrs={"name": "generator"})
    hints = []
    if generator and generator.get("content"):
        hints.append(generator["content"])
    for marker, name in (
        ("wp-content", "WordPress"),
        ("townnews", "BLOX/TownNews"),
        ("tncms", "BLOX/TownNews"),
        ("drupal", "Drupal"),
        ("wixstatic", "Wix"),
        ("squarespace", "Squarespace"),
        ("dvidshub", "DVIDS"),
        ("DesktopModules", "DNN (af.mil)"),
        ("arc-cdn", "Arc XP"),
        ("gannett", "Gannett"),
    ):
        if marker.lower() in html.lower() and name not in hints:
            hints.append(name)
    return ", ".join(hints) or "unknown"


def probe(client: HttpClient, url: str, *, max_shapes: int = 15, out=print) -> None:
    out(f"### {url}")
    try:
        result = client.get(url)
    except FetchError as exc:
        out(f"  ERROR: {exc}")
        return
    headers = result.headers
    out(
        f"  status {result.status}  type {result.content_type or '?'}  "
        f"{len(result.content):,} bytes  final {result.url}"
    )
    flags = [h for h in ("etag", "last-modified", "cf-ray", "server") if h in headers]
    out("  headers: " + ", ".join(f"{h}={headers[h][:40]}" for h in flags))

    if result.content_type == "text/plain":
        lines = result.content.decode("utf-8", errors="replace").splitlines()
        for line in lines[:150]:
            out(f"  | {line}")
        if len(lines) > 150:
            out(f"  | ... {len(lines) - 150} more lines")
        return

    head = result.content[:500].lstrip().lower()
    if b"<rss" in head or b"<feed" in head or b"<rdf" in head or "xml" in result.content_type:
        parsed = feedparser.parse(result.content)
        items = parse_feed(result.content, result.url)
        out(f"  FEED ({parsed.version or 'unknown'}): {len(items)} entries")
        for item in items[:4]:
            out(
                f"   - {item.url}\n     title={item.title!r:.90} date={item.published_at} "
                f"image={'yes' if item.image_url else 'no'} summary={len(item.summary or '')}ch "
                f"tags={item.tags[:5]}"
            )
        return

    html = result.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(result.content, "lxml")
    out(f"  title: {soup.title.get_text(' ', strip=True)[:100] if soup.title else '-'}")
    out(f"  cms: {_cms(soup, html)}")
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        if "alternate" in rel and "xml" in (link.get("type") or ""):
            out(f"  feed advertised: {normalize_url(link['href'], base=result.url)}")
    ld_types = sorted({t for obj in _jsonld_objects(soup) for t in _types(obj)})
    og_type = soup.find("meta", property="og:type")
    out(
        f"  og:type={og_type.get('content') if og_type else '-'}  "
        f"json-ld types={', '.join(ld_types) or '-'}"
    )

    shapes: dict[str, list[str]] = defaultdict(list)
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        link = normalize_url(anchor["href"], base=result.url)
        if not link or link in seen or not same_site(link, result.url):
            continue
        seen.add(link)
        shape = f"{urlsplit(link).netloc}{path_shape(link)}"
        counts[shape] += 1
        if len(shapes[shape]) < 2:
            shapes[shape].append(link)
    out(f"  {len(seen)} same-site links; most common shapes:")
    for shape, count in counts.most_common(max_shapes):
        out(f"   {count:>4}  {shape}")
        for example in shapes[shape]:
            out(f"         e.g. {example}")

    bodies = []
    for css in BODY_SELECTORS:
        nodes = soup.select(css)
        if nodes:
            paragraphs = len(nodes[0].find_all("p"))
            bodies.append(f"{css}({len(nodes)}x, {paragraphs}p)")
    out(f"  body containers: {', '.join(bodies) or '-'}")

    data = extract_article(result.content, result.url)
    out(
        f"  extract: is_article={data.is_article} published={data.published_at} "
        f"author={data.author!r}"
    )
    out(f"    title={data.title!r:.120}")
    out(f"    canonical={data.canonical_url}")
    out(f"    image={data.image_url}")
    out(f"    section={data.section!r} keywords={data.keywords[:6]}")
    out(f"    intro={(data.intro or '')[:300]!r}")


_API_RE = re.compile(r"""["'`](https?://[^"'`\s]{6,200}|/api/[^"'`\s]{1,150})["'`]""")
# Relative API paths in a script that look like they list postings or items.
_PATH_RE = re.compile(
    r"""["'`]((?=[\w/{}$.?=&-]*(?:job|posting|vacanc|announce|opening|position))"""
    r"""/?[A-Za-z][\w-]*/[\w/{}$.?=&-]+)["'`]""",
    re.IGNORECASE,
)


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def outline(client: HttpClient, url: str, *, out=print) -> None:
    """Print a page's readable structure (headings, tables, repeated blocks,
    links), to write a parser for a listing page such as a jobs board. For a
    page built by JavaScript, list the addresses its scripts call."""
    out(f"### outline of {url}")
    try:
        result = client.get(url)
    except FetchError as exc:
        out(f"  ERROR: {exc}")
        return
    if "json" in result.content_type:
        out(f"  JSON: {result.content[:6000].decode('utf-8', errors='replace')}")
        return
    soup = BeautifulSoup(result.content, "lxml")
    for tag in soup(["style", "noscript", "svg"]):
        tag.decompose()
    scripts = [s.get("src") for s in soup.find_all("script") if s.get("src")]
    inline = " ".join(s.get_text() for s in soup.find_all("script") if not s.get("src"))
    for tag in soup(["script"]):
        tag.decompose()
    for chrome in soup.select("header, footer, nav"):
        chrome.decompose()
    text = soup.get_text(" ", strip=True)
    out(f"  {len(text):,} characters of text")

    for heading in soup.find_all(["h1", "h2", "h3", "h4"])[:30]:
        out(f"  <{heading.name}> {_short(heading.get_text(' '), 120)}")

    for number, table in enumerate(soup.find_all("table")[:6], 1):
        rows = table.find_all("tr")
        out(f"  TABLE {number}: {len(rows)} rows, class={table.get('class')} id={table.get('id')}")
        for row in rows[:6]:
            cells = [_short(c.get_text(" "), 50) for c in row.find_all(["th", "td"])]
            links = [a["href"][:120] for a in row.find_all("a", href=True)][:3]
            out(f"    | {' | '.join(cells)}" + (f"   links={links}" if links else ""))
        if rows:
            out(f"    first data row html: {_short(str(rows[min(1, len(rows) - 1)]), 1500)}")

    # Repeated blocks with a link inside: job cards, list items...
    blocks: Counter[str] = Counter()
    examples: dict[str, list] = defaultdict(list)
    for node in soup.find_all(["div", "li", "article", "section", "p"]):
        classes = node.get("class") or []
        if not classes or not node.find("a", href=True):
            continue
        key = f"{node.name}.{'.'.join(classes[:3])}"
        blocks[key] += 1
        if len(examples[key]) < 2:
            examples[key].append(node)
    for key, count in blocks.most_common(8):
        if count < 3:
            break
        out(f"  BLOCK {key} x{count}")
        for node in examples[key]:
            out(f"    text: {_short(node.get_text(' '), 300)}")
        out(f"    html: {_short(str(examples[key][0]), 1500)}")

    main = soup.find("main") or soup.body or soup
    anchors = main.find_all("a", href=True)
    out(f"  {len(anchors)} links in the page body; first 60:")
    for anchor in anchors[:60]:
        out(f"    - {_short(anchor.get_text(' '), 70)!r} -> {anchor['href'][:160]}")

    if len(text) < 1500:
        # Probably built by JavaScript: show what its scripts call.
        found = sorted(set(_API_RE.findall(inline)))[:40]
        for src in scripts[:4]:
            script_url = normalize_url(src, base=result.url)
            if not script_url or not same_site(script_url, result.url):
                continue
            try:
                body = client.get(script_url).content.decode("utf-8", errors="replace")
            except FetchError as exc:
                out(f"  script {script_url}: {exc}")
                continue
            hits = sorted(set(_API_RE.findall(body)))
            paths = sorted(set(_PATH_RE.findall(body)))
            out(f"  script {script_url}: {len(body):,} bytes, {len(hits)} addresses")
            found += hits[:80] + paths[:80]
        for address in found:
            out(f"    calls {address}")
