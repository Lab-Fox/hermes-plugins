"""Build the picture of what is private on this PC, from local sources only.

Secret values are read from the Hermes .env and from secret-named environment variables, kept in
memory, and only ever compared. Nothing here is logged or sent anywhere.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from pathlib import Path

from . import gitscan
from . import guard_core as core

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PAT|AUTH|CREDENTIAL|PRIVATE|SESSION|COOKIE)",
                          re.IGNORECASE)
_NOT_SECRET_VALUE = re.compile(r"^(true|false|yes|no|on|off|none|null|\d+(\.\d+)?|https?://[^@\s]*)$", re.IGNORECASE)
_TTL = 120  # seconds: a new key or label is picked up within two minutes
# Labels that describe a plan or role rather than naming an account: they appear in ordinary code.
_PLAN_WORDS = frozenset({"paygo", "payg", "subscription", "sub", "subs", "paid", "free", "trial", "plan", "pro",
                         "plus", "team", "teams", "work", "personal", "main", "alt", "backup", "primary",
                         "secondary", "mixed", "default", "billing", "credits", "balance", "zen", "go", "api",
                         "key", "keys", "token", "account", "admin", "user", "test", "dev", "prod", "staging"})
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "profile": None}


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("HERMES_HOME") or Path(os.environ.get("LOCALAPPDATA", "~")) / "hermes").expanduser()


def _env_file_values(path: Path) -> dict[str, str]:
    """value -> setting name, for values in a .env file that look like secrets."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        value = _dotenv_value(value)
        if _looks_secret(name, value):
            out[value] = name
    return out


def _dotenv_value(raw: str) -> str:
    """The value as dotenv reads it: up to the closing quote, or before an inline ` # comment`."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _looks_secret(name: str, value: str) -> bool:
    if len(value) < 12 or _NOT_SECRET_VALUE.match(value) or " " in value:
        return False
    # A secret-named setting, or a long random-looking value under any name (keys stored under a label, like *_WORK).
    return bool(_SECRET_NAME.search(name)) or (len(value) >= 20 and bool(re.search(r"\d", value))
                                              and bool(re.search(r"[A-Za-z]", value)))


def _common_words() -> set[str]:
    """English words, so a key label like 'future' is not flagged on every 'from __future__'."""
    candidates = [hermes_home() / "desktop-plugins" / "word-suggest" / "src" / "words.txt"]
    for path in candidates:
        try:
            return set(path.read_text(encoding="utf-8").split())
        except OSError:
            continue
    return {"main", "paid", "free", "work", "home", "test", "future", "default", "personal", "backup", "primary",
            "secondary", "old", "new", "dev", "prod", "staging", "local"}


def _key_labels(home: Path) -> set[str]:
    labels: set[str] = set()
    for store in (home / "plugin-data").glob("*/*.json"):
        if store.name not in ("keys.json", "accounts.json"):
            continue
        try:
            data = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sections = data.get("providers", {}) if isinstance(data, dict) else {}
        for section in (sections.values() if isinstance(sections, dict) else []):
            if isinstance(section, dict):
                labels.update((section.get("keys") or {}).keys())
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accounts, dict):
            labels.update(accounts.keys())
    return {str(label) for label in labels if label}


def settings() -> dict:
    try:
        from hermes_cli.config import load_config

        entry = ((load_config().get("plugins") or {}).get("entries") or {}).get("publish-guard") or {}
        return entry.get("settings") or {}
    except Exception:  # noqa: BLE001
        return {}


def build_profile(repo_dir: str | None = None) -> core.Profile:
    home = hermes_home()
    secrets = _env_file_values(home / ".env")
    for name, value in os.environ.items():
        value = (value or "").strip()
        if _SECRET_NAME.search(name) and _looks_secret(name, value):
            secrets.setdefault(value, name)
    config = settings()
    emails = {e for e in (gitscan.git_config(None, "user.email"),
                          gitscan.git_config(repo_dir, "user.email") if repo_dir else "") if e}
    emails |= {str(e).strip() for e in (config.get("emails") or []) if str(e).strip()}
    emails = {e for e in emails if not core.NOREPLY.search(e)}
    home_names = {Path.home().name, os.environ.get("USERNAME", ""), os.environ.get("USER", "")}
    hostnames = {h for h in {os.environ.get("COMPUTERNAME", ""), socket.gethostname().split(".")[0]}
                 if len(h) >= 5 and h.lower() not in ("localhost", "desktop", "laptop")}
    common = _common_words() | _PLAN_WORDS
    labels = {label for label in _key_labels(home) if len(label) >= 5 and label.lower() not in common}
    labels -= {str(t).strip() for t in (config.get("ignore_terms") or [])}
    labels |= {str(t).strip() for t in (config.get("extra_terms") or []) if len(str(t).strip()) >= 4}
    from .allowlist import salt  # late: allowlist imports this module

    return core.Profile(secret_values=secrets, emails=emails, home_names={n for n in home_names if n},
                        hostnames=hostnames, labels=labels, salt=salt())


def cached_profile(repo_dir: str | None = None) -> core.Profile:
    """The profile, rebuilt at most every couple of minutes (repo-specific email added each time)."""
    with _lock:
        fresh = _cache["profile"] is not None and time.monotonic() - _cache["at"] < _TTL
        base = _cache["profile"] if fresh else None
    if base is None:
        base = build_profile()
        with _lock:
            _cache.update(at=time.monotonic(), profile=base)
    repo_email = gitscan.git_config(repo_dir, "user.email") if repo_dir else ""
    if repo_email and repo_email not in base.emails and not core.NOREPLY.search(repo_email):
        return core.Profile(secret_values=base.secret_values, emails=base.emails | {repo_email},
                            home_names=base.home_names, hostnames=base.hostnames, labels=base.labels, salt=base.salt)
    return base
