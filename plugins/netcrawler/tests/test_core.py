"""Offline tests for the NetCrawler engine.

Run from the plugin directory:

    PYTHONPATH= python -m pytest tests -q

No network access is required: every case uses a fixture document.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import filters, gates, selectors, sitemap, urlutil  # noqa: E402
from core.extract import (content_hash, extract_document, hamming, simhash)  # noqa: E402
from core.fetch import api_variants  # noqa: E402
from core.httpfetch import FetchResult, classify_block, is_js_shell  # noqa: E402
from core.robots import RobotsRules  # noqa: E402
from core.store import Corpus  # noqa: E402

ARTICLE = """<!doctype html><html lang="en"><head>
<title>Deep Crawling Explained — Example Blog</title>
<meta name="description" content="How crawlers decide what to fetch next.">
<link rel="canonical" href="https://example.com/deep-crawling">
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
<script type="application/ld+json">{"@type":"Article","headline":"Deep Crawling"}</script>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"slug":"deep-crawling"}}}</script>
</head><body>
<nav class="site-nav"><a href="/">Home</a><a href="/blog">Blog</a></nav>
<article class="post-content">
  <h1>Deep Crawling Explained</h1>
  <p>Best-first crawling ranks every discovered URL against the query, so the crawler
     spends its budget on pages that are likely to answer it rather than on tag archives.</p>
  <p>Adaptive stopping watches marginal yield: when new pages stop introducing new terms,
     the useful part of the site has been exhausted and the crawl can end early.</p>
  <p>Politeness matters: honour robots.txt, respect crawl-delay, and never hammer a host
     with parallel requests it did not agree to serve.</p>
</article>
<aside class="related-posts"><a href="/other">Other post</a></aside>
<footer class="site-footer">© 2026 Example</footer>
</body></html>"""


# ------------------------------------------------------------------ url utils
def test_normalize_strips_tracking_and_fragment():
    url = "https://WWW.Example.com:443/a/b/?utm_source=x&id=7&fbclid=abc#frag"
    assert urlutil.normalize(url) == "https://www.example.com/a/b?id=7"


def test_url_key_folds_scheme_and_www():
    assert urlutil.url_key("http://www.example.com/a/") == urlutil.url_key("https://example.com/a")


def test_registrable_domain_handles_multi_suffix():
    assert urlutil.registrable_domain("news.bbc.co.uk") == "bbc.co.uk"
    assert urlutil.registrable_domain("www.example.com") == "example.com"


def test_scope_allows_subdomains_same_site():
    seed = "https://blog.example.com/post"
    assert urlutil.in_scope("https://docs.example.com/x", seed)
    assert not urlutil.in_scope("https://example.org/x", seed)


def test_assets_and_noise_are_rejected():
    assert urlutil.is_asset("https://example.com/logo.png")
    assert urlutil.is_noise("https://example.com/wp-admin/admin.php")
    assert urlutil.normalize("mailto:a@b.c") is None


# --------------------------------------------------------------------- robots
ROBOTS = """
User-agent: *
Disallow: /private/
Allow: /private/public.html
Crawl-delay: 2

Sitemap: https://example.com/sitemap.xml
"""


def test_robots_longest_match_wins():
    rules = RobotsRules.parse(ROBOTS)
    assert not rules.allowed("https://example.com/private/x", "netcrawler")
    assert rules.allowed("https://example.com/private/public.html", "netcrawler")
    assert rules.allowed("https://example.com/blog/x", "netcrawler")


def test_robots_crawl_delay_and_sitemap():
    rules = RobotsRules.parse(ROBOTS)
    assert rules.crawl_delay("netcrawler") == 2.0
    assert rules.sitemaps == ["https://example.com/sitemap.xml"]


def test_robots_wildcard_and_end_anchor():
    rules = RobotsRules.parse("User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp*\n")
    assert not rules.allowed("https://example.com/file.pdf", "netcrawler")
    assert rules.allowed("https://example.com/file.pdf?x=1", "netcrawler")
    assert not rules.allowed("https://example.com/tmp1", "netcrawler")


# -------------------------------------------------------------------- sitemap
SITEMAP_INDEX = b"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-a.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-b.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_URLSET = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc><lastmod>2026-01-01</lastmod></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""


def test_sitemap_index_yields_children():
    urls, children = sitemap.parse_sitemap(SITEMAP_INDEX, "application/xml", "https://example.com/sitemap.xml")
    assert urls == []
    assert children == ["https://example.com/sitemap-a.xml", "https://example.com/sitemap-b.xml"]


def test_sitemap_urlset_yields_pages():
    urls, children = sitemap.parse_sitemap(SITEMAP_URLSET, "application/xml", "https://example.com/sitemap.xml")
    assert urls == ["https://example.com/a", "https://example.com/b"]
    assert children == []


def test_sitemap_gzip_roundtrip():
    import gzip

    raw = gzip.compress(SITEMAP_URLSET)
    urls, _ = sitemap.parse_sitemap(raw, "application/gzip", "https://example.com/sitemap.xml.gz")
    assert len(urls) == 2


def test_plain_text_sitemap():
    urls, _ = sitemap.parse_sitemap(b"https://example.com/a\nhttps://example.com/b\n", "text/plain", "")
    assert urls == ["https://example.com/a", "https://example.com/b"]


# -------------------------------------------------------------------- filters
def test_prune_keeps_article_drops_navigation():
    text = filters.prune_to_markdown(ARTICLE)
    assert "Best-first crawling ranks" in text
    assert "Home" not in text.split("\n")[0]
    assert "© 2026 Example" not in text


def test_bm25_ranks_query_relevant_block_first():
    blocks = [
        "The weather today is mild with light winds across the region.",
        "Adaptive stopping watches marginal yield and ends the crawl early.",
    ]
    ranked = filters.bm25_rank(blocks, "adaptive crawl stopping")
    assert "Adaptive stopping" in ranked[0][1]


def test_fit_markdown_is_shorter_than_full():
    full = " ".join(ARTICLE.split())
    fit = filters.prune_to_markdown(ARTICLE, query="best-first crawling")
    assert fit and len(fit) < len(full)


# -------------------------------------------------------------------- extract
def test_extract_document_metadata_links_and_state():
    result = FetchResult(
        url="https://example.com/deep-crawling",
        final_url="https://example.com/deep-crawling",
        status=200, ok=True, text=ARTICLE,
        content=ARTICLE.encode(), content_type="text/html; charset=utf-8",
    )
    doc = extract_document(result)
    assert doc["title"].startswith("Deep Crawling")
    assert doc["lang"] == "en"
    assert doc["canonical"] == "https://example.com/deep-crawling"
    assert any(link["url"].endswith("/other") for link in doc["links"])
    assert "/feed.xml" in (doc["feeds"][0] if doc["feeds"] else "")
    assert doc["jsonld"] and doc["jsonld"][0]["@type"] == "Article"
    assert "next_data" in doc["spa_state"]
    assert doc["words"] > 40


def test_content_hash_ignores_whitespace_case():
    a = content_hash("Hello   World\n")
    b = content_hash("hello world")
    assert a == b


def test_simhash_detects_near_duplicates():
    text = " ".join(["the crawler ranks urls by relevance and depth"] * 8)
    other = text + " and then it stops"
    assert hamming(simhash(text), simhash(other)) < 8
    assert hamming(simhash(text), simhash("completely different words about cooking pasta")) > 8


# ------------------------------------------------------------------ selectors
LISTING = """<html><body><ul>
<li class="product"><h2><a href="/p/1">Widget</a></h2><span class="price">9.99</span>
  <span class="tag">red</span><span class="tag">small</span></li>
<li class="product"><h2><a href="/p/2">Gadget</a></h2><span class="price">19.50</span>
  <span class="tag">blue</span></li>
</ul></body></html>"""


def test_selector_records_with_attr_and_lists():
    spec = {
        "item": "li.product",
        "fields": {
            "name": "h2 a",
            "url": {"selector": "h2 a", "attr": "href"},
            "price": "span.price",
            "tags": {"selector": "span.tag", "all": True},
        },
    }
    records = selectors.extract_records(LISTING, spec)
    assert len(records) == 2
    assert records[0]["name"] == "Widget"
    assert records[0]["url"] == "/p/1"
    assert records[0]["price"] == "9.99"
    assert records[0]["tags"] == ["red", "small"]
    assert records[1]["tags"] == ["blue"]


def test_selector_csv_output():
    spec = {"item": "li.product", "fields": {"name": "h2 a", "price": "span.price"}}
    records = selectors.extract_records(LISTING, spec)
    csv_text = selectors.records_to_csv(records)
    assert "name,price" in csv_text
    assert "Widget" in csv_text


# ----------------------------------------------------------------------- gates
def test_refusal_patterns_block_leaving_and_declining():
    import re

    for text in ("I am under 18", "Exit", "No thanks", "Decline", "Subscribe", "Add to cart"):
        assert any(re.search(pattern, text, re.I) for pattern in gates.REFUSALS), text


def test_gate_pattern_ranks_affirmative_above_dismissal():
    import re

    def score(text: str) -> int:
        best = 0
        for pattern, points in gates.PATTERNS:
            if re.search(pattern, text, re.I):
                best = max(best, points)
        return best

    assert score("I am 18 or older") > score("Close")
    assert score("Enter") > score("Close")
    assert score("Accept all") >= gates.MIN_SCORE


def test_looks_gated_signals():
    assert gates.looks_gated("<div>Please verify your age</div>")
    assert not gates.looks_gated("<p>ordinary article text</p>")


# -------------------------------------------------------------------- fetch
def test_block_classification():
    assert classify_block(403, "<html><title>Just a moment...</title>") == "http-403"
    assert classify_block(429, "slow down") == "rate-limited"
    assert classify_block(200, "<html>fine article body here</html>") == ""
    assert classify_block(200, "please complete the security check") == "challenge-page"


def test_js_shell_detection():
    shell = '<html><body><div id="root"></div><script src="a.js"></script>' \
            '<script src="b.js"></script><script src="c.js"></script></body></html>'
    assert is_js_shell(shell)
    assert not is_js_shell(ARTICLE)


def test_api_variants():
    variants = api_variants("https://example.com/blog/post")
    assert "https://example.com/blog/post.json" in variants
    assert api_variants("https://example.com/") == []


# -------------------------------------------------------------------- corpus
@pytest.fixture()
def corpus(tmp_path):
    store = Corpus(tmp_path / "corpus.db")
    yield store
    store.close()


def _doc(url: str, text: str, title: str = "T") -> dict:
    return {
        "url": url, "final_url": url, "title": title, "text": text, "markdown": text,
        "lang": "en", "canonical": url, "links": [{"url": url + "/child", "text": "child"}],
        "method": "http", "provenance": "live", "status": 200, "words": len(text.split()),
        "content_hash": content_hash(text), "simhash": simhash(text), "kind": "html",
        "fit_markdown": "", "description": "", "jsonld": [], "spa_state": {},
    }


def test_corpus_upsert_and_fts_search(corpus):
    corpus.upsert_doc(_doc("https://example.com/a", "adaptive crawling stops early", "Crawling"))
    corpus.upsert_doc(_doc("https://example.com/b", "unrelated content about pasta", "Pasta"))
    rows = corpus.search("adaptive crawl")
    assert rows and rows[0]["url"].endswith("/a")
    assert "adaptive" in rows[0]["snippet"].lower()


def test_corpus_reports_duplicate_content(corpus):
    first = corpus.upsert_doc(_doc("https://example.com/a", "same text"))
    second = corpus.upsert_doc(_doc("https://example.com/b", "same text"))
    assert first["new"] is True
    assert second["duplicate"] is True


def test_corpus_stats_and_prune(corpus):
    for i in range(5):
        corpus.upsert_doc(_doc(f"https://example.com/{i}", f"document number {i} unique"))
    assert corpus.stats()["documents"] == 5
    removed = corpus.prune(3)
    assert removed == 2
    assert corpus.stats()["documents"] == 3


def test_corpus_run_lifecycle(corpus):
    corpus.save_run("run1", "https://example.com", {"max_pages": 10})
    corpus.update_run("run1", status="done", counts={"pages": 4})
    run = corpus.get_run("run1")
    assert run["status"] == "done"
    assert run["counts"]["pages"] == 4


def test_host_cookie_roundtrip(corpus):
    corpus.set_host_cookies("example.com", {"cf_clearance": "abc"})
    assert corpus.get_host_cookies("example.com")["cf_clearance"] == "abc"
    assert corpus.all_host_cookies()["example.com"]["cf_clearance"] == "abc"