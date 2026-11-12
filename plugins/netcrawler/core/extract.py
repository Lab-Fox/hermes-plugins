"""Content extraction: HTML → clean markdown + metadata + links.

Primary extractor is ``trafilatura`` — it wins the independent 2026 main-text
benchmarks (0.924 accuracy / 0.926 F-score overall, 0.955–0.958 on clean article
sets) and ships its own fallbacks. On top of the plain extraction we recover the
things a crawler needs and a reader does not:

* absolute, asset-free links for the frontier,
* JSON-LD and embedded SPA state (``__NEXT_DATA__``, ``__NUXT__``,
  ``__APOLLO_STATE__``, ``__INITIAL_STATE__``) so JS-only sites often need no
  browser at all,
* a content hash for exact dedupe and a SimHash for near-duplicate detection,
* PDF text when the "page" is a document.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from lxml import etree, html as lhtml

from .httpfetch import FetchResult
from .urlutil import is_asset, normalize, tokens_from_url

logger = logging.getLogger("netcrawler.extract")

MAX_LINKS = 3000
MAX_JSONLD_CHARS = 200_000
MAX_SPA_CHARS = 400_000

_SPA_PATTERNS = (
    ("next_data", re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I)),
    ("nuxt", re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)),
    ("apollo", re.compile(r"__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)),
    ("initial_state", re.compile(r"__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)),
    ("preloaded_state", re.compile(r"__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)),
)


# --------------------------------------------------------------------- hashes
def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm[:400_000].encode("utf-8", "replace")).hexdigest()


def simhash(text: str, bits: int = 64) -> int:
    """64-bit SimHash over word bigrams — Hamming distance < 4 means near-duplicate."""
    words = re.findall(r"[a-z0-9']{3,}", (text or "").lower())[:8000]
    if not words:
        return 0
    grams = [f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)] or words
    vector = [0] * bits
    for gram in grams:
        digest = int(hashlib.md5(gram.encode("utf-8", "replace")).hexdigest(), 16)
        for i in range(bits):
            vector[i] += 1 if (digest >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vector[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    mask = (1 << 64) - 1
    return bin((int(a) & mask) ^ (int(b) & mask)).count("1")


# ------------------------------------------------------------------ structure
def _parse(html: str):
    parser = lhtml.HTMLParser(recover=True, huge_tree=True)
    try:
        return lhtml.fromstring(html or "", parser=parser)
    except (etree.ParserError, ValueError):
        return None


def extract_links(doc, base_url: str) -> list[dict[str, str]]:
    """Absolute, in-scope-agnostic links with their anchor text."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    if doc is None:
        return out
    for el in doc.xpath("//a[@href]"):
        href = (el.get("href") or "").strip()
        if not href:
            continue
        absolute = normalize(href, base_url)
        if not absolute or is_asset(absolute):
            continue
        key = absolute.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        text = " ".join((el.text_content() or "").split())[:200]
        out.append({"url": absolute, "text": text})
        if len(out) >= MAX_LINKS:
            break
    return out


def extract_feeds(doc, base_url: str) -> list[str]:
    out: list[str] = []
    if doc is None:
        return out
    for el in doc.xpath("//link[@href]"):
        rel = (el.get("rel") or "").lower()
        if "alternate" in rel and any(t in (el.get("type") or "").lower()
                                      for t in ("rss", "atom", "xml", "feed")):
            absolute = normalize(el.get("href") or "", base_url)
            if absolute:
                out.append(absolute)
    return out


def extract_jsonld(doc) -> list[Any]:
    blocks: list[Any] = []
    if doc is None:
        return blocks
    total = 0
    for el in doc.xpath("//script[@type='application/ld+json']"):
        raw = (el.text or "").strip()
        if not raw:
            continue
        total += len(raw)
        if total > MAX_JSONLD_CHARS:
            break
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def extract_spa_state(html: str) -> dict[str, Any]:
    """Embedded application state — the data the page would render client-side."""
    found: dict[str, Any] = {}
    for name, pattern in _SPA_PATTERNS:
        match = pattern.search(html or "")
        if not match:
            continue
        raw = (match.group(1) or "").strip()
        if not raw or len(raw) > MAX_SPA_CHARS:
            continue
        try:
            found[name] = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            found[name] = {"_raw_truncated": raw[:2000]}
    return found


# --------------------------------------------------------------------- text
def _trafilatura(html: str, url: str) -> tuple[str, str, dict[str, Any]]:
    """Return ``(text, markdown, metadata)``; empty strings when it finds nothing."""
    try:
        import trafilatura
        from trafilatura.settings import use_config
    except ImportError:
        return "", "", {}

    cfg = use_config()
    cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "15")
    meta: dict[str, Any] = {}
    text = ""
    markdown = ""
    try:
        doc_meta = trafilatura.extract_metadata(html, default_url=url)
        if doc_meta:
            meta = {
                "title": getattr(doc_meta, "title", "") or "",
                "description": getattr(doc_meta, "description", "") or "",
                "sitename": getattr(doc_meta, "sitename", "") or "",
                "author": getattr(doc_meta, "author", "") or "",
                "date": getattr(doc_meta, "date", "") or "",
                "language": getattr(doc_meta, "language", "") or "",
                "canonical": getattr(doc_meta, "url", "") or "",
            }
    except Exception as exc:
        logger.debug("metadata extraction failed for %s: %s", url, exc)
    try:
        text = trafilatura.extract(
            html, url=url, config=cfg, include_comments=False, include_tables=True,
            include_links=False, include_images=False, favor_precision=False,
            favor_recall=False, deduplicate=True, output_format="txt",
        ) or ""
    except Exception as exc:
        logger.debug("trafilatura text failed for %s: %s", url, exc)
    try:
        markdown = trafilatura.extract(
            html, url=url, config=cfg, include_comments=False, include_tables=True,
            include_links=True, include_images=False, favor_precision=False,
            favor_recall=False, deduplicate=True, output_format="markdown",
        ) or ""
    except Exception as exc:
        logger.debug("trafilatura markdown failed for %s: %s", url, exc)
    return text, markdown, meta


def _justext(html: str) -> str:
    """Fallback: paragraph-level boilerplate removal."""
    try:
        import justext
    except ImportError:
        return ""
    try:
        paragraphs = justext.justext(html, justext.get_stoplist("English"))
        return "\n\n".join(p.text for p in paragraphs if not p.is_boilerplate)
    except Exception:
        return ""


def _plain_text(doc) -> str:
    if doc is None:
        return ""
    for bad in doc.xpath("//script|//style|//noscript|//template"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)
    return " ".join((doc.text_content() or "").split())


def _html_to_markdown(html: str) -> str:
    try:
        from markdownify import markdownify

        return markdownify(html, heading_style="ATX", strip=["script", "style", "noscript"])
    except Exception:
        return ""


# ---------------------------------------------------------------------- PDF
def pdf_text(raw: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
        from io import BytesIO

        reader = PdfReader(BytesIO(raw))
        pages = []
        for page in reader.pages[:200]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        return "\n\n".join(p for p in pages if p.strip()), len(reader.pages)
    except Exception as exc:
        logger.debug("pdf extraction failed: %s", exc)
        return "", 0


# --------------------------------------------------------------------- main
def extract_document(result: FetchResult, *, query: str = "") -> dict[str, Any]:
    """Turn a fetch result into the corpus document shape."""
    url = result.final_url or result.url
    ct = (result.content_type or "").lower()
    doc: dict[str, Any] = {
        "url": result.url,
        "final_url": url,
        "status": result.status,
        "method": result.method,
        "provenance": result.provenance,
        "gates_clicked": list(result.gates_clicked),
        "title": "",
        "description": "",
        "canonical": "",
        "lang": "",
        "author": "",
        "date": "",
        "sitename": "",
        "text": "",
        "markdown": "",
        "fit_markdown": "",
        "links": [],
        "feeds": [],
        "jsonld": [],
        "spa_state": {},
        "words": 0,
        "content_hash": "",
        "simhash": 0,
        "pdf_pages": 0,
        "kind": "html",
    }

    if "pdf" in ct or url.lower().endswith(".pdf"):
        text, pages = pdf_text(result.content)
        doc.update(kind="pdf", text=text, markdown=text, pdf_pages=pages)
        doc["words"] = len(text.split())
        doc["content_hash"] = content_hash(text)
        doc["simhash"] = simhash(text)
        doc["title"] = _pdf_title(text) or url.rsplit("/", 1)[-1]
        return doc

    if not result.is_html and not result.text.lstrip().startswith("<"):
        text = (result.text or "").strip()
        doc.update(kind="text", text=text, markdown=text,
                   title=url.rsplit("/", 1)[-1])
        doc["words"] = len(text.split())
        doc["content_hash"] = content_hash(text)
        doc["simhash"] = simhash(text)
        return doc

    html = result.text or ""
    tree = _parse(html)
    text, markdown, meta = _trafilatura(html, url)
    if len(text.split()) < 40:
        fallback = _justext(html)
        if len(fallback.split()) > len(text.split()):
            text = fallback
            markdown = markdown or _html_to_markdown(fallback)
    if len(text.split()) < 20:
        plain = _plain_text(tree)
        if len(plain.split()) > len(text.split()):
            text = plain
            markdown = markdown or plain

    title = meta.get("title") or ""
    if not title and tree is not None:
        titles = tree.xpath("//title/text()") or tree.xpath("//meta[@property='og:title']/@content")
        title = (titles[0].strip() if titles else "") or ""
    if not title and tree is not None:
        h1 = tree.xpath("//h1//text()")
        title = " ".join(t.strip() for t in h1[:3]).strip()[:200] if h1 else ""

    canonical = meta.get("canonical") or ""
    if not canonical and tree is not None:
        canon = tree.xpath("//link[@rel='canonical']/@href")
        canonical = normalize(canon[0], url) if canon else ""

    description = meta.get("description") or ""
    if not description and tree is not None:
        desc = tree.xpath("//meta[@name='description']/@content") or \
            tree.xpath("//meta[@property='og:description']/@content")
        description = (desc[0].strip() if desc else "")[:600]

    lang = meta.get("language") or ""
    if not lang and tree is not None:
        lang = (tree.xpath("/html/@lang") or [""])[0][:12]

    doc.update(
        title=title[:300],
        description=description,
        canonical=canonical or "",
        lang=lang,
        author=(meta.get("author") or "")[:200],
        date=(meta.get("date") or "")[:40],
        sitename=(meta.get("sitename") or "")[:120],
        text=text,
        markdown=markdown,
        links=extract_links(tree, url),
        feeds=extract_feeds(tree, url),
        jsonld=extract_jsonld(tree),
        spa_state=extract_spa_state(html),
    )
    doc["words"] = len(text.split())
    doc["content_hash"] = content_hash(text)
    doc["simhash"] = simhash(text)
    return doc


def _pdf_title(text: str) -> str:
    for line in (text or "").splitlines()[:8]:
        line = line.strip()
        if 8 <= len(line) <= 160:
            return line
    return ""


def relevance_terms(doc: dict[str, Any], query: str) -> set[str]:
    """Distinct query-ish terms present in a document, for adaptive stopping."""
    if not query:
        return set()
    vocab = set(re.findall(r"[a-z0-9]{4,}", (doc.get("text") or "").lower()))
    wanted = set(re.findall(r"[a-z0-9]{4,}", query.lower())) | set(tokens_from_url(query))
    return vocab & wanted