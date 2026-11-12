"""The cheap rung: HTTP with a real browser's TLS/HTTP2 fingerprint.

``curl_cffi`` binds curl-impersonate, so the ClientHello (JA3/JA4), HTTP/2
SETTINGS frame and header order are Chrome's. That clears the large majority of
"python is blocked" walls at roughly a millisecond-level cost per page, with no
browser process. Everything that needs JavaScript is left to the ladder.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Settings

logger = logging.getLogger("netcrawler.http")

# Markers that mean "the server answered, but not with the document".
CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "cf-challenge",
    "cf_chl_opt",
    "attention required",
    "enable javascript and cookies to continue",
    "ddos protection by",
    "please complete the security check",
    "g-recaptcha",
    "h-captcha",
    "hcaptcha",
    "px-captcha",
    "perimeterx",
    "datadome",
    "access denied",
    "request unsuccessful. incapsula",
    "you have been blocked",
    "unusual traffic",
)

# Signals that the page is a client-rendered shell with no content in the HTML.
SHELL_MARKERS = (
    '<div id="root"></div>',
    '<div id="app"></div>',
    '<div id="__next"></div>',
    "you need to enable javascript",
    "this app requires javascript",
)

BLOCK_STATUSES = frozenset({401, 403, 407, 429, 451, 503, 520, 521, 522, 524})


@dataclass
class FetchResult:
    """One document, however it was obtained."""

    url: str
    final_url: str = ""
    status: int = 0
    ok: bool = False
    content: bytes = b""
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = ""
    method: str = "http"  # http | browser | archive
    blocked: str = ""     # challenge reason, empty when the body is genuine
    error: str = ""
    elapsed_ms: int = 0
    gates_clicked: list[str] = field(default_factory=list)
    provenance: str = "live"  # live | snapshot
    extra_cookies: dict[str, str] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_html(self) -> bool:
        ct = (self.content_type or "").lower()
        return "html" in ct or (not ct and self.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")))


def classify_block(status: int, text: str, headers: Optional[dict[str, str]] = None) -> str:
    """Return a short block reason, or ``""`` when the body can be trusted."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if headers.get("cf-mitigated") or headers.get("x-datadome"):
        return "waf-header"
    body = (text or "")
    low = body[:20000].lower()
    if status in (429,):
        return "rate-limited"
    if status == 451:
        return "legal-block"
    if status in BLOCK_STATUSES:
        # 403/503 pages are sometimes real content; only call it a block when the
        # body looks like an interstitial rather than the requested document.
        if any(m in low for m in CHALLENGE_MARKERS) or len(body) < 400:
            return f"http-{status}"
        if status == 403:
            return "http-403"
        return ""
    if any(m in low for m in CHALLENGE_MARKERS):
        return "challenge-page"
    return ""


def is_js_shell(html: str) -> bool:
    """True when the HTML is a client-rendered shell or a gate that injects content.

    Deliberately precise: a short page is *not* automatically a shell, because
    escalating every stub to a browser costs a second and a few hundred MB. The
    signals are app bundles, or DOM-writing script on a page whose text talks
    about age/consent/JavaScript.
    """
    if not html:
        return False
    low = html.lower()
    if any(m in low for m in SHELL_MARKERS):
        return True
    try:
        from lxml import html as lhtml

        doc = lhtml.fromstring(html)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.getparent().remove(bad)
        text = " ".join((doc.text_content() or "").split())
    except Exception:
        text = ""
    if len(text) >= 200:
        return False

    script_count = low.count("<script")
    has_bundle = script_count >= 2 or re.search(r"<script[^>]+src=", low) is not None
    if has_bundle:
        return True

    dom_writes = any(marker in low for marker in (
        "getelementbyid", "innerhtml", "appendchild", "document.write", "createelement",
        "addeventlistener",
    ))
    gate_words = ("age", "18", "21", "consent", "cookie", "javascript", "verify",
                  "enter", "continue", "privacy")
    return dom_writes and any(word in low for word in gate_words)


class RateLimiter:
    """Per-host token spacing with jitter.

    ``crawl-delay`` from robots.txt wins when it is larger than the configured
    delay: the site asked, and honouring it is what keeps a crawl polite.
    """

    def __init__(self, delay_ms: int) -> None:
        self.base = max(0.0, delay_ms / 1000.0)
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()
        self._override: dict[str, float] = {}

    def set_override(self, host: str, seconds: float) -> None:
        if seconds and seconds > 0:
            with self._lock:
                self._override[host] = min(float(seconds), 30.0)

    def wait(self, host: str) -> float:
        """Block until this host may be hit again; return the delay applied."""
        delay = max(self.base, self._override.get(host, 0.0))
        if delay <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            ready = self._next.get(host, 0.0)
            sleep_for = max(0.0, ready - now)
            self._next[host] = max(now, ready) + delay + random.uniform(0, delay * 0.3)
        if sleep_for > 0:
            time.sleep(min(sleep_for, 30.0))
        return sleep_for


class HttpClient:
    """curl_cffi session with a per-host cookie jar for clearance replay."""

    def __init__(self, settings: Settings, clearances: Optional[dict[str, dict[str, str]]] = None) -> None:
        self.settings = settings
        self.clearances = clearances if clearances is not None else {}
        self._session: Any = None
        self._lock = threading.Lock()

    # -- session ------------------------------------------------------------
    def _build(self) -> Any:
        try:
            from curl_cffi import requests as crequests
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError(
                "curl_cffi is not installed (pip install curl_cffi) — the HTTP rung is unavailable"
            ) from exc
        kwargs: dict[str, Any] = {
            "impersonate": "chrome",
            "timeout": self.settings.timeout_s,
            "allow_redirects": True,
        }
        if self.settings.proxy:
            kwargs["proxies"] = {"http": self.settings.proxy, "https": self.settings.proxy}
        session = crequests.Session(**kwargs)
        session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        })
        return session

    @property
    def session(self) -> Any:
        with self._lock:
            if self._session is None:
                self._session = self._build()
            return self._session

    # -- clearance ----------------------------------------------------------
    def apply_clearance(self, host: str) -> bool:
        """Install stored clearance cookies for a host. Returns True when applied."""
        cookies = self.clearances.get(host)
        if not cookies:
            return False
        try:
            session = self.session
            for name, value in cookies.items():
                session.cookies.set(name, value, domain=host, path="/")
            return True
        except Exception as exc:
            logger.debug("clearance apply failed for %s: %s", host, exc)
            return False

    def store_clearance(self, host: str, cookies: dict[str, str]) -> None:
        if cookies:
            self.clearances[host] = dict(cookies)

    # -- fetch --------------------------------------------------------------
    def get(self, url: str, *, referer: str = "") -> FetchResult:
        started = time.monotonic()
        result = FetchResult(url=url, final_url=url)
        host = (url.split("/")[2].split(":")[0] if "//" in url else "").lower()
        self.apply_clearance(host)
        try:
            headers = {"Referer": referer} if referer else {}
            response = self.session.get(url, headers=headers)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        body_bytes = response.content or b""
        if len(body_bytes) > self.settings.max_page_bytes:
            body_bytes = body_bytes[: self.settings.max_page_bytes]
        try:
            text = body_bytes.decode(response.encoding or "utf-8", "replace")
        except (LookupError, TypeError):
            text = body_bytes.decode("utf-8", "replace")

        result.status = int(response.status_code or 0)
        result.content = body_bytes
        result.text = text
        result.final_url = str(response.url or url)
        result.headers = {str(k).lower(): str(v) for k, v in dict(response.headers or {}).items()}
        result.content_type = result.headers.get("content-type", "")
        result.blocked = classify_block(result.status, text, result.headers)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.ok = (
            not result.error
            and result.status < 400
            and not result.blocked
            and bool(body_bytes)
        )
        # A 200 that is a JS shell is "not blocked" but also not usable.
        if result.ok and result.is_html and is_js_shell(text):
            result.blocked = "js-shell"
            result.ok = False
        return result


def parse_set_cookie_headers(headers: dict[str, str]) -> dict[str, str]:
    """Extract ``name=value`` pairs from a raw Set-Cookie header blob."""
    out: dict[str, str] = {}
    raw = headers.get("set-cookie", "")
    if not raw:
        return out
    for chunk in raw.split(","):
        pair = chunk.split(";", 1)[0].strip()
        if "=" in pair:
            name, _, value = pair.partition("=")
            if name.strip():
                out[name.strip()] = value.strip()
    return out