"""Query-focused content filters (Crawl4AI's best idea, implemented natively).

Two passes, both cheap and dependency-light:

1. **Pruning** — score every block by text density × (1 − link density) × tag
   weight, minus noise signals from tag/class/id (comment, sidebar, share,
   related, cookie...). This throws away chrome without needing a model.
2. **BM25** — rank the surviving blocks against the crawl query and keep the
   ones that actually answer it.

The output is ``fit_markdown``: the same page, stripped to what the crawl was
about, which is often 10× cheaper to put in a context window than the full
document. The full markdown is always kept alongside it.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

from lxml import etree, html as lhtml

BLOCK_TAGS = frozenset({
    "div", "section", "article", "main", "aside", "p", "li", "td", "blockquote",
    "pre", "figure", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
})

NOISE_RE = re.compile(
    r"(comment|sidebar|side-bar|footer|foot-|\bnav\b|navbar|menu|share|social|related|"
    r"promo|advert|\bads?\b|banner|cookie|consent|newsletter|breadcrumb|pagination|"
    r"widget|sponsor|subscribe|popup|modal|signup|login|toolbar|masthead|byline-social)",
    re.I,
)

GOOD_RE = re.compile(r"(article|content|main|post|entry|body|story|text|prose)", re.I)

MIN_BLOCK_SCORE = 14.0
MIN_BLOCK_CHARS = 80


def _tag_weight(tag: str) -> float:
    if tag in ("article", "main"):
        return 1.35
    if tag in ("h1", "h2", "h3", "h4"):
        return 1.15
    if tag in ("p", "blockquote", "pre"):
        return 1.10
    if tag in ("li", "td"):
        return 0.95
    return 0.85


def _block_score(el) -> float:
    text = " ".join((el.text_content() or "").split())
    chars = len(text)
    if chars < MIN_BLOCK_CHARS:
        return 0.0
    link_chars = sum(len(" ".join((a.text_content() or "").split())) for a in el.iter("a"))
    link_density = min(1.0, link_chars / max(chars, 1))
    if link_density > 0.55:
        return 0.0
    tag = (el.tag if isinstance(el.tag, str) else "").lower()
    weight = _tag_weight(tag)
    attrs = " ".join(filter(None, [el.get("class"), el.get("id"), el.get("role")]))
    if attrs and NOISE_RE.search(attrs):
        weight *= 0.25
    elif attrs and GOOD_RE.search(attrs):
        weight *= 1.20
    commas = text.count(",")
    score = chars * (1.0 - link_density) * weight + min(commas, 40) * 3.0
    return score


def prune_tree(html: str):
    """Parse and mark blocks to keep. Returns ``(root, [kept elements])``."""
    parser = lhtml.HTMLParser(recover=True, huge_tree=True)
    try:
        root = lhtml.fromstring(html or "", parser=parser)
    except (etree.ParserError, ValueError):
        return None, []
    if root is None:
        return None, []

    for bad in root.xpath("//script|//style|//noscript|//template|//svg|//iframe"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)

    scored: list[tuple[float, object]] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag.lower() not in BLOCK_TAGS:
            continue
        score = _block_score(el)
        if score >= MIN_BLOCK_SCORE:
            scored.append((score, el))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    kept: list[object] = []
    kept_ids: set[int] = set()
    for _score, el in scored:
        # Skip a block whose ancestor is already kept: the ancestor carries it.
        if any(id(anc) in kept_ids for anc in el.iterancestors()):
            continue
        kept.append(el)
        kept_ids.add(id(el))
    return root, kept


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']{3,}", (text or "").lower())]


def bm25_rank(blocks: list[str], query: str, k1: float = 1.5, b: float = 0.75) -> list[tuple[float, str]]:
    """Rank blocks against the query; returns ``(score, block)`` in document order."""
    if not query or not blocks:
        return [(1.0, block) for block in blocks]
    terms = set(_tokenize(query))
    if not terms:
        return [(1.0, block) for block in blocks]

    tokenized = [_tokenize(block) for block in blocks]
    lengths = [len(t) or 1 for t in tokenized]
    avg = sum(lengths) / max(len(lengths), 1)
    df: Counter = Counter()
    for toks in tokenized:
        for term in terms & set(toks):
            df[term] += 1
    total = len(blocks)
    idf = {term: math.log(1 + (total - df[term] + 0.5) / (df[term] + 0.5)) for term in terms}

    ranked: list[tuple[float, str]] = []
    for toks, block, length in zip(tokenized, blocks, lengths):
        counts = Counter(toks)
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
            score += idf[term] * norm
        ranked.append((score, block))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked


def prune_to_markdown(html: str, query: str = "", *, threshold_ratio: float = 0.15) -> str:
    """Pruned (+ optionally BM25-filtered) markdown of the page's real content."""
    root, kept = prune_tree(html)
    if root is None or not kept:
        return ""
    blocks = [" ".join((el.text_content() or "").split()) for el in kept]
    blocks = [b for b in blocks if len(b) >= MIN_BLOCK_CHARS]
    if not blocks:
        return ""

    if query:
        ranked = bm25_rank(blocks, query)
        top = max((score for score, _ in ranked), default=0.0)
        if top > 0:
            cutoff = top * threshold_ratio
            kept_blocks = [block for score, block in ranked if score >= cutoff]
            if kept_blocks:
                blocks = kept_blocks

    # Preserve original order: kept elements were sorted by score, so re-sort by
    # document position to keep the text readable.
    order = {id(el): i for i, el in enumerate(root.iter())}
    pairs = [(order.get(id(el), 0), " ".join((el.text_content() or "").split())) for el in kept]
    pairs.sort(key=lambda p: p[0])
    selected = set(blocks)
    out = [text for _pos, text in pairs if text in selected]
    return "\n\n".join(out)


def compact(text: str, max_chars: int = 8000) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.7)]
    tail = text[-int(max_chars * 0.25):]
    return f"{head}\n\n… [{len(text) - max_chars} chars elided] …\n\n{tail}"