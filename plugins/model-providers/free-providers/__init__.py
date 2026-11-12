"""Free-tier providers with switchable keys.

Two things happen here, both driven by ``catalog.py``:

* Providers Hermes does not ship (ModelScope, LLM7, OrcaRouter, ...) are
  registered as plain OpenAI-compatible API-key providers.
* Providers Hermes already ships (NVIDIA, OpenRouter, ...) are re-registered as
  a subclass of their own profile class that only changes the key lookup, so
  their headers, reasoning controls and catalogs stay Hermes's.

Every one of them resolves its API key from ``provider_keys.py``: several keys
saved under labels, one of which starts first. Hermes's credential pool moves to
the next key when one is limited (see ``limits.py`` for why that needed help). A
provider with no saved keys behaves exactly as Hermes ships it.

Switch with ``hermes keys use <provider> <label>`` or ``/keys use ...`` (the
``provider-keys`` plugin).
"""

from __future__ import annotations

import copy
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import limits  # noqa: E402
import provider_keys as keys  # noqa: E402 -- Hermes imports plugin dirs without a package name
import providers  # noqa: E402
from providers import register_provider  # noqa: E402
from providers.base import ProviderProfile  # noqa: E402

_spec = importlib.util.spec_from_file_location("free_providers_catalog", _PLUGIN_DIR / "catalog.py")
catalog = importlib.util.module_from_spec(_spec)
sys.modules["free_providers_catalog"] = catalog
_spec.loader.exec_module(catalog)

logger = logging.getLogger(__name__)
_BUNDLED_DIR = Path(providers.__file__).resolve().parent.parent / "plugins" / "model-providers"


def _auth_handler(action: str, args: Any) -> bool:
    """``hermes auth add|status <provider>`` go to the key store, never the pool."""
    provider = str(getattr(args, "provider", "") or "").strip().lower()
    profile = providers.get_provider_profile(provider) if provider else None
    provider = profile.name if profile is not None else provider
    if not provider:
        return False
    if action == "add":
        import getpass

        label = getattr(args, "label", None) or input(f"Label for this {provider} key (e.g. free, paid): ")
        secret = getattr(args, "api_key", None) or getpass.getpass(f"{provider} API key (hidden): ")
        try:
            key = keys.add_key(provider, label, secret)
        except keys.KeyError_ as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Saved {provider} key '{key.label}'. Switch to it with: hermes keys use {provider} {key.label}")
        return True
    if action == "status" and keys.list_keys(provider):
        for key in keys.list_keys(provider):
            print(f"  {'*' if key.active else ' '} {key.label:<14} {key.masked}")
        return True
    return False


class _SwitchableKeys:
    """Mixed in ahead of a provider profile class."""

    def resolve_api_key_secret(self) -> tuple[str, str] | None:
        key = keys.active_key(self.name)
        if key is None or not key.secret:
            return super().resolve_api_key_secret()
        return key.secret, key.env_var

    def seed_pool_credentials(self, *, suppressed_sources, explicitly_configured):
        if not keys.list_keys(self.name):
            return super().seed_pool_credentials(
                suppressed_sources=suppressed_sources, explicitly_configured=explicitly_configured)
        # Hermes's own credential pool does the cycling: it uses the first row and
        # moves to the next when one is rate-limited or out of quota. The rows are
        # the saved keys in cycle order (just the active key when cycling is off).
        return [{"source": f"env:{key.env_var}", "access_token": key.secret, "base_url": None, "label": key.label}
                for key in keys.cycle_order(self.name) if f"env:{key.env_var}" not in suppressed_sources]


def _free_only_catalog(profile: Any, rule: str, inner: Any):
    """A ``picker_catalog`` hook listing only the ids matching *rule* while paid models are hidden."""
    pattern = re.compile(rule, re.IGNORECASE)

    def catalog(force_refresh: bool = False):
        if inner is not None:
            models = inner(force_refresh)
        else:
            key = keys.active_key(profile.name)
            models = profile.fetch_models(api_key=key.secret if key else None) if key else None
        if not models or keys.show_paid():
            return models
        free = [m for m in models if pattern.search(str(m))]
        return free or models  # a rule that matches nothing is out of date: show the catalog

    return catalog


def _spent_key_classifier(profile: Any, inner: Any):
    """A ``classify_api_error`` hook: a key that is out of allowance is left at once."""

    def classify(error, *, status_code=None, error_code="", message="", body=None, model=None, **_ignored):
        if callable(inner):
            verdict = inner(error, status_code=status_code, error_code=error_code, message=message,
                            body=body, model=model)
            if verdict is not None:
                return verdict
        if len(keys.cycle_order(profile.name)) < 2:
            return None  # one key, or cycling switched off: nothing to move to, keep Hermes's handling
        if not limits.key_is_spent(status_code, error_code, message, limits.retry_after_seconds(error)):
            return None
        return {"reason": "billing", "should_rotate_credential": True}

    return classify


def _switchable(profile: Any) -> Any:
    cls = type(f"Switchable{type(profile).__name__}", (_SwitchableKeys, type(profile)), {})
    clone = copy.copy(profile)
    object.__setattr__(clone, "__class__", cls)
    object.__setattr__(clone, "classify_api_error",
                       _spent_key_classifier(clone, getattr(clone, "classify_api_error", None)))
    if getattr(clone, "auth_handler", None) is None:
        object.__setattr__(clone, "auth_handler", _auth_handler)
    rule = catalog.FREE_MODEL_RULES.get(clone.name)
    if rule:
        object.__setattr__(clone, "picker_catalog",
                           _free_only_catalog(clone, rule, getattr(clone, "picker_catalog", None)))
    return clone


def _key_env_vars(profile: Any) -> tuple[str, ...]:
    return tuple(v for v in (getattr(profile, "env_vars", ()) or ()) if not v.endswith("_BASE_URL"))


def _builtin_profile(name: str) -> Any | None:
    """The profile Hermes ships for *name*, loaded from the Hermes tree."""
    init = _BUNDLED_DIR / name / "__init__.py"
    if not init.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"_free_providers_builtin_{name.replace('-', '_')}", init, submodule_search_locations=[str(init.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # its own registration is replaced by the caller
    for value in vars(module).values():
        if isinstance(value, ProviderProfile) and value.name == name:
            return value
    return None


def _register_wrapped() -> None:
    for name in catalog.WRAPPED:
        try:
            profile = _builtin_profile(name)
            if profile is None or profile.auth_type != "api_key":
                logger.debug("free-providers: built-in %s not wrappable; left as shipped", name)
                continue
            keys.LEGACY_ENV_VARS[name] = _key_env_vars(profile)
            if keys.LEGACY_ENV_VARS[name]:
                keys.ENV_MIRROR[name] = keys.LEGACY_ENV_VARS[name][0]
            register_provider(_switchable(profile))
        except Exception as exc:  # noqa: BLE001 -- never take a built-in provider down with us
            logger.warning("free-providers: %s keeps its built-in behaviour (%s)", name, exc)


def _register_added() -> None:
    for entry in catalog.ADDED:
        if (_BUNDLED_DIR / entry.name).is_dir():
            continue  # Hermes ships its own now: do not shadow it
        env_var = catalog.env_var_for(entry.name)
        keys.LEGACY_ENV_VARS[entry.name] = (env_var,)
        keys.ENV_MIRROR[entry.name] = env_var
        register_provider(_switchable(ProviderProfile(
            name=entry.name, aliases=entry.aliases, display_name=entry.display_name,
            description=f"{entry.display_name}: {entry.free}", signup_url=entry.signup_url,
            env_vars=(env_var,), base_url=entry.base_url, auth_type="api_key")))


_register_wrapped()
_register_added()
try:
    # Hermes lists a provider in its model pickers only when its own env var holds a key.
    keys.sync_all()
except Exception as exc:  # noqa: BLE001 -- a locked .env must not stop the providers loading
    logger.warning("free-providers: could not refresh provider env vars (%s)", exc)
