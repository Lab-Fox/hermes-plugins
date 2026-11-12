"""Federated keyless web search.

Why federation instead of one engine: every free endpoint fails differently.
DuckDuckGo rate-limits a busy IP, Bing returns an empty organic set on some
queries, Yahoo drift changes markup, Mojeek serves a CAPTCHA to datacenter
ranges. Querying several and merging with **Reciprocal Rank Fusion** means one
engine's bad day degrades the answer instead of emptying it — and no engine
requires a key.

Block handling is the part naive metasearch gets wrong: a page that returns an
unrelated organic set is treated as a *block*, never merged, or the answer gets
quietly poisoned with content nobody asked for.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from lxml import etree, html as lhtml

from .config import CHROME_UA, Settings
from .urlutil import normalize, registrable_domain, url_key

logger = logging.getLogger("netcrawler.search")

RRF_K = 60
CACHE_TTL = 300.0
FAILURE_COOLDOWN = 180.0
BLOCK_COOLDOWN = 900.0

CAPTCHA_MARKERS = (
    "captcha", "unusual traffic", "are you a robot", "automated queries",
    "one more step", "verify you are human", "access denied", "blocked",
    "too many requests", "rate limit",
)


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    rank: int = 0
    kind: str = "web"          # web | news | reference | code | paper | discussion
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "url": self.url, "snippet": self.snippet,
            "kind": self.kind, **({"extra": self.extra} if self.extra else {}),
        }


# --------------------------------------------------------------------- helpers
def _session():
    from curl_cffi import requests as crequests

    session = crequests.Session(impersonate="chrome", timeout=20, allow_redirects=True)
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def _get(url: str, *, headers: Optional[dict[str, str]] = None,
         session: Any = None) -> tuple[int, str]:
    own = session is None
    session = session or _session()
    try:
        response = session.get(url, headers=headers or {})
        return int(response.status_code or 0), response.text or ""
    except Exception as exc:
        logger.debug("search fetch failed %s: %s", url, exc)
        return 0, ""
    finally:
        if own:
            try:
                session.close()
            except Exception:
                pass


def _looks_blocked(text: str) -> bool:
    low = (text or "")[:40000].lower()
    return any(marker in low for marker in CAPTCHA_MARKERS) and len(low) < 200_000


def _unwrap(href: str) -> str:
    """Resolve DuckDuckGo/Bing click-redirect wrappers to the real target."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    lower = href.lower()
    if "duckduckgo.com/l/" in lower or "uddg=" in lower:
        try:
            query = parse_qs(urlsplit(href).query)
            if query.get("uddg"):
                return unquote(query["uddg"][0])
        except Exception:
            pass
    if "bing.com/ck/a" in lower:
        try:
            query = parse_qs(urlsplit(href).query)
            u = (query.get("u") or [""])[0]
            if u.startswith("a1"):
                u = u[2:]
                u += "=" * (-len(u) % 4)
                return base64.urlsafe_b64decode(u).decode("utf-8", "replace")
        except Exception:
            pass
    return href


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _relevance_guard(query: str, hits: list[Hit]) -> list[Hit]:
    """Reject an organic set that shares nothing with the query.

    Protects against the "200 OK, unrelated results" block mode that silently
    poisons metasearch answers.
    """
    if not hits:
        return hits
    q_terms = {t for t in re.findall(r"[a-z0-9]{3,}", query.lower())}
    if not q_terms:
        return hits
    matched = 0
    for hit in hits:
        blob = f"{hit.title} {hit.snippet} {hit.url}".lower()
        if any(term in blob for term in q_terms):
            matched += 1
    return hits if matched else []


def _parse_rss(text: str, engine: str, kind: str) -> list[Hit]:
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring((text or "").encode("utf-8", "replace"), parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        return []
    if root is None:
        return []
    hits: list[Hit] = []
    nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for index, node in enumerate(nodes):
        def _text(tag: str) -> str:
            el = node.find(tag)
            if el is None:
                el = node.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
            return _clean("".join(el.itertext())) if el is not None else ""

        title = _text("title")
        link = _text("link")
        if not link:
            el = node.find("{http://www.w3.org/2005/Atom}link")
            link = el.get("href") if el is not None else ""
        snippet = _text("description") or _text("summary")
        # News RSS descriptions are HTML blobs with the source anchor appended.
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = _clean(unquote(snippet))[:400]
        if title and link:
            hits.append(Hit(title=title[:300], url=link, snippet=snippet,
                            engine=engine, rank=index + 1, kind=kind))
    return hits


# ---------------------------------------------------------------------- engines
class Engine:
    name = "base"
    kind = "web"
    needs: tuple[str, ...] = ()

    def available(self) -> bool:
        return all(os.getenv(var) for var in self.needs)

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:  # pragma: no cover
        raise NotImplementedError


class DuckDuckGoHTML(Engine):
    name = "ddg-html"
    kind = "web"

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        status, text = _get(
            f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
            headers={"Referer": "https://duckduckgo.com/"}, session=session,
        )
        if status != 200 or _looks_blocked(text):
            return []
        tree = lhtml.fromstring(text)
        hits: list[Hit] = []
        for index, node in enumerate(tree.xpath("//div[contains(@class,'result') and not(contains(@class,'result--ad'))]")):
            anchors = node.xpath(".//a[contains(@class,'result__a')]")
            if not anchors:
                continue
            anchor = anchors[0]
            url = _unwrap(anchor.get("href") or "")
            snippet_nodes = node.xpath(".//a[contains(@class,'result__snippet')]")
            snippet = _clean(snippet_nodes[0].text_content()) if snippet_nodes else ""
            if url:
                hits.append(Hit(_clean(anchor.text_content())[:300], url, snippet[:400],
                                engine=self.name, rank=index + 1))
            if len(hits) >= limit:
                break
        return _relevance_guard(query, hits)


class BingHTML(Engine):
    name = "bing-html"
    kind = "web"

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        status, text = _get(
            f"https://www.bing.com/search?q={quote_plus(query)}&count={max(limit, 10)}&setlang=en",
            session=session,
        )
        if status != 200 or _looks_blocked(text):
            return []
        tree = lhtml.fromstring(text)
        hits: list[Hit] = []
        for index, node in enumerate(tree.xpath("//li[contains(@class,'b_algo')]")):
            anchors = node.xpath(".//h2//a")
            if not anchors:
                continue
            anchor = anchors[0]
            url = _unwrap(anchor.get("href") or "")
            paras = node.xpath(".//p")
            snippet = _clean(paras[0].text_content()) if paras else ""
            if url:
                hits.append(Hit(_clean(anchor.text_content())[:300], url, snippet[:400],
                                engine=self.name, rank=index + 1))
            if len(hits) >= limit:
                break
        return _relevance_guard(query, hits)


class BingRSS(Engine):
    name = "bing-rss"
    kind = "web"

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        status, text = _get(
            f"https://www.bing.com/search?q={quote_plus(query)}&format=rss&count={max(limit, 10)}",
            session=session,
        )
        if status != 200:
            return []
        return _relevance_guard(query, _parse_rss(text, self.name, "web")[:limit])


class YahooHTML(Engine):
    name = "yahoo"
    kind = "web"

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        status, text = _get(f"https://search.yahoo.com/search?p={quote_plus(query)}", session=session)
        if status != 200 or _looks_blocked(text):
            return []
        tree = lhtml.fromstring(text)
        hits: list[Hit] = []
        for index, node in enumerate(tree.xpath("//div[contains(@class,'algo')]")):
            anchors = node.xpath(".//h3//a")
            if not anchors:
                continue
            anchor = anchors[0]
            url = _unwrap(anchor.get("href") or "")
            snippet_nodes = node.xpath(".//div[contains(@class,'compText')]") or node.xpath(".//p")
            snippet = _clean(snippet_nodes[0].text_content()) if snippet_nodes else ""
            if url:
                hits.append(Hit(_clean(anchor.text_content())[:300], url, snippet[:400],
                                engine=self.name, rank=index + 1))
            if len(hits) >= limit:
                break
        return _relevance_guard(query, hits)


class GoogleNewsRSS(Engine):
    name = "google-news"
    kind = "news"

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        status, text = _get(
            f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en",
            session=session,
        )
        if status != 200:
            return []
        return _parse_rss(text, self.name, "news")[:limit]


class BingNewsRSS(Engine):
    name = "bing-news"
    kind = "news"

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        status, text = _get(
            f"https://www.bing.com/news/search?q={quote_plus(query)}&format=RSS", session=session
        )
        if status != 200:
            return []
        return _parse_rss(text, self.name, "news")[:limit]


class MarginaliaJSON(Engine):
    name = "marginalia"
    kind = "web"

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        status, text = _get(
            f"https://api2.marginalia-search.com/public/search?query={quote_plus(query)}",
            headers={"API-Key": "public", "Accept": "application/json"}, session=session,
        )
        if status != 200:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        rows = payload.get("results") or []
        hits: list[Hit] = []
        for index, row in enumerate(rows[:limit]):
            url = row.get("url") or ""
            if not url:
                continue
            hits.append(Hit(_clean(str(row.get("title") or url))[:300], url,
                            _clean(str(row.get("description") or ""))[:400],
                            engine=self.name, rank=index + 1))
        return hits


class SearxNG(Engine):
    name = "searxng"
    kind = "web"

    def available(self) -> bool:
        return bool(os.getenv("SEARXNG_URL"))

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        base = (os.getenv("SEARXNG_URL") or "").rstrip("/")
        if not base:
            return []
        status, text = _get(
            f"{base}/search?q={quote_plus(query)}&format=json",
            headers={"Accept": "application/json"}, session=session,
        )
        if status != 200:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        hits: list[Hit] = []
        rows = sorted(payload.get("results") or [], key=lambda r: float(r.get("score") or 0), reverse=True)
        for index, row in enumerate(rows[:limit]):
            url = row.get("url") or ""
            if not url:
                continue
            hits.append(Hit(_clean(str(row.get("title") or url))[:300], url,
                            _clean(str(row.get("content") or ""))[:400],
                            engine=self.name, rank=index + 1))
        return hits


class BraveFree(Engine):
    name = "brave-free"
    kind = "web"

    def available(self) -> bool:
        return bool(os.getenv("BRAVE_SEARCH_API_KEY"))

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        key = os.getenv("BRAVE_SEARCH_API_KEY") or ""
        if not key:
            return []
        status, text = _get(
            f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}&count={min(limit, 20)}",
            headers={"X-Subscription-Token": key, "Accept": "application/json"}, session=session,
        )
        if status != 200:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        rows = ((payload.get("web") or {}).get("results") or [])[:limit]
        hits = []
        for index, row in enumerate(rows):
            url = row.get("url") or ""
            if url:
                hits.append(Hit(_clean(str(row.get("title") or url))[:300], url,
                                _clean(str(row.get("description") or ""))[:400],
                                engine=self.name, rank=index + 1))
        return hits


class DDGSPackage(Engine):
    name = "ddgs"
    kind = "web"

    def available(self) -> bool:
        try:
            import ddgs  # noqa: F401
            return True
        except ImportError:
            return False

    def search(self, query: str, limit: int, session: Any = None) -> list[Hit]:
        try:
            from ddgs import DDGS

            hits: list[Hit] = []
            with DDGS(timeout=10) as client:
                for index, row in enumerate(client.text(query, max_results=limit)):
                    url = row.get("href") or row.get("url") or ""
                    if url:
                        hits.append(Hit(_clean(str(row.get("title") or url))[:300], url,
                                        _clean(str(row.get("body") or ""))[:400],
                                        engine=self.name, rank=index + 1))
                    if len(hits) >= limit:
                        break
            return hits
        except Exception as exc:
            logger.debug("ddgs engine failed: %s", exc)
            return []


DEFAULT_ENGINES: tuple[Engine, ...] = (
    DuckDuckGoHTML(), BingRSS(), BingHTML(), YahooHTML(), GoogleNewsRSS(),
    BingNewsRSS(), MarginaliaJSON(), SearxNG(), BraveFree(), DDGSPackage(),
)
ENGINES_BY_NAME = {engine.name: engine for engine in DEFAULT_ENGINES}


# ------------------------------------------------------------------- verticals
def _wiki(query: str, limit: int) -> list[Hit]:
    status, text = _get(
        "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit="
        f"{limit}&srsearch={quote_plus(query)}",
        headers={"Accept": "application/json"},
    )
    if status != 200:
        return []
    try:
        rows = (json.loads(text).get("query") or {}).get("search") or []
    except json.JSONDecodeError:
        return []
    return [
        Hit(_clean(row.get("title") or ""), "https://en.wikipedia.org/wiki/" +
            quote_plus(str(row.get("title") or "").replace(" ", "_")),
            _clean(re.sub("<[^>]+>", " ", str(row.get("snippet") or "")))[:400],
            engine="wikipedia", rank=i + 1, kind="reference")
        for i, row in enumerate(rows[:limit])
    ]


def _stackexchange(query: str, limit: int) -> list[Hit]:
    status, text = _get(
        "https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&site=stackoverflow"
        f"&pagesize={limit}&q={quote_plus(query)}&filter=default",
        headers={"Accept": "application/json"},
    )
    if status != 200:
        return []
    try:
        rows = json.loads(text).get("items") or []
    except json.JSONDecodeError:
        return []
    return [
        Hit(_clean(str(row.get("title") or ""))[:300], str(row.get("link") or ""),
            f"score {row.get('score')} · answers {row.get('answer_count')}",
            engine="stackexchange", rank=i + 1, kind="code")
        for i, row in enumerate(rows[:limit]) if row.get("link")
    ]


def _arxiv(query: str, limit: int) -> list[Hit]:
    status, text = _get(
        f"http://export.arxiv.org/api/query?search_query=all:{quote_plus(query)}&max_results={limit}"
    )
    if status != 200:
        return []
    hits = _parse_rss(text, "arxiv", "paper")
    return hits[:limit]


def _reddit(query: str, limit: int) -> list[Hit]:
    status, text = _get(
        f"https://www.reddit.com/search.json?q={quote_plus(query)}&limit={limit}&sort=relevance",
        headers={"Accept": "application/json"}, 
    )
    if status != 200:
        return []
    try:
        children = (json.loads(text).get("data") or {}).get("children") or []
    except json.JSONDecodeError:
        return []
    hits = []
    for index, child in enumerate(children[:limit]):
        data = child.get("data") or {}
        permalink = data.get("permalink")
        if not permalink:
            continue
        hits.append(Hit(_clean(str(data.get("title") or ""))[:300],
                        f"https://www.reddit.com{permalink}",
                        _clean(str(data.get("selftext") or ""))[:400],
                        engine="reddit", rank=index + 1, kind="discussion",
                        extra={"subreddit": data.get("subreddit"), "score": data.get("score")}))
    return hits


def _hackernews(query: str, limit: int) -> list[Hit]:
    status, text = _get(
        f"https://hn.algolia.com/api/v1/search?query={quote_plus(query)}&hitsPerPage={limit}",
        headers={"Accept": "application/json"},
    )
    if status != 200:
        return []
    try:
        rows = json.loads(text).get("hits") or []
    except json.JSONDecodeError:
        return []
    hits = []
    for index, row in enumerate(rows[:limit]):
        url = row.get("url") or f"https://news.ycombinator.com/item?id={row.get('objectID')}"
        hits.append(Hit(_clean(str(row.get("title") or row.get("story_title") or ""))[:300], url,
                        f"{row.get('points', 0)} points · {row.get('num_comments', 0)} comments",
                        engine="hackernews", rank=index + 1, kind="discussion"))
    return hits


VERTICALS: dict[str, Callable[[str, int], list[Hit]]] = {
    "web": lambda q, n: [],          # web = the federation above
    "news": lambda q, n: [],         # news engines are in the federation
    "reference": _wiki,
    "code": _stackexchange,
    "papers": _arxiv,
    "discussions": lambda q, n: _reddit(q, n) or _hackernews(q, n),
}


# ------------------------------------------------------------------ federation
class Federation:
    """Runs engines in parallel, merges by RRF, and remembers what is broken."""

    def __init__(self, settings: Settings, engines: Optional[Iterable[Engine]] = None) -> None:
        self.settings = settings
        self.engines = list(engines) if engines is not None else list(DEFAULT_ENGINES)
        self._cache: dict[tuple[str, str], tuple[float, list[Hit]]] = {}
        self._cooldown: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- state --------------------------------------------------------------
    def _usable(self, engine: Engine) -> bool:
        with self._lock:
            until = self._cooldown.get(engine.name, 0.0)
        return time.time() >= until and engine.available()

    def _penalise(self, engine: Engine, *, blocked: bool) -> None:
        with self._lock:
            self._cooldown[engine.name] = time.time() + (BLOCK_COOLDOWN if blocked else FAILURE_COOLDOWN)

    def _cached(self, engine: Engine, query: str) -> Optional[list[Hit]]:
        with self._lock:
            entry = self._cache.get((engine.name, query.lower()))
        if not entry:
            return None
        stamp, hits = entry
        return hits if time.time() - stamp < CACHE_TTL else None

    def _store(self, engine: Engine, query: str, hits: list[Hit]) -> None:
        with self._lock:
            self._cache[(engine.name, query.lower())] = (time.time(), hits)
            if len(self._cache) > 512:
                for key in list(self._cache)[:128]:
                    self._cache.pop(key, None)

    # -- search -------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        engines: Optional[Iterable[str]] = None,
        vertical: str = "web",
        extra: Optional[Iterable[Hit]] = None,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"ok": False, "error": "empty query", "results": []}

        selected = [e for e in self.engines if e.name in engines] if engines else list(self.engines)
        report: dict[str, Any] = {}
        buckets: list[list[Hit]] = []

        vertical_hits: list[Hit] = []
        if vertical and vertical != "web":
            handler = VERTICALS.get(vertical)
            if handler:
                try:
                    vertical_hits = handler(query, max(limit, 10))
                    report[f"vertical:{vertical}"] = len(vertical_hits)
                except Exception as exc:
                    report[f"vertical:{vertical}"] = f"error: {exc}"

        session = _session()
        tasks = {}
        per_engine = max(10, limit)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for engine in selected:
                if not self._usable(engine):
                    report[engine.name] = "cooling-down"
                    continue
                cached = self._cached(engine, query)
                if cached is not None:
                    report[engine.name] = f"cache:{len(cached)}"
                    if cached:
                        buckets.append(cached)
                    continue
                tasks[pool.submit(self._run_engine, engine, query, per_engine, session)] = engine

            try:
                for future in as_completed(tasks, timeout=45):
                    engine = tasks[future]
                    try:
                        hits = future.result()
                    except Exception as exc:
                        self._penalise(engine, blocked=False)
                        report[engine.name] = f"error: {type(exc).__name__}"
                        continue
                    if hits:
                        self._store(engine, query, hits)
                        buckets.append(hits)
                        report[engine.name] = len(hits)
                    else:
                        self._penalise(engine, blocked=True)
                        report[engine.name] = "blocked-or-empty"
            except TimeoutError:
                report["timeout"] = "some engines did not answer within 45s"
        try:
            session.close()
        except Exception:
            pass

        if vertical_hits:
            buckets.append(vertical_hits)
        if extra:
            extra_hits = [hit for hit in extra if hit.url]
            if extra_hits:
                buckets.append(extra_hits)
                report["hermes-web_search"] = len(extra_hits)

        merged = _rrf(buckets, limit)
        return {
            "ok": bool(merged),
            "query": query,
            "vertical": vertical,
            "count": len(merged),
            "results": [hit.as_dict() for hit in merged],
            "engines": report,
        }

    def _run_engine(self, engine: Engine, query: str, limit: int, session: Any) -> list[Hit]:
        hits = engine.search(query, limit, session=session)
        # Dedupe inside one engine's own list.
        seen: set[str] = set()
        out: list[Hit] = []
        for hit in hits:
            norm = normalize(_unwrap(hit.url))
            if not norm:
                continue
            key = url_key(norm) or norm
            if key in seen:
                continue
            seen.add(key)
            hit.url = norm
            out.append(hit)
        return out


def _rrf(buckets: list[list[Hit]], limit: int) -> list[Hit]:
    """Reciprocal Rank Fusion: rank position beats raw score across engines."""
    scores: dict[str, float] = {}
    best: dict[str, Hit] = {}
    sources: dict[str, set[str]] = {}
    for bucket in buckets:
        for position, hit in enumerate(bucket, start=1):
            key = url_key(hit.url) or hit.url
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + position)
            sources.setdefault(key, set()).add(hit.engine)
            current = best.get(key)
            if current is None or (not current.snippet and hit.snippet):
                best[key] = hit
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out: list[Hit] = []
    for key, score in ordered[:limit]:
        hit = best[key]
        hit.extra = {**(hit.extra or {}), "engines": sorted(sources[key]), "rrf": round(score, 5)}
        out.append(hit)
    return out


def hits_from_web_search(payload: Any) -> list[Hit]:
    """Adapt a Hermes ``web_search`` result payload into Hits."""
    hits: list[Hit] = []
    rows = []
    if isinstance(payload, dict):
        rows = (payload.get("data") or {}).get("web") or payload.get("results") or []
    elif isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            rows = (parsed.get("data") or {}).get("web") or parsed.get("results") or []
        except (json.JSONDecodeError, AttributeError):
            return []
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        url = row.get("url") or row.get("href") or row.get("link") or ""
        if not url:
            continue
        hits.append(Hit(
            title=_clean(str(row.get("title") or url))[:300], url=url,
            snippet=_clean(str(row.get("description") or row.get("snippet") or row.get("body") or ""))[:400],
            engine="hermes", rank=index + 1,
        ))
    return hits