"""The escalation ladder: cheap HTTP → stealth browser → archive.

Order is not a matter of taste. ``curl_cffi`` costs milliseconds and clears most
walls; a browser costs ~1 s and a few hundred MB but is the only thing that runs
a JavaScript challenge or clicks an age gate; an archive is free and instant but
is a *snapshot* and must be labelled as one. Each rung reports why it was tried
so a crawl's run report explains itself.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from . import browser as browser_rung
from .config import Settings
from .httpfetch import FetchResult, HttpClient, RateLimiter, classify_block
from .urlutil import host_of, normalize

logger = logging.getLogger("netcrawler.ladder")

WAYBACK_AVAILABLE = "https://archive.org/wayback/available"


def _attempt(attempts: list[dict], method: str, result: FetchResult, **extra) -> None:
    attempts.append({
        "method": method,
        "status": result.status,
        "ok": bool(result.ok),
        "blocked": result.blocked,
        "error": result.error[:200] if result.error else "",
        "ms": result.elapsed_ms,
        **extra,
    })


def api_variants(url: str) -> list[str]:
    """Cheap "is there a data endpoint instead" guesses.

    WAFs guard HTML far more aggressively than the JSON behind it. These are the
    three patterns that pay off most often; a hit turns a blocked page into a
    clean document with no browser at all.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path or path == "/":
        return []
    base = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return [base + ".json", base + "?format=json", base + "?_format=json"]


class Ladder:
    """Owns the HTTP client, the rate limiter and per-host clearance cookies."""

    def __init__(
        self,
        settings: Settings,
        *,
        clearances: Optional[dict[str, dict[str, str]]] = None,
        on_clearance: Optional[Callable[[str, dict[str, str]], None]] = None,
        enable_browser: bool = True,
    ) -> None:
        self.settings = settings
        self.clearances = clearances if clearances is not None else {}
        self.on_clearance = on_clearance
        self.http = HttpClient(settings, self.clearances)
        self.limiter = RateLimiter(settings.request_delay_ms)
        self.enable_browser = enable_browser

    # ------------------------------------------------------------------ http
    def _http(self, url: str, referer: str = "") -> FetchResult:
        host = host_of(url)
        self.limiter.wait(host)
        return self.http.get(url, referer=referer)

    # --------------------------------------------------------------- browser
    def _browser(self, url: str, dismiss_gates: bool, headless: Optional[bool]) -> FetchResult:
        result = browser_rung.render(
            url, self.settings, dismiss_gates=dismiss_gates, headless=headless
        )
        if result.ok and result.extra_cookies:
            host = host_of(result.final_url or url)
            self.http.store_clearance(host, result.extra_cookies)
            if self.on_clearance:
                try:
                    self.on_clearance(host, result.extra_cookies)
                except Exception as exc:  # persistence must never break a fetch
                    logger.debug("clearance persist failed: %s", exc)
        return result

    # --------------------------------------------------------------- archive
    def _archive(self, url: str) -> FetchResult:
        lookup = f"{WAYBACK_AVAILABLE}?url={quote(url, safe='')}"
        probe = self.http.get(lookup)
        snapshot_url = ""
        timestamp = ""
        if probe.ok:
            try:
                import json

                payload = json.loads(probe.text or "{}")
                closest = (payload.get("archived_snapshots") or {}).get("closest") or {}
                if closest.get("available"):
                    snapshot_url = closest.get("url") or ""
                    timestamp = closest.get("timestamp") or ""
            except Exception:
                snapshot_url = ""
        if not snapshot_url:
            result = FetchResult(url=url, final_url=url, method="archive")
            result.blocked = "no-snapshot"
            result.error = "no Wayback snapshot available"
            return result
        snap = snapshot_url.replace("http://", "https://", 1)
        got = self._http(snap)
        got.method = "archive"
        got.provenance = "snapshot"
        got.url = url
        got.headers["x-netcrawler-snapshot"] = timestamp
        if got.ok:
            got.text = got.text.replace(
                "//web.archive.org/web/", "//web.archive.org/web/"
            )
        return got

    # ------------------------------------------------------------------ main
    def fetch(
        self,
        url: str,
        *,
        mode: str = "auto",
        dismiss_gates: bool = True,
        allow_browser: Optional[bool] = None,
        referer: str = "",
        headless: Optional[bool] = None,
        try_api: bool = False,
    ) -> FetchResult:
        """Fetch one URL down the ladder. Never raises."""
        url = normalize(url) or url
        attempts: list[dict] = []

        if mode in ("auto", "http"):
            primary = self._http(url, referer)
            _attempt(attempts, "http", primary)
            if primary.ok:
                primary.attempts = attempts
                return primary
            if mode == "http":
                primary.attempts = attempts
                return primary

        wants_browser = self.enable_browser and (
            allow_browser is not False
        ) and mode in ("auto", "browser")

        if wants_browser:
            rendered = self._browser(url, dismiss_gates, headless)
            _attempt(attempts, "browser", rendered,
                     gates=rendered.gates_clicked)
            if rendered.ok:
                rendered.attempts = attempts
                return rendered

        # A data endpoint behind the same wall (API-first pivot).
        if try_api and mode in ("auto", "browser"):
            for candidate in api_variants(url):
                probe = self._http(candidate)
                looks_json = "json" in (probe.content_type or "").lower()
                _attempt(attempts, "api", probe, variant=candidate)
                if probe.ok and looks_json:
                    probe.method = "api"
                    probe.attempts = attempts
                    return probe

        if self.settings.archive_fallback:
            archived = self._archive(url)
            _attempt(attempts, "archive", archived)
            if archived.ok:
                archived.attempts = attempts
                return archived

        # Nothing worked: return the most informative failure.
        failure = FetchResult(url=url, final_url=url, attempts=attempts, ok=False)
        for entry in reversed(attempts):
            if entry.get("blocked"):
                failure.blocked = entry["blocked"]
                failure.status = entry.get("status") or 0
                failure.method = entry["method"]
                break
        failure.error = "all rungs failed: " + "; ".join(
            f"{a['method']}={a.get('blocked') or a.get('error') or a.get('status')}"
            for a in attempts
        )
        return failure