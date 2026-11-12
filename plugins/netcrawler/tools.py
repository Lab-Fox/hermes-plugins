"""Tool handlers — the Hermes-aware shell around the engine.

Every handler: takes ``args`` (dict), returns a JSON string, never raises.
Hermes context (config + plugin data dir) is injected once by ``register(ctx)``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from . import schemas
from .core import jobs as crawl_jobs
from .core.config import Settings
from .core.extract import extract_document
from .core.fetch import Ladder
from .core.filters import compact, prune_to_markdown
from .core.search import Federation, Hit, hits_from_web_search
from .core.selectors import extract_records, records_to_csv
from .core.store import Corpus
from .core.urlutil import host_of, normalize

logger = logging.getLogger("netcrawler.tools")

_STATE: dict[str, Any] = {
    "ctx": None,
    "settings": None,
    "corpus": None,
    "federation": None,
}
_LOCK = threading.RLock()


# --------------------------------------------------------------------- helpers
def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _ok(**payload: Any) -> str:
    return _json({"ok": True, **payload})


def _err(code: str, message: str, **payload: Any) -> str:
    return _json({"ok": False, "error": {"code": code, "message": message}, **payload})


def init(ctx: Any) -> None:
    """Wire Hermes context in. Called once from ``register(ctx)``."""
    with _LOCK:
        _STATE["ctx"] = ctx
        _STATE["settings"] = _build_settings(ctx)
        _STATE["corpus"] = None
        _STATE["federation"] = None


def _data_dir(ctx: Any) -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir

        return Path(plugin_data_dir("netcrawler"))
    except Exception:
        home = os.getenv("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home) / "plugin-data" / "netcrawler"


def _config_values(ctx: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    keys = (
        "browser_channel", "headless", "max_pages", "max_depth", "concurrency",
        "request_delay_ms", "respect_robots", "archive_fallback", "timeout_s",
        "corpus_max_docs", "proxy",
    )
    for key in keys:
        try:
            values[key] = ctx.get_config(key)
        except Exception:
            continue
    return {k: v for k, v in values.items() if v is not None}


def _build_settings(ctx: Any) -> Settings:
    settings = Settings.build(_data_dir(ctx), _config_values(ctx))
    try:
        settings.ensure_dirs()
    except OSError as exc:
        logger.warning("could not create netcrawler data dir: %s", exc)
    return settings


def _settings(**overrides: Any) -> Settings:
    """Current settings, with per-call overrides (never persisted)."""
    with _LOCK:
        base: Settings = _STATE.get("settings") or Settings.build(_data_dir(_STATE.get("ctx")))
        if overrides:
            copy = Settings(**{**base.__dict__})
            for key, value in overrides.items():
                if value is not None and hasattr(copy, key):
                    setattr(copy, key, value)
            return copy
        return base


def _corpus() -> Corpus:
    with _LOCK:
        if _STATE.get("corpus") is None:
            settings = _settings()
            _STATE["corpus"] = Corpus(settings.corpus_path)
        return _STATE["corpus"]


def _federation() -> Federation:
    with _LOCK:
        federation = _STATE.get("federation")
        settings = _settings()
        if federation is None or federation.settings is not settings:
            federation = Federation(settings)
            _STATE["federation"] = federation
        return federation


def _ladder(settings: Settings) -> Ladder:
    corpus = _corpus()
    return Ladder(
        settings,
        clearances=corpus.all_host_cookies(),
        on_clearance=lambda host, cookies: corpus.set_host_cookies(host, cookies),
    )


def _dispatch_web_search(query: str, limit: int) -> list[Hit]:
    """Ask Hermes' configured search provider for one more voter."""
    ctx = _STATE.get("ctx")
    if ctx is None or not hasattr(ctx, "dispatch_tool"):
        return []
    try:
        result = ctx.dispatch_tool("web_search", {"query": query, "limit": max(1, min(limit, 20))})
    except Exception as exc:
        logger.debug("web_search dispatch failed: %s", exc)
        return []
    hits = hits_from_web_search(result)
    if not hits and isinstance(result, str):
        # Some surfaces wrap the payload as {"content": "<json>"}.
        try:
            wrapper = json.loads(result)
            if isinstance(wrapper, dict) and "content" in wrapper:
                hits = hits_from_web_search(wrapper["content"])
        except (json.JSONDecodeError, TypeError):
            pass
    return hits


# ------------------------------------------------------------------- net_fetch
def net_fetch(args: dict, **kwargs: Any) -> str:
    try:
        url = normalize(args.get("url") or "")
        if not url:
            return _err("bad_url", "net_fetch needs an absolute http(s) url")
        mode = str(args.get("mode") or "auto").lower()
        if mode not in ("auto", "http", "browser"):
            mode = "auto"
        settings = _settings()
        ladder = _ladder(settings)
        result = ladder.fetch(
            url,
            mode=mode,
            dismiss_gates=True,
            headless=args.get("headless"),
            allow_browser=(mode != "http"),
            try_api=True,
        )
        attempts = result.attempts
        if not result.ok:
            return _json({
                "ok": False,
                "url": url,
                "blocked": result.blocked,
                "attempts": attempts,
                "error": {
                    "code": "fetch_failed",
                    "message": result.error or "all rungs failed",
                },
            })

        query = str(args.get("query") or "")
        doc = extract_document(result, query=query)
        if query and result.is_html:
            doc["fit_markdown"] = prune_to_markdown(result.text, query)
        saved_id = 0
        if args.get("save", True) is not False:
            saved_id = _corpus().upsert_doc(doc).get("id", 0)

        max_chars = int(args.get("max_chars") or 12000)
        payload = {
            "ok": True,
            "url": doc["url"],
            "final_url": doc["final_url"],
            "title": doc["title"],
            "description": doc["description"],
            "lang": doc["lang"],
            "method": doc["method"],
            "provenance": doc["provenance"],
            "gates_clicked": doc["gates_clicked"],
            "words": doc["words"],
            "kind": doc["kind"],
            "content_hash": doc["content_hash"],
            "markdown": compact(doc.get("markdown") or doc.get("text") or "", max_chars),
            "links": (doc.get("links") or [])[:60],
            "jsonld": doc.get("jsonld") or [],
            "spa_state_keys": sorted((doc.get("spa_state") or {}).keys()),
            "attempts": attempts,
            "doc_id": saved_id,
        }
        if query and doc.get("fit_markdown"):
            payload["fit_markdown"] = compact(doc["fit_markdown"], max_chars)
        return _json(payload)
    except Exception as exc:
        logger.exception("net_fetch failed")
        return _err("internal", f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ net_search
def _read_one(url: str, settings: Settings, max_chars: int) -> dict[str, Any]:
    try:
        ladder = _ladder(settings)
        result = ladder.fetch(url, try_api=True)
        if not result.ok:
            return {"url": url, "ok": False, "blocked": result.blocked,
                    "error": result.error or "fetch failed"}
        doc = extract_document(result)
        return {
            "url": doc["final_url"],
            "ok": True,
            "title": doc["title"],
            "method": doc["method"],
            "words": doc["words"],
            "kind": doc["kind"],
            "content_hash": doc["content_hash"],
            "simhash": doc["simhash"],
            "text": compact(doc.get("markdown") or doc.get("text") or "", max_chars),
        }
    except Exception as exc:
        return {"url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def net_search(args: dict, **kwargs: Any) -> str:
    try:
        query = str(args.get("query") or "").strip()
        if not query:
            return _err("empty_query", "net_search needs a query")
        limit = max(1, min(int(args.get("limit") or 8), 30))
        site = str(args.get("site") or "").strip()
        search_query = f"{query} site:{site}" if site else query
        vertical = str(args.get("vertical") or "web").lower()
        engines = args.get("engines") or None
        if isinstance(engines, str):
            engines = [engines]

        settings = _settings()
        extra: list[Hit] = []
        if args.get("use_hermes_search", True) is not False:
            extra = _dispatch_web_search(search_query, limit)

        merged = _federation().search(
            search_query, limit=limit, engines=engines, vertical=vertical, extra=extra
        )
        results = merged.get("results") or []
        if site:
            results = [r for r in results if site.lower() in (r.get("url") or "").lower()] or results

        if args.get("fetch", True) is not False and results:
            read_count = max(0, min(int(args.get("read_count") or 3), 8))
            max_chars = int(args.get("max_chars") or 4000)
            if read_count:
                # Try a few more than asked: news-engine links and redirect stubs
                # often fail, and the caller cares about getting N readable pages.
                window = results[: min(len(results), read_count * 3)]
                urls = [r["url"] for r in window]
                with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
                    reads = list(pool.map(lambda u: _read_one(u, settings, max_chars), urls))
                corpus = _corpus()
                for item, read in zip(window, reads):
                    item["read"] = read
                    if read.get("ok") and read.get("text"):
                        corpus.upsert_doc({
                            "url": read["url"], "final_url": read["url"],
                            "title": read.get("title") or "", "text": read["text"],
                            "markdown": read["text"], "method": read.get("method") or "http",
                            "words": read.get("words") or 0,
                            "kind": read.get("kind") or "html",
                            "content_hash": read.get("content_hash") or "",
                            "simhash": read.get("simhash") or 0,
                        })
                summary = {
                    "attempted": len(reads),
                    "readable": sum(1 for r in reads if r.get("ok")),
                }
            else:
                summary = {}
        else:
            summary = {}

        payload = {
            "ok": bool(results),
            "query": query,
            "count": len(results),
            "results": results,
            "engines": merged.get("engines") or {},
            "read_summary": summary,
            "error": None if results else {
                "code": "no_results",
                "message": "every engine returned nothing or was blocked; retry later or narrow the query",
            },
        }
        return _json(payload)
    except Exception as exc:
        logger.exception("net_search failed")
        return _err("internal", f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------- net_crawl
def net_crawl(args: dict, **kwargs: Any) -> str:
    try:
        url = normalize(args.get("url") or "")
        if not url:
            return _err("bad_url", "net_crawl needs an absolute http(s) url")
        settings = _settings()
        options = crawl_jobs.CrawlOptions(
            seed=url,
            max_pages=max(1, min(int(args.get("max_pages") or settings.max_pages), 2000)),
            max_depth=max(0, min(int(args.get("max_depth") or settings.max_depth), 8)),
            strategy=str(args.get("strategy") or "bfs").lower(),
            include=str(args.get("include") or ""),
            exclude=str(args.get("exclude") or ""),
            allow_subdomains=bool(args.get("allow_subdomains", True)),
            use_sitemap=bool(args.get("use_sitemap", True)),
            respect_robots=bool(args.get("respect_robots", settings.respect_robots)),
            query=str(args.get("query") or ""),
            allow_browser=bool(args.get("allow_browser", True)),
            concurrency=max(1, min(int(args.get("concurrency") or settings.concurrency), 8)),
            deadline_s=max(30, min(int(args.get("deadline_s") or 600), 7200)),
            adaptive=bool(args.get("adaptive", True)),
        )
        job = crawl_jobs.start_crawl(_corpus(), settings, options)

        wait_s = int(args.get("wait_s") or 0)
        if wait_s > 0:
            deadline = min(wait_s, 600)
            import time as _time

            started = _time.monotonic()
            while job.status in ("pending", "running") and _time.monotonic() - started < deadline:
                _time.sleep(0.5)
            payload = job.status_dict()
            payload["ok"] = True
            payload["results"] = _corpus().search(
                options.query or "", site=host_of(url), limit=5
            ) if options.query else []
            return _json(payload)

        return _ok(
            job_id=job.id,
            status=job.status,
            seed=url,
            note="crawl started in the background; poll net_status(job_id) then search with net_corpus",
        )
    except Exception as exc:
        logger.exception("net_crawl failed")
        return _err("internal", f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ net_status
def net_status(args: dict, **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "status").lower()
        job_id = str(args.get("job_id") or "").strip()
        corpus = _corpus()
        if action == "list":
            items = [job.status_dict() for job in crawl_jobs.list_jobs()]
            if not items:
                items = corpus.list_runs(limit=10)
            return _ok(jobs=items)
        if not job_id:
            recent = [job.status_dict() for job in crawl_jobs.list_jobs()]
            return _err(
                "missing_job_id",
                "net_status needs a job_id (use action='list' to see recent jobs)",
                jobs=recent or corpus.list_runs(limit=10),
            )
        if action == "cancel":
            job = crawl_jobs.get_job(job_id)
            if job is None:
                return _err("not_found", f"no live job {job_id}")
            job.cancel()
            return _ok(job_id=job_id, status="cancelling")
        return _json({"ok": True, **crawl_jobs.job_status(corpus, job_id)})
    except Exception as exc:
        logger.exception("net_status failed")
        return _err("internal", f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ net_extract
def net_extract(args: dict, **kwargs: Any) -> str:
    try:
        spec = args.get("spec")
        if not isinstance(spec, dict) or not (spec.get("fields") or spec.get("columns")):
            return _err("bad_spec", "spec must include a 'fields' map, e.g. {\"fields\": {\"title\": \"h1\"}}")

        html = str(args.get("html") or "")
        source = str(args.get("url") or "")
        corpus = _corpus()
        url = normalize(args.get("url") or "")

        if not url and args.get("doc_id"):
            doc = corpus.get_doc(doc_id=int(args["doc_id"]))
            if not doc:
                return _err("not_found", f"no corpus document {args['doc_id']}")
            url = doc.get("url") or ""
            source = url

        if not html:
            if not url:
                return _err("bad_args", "net_extract needs a url, doc_id or html")
            settings = _settings()
            ladder = _ladder(settings)
            result = ladder.fetch(url, try_api=True)
            if not result.ok:
                return _json({
                    "ok": False, "url": url, "blocked": result.blocked,
                    "attempts": result.attempts,
                    "error": {"code": "fetch_failed", "message": result.error or "fetch failed"},
                })
            html = result.text
            source = result.final_url or url

        records = extract_records(html, spec)
        fmt = str(args.get("format") or "json").lower()
        if fmt == "csv":
            return _json({"ok": True, "source": source, "count": len(records),
                          "format": "csv", "csv": records_to_csv(records)})
        return _json({"ok": True, "source": source, "count": len(records),
                      "format": "json", "records": records})
    except Exception as exc:
        logger.exception("net_extract failed")
        return _err("internal", f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------- net_corpus
def net_corpus(args: dict, **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "search").lower()
        corpus = _corpus()
        limit = max(1, min(int(args.get("limit") or 10), 200))

        if action == "stats":
            return _ok(stats=corpus.stats())
        if action == "sites":
            return _ok(sites=corpus.list_sites()[:limit])
        if action == "get":
            doc = None
            if args.get("doc_id"):
                doc = corpus.get_doc(doc_id=int(args["doc_id"]))
            elif args.get("url"):
                doc = corpus.get_doc(url=str(args["url"]))
            if not doc:
                return _err("not_found", "no matching document in the corpus")
            doc.pop("links_json", None)
            doc["markdown"] = compact(doc.get("markdown") or "", 12000)
            doc["text"] = compact(doc.get("text") or "", 4000)
            return _ok(doc=doc)
        if action == "export":
            out_dir = Path(args.get("path") or (settings_export_dir()))
            out_dir.mkdir(parents=True, exist_ok=True)
            rows = corpus.export_docs(site=str(args.get("site") or ""), limit=limit * 50)
            written = 0
            for row in rows:
                safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (row.get("title") or "doc"))[:80]
                target = out_dir / f"{row['id']:06d}-{safe}.md"
                body = f"# {row.get('title') or row.get('url')}\n\n{row.get('url')}\n\n{row.get('markdown') or ''}\n"
                target.write_text(body, encoding="utf-8")
                written += 1
            return _ok(exported=written, directory=str(out_dir))
        if action == "search":
            query = str(args.get("query") or "").strip()
            if not query:
                return _err("empty_query", "action=search needs a query")
            rows = corpus.search(query, site=str(args.get("site") or ""), limit=limit)
            return _ok(query=query, count=len(rows), results=rows)
        return _err("bad_action", f"unknown action {action!r}")
    except Exception as exc:
        logger.exception("net_corpus failed")
        return _err("internal", f"{type(exc).__name__}: {exc}")


def settings_export_dir() -> str:
    return str(_settings().data_dir / "exports")


TOOLS = [
    (schemas.NET_SEARCH["name"], schemas.NET_SEARCH, net_search),
    (schemas.NET_FETCH["name"], schemas.NET_FETCH, net_fetch),
    (schemas.NET_CRAWL["name"], schemas.NET_CRAWL, net_crawl),
    (schemas.NET_STATUS["name"], schemas.NET_STATUS, net_status),
    (schemas.NET_EXTRACT["name"], schemas.NET_EXTRACT, net_extract),
    (schemas.NET_CORPUS["name"], schemas.NET_CORPUS, net_corpus),
]