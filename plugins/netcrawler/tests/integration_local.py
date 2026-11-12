"""Live integration test — real network stack, local fixture site.

Proves the three claims that matter, end to end, without hitting anyone else's
server:

1. the HTTP rung fetches a normal page;
2. the browser rung clears an age gate whose content does not exist in the
   static HTML (the operator's exact failure case);
3. a crawl obeys robots.txt, seeds from the sitemap, stores documents and finds
   them again through FTS.

Run:  PYTHONPATH= python tests/integration_local.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Settings
from core.fetch import Ladder
from core.jobs import CrawlOptions, start_crawl
from core.store import Corpus

SITE: dict[str, str] = {
    "index.html": """<!doctype html><html><head><title>Polymesh Index</title></head><body>
<h1>Polymesh Index</h1>
<p>An index of notes about the polymesh storage format and its tooling.</p>
<a href="/a1.html">Polymesh shard layout</a>
<a href="/a2.html">Polymesh compaction</a>
<a href="/gated.html">Members area</a>
<a href="/private/secret.html">Private</a>
</body></html>""",
    "a1.html": """<!doctype html><html><head><title>Polymesh shard layout</title>
<meta name="description" content="How the polymesh shard layout works."></head><body>
<h1>Polymesh shard layout</h1>
<p>The polymesh storage format splits every record into shards keyed by content hash.</p>
<p>Each shard carries a manifest describing its extents, so a reader can fetch only the
   shards it needs. Manifest compaction happens lazily, and the indexer rewrites stale
   manifests when the shard count crosses a threshold.</p>
<a href="/a2.html">Next: compaction</a>
</body></html>""",
    "a2.html": """<!doctype html><html><head><title>Polymesh compaction</title></head><body>
<h1>Polymesh compaction</h1>
<p>Compaction merges adjacent shards when their combined extents fit a single block.</p>
<p>The polymesh indexer schedules compaction in the background and never blocks a write.</p>
</body></html>""",
    # The gate: the article text exists ONLY inside a JS string. A static fetch
    # cannot see it, exactly like a real age-gated page.
    "gated.html": """<!doctype html><html><head><title>Members area — verify your age</title></head><body>
<div id="gate" role="dialog" style="position:fixed;inset:0;background:#111;color:#fff;z-index:9999">
  <p>You must be 18 or older to enter this site.</p>
  <button id="yes">I am 18 or older</button>
  <button id="no">Exit</button>
</div>
<div id="content" style="display:none"></div>
<script>
  var ARTICLE = "Restricted notes: the polymesh indexer keeps a secondary ledger of shard " +
    "manifests so recovery after a partial write never needs a full scan. Operators rotate " +
    "the ledger weekly and verify shard checksums during compaction.";
  document.getElementById('yes').onclick = function () {
    var c = document.getElementById('content');
    c.innerHTML = '<h1>Restricted notes</h1><p>' + ARTICLE + '</p>';
    c.style.display = 'block';
    document.getElementById('gate').remove();
  };
  document.getElementById('no').onclick = function () { location.href = '/index.html'; };
</script>
</body></html>""",
    "private/secret.html": "<html><body><h1>Secret</h1><p>do not index</p></body></html>",
    "robots.txt": "User-agent: *\nDisallow: /private/\nSitemap: {BASE}/sitemap.xml\n",
    "sitemap.xml": """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{BASE}/a1.html</loc></url>
  <url><loc>{BASE}/a2.html</loc></url>
  <url><loc>{BASE}/gated.html</loc></url>
  <url><loc>{BASE}/private/secret.html</loc></url>
</urlset>""",
}


def build_site(root: Path, base: str) -> None:
    for name, body in SITE.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.replace("{BASE}", base), encoding="utf-8")


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass


def main() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {name}{(' — ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "site"
        root.mkdir()

        handler = lambda *a, **kw: Handler(*a, directory=str(root), **kw)  # noqa: E731
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        build_site(root, base)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.3)

        data_dir = Path(tmp) / "data"
        settings = Settings.build(data_dir, {"headless": True, "request_delay_ms": 0})
        settings.ensure_dirs()
        corpus = Corpus(settings.corpus_path)
        ladder = Ladder(settings, clearances={}, enable_browser=True)

        # 1. Plain HTTP rung.
        plain = ladder.fetch(f"{base}/a1.html", mode="http")
        check("HTTP rung fetches a static page", plain.ok and "shard" in plain.text.lower(),
              f"status={plain.status} blocked={plain.blocked} err={plain.error[:80]}")

        # 2. Age gate: the article text exists only inside a JS string, so a plain
        #    extraction of the static HTML must not contain it — and the HTTP rung
        #    must classify the page as a shell and hand over to the browser.
        from core.extract import extract_document

        gated_static = ladder.fetch(f"{base}/gated.html", mode="http")
        static_doc = extract_document(gated_static)
        check("Static extraction cannot see gated content",
              "secondary ledger" not in (static_doc.get("text") or ""),
              "gate content leaked into extracted text")
        check("Thin gated page is classified as needing a browser",
              gated_static.ok is False and gated_static.blocked == "js-shell",
              f"ok={gated_static.ok} blocked={gated_static.blocked}")

        gated = ladder.fetch(f"{base}/gated.html", mode="auto")
        check("Ladder escalates and clears the age gate",
              gated.ok and "secondary ledger" in (gated.text or ""),
              f"method={gated.method} gates={gated.gates_clicked} blocked={gated.blocked} "
              f"err={gated.error[:120]} attempts={gated.attempts}")
        check("Gate click was recorded", bool(gated.gates_clicked), f"{gated.gates_clicked}")
        check("Gate cleared by the browser rung", gated.method == "browser", gated.method)

        # 3. Crawl: sitemap seeding, robots.txt, storage, FTS.
        options = CrawlOptions(
            seed=f"{base}/", max_pages=12, max_depth=2, strategy="bfs",
            use_sitemap=True, respect_robots=True, allow_browser=True,
            concurrency=2, deadline_s=180, query="polymesh shard",
        )
        job = start_crawl(corpus, settings, options)
        deadline = time.time() + 180
        while job.status in ("pending", "running") and time.time() < deadline:
            time.sleep(0.5)

        counts = job.counts()
        print(f"     crawl: status={job.status} {counts}")
        check("Crawl finished", job.status == "done", f"status={job.status} error={job.error}")

        urls = {row["url"] for row in corpus.export_docs(limit=200)}
        check("Static articles stored", any(u.endswith("/a1.html") for u in urls), str(sorted(urls)))
        check("Gated article stored", any(u.endswith("/gated.html") for u in urls), str(sorted(urls)))
        check("robots.txt disallow honoured",
              not any("/private/" in u for u in urls),
              f"private path was crawled: {sorted(urls)}")
        check("robots skip counted", counts.get("skipped_robots", 0) >= 1, str(counts))

        hits = corpus.search("polymesh indexer")
        check("FTS finds stored documents", bool(hits), f"{len(hits)} hits")
        if hits:
            print(f"     top hit: {hits[0]['url']} :: {str(hits[0].get('snippet'))[:90]}")

        gated_doc = next((u for u in urls if u.endswith("/gated.html")), None)
        if gated_doc:
            row = corpus.search("secondary ledger")
            check("Gated text is searchable after the crawl",
                  any(r["url"].endswith("/gated.html") for r in row),
                  f"{[r['url'] for r in row]}")

        server.shutdown()
        corpus.close()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        return 1
    print("ALL INTEGRATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())