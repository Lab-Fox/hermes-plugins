"""The crawl engine: frontier, politeness, adaptive stopping, background jobs.

Three traversal strategies share one frontier:

* ``bfs`` — depth order, exhaustive when you want a whole site.
* ``dfs`` — dive into a branch before widening (good for docs trees).
* ``best-first`` — score every discovered URL by
  ``relevance(anchor+slug, query) × depth penalty × novelty`` and fetch the
  highest first. This is what makes a query-driven crawl stop finding new
  material once the site's relevant pages are exhausted.

Adaptive stop uses marginal yield: if the last N pages introduced almost no new
query terms, the useful part of the site is exhausted and the crawl ends early
instead of burning the whole page budget on tag archives.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Settings
from .extract import extract_document, hamming, relevance_terms
from .fetch import Ladder
from .filters import prune_to_markdown
from .robots import RobotsRules
from .sitemap import candidate_sitemap_urls, looks_like_sitemap, parse_sitemap, origin
from .store import Corpus
from .urlutil import (host_of, in_scope, is_asset, is_noise, normalize, tokens_from_url,
                      url_key)

logger = logging.getLogger("netcrawler.jobs")

MAX_SITEMAPS_PER_CRAWL = 6
ADAPTIVE_WINDOW = 8
ADAPTIVE_MIN_PAGES = 12


@dataclass
class CrawlOptions:
    seed: str
    max_pages: int = 40
    max_depth: int = 2
    strategy: str = "bfs"
    include: str = ""
    exclude: str = ""
    allow_subdomains: bool = True
    use_sitemap: bool = True
    respect_robots: bool = True
    query: str = ""
    allow_browser: bool = True
    concurrency: int = 4
    deadline_s: int = 600
    adaptive: bool = True
    same_site: bool = True
    refresh: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed, "max_pages": self.max_pages, "max_depth": self.max_depth,
            "strategy": self.strategy, "include": self.include, "exclude": self.exclude,
            "allow_subdomains": self.allow_subdomains, "use_sitemap": self.use_sitemap,
            "respect_robots": self.respect_robots, "query": self.query,
            "allow_browser": self.allow_browser, "concurrency": self.concurrency,
            "deadline_s": self.deadline_s, "adaptive": self.adaptive,
            "same_site": self.same_site, "refresh": self.refresh,
        }


def _score_url(url: str, anchor: str, depth: int, query: str) -> float:
    """Relevance prior for best-first traversal."""
    if not query:
        return -float(depth)
    terms = {t for t in re.findall(r"[a-z0-9]{3,}", query.lower())}
    if not terms:
        return -float(depth)
    blob = f"{anchor} {' '.join(tokens_from_url(url))}".lower()
    hits = sum(1 for term in terms if term in blob)
    return hits * 10.0 - depth * 1.5


class CrawlJob:
    """One crawl run. Owns its frontier, counters and cancellation flag."""

    def __init__(self, corpus: Corpus, settings: Settings, options: CrawlOptions,
                 clearances: Optional[dict[str, dict[str, str]]] = None) -> None:
        self.id = f"crawl_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.corpus = corpus
        self.settings = settings
        self.options = options
        self.stop = threading.Event()
        self.started_at = time.monotonic()
        self.deadline = self.started_at + max(30, options.deadline_s)

        self.ladder = Ladder(
            settings,
            clearances=clearances if clearances is not None else corpus.all_host_cookies(),
            on_clearance=lambda host, cookies: corpus.set_host_cookies(host, cookies),
            enable_browser=options.allow_browser,
        )

        self._lock = threading.RLock()
        self._seen: set[str] = set()
        self._heap: list[tuple[float, int, str, int, str]] = []  # (-priority, seq, url, depth, anchor)
        self._seq = itertools.count()
        self._inflight = 0
        self._cond = threading.Condition(self._lock)

        self.pages_done = 0
        self.pages_failed = 0
        self.duplicates = 0
        self.skipped_robots = 0
        self.bytes_total = 0
        self.status = "pending"
        self.error = ""
        self._robots: dict[str, RobotsRules] = {}
        self._terms_seen: set[str] = set()
        self._recent_gain: list[int] = []
        self.stopped_reason = ""

    # ------------------------------------------------------------- frontier
    def _priority(self, url: str, anchor: str, depth: int) -> float:
        if self.options.strategy == "dfs":
            return float(depth)
        if self.options.strategy == "best-first":
            return -_score_url(url, anchor, depth, self.options.query)
        return float(depth)

    def add(self, url: str, depth: int, anchor: str = "") -> bool:
        norm = normalize(url)
        if not norm:
            return False
        key = url_key(norm)
        if not key or is_asset(norm):
            return False
        if self.options.same_site and not in_scope(
            norm, self.options.seed, self.options.include, self.options.exclude,
            allow_subdomains=self.options.allow_subdomains,
        ):
            return False
        if self.options.include and not re.search(self.options.include, norm, re.I):
            return False
        if self.options.exclude and re.search(self.options.exclude, norm, re.I):
            return False
        if is_noise(norm):
            return False
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            priority = self._priority(norm, anchor, depth)
            heapq.heappush(self._heap, (priority, next(self._seq), norm, depth, anchor))
            self._cond.notify()
        return True

    def _pop(self) -> Optional[tuple[str, int, str]]:
        with self._lock:
            if self._heap:
                _prio, _seq, url, depth, anchor = heapq.heappop(self._heap)
                return url, depth, anchor
        return None

    def _queued(self) -> int:
        with self._lock:
            return len(self._heap) + self._inflight

    # --------------------------------------------------------------- robots
    def _robots_for(self, url: str) -> tuple[RobotsRules, list[str]]:
        host = host_of(url)
        if host in self._robots:
            rules = self._robots[host]
            return rules, rules.sitemaps
        base = origin(url)
        result = self.ladder.fetch(f"{base}/robots.txt", mode="http", allow_browser=False)
        if result.ok and result.text:
            rules = RobotsRules.parse(result.text)
            delay = rules.crawl_delay("netcrawler")
            if delay:
                self.ladder.limiter.set_override(host, delay)
        else:
            rules = RobotsRules()
        self._robots[host] = rules
        return rules, rules.sitemaps

    # -------------------------------------------------------------- sitemaps
    def _seed_from_sitemaps(self) -> int:
        if not self.options.use_sitemap:
            return 0
        _rules, declared = self._robots_for(self.options.seed)
        candidates = declared[:MAX_SITEMAPS_PER_CRAWL] or candidate_sitemap_urls(origin(self.options.seed))
        added = 0
        budget = max(20, self.options.max_pages * 4)
        queue_: list[tuple[str, int]] = [(url, 0) for url in candidates]
        visited_sitemaps: set[str] = set()

        while queue_ and added < budget:
            url, depth = queue_.pop(0)
            if url in visited_sitemaps or depth > 2:
                continue
            visited_sitemaps.add(url)
            if len(visited_sitemaps) > MAX_SITEMAPS_PER_CRAWL * 2:
                break
            result = self.ladder.fetch(url, mode="http", allow_browser=False)
            if not result.ok:
                continue
            urls, children = parse_sitemap(result.content, result.content_type, url)
            if not urls and not children and not looks_like_sitemap(result.content):
                continue
            for child in children[:MAX_SITEMAPS_PER_CRAWL]:
                queue_.append((child, depth + 1))
            for page in urls:
                if self.add(page, depth=1, anchor="sitemap"):
                    added += 1
                if added >= budget:
                    break
        logger.info("sitemap seeding added %d urls for %s", added, self.options.seed)
        return added

    # ------------------------------------------------------------- adaptive
    def _note_yield(self, terms: set[str]) -> None:
        with self._lock:
            new_terms = len(terms - self._terms_seen)
            self._terms_seen |= terms
            self._recent_gain.append(new_terms)
            if len(self._recent_gain) > ADAPTIVE_WINDOW:
                self._recent_gain.pop(0)

    def _should_stop_adapting(self) -> bool:
        if not self.options.adaptive or not self.options.query:
            return False
        with self._lock:
            if self.pages_done < ADAPTIVE_MIN_PAGES or len(self._recent_gain) < ADAPTIVE_WINDOW:
                return False
            return sum(self._recent_gain) <= 2
        return False

    # --------------------------------------------------------------- process
    def _process(self, url: str, depth: int, anchor: str) -> None:
        if self.stop.is_set():
            return
        if self.options.respect_robots:
            rules, _ = self._robots_for(url)
            if not rules.allowed(url, "netcrawler"):
                with self._lock:
                    self.skipped_robots += 1
                return

        ahead = self.corpus.get_doc(url=url) if not self.options.refresh else None
        if ahead and ahead.get("text"):
            with self._lock:
                self.duplicates += 1
            # Cached: still harvest its links so coverage continues.
            try:
                import json as _json

                for link in _json.loads(ahead.get("links_json") or "[]"):
                    if isinstance(link, dict) and link.get("url") and depth < self.options.max_depth:
                        self.add(link["url"], depth + 1, link.get("text") or "")
            except Exception:
                pass
            return

        result = self.ladder.fetch(url, dismiss_gates=True, try_api=True,
                                   allow_browser=self.options.allow_browser)
        if not result.ok:
            with self._lock:
                self.pages_failed += 1
            self.corpus.update_run(self.id, counts=self.counts())
            return

        doc = extract_document(result, query=self.options.query)
        if self.options.query and doc.get("markdown"):
            doc["fit_markdown"] = prune_to_markdown(result.text, self.options.query)
        saved = self.corpus.upsert_doc(doc, run_id=self.id)

        with self._lock:
            self.pages_done += 1
            self.bytes_total += len(result.content or b"")
            if saved.get("duplicate"):
                self.duplicates += 1

        if self.options.query:
            self._note_yield(relevance_terms(doc, self.options.query))

        if depth < self.options.max_depth:
            for link in doc.get("links") or []:
                self.add(link.get("url") or "", depth + 1, link.get("text") or "")

        if self.pages_done % 5 == 0:
            self.corpus.update_run(self.id, counts=self.counts())

    # ---------------------------------------------------------------- counts
    def counts(self) -> dict[str, Any]:
        with self._lock:
            elapsed = max(1.0, time.monotonic() - self.started_at)
            return {
                "pages": self.pages_done,
                "failed": self.pages_failed,
                "duplicates": self.duplicates,
                "skipped_robots": self.skipped_robots,
                "queued": len(self._heap),
                "inflight": self._inflight,
                "bytes": self.bytes_total,
                "pages_per_min": round(self.pages_done / elapsed * 60, 1),
                "elapsed_s": round(elapsed, 1),
                "stopped": self.stopped_reason,
            }

    def status_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "seed": self.options.seed,
            "strategy": self.options.strategy,
            "error": self.error,
            **self.counts(),
        }

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        self.status = "running"
        self.corpus.save_run(self.id, self.options.seed, self.options.as_dict())
        try:
            self.add(self.options.seed, 0, anchor="seed")
            self._seed_from_sitemaps()

            workers = max(1, min(self.options.concurrency, 8))
            threads = [threading.Thread(target=self._worker, daemon=True, name=f"netcrawler-{self.id}-{i}")
                       for i in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            if self.stopped_reason:
                self.status = "done" if self.stopped_reason in ("page-cap", "adaptive", "frontier-empty") else "cancelled"
            else:
                self.status = "done"
        except Exception as exc:  # a crawl failure must never kill the process
            logger.exception("crawl %s failed", self.id)
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.corpus.update_run(self.id, status=self.status, counts=self.counts())
            self.corpus.prune(self.settings.corpus_max_docs)

    def _worker(self) -> None:
        while not self.stop.is_set():
            item = self._pop()
            if item is None:
                with self._lock:
                    if self._inflight == 0:
                        self.stopped_reason = self.stopped_reason or "frontier-empty"
                        self._cond.notify_all()
                        return
                    self._cond.wait(timeout=0.6)
                if time.monotonic() > self.deadline:
                    self.stopped_reason = "deadline"
                    self.stop.set()
                    return
                continue

            url, depth, anchor = item
            with self._lock:
                if self.pages_done + self._inflight >= self.options.max_pages:
                    self.stopped_reason = "page-cap"
                    self.stop.set()
                    return
                self._inflight += 1
            try:
                self._process(url, depth, anchor)
            except Exception as exc:
                logger.warning("crawl step failed for %s: %s", url, exc)
                with self._lock:
                    self.pages_failed += 1
            finally:
                with self._lock:
                    self._inflight -= 1
                    self._cond.notify_all()

            if self._should_stop_adapting():
                self.stopped_reason = "adaptive"
                self.stop.set()
                return
            if time.monotonic() > self.deadline:
                self.stopped_reason = "deadline"
                self.stop.set()
                return

    def cancel(self) -> None:
        self.stop.set()
        self.stopped_reason = self.stopped_reason or "cancelled"


# ------------------------------------------------------------------ registry
_JOBS: dict[str, CrawlJob] = {}
_JOBS_LOCK = threading.Lock()


def start_crawl(corpus: Corpus, settings: Settings, options: CrawlOptions) -> CrawlJob:
    job = CrawlJob(corpus, settings, options)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        # Keep the registry bounded in a long-lived session.
        if len(_JOBS) > 32:
            for stale in sorted(_JOBS)[:8]:
                _JOBS.pop(stale, None)
    thread = threading.Thread(target=job.run, daemon=True, name=f"netcrawler-job-{job.id}")
    thread.start()
    return job


def get_job(job_id: str) -> Optional[CrawlJob]:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> list[CrawlJob]:
    with _JOBS_LOCK:
        return list(_JOBS.values())


def job_status(corpus: Corpus, job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job is not None:
        return job.status_dict()
    run = corpus.get_run(job_id)
    if not run:
        return {"job_id": job_id, "status": "unknown"}
    counts = run.get("counts") or {}
    return {
        "job_id": job_id,
        "status": run.get("status") or "unknown",
        "seed": run.get("seed"),
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        **counts,
    }