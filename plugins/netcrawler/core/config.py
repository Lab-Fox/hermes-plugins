"""Runtime settings.

The plugin shell reads ``plugins.entries.netcrawler.settings`` and passes a plain
mapping in; this module never touches Hermes. Every key has a default so the
engine is fully usable with an empty mapping (keyless, zero-config).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

# A current desktop Chrome UA. Kept in one place: the HTTP rung, the browser rung
# and the clearance-replay path must agree, or Cloudflare rejects the replayed
# cf_clearance cookie (it is bound to the UA that earned it).
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

DEFAULTS: dict[str, Any] = {
    "browser_channel": "chrome",
    "headless": True,
    "max_pages": 40,
    "max_depth": 2,
    "concurrency": 4,
    "request_delay_ms": 400,
    "respect_robots": True,
    "archive_fallback": False,
    "timeout_s": 30,
    "corpus_max_docs": 100_000,
    "proxy": "",
    "max_page_bytes": 8_000_000,
}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """Resolved engine settings."""

    data_dir: Path
    browser_channel: str = "chrome"
    headless: bool = True
    max_pages: int = 40
    max_depth: int = 2
    concurrency: int = 4
    request_delay_ms: int = 400
    respect_robots: bool = True
    archive_fallback: bool = False
    timeout_s: int = 30
    corpus_max_docs: int = 100_000
    proxy: str = ""
    max_page_bytes: int = 8_000_000
    user_agent: str = CHROME_UA
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        data_dir: str | Path,
        values: Optional[Mapping[str, Any]] = None,
        *,
        overrides: Optional[Mapping[str, Any]] = None,
    ) -> "Settings":
        merged: dict[str, Any] = dict(DEFAULTS)
        for source in (values or {}, overrides or {}):
            for key, val in source.items():
                if val is not None:
                    merged[key] = val
        channel = str(merged.get("browser_channel") or "chrome").strip().lower()
        if channel not in ("chrome", "msedge", "chromium"):
            channel = "chrome"
        return cls(
            data_dir=Path(data_dir),
            browser_channel=channel,
            headless=_as_bool(merged.get("headless"), True),
            max_pages=_as_int(merged.get("max_pages"), 40, 1, 5000),
            max_depth=_as_int(merged.get("max_depth"), 2, 0, 12),
            concurrency=_as_int(merged.get("concurrency"), 4, 1, 16),
            request_delay_ms=_as_int(merged.get("request_delay_ms"), 400, 0, 60_000),
            respect_robots=_as_bool(merged.get("respect_robots"), True),
            archive_fallback=_as_bool(merged.get("archive_fallback"), False),
            timeout_s=_as_int(merged.get("timeout_s"), 30, 5, 180),
            corpus_max_docs=_as_int(merged.get("corpus_max_docs"), 100_000, 100, 5_000_000),
            proxy=str(merged.get("proxy") or "").strip(),
            max_page_bytes=_as_int(merged.get("max_page_bytes"), 8_000_000, 10_000, 200_000_000),
            user_agent=str(merged.get("user_agent") or CHROME_UA),
            extra={k: v for k, v in merged.items() if k not in DEFAULTS},
        )

    @property
    def corpus_path(self) -> Path:
        return self.data_dir / "corpus.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "browser").mkdir(parents=True, exist_ok=True)