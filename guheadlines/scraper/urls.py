"""URL normalization, so the same story is never stored or fetched twice."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# Query parameters that only track where a click came from.
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref_src",
    "cmpid",
    "ocid",
    "amp",
    "outputtype",
    "_ga",
    "s_cid",
}


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Return a canonical form of an http(s) URL, or None if it is not one.

    - resolves relative links against ``base``
    - lowercases scheme and host, drops default ports
    - drops fragments, tracking parameters (utm_* and friends) and AMP suffixes
    - sorts the remaining query parameters
    """
    if not url:
        return None
    url = url.strip()
    if base:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        return None
    port = parts.port if parts.port not in (None, 80, 443) else None
    netloc = f"{host}:{port}" if port else host

    path = parts.path or "/"
    for suffix in ("/amp/", "/amp"):
        if path.endswith(suffix) and len(path) > len(suffix):
            path = path[: -len(suffix)] + ("/" if suffix.endswith("/") else "")
            break
    while "//" in path:
        path = path.replace("//", "/")

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def url_variants(url: str) -> list[str]:
    """Equivalent spellings of a normalized URL (http/https, www, trailing slash)."""
    parts = urlsplit(url)
    schemes = {"http", "https"}
    hosts = {parts.netloc}
    if parts.netloc.startswith("www."):
        hosts.add(parts.netloc[4:])
    else:
        hosts.add("www." + parts.netloc)
    paths = {parts.path}
    if parts.path.endswith("/") and len(parts.path) > 1:
        paths.add(parts.path.rstrip("/"))
    elif not parts.path.endswith("/"):
        paths.add(parts.path + "/")
    return sorted(
        urlunsplit((sc, h, p, parts.query, "")) for sc in schemes for h in hosts for p in paths
    )


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def same_site(url: str, other: str) -> bool:
    """True when both URLs are on the same registrable-ish domain (ignores www/subdomains)."""

    def root(host: str) -> str:
        labels = host.split(".")
        return ".".join(labels[-2:]) if len(labels) >= 2 else host

    return root(host_of(url)) == root(host_of(other))
