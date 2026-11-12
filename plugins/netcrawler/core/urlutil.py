"""URL normalization, scope rules and cheap relevance signals.

One canonical form everywhere: the frontier, the visited set, the corpus and the
search merger all key on :func:`url_key`, so a page reached by four different
links is fetched once.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# Query parameters that never change the document. Stripping them is what stops a
# crawl from fetching the same page once per marketing campaign.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_name", "utm_reader", "utm_brand", "utm_social", "utm_social-type",
    "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "twclid", "ttclid",
    "yclid", "igshid", "igsh", "mc_cid", "mc_eid", "_ga", "_gl", "ref", "ref_src",
    "ref_url", "referrer", "source", "spm", "scm", "share", "share_id", "s_cid",
    "cmpid", "campaign", "mkt_tok", "vero_id", "vero_conv", "oly_anon_id",
    "oly_enc_id", "wickedid", "trk", "si", "feature", "s_kwcid", "epik", "srsltid",
})

# File types that are never crawl targets: binary, media, archives, fonts.
ASSET_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp",
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".m4a", ".wav",
    ".ogg", ".flac", ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".msi",
    ".dmg", ".pkg", ".deb", ".rpm", ".apk", ".iso", ".woff", ".woff2", ".ttf",
    ".eot", ".otf", ".js", ".mjs", ".css", ".map", ".wasm",
})

# Paths that are almost never useful content and can be excluded by default.
NOISE_PATH_RE = re.compile(
    r"/(?:wp-admin|wp-login|wp-json/cache|cart|checkout|login|signin|signup|register|"
    r"logout|account|admin|feed/(?!rss)|cdn-cgi)(?:/|$|\?)",
    re.I,
)

# Common multi-label public suffixes. Not a full PSL download: enough to keep
# same-site scope correct for the overwhelming majority of hosts.
_MULTI_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "net.nz", "org.nz",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "co.kr", "or.kr", "co.in",
    "net.in", "org.in", "co.za", "org.za", "com.br", "net.br", "org.br",
    "com.cn", "net.cn", "org.cn", "com.mx", "com.ar", "com.tr", "com.sg",
    "com.hk", "com.tw", "com.my", "co.id", "co.th", "co.il", "com.ua", "com.pl",
    "com.es", "com.pt", "com.ru", "co.ve", "com.co", "com.pe", "com.ec",
})


def _split(url: str):
    return urlsplit(url.strip())


def strip_www(host: str) -> str:
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def registrable_domain(host: str) -> str:
    """eTLD+1 for scope checks (``news.bbc.co.uk`` → ``bbc.co.uk``)."""
    host = strip_www(host.split(":")[0])
    if not host or host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last_two = ".".join(parts[-2:])
    if last_two in _MULTI_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last_two


def normalize(url: str, base: Optional[str] = None) -> Optional[str]:
    """Return the canonical absolute form, or ``None`` if unusable."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith(("mailto:", "javascript:", "tel:", "data:", "#")):
        return None
    if base:
        url = urljoin(base, url)
    try:
        parts = _split(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = parts.hostname
    if not host:
        return None
    host = host.lower().rstrip(".")
    # Drop default ports, keep non-default ones.
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    if parts.username:
        netloc = f"{parts.username}@{netloc}"

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    # Percent-encoding: keep as-is, but collapse the empty-path case.
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def url_key(url: str) -> str:
    """Dedupe key: canonical URL with the scheme and ``www.`` folded away."""
    norm = normalize(url)
    if not norm:
        return ""
    parts = _split(norm)
    host = strip_www(parts.hostname or "")
    path = parts.path or "/"
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def origin_of(url: str) -> str:
    parts = _split(url)
    return f"{parts.scheme}://{parts.netloc}"


def is_asset(url: str) -> bool:
    path = urlsplit(url).path.lower()
    for ext in ASSET_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


def is_noise(url: str) -> bool:
    return bool(NOISE_PATH_RE.search(urlsplit(url).path or "/"))


def same_site(url: str, seed_url: str) -> bool:
    a = registrable_domain(host_of(url))
    b = registrable_domain(host_of(seed_url))
    return bool(a) and a == b


def in_scope(
    url: str,
    seed_url: str,
    include: Optional[str] = None,
    exclude: Optional[str] = None,
    allow_subdomains: bool = True,
) -> bool:
    """Scope test: same registrable domain (or same host), plus regex overrides."""
    if not url:
        return False
    if include:
        try:
            if not re.search(include, url, re.I):
                return False
        except re.error:
            pass
    if exclude:
        try:
            if re.search(exclude, url, re.I):
                return False
        except re.error:
            pass
    if allow_subdomains:
        return same_site(url, seed_url)
    return host_of(url) == host_of(seed_url)


def tokens_from_url(url: str) -> list[str]:
    """Words from the path and query — the cheap relevance signal for scoring."""
    parts = _split(url)
    raw = f"{parts.path} {parts.query}".lower()
    return [t for t in re.split(r"[^a-z0-9]+", raw) if len(t) > 2]


def dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        key = url_key(url)
        if key and key not in seen:
            seen.add(key)
            out.append(normalize(url) or url)
    return out