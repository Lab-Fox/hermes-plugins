"""Named OpenCode accounts: several Console API keys, one deliberately chosen as active.

One account is one OpenCode Console API key. The active key is used for both
Hermes OpenCode providers (``opencode-zen`` pay-per-use and ``opencode-go``
subscription), so switching account switches both.

Secrets stay in ``<hermes home>/.env``. This module records only which env var
each label points at and which label is active, in
``<hermes home>/plugin-data/opencode-accounts/accounts.json`` (no key material).

There is no automatic rotation between accounts: switching is always a
deliberate command.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

PROVIDERS = ("opencode-zen", "opencode-go")
# Env vars the built-in providers read; they become accounts on first use.
LEGACY_ENV_VARS = ("OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY")
ENV_PREFIX = "OPENCODE_API_KEY_"

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_lock = threading.RLock()
_cache: dict[str, tuple[tuple, dict]] = {}


class AccountError(ValueError):
    """A user-facing problem (bad label, unknown account, missing key)."""


@dataclass(frozen=True)
class Account:
    label: str
    env_var: str
    active: bool

    @property
    def key(self) -> str:
        return read_secret(self.env_var)

    @property
    def masked(self) -> str:
        key = self.key
        if not key:
            return "(key missing from .env)"
        return f"...{key[-4:]}" if len(key) > 8 else "..."


# ── Hermes seams (late imports: this module loads during provider discovery) ──


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def read_secret(env_var: str) -> str:
    if not env_var:
        return ""
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv

        return (get_env_value_prefer_dotenv(env_var) or "").strip()
    except Exception:
        return (os.environ.get(env_var) or "").strip()


def _write_secret(env_var: str, value: str) -> None:
    from hermes_cli.config import save_env_value

    save_env_value(env_var, value)


def _delete_secret(env_var: str) -> None:
    from hermes_cli.config import remove_env_value

    remove_env_value(env_var)


# ── Store ─────────────────────────────────────────────────────────────────────


def store_path() -> Path:
    return _home() / "plugin-data" / "opencode-accounts" / "accounts.json"


def _empty() -> dict:
    return {"version": 1, "active": "", "accounts": {}}


def _load() -> dict:
    path = store_path()
    try:
        stat = path.stat()
    except OSError:
        return _empty()
    signature = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        cached = _cache.get(str(path))
        if cached and cached[0] == signature:
            return cached[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _empty()
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            return _empty()
        _cache[str(path)] = (signature, data)
        return data


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    with _lock:
        _cache.pop(str(path), None)


def _mutable() -> dict:
    return json.loads(json.dumps(_load()))


def normalize_label(label: str) -> str:
    clean = (label or "").strip().lower()
    if not _LABEL_RE.match(clean):
        raise AccountError(
            f"'{label}' is not a valid label. Use 1-32 characters: a-z, 0-9, '-' or '_', "
            "starting with a letter or digit."
        )
    return clean


def list_accounts() -> list[Account]:
    data = _load()
    active = data.get("active") or ""
    return [
        Account(label, str(row.get("env") or ""), label == active)
        for label, row in sorted(data["accounts"].items())
        if isinstance(row, dict)
    ]


def get_account(label: str) -> Account:
    clean = (label or "").strip().lower()
    for account in list_accounts():
        if account.label == clean:
            return account
    known = ", ".join(a.label for a in list_accounts()) or "none yet"
    raise AccountError(f"No OpenCode account named '{label}'. Known accounts: {known}.")


def active_account() -> Account | None:
    for account in list_accounts():
        if account.active:
            return account
    return None


def add_account(label: str, key: str) -> Account:
    """Store *key* in ``.env`` under a label. The first account becomes active."""
    clean = normalize_label(label)
    key = (key or "").strip()
    if not key:
        raise AccountError("The API key is empty.")
    data = _mutable()
    if clean in data["accounts"]:
        raise AccountError(f"An account named '{clean}' already exists. Remove it first or pick another label.")
    for account in list_accounts():
        if account.key == key:
            raise AccountError(f"That key is already saved as '{account.label}'.")
    env_var = ENV_PREFIX + re.sub(r"[^A-Z0-9]", "_", clean.upper())
    if any(isinstance(row, dict) and row.get("env") == env_var for row in data["accounts"].values()):
        raise AccountError(f"'{clean}' is too close to an existing label. Pick a more distinct one.")
    _write_secret(env_var, key)
    data["accounts"][clean] = {"env": env_var}
    if not data.get("active"):
        data["active"] = clean
    _save(data)
    return get_account(clean)


def use_account(label: str) -> Account:
    account = get_account(label)
    if not account.key:
        raise AccountError(f"Account '{account.label}' has no key: {account.env_var} is missing from .env.")
    data = _mutable()
    data["active"] = account.label
    _save(data)
    return get_account(account.label)


def rename_account(old: str, new: str) -> Account:
    account = get_account(old)
    clean = normalize_label(new)
    data = _mutable()
    if clean in data["accounts"]:
        raise AccountError(f"An account named '{clean}' already exists.")
    data["accounts"][clean] = data["accounts"].pop(account.label)
    if data.get("active") == account.label:
        data["active"] = clean
    _save(data)
    return get_account(clean)


def remove_account(label: str) -> Account:
    """Forget an account. Keys this plugin wrote are deleted from ``.env``; the
    built-in ``OPENCODE_*_API_KEY`` vars are left alone."""
    account = get_account(label)
    data = _mutable()
    data["accounts"].pop(account.label, None)
    if data.get("active") == account.label:
        data["active"] = next(iter(sorted(data["accounts"])), "")
    _save(data)
    if account.env_var.startswith(ENV_PREFIX):
        _delete_secret(account.env_var)
    return account


def adopt_existing_keys() -> list[Account]:
    """One-time takeover of OpenCode keys Hermes already knows.

    Each built-in env var that holds a key becomes an account pointing at that
    var (nothing is copied), then the providers' credential-pool rows are
    removed so the pool cannot rotate between accounts on its own.
    """
    adopted: list[Account] = []
    data = _mutable()
    known_envs = {str(row.get("env")) for row in data["accounts"].values() if isinstance(row, dict)}
    seen_keys = {a.key for a in list_accounts() if a.key}
    for env_var in LEGACY_ENV_VARS:
        key = read_secret(env_var)
        if not key or env_var in known_envs or key in seen_keys:
            continue
        label, n = "main", 2
        while label in data["accounts"]:
            label, n = f"main{n}", n + 1
        data["accounts"][label] = {"env": env_var}
        seen_keys.add(key)
        if not data.get("active"):
            data["active"] = label
        _save(data)
        adopted.append(get_account(label))
    _drop_pool_rows()
    return adopted


def _drop_pool_rows() -> int:
    removed = 0
    for provider in PROVIDERS:
        try:
            from agent.credential_pool import load_pool

            pool = load_pool(provider)
            while pool.entries():
                if pool.remove_index(1) is None:
                    break
                removed += 1
        except Exception:
            continue
    return removed
