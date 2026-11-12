"""Stealth browser rung — runs as a disposable child process.

Why a subprocess and not an in-process call:

* Playwright's sync API refuses to run inside a thread that owns an asyncio
  event loop, which is exactly the situation inside a gateway/desktop session.
* A browser that hangs or crashes must not take the agent with it; the parent
  enforces a wall-clock deadline and kills this process.
* Nothing leaks between calls: fresh browser, fresh profile, no zombie state.

Protocol: one JSON object on stdin, one JSON object on stdout.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "verifying you are human",
    "cf-challenge", "attention required", "enable javascript and cookies",
    "please wait", "ddos protection",
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-dev-shm-usage",
]


def _blocked(html: str) -> bool:
    low = (html or "")[:60000].lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def _slim_cookies(raw: list[dict], host: str) -> dict[str, str]:
    out: dict[str, str] = {}
    bare = host.split(":")[0].lower()
    for cookie in raw or []:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name:
            continue
        if not domain or domain in bare or bare.endswith(domain):
            out[name] = value
    return out


def _wait_challenge(page, deadline: float) -> None:
    """Poll until the JS challenge resolves or the deadline passes."""
    while time.monotonic() < deadline:
        try:
            if not _blocked(page.content()):
                return
        except Exception:
            return
        time.sleep(0.8)


def run(req: dict[str, Any]) -> dict[str, Any]:
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright  # graceful fallback
        except ImportError as exc:
            return {"ok": False, "error": f"no browser driver installed: {exc}"}

    try:
        from core.gates import GATE_JS, looks_gated, outcome_from_js  # type: ignore
    except ImportError:
        from gates import GATE_JS, looks_gated, outcome_from_js  # type: ignore

    url = req["url"]
    channels = [req.get("channel") or "chrome"]
    for extra in ("chrome", "msedge", "chromium"):
        if extra not in channels:
            channels.append(extra)
    headless = bool(req.get("headless", True))
    timeout_ms = int(float(req.get("timeout_s") or 45) * 1000)
    proxy = (req.get("proxy") or "").strip()
    payload = req.get("patterns") or {}
    total_deadline = time.monotonic() + max(30.0, timeout_ms / 1000.0 + 25.0)

    last_error = ""
    with sync_playwright() as pw:
        for channel in channels:
            try:
                launch_kwargs: dict[str, Any] = {
                    "headless": headless,
                    "args": LAUNCH_ARGS,
                    "ignore_default_args": ["--enable-automation"],
                }
                if channel != "chromium":
                    launch_kwargs["channel"] = channel
                if proxy:
                    launch_kwargs["proxy"] = {"server": proxy}
                browser = pw.chromium.launch(**launch_kwargs)
            except Exception as exc:
                last_error = f"{channel}: {exc}"
                continue

            started = time.monotonic()
            gates_hit: list[str] = []
            try:
                context = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as exc:
                    last_error = f"{channel} goto: {exc}"
                    context.close()
                    browser.close()
                    continue

                # 1. Dismiss age gates / consent walls / overlays.
                if req.get("dismiss_gates", True):
                    for _ in range(4):
                        html = ""
                        try:
                            html = page.content()
                        except Exception:
                            pass
                        if not looks_gated(html):
                            break
                        try:
                            result = page.evaluate(GATE_JS, payload)
                        except Exception:
                            break
                        outcome = outcome_from_js(result if isinstance(result, dict) else {})
                        if not outcome.clicked:
                            break
                        gates_hit.append(outcome.label or "gate")
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            pass
                        time.sleep(0.7)

                # 2. Let the page settle.
                for state in ("networkidle", "load"):
                    try:
                        page.wait_for_load_state(state, timeout=6000)
                        break
                    except Exception:
                        continue

                # 3. If a JS challenge is still up, give it time to clear itself.
                html = page.content()
                if _blocked(html):
                    _wait_challenge(page, min(total_deadline - 5, time.monotonic() + 20))
                    html = page.content()

                try:
                    title = page.title()
                except Exception:
                    title = ""
                try:
                    final_url = page.url
                except Exception:
                    final_url = url
                try:
                    cookies = _slim_cookies(context.cookies(), (final_url or url))
                except Exception:
                    cookies = {}

                elapsed = int((time.monotonic() - started) * 1000)
                browser.close()
                return {
                    "ok": True,
                    "status": 200,
                    "final_url": final_url,
                    "title": title,
                    "html": html,
                    "cookies": cookies,
                    "gates_clicked": gates_hit,
                    "blocked": _blocked(html),
                    "elapsed_ms": elapsed,
                    "channel": channel,
                }
            except Exception as exc:
                last_error = f"{channel}: {type(exc).__name__}: {exc}"
                try:
                    browser.close()
                except Exception:
                    pass
        return {"ok": False, "error": last_error or "no browser channel could be launched"}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        raw = sys.stdin.read()
        req = json.loads(raw) if raw.strip() else {}
        out = run(req)
    except Exception as exc:  # never let a traceback corrupt the protocol
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()