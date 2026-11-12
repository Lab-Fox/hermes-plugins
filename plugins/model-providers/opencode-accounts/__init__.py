"""Account switching for Hermes's built-in OpenCode providers (Zen and Go).

This does not replace the built-in ``opencode-zen`` / ``opencode-go`` profiles:
it loads them from the Hermes tree, then re-registers each one as a subclass of
its own class that only changes where the API key comes from. Base URLs,
headers, reasoning controls and the Go usage windows stay Hermes's, so they keep
following Hermes updates.

Only OpenCode's documented API-key routes are used. Free-tier models that
OpenCode restricts to its own client are not unlocked by this plugin.

If the built-in profiles cannot be loaded (a future Hermes moved them), nothing
is registered and the built-ins keep working without account switching.
"""

from __future__ import annotations

import copy
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import opencode_accounts as accounts  # noqa: E402 -- Hermes imports plugin dirs without a package name
import providers  # noqa: E402
from providers import register_provider  # noqa: E402

logger = logging.getLogger(__name__)


def _auth_handler(action: str, args: Any) -> bool:
    """``hermes auth add|status opencode-zen|opencode-go`` go to the account store."""
    if action == "add":
        import getpass

        label = getattr(args, "label", None) or input("Label for this OpenCode account (e.g. personal, work): ")
        key = getattr(args, "api_key", None) or getpass.getpass("OpenCode API key (hidden): ")
        try:
            accounts.adopt_existing_keys()
            account = accounts.add_account(label, key)
        except accounts.AccountError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Saved OpenCode account '{account.label}'. Switch to it with: hermes opencode use {account.label}")
        return True
    if action == "status":
        rows = accounts.list_accounts()
        if not rows:
            print("opencode: no accounts. Add one with: hermes opencode add <label>")
        for row in rows:
            print(f"  {'*' if row.active else ' '} {row.label:<14} {row.masked}")
        return True
    return False


class _AccountSwitching:
    """Mixed in ahead of a built-in OpenCode profile class."""

    def resolve_api_key_secret(self) -> tuple[str, str] | None:
        account = accounts.active_account()
        if account is None or not account.key:
            return None  # no saved accounts: the built-in env var resolution
        return account.key, account.env_var

    def seed_pool_credentials(self, *, suppressed_sources, explicitly_configured) -> list[dict]:
        # Empty on purpose: pooled keys would rotate between accounts by themselves.
        return []


def _load_builtin_profiles() -> list[Any]:
    init = Path(providers.__file__).resolve().parent.parent / "plugins" / "model-providers" / "opencode-zen" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_opencode_accounts_builtin", init)
    if spec is None or spec.loader is None:
        raise ImportError(f"built-in OpenCode profiles not found at {init}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # its own register_provider calls are replaced just below
    found = [getattr(module, name, None) for name in ("opencode_zen", "opencode_go")]
    if any(p is None or p.name not in accounts.PROVIDERS for p in found):
        raise ImportError("built-in OpenCode profiles changed shape")
    return found


def _with_accounts(profile: Any) -> Any:
    switched = type(f"Switchable{type(profile).__name__}", (_AccountSwitching, type(profile)), {})
    clone = copy.copy(profile)
    object.__setattr__(clone, "__class__", switched)
    object.__setattr__(clone, "auth_handler", _auth_handler)
    return clone


try:
    for _profile in _load_builtin_profiles():
        register_provider(_with_accounts(_profile))
except Exception as exc:  # noqa: BLE001 -- never take the built-in providers down with us
    logger.warning("opencode-accounts: account switching is inactive (%s); "
                   "the built-in OpenCode providers are unchanged.", exc)
