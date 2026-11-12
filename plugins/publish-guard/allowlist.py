"""publish-guard's own state: findings the user allowed, scans the user verified, and a per-install salt.

Only the user's own terminal should change these files. The hook refuses agent tool calls that name them
or run the allow command; an agent running as the same OS user could still write them by other means,
which is why every change here is a deliberate, confirmed CLI action and every entry is dated.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from .profile import hermes_home

VERIFIED_DAYS = 7


def state_dir() -> Path:
    return hermes_home() / "plugin-data" / "publish-guard"


def _read(name: str) -> dict:
    try:
        data = json.loads((state_dir() / name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict) -> None:
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=f".{name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, folder / name)


def path() -> Path:
    return state_dir() / "allowed.json"


def load() -> dict:
    return _read("allowed.json")


def allow(finding_id: str, reason: str = "") -> None:
    data = load()
    data[finding_id] = {"allowed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}
    _write("allowed.json", data)


def forget(finding_id: str | None) -> int:
    data = load()
    if finding_id is None:
        _write("allowed.json", {})
        return len(data)
    removed = 1 if data.pop(finding_id, None) is not None else 0
    _write("allowed.json", data)
    return removed


def verify(state_key: str) -> None:
    """Record that the user ran a complete, clean scan of exactly this state."""
    data = {k: v for k, v in _read("verified.json").items()
            if isinstance(v, (int, float)) and time.time() - v < VERIFIED_DAYS * 86400}
    data[state_key] = time.time()
    _write("verified.json", data)


def is_verified(state_key: str) -> bool:
    stamp = _read("verified.json").get(state_key) if state_key else None
    return isinstance(stamp, (int, float)) and time.time() - stamp < VERIFIED_DAYS * 86400


def salt() -> bytes:
    """Per-install key for finding ids: a different secret value always gets a different id."""
    target = state_dir() / "salt"
    try:
        value = target.read_bytes()
        if len(value) >= 16:
            return value
    except OSError:
        pass
    value = secrets.token_bytes(32)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    except OSError:
        pass
    return value
