"""Named NanoGPT accounts: several API keys, one deliberately chosen as active.

Secrets stay where Hermes keeps secrets: ``<hermes home>/.env``. This module only
records which env var each label points at and which label is active, in
``<hermes home>/plugin-data/nanogpt-accounts/accounts.json`` (no key material).

There is no automatic failover between accounts. A subscription key that runs
out of quota fails loudly instead of silently spending a Pay As You Go balance.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROVIDER = "nanogpt"
API_ROOT = "https://api.nano-gpt.com/api"
BASE_URL = f"{API_ROOT}/v1"
SUBSCRIPTION_BASE_URL = f"{API_ROOT}/subscription/v1"
USER_AGENT = "HermesAgent-nanogpt-accounts/2.0"

# Env vars the provider has always read; they become accounts on first use.
LEGACY_ENV_VARS = ("NANOGPT_API_KEY", "HERMES_CUSTOM_NANOGPT_API_KEY")
ENV_PREFIX = "NANOGPT_API_KEY_"

KIND_SUBSCRIPTION = "subscription"  # key may only spend the subscription
KIND_PAYGO = "paygo"                # no active subscription: balance only
KIND_MIXED = "mixed"                # subscription first, balance allowed
KIND_UNKNOWN = "unknown"
KINDS = (KIND_SUBSCRIPTION, KIND_PAYGO, KIND_MIXED, KIND_UNKNOWN)

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_lock = threading.RLock()
_cache: dict[str, tuple[tuple, dict]] = {}


class AccountError(ValueError):
    """A user-facing problem (bad label, unknown account, missing key)."""


@dataclass(frozen=True)
class Account:
    label: str
    env_var: str
    kind: str
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
    return _home() / "plugin-data" / "nanogpt-accounts" / "accounts.json"


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
        Account(label, str(row.get("env") or ""), str(row.get("kind") or KIND_UNKNOWN), label == active)
        for label, row in sorted(data["accounts"].items())
        if isinstance(row, dict)
    ]


def get_account(label: str) -> Account:
    clean = (label or "").strip().lower()
    for account in list_accounts():
        if account.label == clean:
            return account
    known = ", ".join(a.label for a in list_accounts()) or "none yet"
    raise AccountError(f"No NanoGPT account named '{label}'. Known accounts: {known}.")


def active_account() -> Account | None:
    for account in list_accounts():
        if account.active:
            return account
    return None


def account_for_key(key: str) -> Account | None:
    if not key:
        return None
    for account in list_accounts():
        if account.key == key:
            return account
    return None


def add_account(label: str, key: str, *, kind: str | None = None) -> Account:
    """Store *key* in ``.env`` under a label. The first account becomes active."""
    clean = normalize_label(label)
    key = (key or "").strip()
    if not key:
        raise AccountError("The API key is empty.")
    data = _mutable()
    if clean in data["accounts"]:
        raise AccountError(f"An account named '{clean}' already exists. Remove it first or pick another label.")
    duplicate = account_for_key(key)
    if duplicate is not None:
        raise AccountError(f"That key is already saved as '{duplicate.label}'.")
    env_var = ENV_PREFIX + re.sub(r"[^A-Z0-9]", "_", clean.upper())
    if any(isinstance(row, dict) and row.get("env") == env_var for row in data["accounts"].values()):
        raise AccountError(f"'{clean}' is too close to an existing label. Pick a more distinct one.")
    _write_secret(env_var, key)
    data["accounts"][clean] = {"env": env_var, "kind": kind if kind in KINDS else detect_kind(key)}
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
    original ``NANOGPT_API_KEY`` style vars are left alone (other plugins read them)."""
    account = get_account(label)
    data = _mutable()
    data["accounts"].pop(account.label, None)
    if data.get("active") == account.label:
        data["active"] = next(iter(sorted(data["accounts"])), "")
    _save(data)
    if account.env_var.startswith(ENV_PREFIX):
        _delete_secret(account.env_var)
    return account


def set_kind(label: str, kind: str) -> Account:
    account = get_account(label)
    if kind not in KINDS:
        raise AccountError(f"Kind must be one of: {', '.join(KINDS)}.")
    data = _mutable()
    data["accounts"][account.label]["kind"] = kind
    _save(data)
    return get_account(account.label)


def adopt_existing_keys() -> list[Account]:
    """One-time takeover of keys Hermes already knows.

    Each legacy env var that holds a key becomes an account pointing at that var
    (nothing is copied). The provider's credential-pool rows are then removed:
    the pool rotates to the next key on a billing error, which is exactly the
    automatic failover this plugin exists to prevent.
    """
    adopted: list[Account] = []
    data = _mutable()
    known_envs = {str(row.get("env")) for row in data["accounts"].values() if isinstance(row, dict)}
    seen_keys = {a.key for a in list_accounts() if a.key}
    for env_var in LEGACY_ENV_VARS:
        key = read_secret(env_var)
        if not key or env_var in known_envs or key in seen_keys:
            continue
        kind = detect_kind(key)
        label = _free_label(data, _suggest_label(kind))
        data["accounts"][label] = {"env": env_var, "kind": kind}
        seen_keys.add(key)
        if not data.get("active"):
            data["active"] = label
        _save(data)
        adopted.append(get_account(label))
    _drop_pool_rows()
    return adopted


def _suggest_label(kind: str) -> str:
    return {KIND_SUBSCRIPTION: "sub", KIND_PAYGO: "paygo", KIND_MIXED: "sub"}.get(kind, "key")


def _free_label(data: dict, base: str) -> str:
    if base not in data["accounts"]:
        return base
    n = 2
    while f"{base}{n}" in data["accounts"]:
        n += 1
    return f"{base}{n}"


def _drop_pool_rows() -> int:
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(PROVIDER)
        removed = 0
        while pool.entries():
            if pool.remove_index(1) is None:
                break
            removed += 1
        return removed
    except Exception:
        return 0


# ── NanoGPT account API ───────────────────────────────────────────────────────


def _request(url: str, key: str, *, method: str = "GET", timeout: float = 4.0) -> Any:
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("x-api-key", key)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
        return json.loads(response.read().decode("utf-8"))


def subscription_usage(key: str, *, timeout: float = 4.0) -> dict | None:
    try:
        data = _request(f"{SUBSCRIPTION_BASE_URL}/usage", key, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def balance(key: str, *, timeout: float = 4.0) -> dict | None:
    try:
        data = _request(f"{API_ROOT}/check-balance", key, method="POST", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def kind_from_usage(usage: dict | None) -> str:
    if not isinstance(usage, dict):
        return KIND_UNKNOWN
    if not usage.get("active"):
        return KIND_PAYGO
    routing = usage.get("routing") if isinstance(usage.get("routing"), dict) else {}
    # The key's own billing setting, when NanoGPT reports it, is the direct answer.
    mode = str(routing.get("billingMode") or "").lower()
    if mode in ("payg_only", "paygo_only", "paid_only"):
        return KIND_PAYGO
    if mode == "subscription_only":
        return KIND_SUBSCRIPTION
    if mode == "both":
        return KIND_MIXED
    allows_balance = routing.get("paidSpendPolicyAllowsBalance")
    if allows_balance is False:
        return KIND_SUBSCRIPTION
    # A key set to bill the balance even though subscription quota is left is a
    # Pay As You Go key that happens to live on a subscribed account.
    if routing.get("recommendedMode") == "paygo" and routing.get("subscriptionQuotaAvailable") is not False:
        return KIND_PAYGO
    if allows_balance is True:
        return KIND_MIXED
    return KIND_UNKNOWN


def detect_kind(key: str) -> str:
    return kind_from_usage(subscription_usage(key))


def models_base_url(kind: str) -> str:
    """Catalog that matches what the key can actually be billed for."""
    return SUBSCRIPTION_BASE_URL if kind == KIND_SUBSCRIPTION else BASE_URL
