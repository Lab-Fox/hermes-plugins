"""Named API keys per provider, one deliberately chosen as active.

Shared by every provider this plugin adds or wraps. Secrets stay in
``<hermes home>/.env``; this module records only which env var each label points
at and which label is active per provider, in
``<hermes home>/plugin-data/provider-keys/keys.json`` (no key material).

There is no automatic rotation between keys: switching is always a deliberate
command, so a free key never silently hands over to a paid one.

A provider with no saved keys is left exactly as Hermes ships it.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_lock = threading.RLock()
_cache: dict[str, tuple[tuple, dict]] = {}

# provider -> env vars Hermes itself reads for it (filled in by the provider plugin).
LEGACY_ENV_VARS: dict[str, tuple[str, ...]] = {}

# provider -> the env var Hermes itself checks to decide the provider is set up
# (model picker, "explicitly configured" checks, OpenRouter's key lookup). The
# active key is copied into it on every switch; saved keys each keep their own
# separate var, so nothing is lost when the copy is overwritten.
ENV_MIRROR: dict[str, str] = {}


def _sync_mirror(provider: str) -> None:
    mirror = ENV_MIRROR.get(provider)
    if not mirror:
        return
    key = active_key(provider)
    if key is None:
        # Last key removed: a leftover copy would keep the provider listed with a dead key.
        if read_secret(mirror):
            _delete_secret(mirror)
    elif key.secret and read_secret(mirror) != key.secret:
        _write_secret(mirror, key.secret)


class KeyError_(ValueError):
    """A user-facing problem (bad label, unknown provider or key)."""


@dataclass(frozen=True)
class Key:
    provider: str
    label: str
    env_var: str
    active: bool
    skip: bool = False  # never picked by auto-cycling; only used when made active by hand

    @property
    def secret(self) -> str:
        return read_secret(self.env_var)

    @property
    def masked(self) -> str:
        secret = self.secret
        if not secret:
            return "(key missing from .env)"
        return f"...{secret[-4:]}" if len(secret) > 8 else "..."


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


def _drop_pool_rows(provider: str) -> int:
    """Empty the provider's credential pool: pooled keys rotate on billing errors."""
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(provider)
        removed = 0
        while pool.entries():
            if pool.remove_index(1) is None:
                break
            removed += 1
        return removed
    except Exception:
        return 0


# ── Store ─────────────────────────────────────────────────────────────────────


def store_path() -> Path:
    return _home() / "plugin-data" / "provider-keys" / "keys.json"


def _empty() -> dict:
    return {"version": 1, "providers": {}}


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
        if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
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


def _section(data: dict, provider: str) -> dict:
    section = data["providers"].setdefault(provider, {})
    section.setdefault("active", "")
    if not isinstance(section.get("keys"), dict):
        section["keys"] = {}
    return section


def normalize_label(label: str) -> str:
    clean = (label or "").strip().lower()
    if not _LABEL_RE.match(clean):
        raise KeyError_(
            f"'{label}' is not a valid label. Use 1-32 characters: a-z, 0-9, '-' or '_', "
            "starting with a letter or digit."
        )
    return clean


def providers_with_keys() -> list[str]:
    return sorted(p for p, s in _load()["providers"].items() if isinstance(s, dict) and s.get("keys"))


def list_keys(provider: str) -> list[Key]:
    section = _load()["providers"].get(provider)
    if not isinstance(section, dict) or not isinstance(section.get("keys"), dict):
        return []
    active = section.get("active") or ""
    return [Key(provider, label, str(row.get("env") or ""), label == active, bool(row.get("skip")))
            for label, row in sorted(section["keys"].items()) if isinstance(row, dict)]


# ── Auto-cycling and the free-only model list ─────────────────────────────────


def auto_cycle(provider: str) -> bool:
    """Whether Hermes may move to the provider's next key when one hits its limit (default on)."""
    section = _load()["providers"].get(provider)
    return not isinstance(section, dict) or section.get("auto", True) is not False


def set_auto_cycle(provider: str, on: bool) -> None:
    data = _mutable()
    _section(data, provider)["auto"] = bool(on)
    _save(data)
    _drop_pool_rows(provider)


def set_skip(provider: str, label: str, on: bool) -> Key:
    key = get_key(provider, label)
    data = _mutable()
    row = _section(data, provider)["keys"][key.label]
    if on:
        row["skip"] = True
    else:
        row.pop("skip", None)
    _save(data)
    _drop_pool_rows(provider)
    return get_key(provider, key.label)


def cycle_order(provider: str) -> list[Key]:
    """Keys Hermes may use for *provider*, in order: the active one, then the rest.

    With auto-cycling off this is just the active key. Keys marked ``skip`` are
    left out unless they are the active one (chosen by hand).
    """
    keys = [k for k in list_keys(provider) if k.secret]
    active = [k for k in keys if k.active]
    if not auto_cycle(provider):
        return active
    start = keys.index(active[0]) if active else 0
    rotated = keys[start:] + keys[:start]  # carry on from the active key, wrapping round
    return [k for k in rotated if k.active or not k.skip]


def show_paid() -> bool:
    """False (default): model lists show only what the provider gives for free."""
    return _load().get("show_paid") is True


def set_show_paid(on: bool) -> None:
    data = _mutable()
    data["show_paid"] = bool(on)
    _save(data)


def sync_all() -> None:
    """Make Hermes's own env var for every provider with saved keys hold the active key."""
    for provider in providers_with_keys():
        if provider in ENV_MIRROR:
            adopt_existing_keys(provider)
            _sync_mirror(provider)


def get_key(provider: str, label: str) -> Key:
    clean = (label or "").strip().lower()
    for key in list_keys(provider):
        if key.label == clean:
            return key
    known = ", ".join(k.label for k in list_keys(provider)) or "none yet"
    raise KeyError_(f"No {provider} key named '{label}'. Saved keys: {known}.")


def active_key(provider: str) -> Key | None:
    for key in list_keys(provider):
        if key.active:
            return key
    return None


def _env_name(provider: str, label: str) -> str:
    slug = lambda text: re.sub(r"[^A-Z0-9]", "_", text.upper())  # noqa: E731
    return f"{slug(provider)}_API_KEY_{slug(label)}"


def adopt_existing_keys(provider: str) -> list[Key]:
    """Take over the keys Hermes already has for *provider*, then empty its pool.

    Each env var Hermes reads for the provider that holds a key becomes a saved
    key pointing at that var (nothing is copied or moved).
    """
    adopted: list[Key] = []
    data = _mutable()
    section = _section(data, provider)
    known_envs = {str(row.get("env")) for row in section["keys"].values() if isinstance(row, dict)}
    seen = {k.secret for k in list_keys(provider) if k.secret}
    for env_var in LEGACY_ENV_VARS.get(provider, ()):
        secret = read_secret(env_var)
        if not secret or env_var in known_envs or secret in seen:
            continue
        label, n = "main", 2
        while label in section["keys"]:
            label, n = f"main{n}", n + 1
        if env_var == ENV_MIRROR.get(provider):
            # The mirror var gets overwritten on every switch: give this key its own var.
            env_var = _env_name(provider, label)
            _write_secret(env_var, secret)
        section["keys"][label] = {"env": env_var}
        seen.add(secret)
        if not section["active"]:
            section["active"] = label
        _save(data)
        adopted.append(get_key(provider, label))
    if adopted:
        _drop_pool_rows(provider)  # rebuilt from the saved keys, in cycle order, on next use
    return adopted


def add_key(provider: str, label: str, secret: str) -> Key:
    """Store *secret* in ``.env`` under a label. A provider's first key becomes active."""
    clean = normalize_label(label)
    secret = (secret or "").strip()
    if not secret:
        raise KeyError_("The API key is empty.")
    adopt_existing_keys(provider)
    data = _mutable()
    section = _section(data, provider)
    if clean in section["keys"]:
        raise KeyError_(f"{provider} already has a key named '{clean}'. Remove it first or pick another label.")
    for key in list_keys(provider):
        if key.secret == secret:
            raise KeyError_(f"That key is already saved as '{key.label}'.")
    env_var = _env_name(provider, clean)
    if any(isinstance(row, dict) and row.get("env") == env_var for row in section["keys"].values()):
        raise KeyError_(f"'{clean}' is too close to an existing label. Pick a more distinct one.")
    _write_secret(env_var, secret)
    section["keys"][clean] = {"env": env_var}
    if not section["active"]:
        section["active"] = clean
    _save(data)
    _sync_mirror(provider)
    _drop_pool_rows(provider)
    return get_key(provider, clean)


def use_key(provider: str, label: str) -> Key:
    key = get_key(provider, label)
    if not key.secret:
        raise KeyError_(f"Key '{key.label}' is empty: {key.env_var} is missing from .env.")
    data = _mutable()
    _section(data, provider)["active"] = key.label
    _save(data)
    _sync_mirror(provider)
    _drop_pool_rows(provider)  # restart the cycle from the key just chosen
    return get_key(provider, key.label)


def rename_key(provider: str, old: str, new: str) -> Key:
    key = get_key(provider, old)
    clean = normalize_label(new)
    data = _mutable()
    section = _section(data, provider)
    if clean in section["keys"]:
        raise KeyError_(f"{provider} already has a key named '{clean}'.")
    section["keys"][clean] = section["keys"].pop(key.label)
    if section["active"] == key.label:
        section["active"] = clean
    _save(data)
    _drop_pool_rows(provider)
    return get_key(provider, clean)


def remove_key(provider: str, label: str) -> Key:
    """Forget a key and delete its secret from ``.env``. Hermes's own env var for
    the provider follows the active key, and is cleared when the last key goes."""
    key = get_key(provider, label)
    data = _mutable()
    section = _section(data, provider)
    section["keys"].pop(key.label, None)
    if section["active"] == key.label:
        section["active"] = next(iter(sorted(section["keys"])), "")
    _save(data)
    if key.env_var == _env_name(provider, key.label):
        _delete_secret(key.env_var)
    _sync_mirror(provider)
    _drop_pool_rows(provider)
    return key
