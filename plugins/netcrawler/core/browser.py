"""Parent side of the stealth browser rung.

Spawns ``browser_worker.py`` in the plugin's own interpreter, hands it one URL,
and enforces a hard deadline. A hung challenge page costs one terminated child,
never a hung agent turn.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .gates import pattern_payload
from .httpfetch import FetchResult

logger = logging.getLogger("netcrawler.browser")

_WORKER = Path(__file__).with_name("browser_worker.py")
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def available() -> bool:
    """True when a browser driver is importable (no network, no launch)."""
    for module in ("patchright", "playwright"):
        try:
            __import__(module)
            return True
        except ImportError:
            continue
    return False


def render(
    url: str,
    settings: Settings,
    *,
    dismiss_gates: bool = True,
    timeout_s: Optional[float] = None,
    headless: Optional[bool] = None,
) -> FetchResult:
    """Fetch one URL through the stealth browser. Never raises."""
    budget = float(timeout_s or settings.timeout_s + 30)
    result = FetchResult(url=url, final_url=url, method="browser")
    if not available():
        result.error = ("no browser driver installed (pip install patchright) — "
                        "run the HTTP rung only")
        return result

    request = {
        "url": url,
        "channel": settings.browser_channel,
        "headless": settings.headless if headless is None else bool(headless),
        "timeout_s": settings.timeout_s,
        "proxy": settings.proxy,
        "dismiss_gates": dismiss_gates,
        "patterns": pattern_payload(),
    }

    env = dict(os.environ)
    # The worker imports `gates` from the plugin root; keep that on the path.
    env["PYTHONPATH"] = str(PLUGIN_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.setdefault("PYTHONIOENCODING", "utf-8")

    started = time.monotonic()
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(PLUGIN_DIR),
        )
        raw_out, raw_err = proc.communicate(json.dumps(request), timeout=budget)
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        result.error = f"browser rung exceeded {budget:.0f}s and was terminated"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    elapsed = int((time.monotonic() - started) * 1000)
    payload: dict[str, Any] = {}
    try:
        payload = json.loads((raw_out or "").strip() or "{}")
    except json.JSONDecodeError:
        snippet = (raw_err or raw_out or "")[-400:]
        result.error = f"browser worker returned no JSON ({snippet.strip()[:300]})"
        result.elapsed_ms = elapsed
        return result

    if not payload.get("ok"):
        result.error = str(payload.get("error") or "browser rung failed")
        if raw_err and not result.error:
            result.error = raw_err[-300:]
        result.elapsed_ms = elapsed
        return result

    html = payload.get("html") or ""
    result.text = html
    result.content = html.encode("utf-8", "replace")
    result.content_type = "text/html; charset=utf-8"
    result.final_url = payload.get("final_url") or url
    result.status = int(payload.get("status") or 200)
    result.method = "browser"
    result.elapsed_ms = int(payload.get("elapsed_ms") or elapsed)
    result.gates_clicked = list(payload.get("gates_clicked") or [])
    result.headers = {"x-netcrawler-channel": str(payload.get("channel") or "")}
    result.blocked = "challenge-page" if payload.get("blocked") else ""
    result.ok = bool(html) and not result.blocked
    result.extra_cookies = dict(payload.get("cookies") or {})  # type: ignore[attr-defined]
    return result