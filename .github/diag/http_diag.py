"""Temporary: compare how sites answer different HTTP clients (removed before merge)."""

import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx

from guheadlines.settings import load_settings

UA = load_settings().user_agent
OURS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "application/rss+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
KEYS = ("server", "content-type", "x-ratelimit-used", "x-ratelimit-remaining",
        "x-ratelimit-reset", "retry-after", "location", "x-served-by", "x-cache")


def show(label, status, headers, body):
    picked = {k: headers.get(k) for k in KEYS if headers.get(k)}
    print(f"  {label:28} {status}  {len(body):>7} bytes  {picked}")
    if status >= 300:
        print("     body:", body[:240])


def via_httpx(url, headers, http2=False):
    with httpx.Client(headers=headers, http2=http2, follow_redirects=True, timeout=20) as c:
        r = c.get(url)
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content, r.http_version


def via_curl(url, extra):
    out = subprocess.run(
        ["curl", "-sS", "-L", "-m", "20", "-A", UA, "-D", "/tmp/h.txt", "-o", "/tmp/b.bin", *extra, url],
        capture_output=True, text=True,
    )
    raw = open("/tmp/h.txt", encoding="latin-1").read().strip().split("\r\n\r\n")[-1].splitlines()
    status = int(raw[0].split()[1]) if raw else 0
    headers = {}
    for line in raw[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, open("/tmp/b.bin", "rb").read()


VARIANTS = [
    ("httpx h1 (scraper headers)", lambda u: via_httpx(u, OURS)),
    ("httpx h1 UA + Accept */*", lambda u: via_httpx(u, {"User-Agent": UA, "Accept": "*/*"})),
    ("httpx h1 no Accept-Language", lambda u: via_httpx(u, {k: v for k, v in OURS.items() if k != "Accept-Language"})),
    ("httpx h2 (scraper headers)", lambda u: via_httpx(u, OURS, http2=True)),
    ("curl h2 (default headers)", lambda u: via_curl(u, [])),
    ("curl h1.1 (default headers)", lambda u: via_curl(u, ["--http1.1"])),
    ("curl h2 (scraper headers)", lambda u: via_curl(u, ["-H", "Accept: " + OURS["Accept"], "-H", "Accept-Language: en-US,en;q=0.9"])),
]

import os

if os.environ.get("DIAG_ONE"):
    # One request per address, a minute apart (Reddit allows about one a minute).
    VARIANTS = VARIANTS[:1]

for url in sys.argv[1:]:
    print(f"### {url}")
    pause = 10 if "reddit" in urlsplit(url).netloc else 2
    if os.environ.get("DIAG_ONE") and "reddit" in urlsplit(url).netloc:
        time.sleep(65)
    for label, fn in VARIANTS:
        try:
            res = fn(url)
            version = res[3] if len(res) > 3 else ""
            show(f"{label} {version}".strip(), *res[:3])
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:28} ERROR {type(exc).__name__}: {exc}")
        time.sleep(pause)
    print()
