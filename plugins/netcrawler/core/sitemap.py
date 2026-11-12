"""Sitemap discovery and parsing (XML, sitemap index, plain text, gzip).

Sitemaps are the cheapest possible crawl seed: a site that publishes one hands
over its whole URL inventory without a single link pass.
"""

from __future__ import annotations

import gzip
import io
import re
import zlib
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from lxml import etree

MAX_INDEX_DEPTH = 4
MAX_URLS_PER_SITEMAP = 50_000

_SM_RE = re.compile(r"<sitemap\b", re.I)
_URL_RE = re.compile(r"<url\b", re.I)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.I)


def _decompress(raw: bytes, content_type: str = "", url: str = "") -> bytes:
    if url.endswith(".gz") or "gzip" in (content_type or "").lower():
        for attempt in (gzip.decompress, zlib.decompress):
            try:
                return attempt(raw)
            except Exception:
                continue
    return raw


def looks_like_sitemap(raw: bytes) -> bool:
    head = raw[:512].lower()
    return b"<urlset" in head or b"<sitemapindex" in head or _SM_RE.search(
        raw[:2048].decode("utf-8", "replace")
    ) is not None


def parse_sitemap(raw: bytes, content_type: str = "", url: str = "") -> tuple[list[str], list[str]]:
    """Return ``(page_urls, child_sitemaps)`` from one sitemap document."""
    body = _decompress(raw or b"", content_type, url)
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return [], []

    # Plain-text sitemap: one URL per line.
    if not text.lower().startswith("<") and "\n" in text:
        urls = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("http")]
        return urls[:MAX_URLS_PER_SITEMAP], []

    try:
        parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
        root = etree.fromstring(body, parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        # Fall back to regex when the document is malformed but still lists <loc>.
        locs = _LOC_RE.findall(text)
        return (locs[:MAX_URLS_PER_SITEMAP], []) if locs else ([], [])

    if root is None:
        return [], []
    tag = etree.QName(root).localname.lower() if root.tag else ""
    locs = [(el.text or "").strip() for el in root.iter() if isinstance(el.tag, str)
            and etree.QName(el).localname.lower() == "loc" and el.text]
    locs = [l for l in locs if l.startswith("http")]

    if tag == "sitemapindex" or "<sitemapindex" in text[:400].lower():
        return [], locs
    return locs[:MAX_URLS_PER_SITEMAP], []


def candidate_sitemap_urls(origin: str) -> list[str]:
    """Conventional locations to try when robots.txt lists none."""
    return [
        f"{origin}/sitemap.xml",
        f"{origin}/sitemap_index.xml",
        f"{origin}/sitemap-index.xml",
        f"{origin}/sitemap/sitemap.xml",
        f"{origin}/wp-sitemap.xml",
        f"{origin}/sitemap.txt",
    ]


def origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))