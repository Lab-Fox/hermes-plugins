"""Live tool-surface test: exercises the six handlers against the real internet.

Uses a fake Hermes context (config + dispatch_tool) so the same code path the
agent uses is what gets tested. Requires network. Not part of the offline suite.

Run:  PYTHONPATH= python tests/live_tools.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT.parent))  # import `netcrawler` as a package

TMP = tempfile.mkdtemp(prefix="netcrawler-live-")
os.environ["HERMES_HOME"] = TMP

from netcrawler.tools import (net_corpus, net_crawl, net_extract,  # noqa: E402
                              net_fetch, net_search, net_status, init)


class FakeCtx:
    """Minimal stand-in for PluginContext."""

    def __init__(self) -> None:
        self.values = {"headless": True, "max_pages": 12, "request_delay_ms": 150,
                       "timeout_s": 30, "concurrency": 3}

    def get_config(self, key, default=None):
        return self.values.get(key, default)

    def dispatch_tool(self, name, args):
        # Deliberately returns nothing: proves the federation works with no
        # Hermes search provider configured (the keyless path).
        return {"success": False, "error": "not configured in this harness"}


def show(label: str, payload: str, keys: tuple[str, ...]) -> dict:
    data = json.loads(payload)
    ok = data.get("ok")
    print(f"\n=== {label} — ok={ok}")
    for key in keys:
        if key in data:
            value = data[key]
            if isinstance(value, (list, dict)):
                preview = json.dumps(value, ensure_ascii=False)[:300]
            else:
                preview = str(value)[:300]
            print(f"    {key}: {preview}")
    if not ok:
        err = data.get("error")
        if err:
            print(f"    error: {json.dumps(err, ensure_ascii=False)[:300]}")
        attempts = data.get("attempts")
        if attempts:
            print(f"    attempts: {json.dumps(attempts, ensure_ascii=False)[:400]}")
    return data


def main() -> int:
    init(FakeCtx())
    failures: list[str] = []

    # 1. Keyless federated search, with content extraction.
    search = show("net_search", net_search({
        "query": "crawl4ai adaptive crawler", "limit": 6, "fetch": True, "read_count": 2,
        "max_chars": 1500,
    }), ("count", "engines", "results"))
    engines = search.get("engines") or {}
    live = {k: v for k, v in engines.items() if isinstance(v, int) and v > 0}
    if not search.get("ok"):
        failures.append("net_search returned nothing")
    if len(live) < 2:
        failures.append(f"federation only produced {len(live)} engines: {engines}")
    read_ok = (search.get("read_summary") or {}).get("readable", 0)
    if not read_ok:
        failures.append(f"no search result could be read: {search.get('read_summary')}")

    # 2. A real Cloudflare-protected page (the anti-bot claim).
    fetch = show("net_fetch (Cloudflare test page)", net_fetch({"url": "https://nowsecure.nl"}),
                 ("method", "words", "title", "gates_clicked"))
    if not fetch.get("ok"):
        failures.append("net_fetch failed on nowsecure.nl")

    # 3. Crawl a real sandbox site.
    crawl = show("net_crawl", net_crawl({
        "url": "https://quotes.toscrape.com/", "max_pages": 5, "max_depth": 1,
        "query": "quote", "wait_s": 120,
    }), ("job_id", "status", "pages", "failed", "duplicates", "queued", "stopped"))
    if crawl.get("status") not in ("done", "running", "cancelled"):
        failures.append(f"crawl status {crawl.get('status')}")

    # 4. Job status by id.
    if crawl.get("job_id"):
        status = show("net_status", net_status({"job_id": crawl["job_id"]}), ("status", "pages", "queued"))
        if status.get("job_id") != crawl["job_id"]:
            failures.append("net_status did not return the job")

    # 5. Corpus stats + search.
    stats = show("net_corpus stats", net_corpus({"action": "stats"}), ("stats",))
    docs = (stats.get("stats") or {}).get("documents", 0)
    if docs < 3:
        failures.append(f"corpus only holds {docs} documents")
    found = show("net_corpus search", net_corpus({"query": "quote", "limit": 3}),
                 ("count", "results"))
    if not found.get("count"):
        failures.append("corpus search found nothing")

    # 6. Structured extraction with CSS selectors.
    extract = show("net_extract", net_extract({
        "url": "https://quotes.toscrape.com/",
        "spec": {"item": "div.quote", "limit": 3,
                 "fields": {"text": "span.text", "author": "small.author",
                            "tags": {"selector": "a.tag", "all": True}}},
    }), ("count", "records"))
    if not extract.get("count"):
        failures.append("net_extract produced no records")

    print()
    if failures:
        print("FAILED CHECKS:")
        for item in failures:
            print("  -", item)
        return 1
    print("ALL LIVE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())