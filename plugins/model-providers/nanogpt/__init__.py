"""NanoGPT provider profile with named, manually switched accounts.

NanoGPT (https://nano-gpt.com) resells models from many vendors behind one
OpenAI-compatible endpoint. Model ids keep their vendor prefix
(``anthropic/claude-sonnet-5.5``) and reasoning variants use a ``:thinking``
suffix, so ids are passed through verbatim.

API-key traffic must use the direct API host ``https://api.nano-gpt.com``: it
allows larger request bodies and longer runtimes than the website host.

Accounts (``nanogpt_accounts.py``): several keys can be saved under labels and
exactly one is active. The profile resolves the active key itself and keeps the
credential pool empty, so Hermes never rotates from a subscription key to a
Pay As You Go key on its own. Switch with ``hermes nanogpt use <label>`` or
``/nanogpt use <label>`` (the ``nanogpt-accounts`` plugin).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import nanogpt_accounts as accounts  # noqa: E402 -- Hermes imports plugin dirs without a package name
from providers import register_provider  # noqa: E402
from providers.base import ProviderProfile  # noqa: E402


def _auth_handler(action: str, args: Any) -> bool:
    """``hermes auth add|status nanogpt`` go to the account store, never the pool."""
    if action == "add":
        import getpass

        label = getattr(args, "label", None) or input("Label for this key (e.g. sub, paygo): ")
        key = getattr(args, "api_key", None) or getpass.getpass("NanoGPT API key: ")
        try:
            accounts.adopt_existing_keys()
            account = accounts.add_account(label, key)
        except accounts.AccountError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Saved NanoGPT account '{account.label}' ({account.kind}). "
              f"Switch to it with: hermes nanogpt use {account.label}")
        return True
    if action == "status":
        rows = accounts.list_accounts()
        if not rows:
            print("nanogpt: no accounts. Add one with: hermes nanogpt add <label>")
        for row in rows:
            print(f"  {'*' if row.active else ' '} {row.label:<12} {row.kind:<12} {row.masked}")
        return True
    return False


def _percent(window: Any) -> float | None:
    if not isinstance(window, dict):
        return None
    used, remaining = window.get("used"), window.get("remaining")
    # Derived from the raw counts: the docs do not say whether percentUsed is 0-1 or 0-100.
    if isinstance(used, (int, float)) and isinstance(remaining, (int, float)) and used + remaining > 0:
        return 100.0 * used / (used + remaining)
    return None


def _reset_at(window: Any) -> datetime | None:
    raw = window.get("resetAt") if isinstance(window, dict) else None
    if not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)


class NanoGPTProfile(ProviderProfile):
    def resolve_api_key_secret(self) -> tuple[str, str] | None:
        account = accounts.active_account()
        if account is None or not account.key:
            return None  # no saved accounts: plain NANOGPT_API_KEY resolution
        return account.key, account.env_var

    def seed_pool_credentials(self, *, suppressed_sources, explicitly_configured) -> list[dict]:
        # An empty pool is the point: pooled keys rotate on billing errors.
        return []

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None,
                     timeout: float = 8.0) -> list[str] | None:
        key = api_key or (self.resolve_api_key_secret() or ("", ""))[0]
        account = accounts.account_for_key(key)
        if account is not None and account.kind == accounts.KIND_SUBSCRIPTION and (
            not base_url or base_url.rstrip("/") == self.base_url
        ):
            # A subscription-only key cannot pay for the rest of the catalog.
            included = super().fetch_models(
                api_key=key, base_url=accounts.SUBSCRIPTION_BASE_URL, timeout=timeout)
            if included:
                return included
        return super().fetch_models(api_key=key or None, base_url=base_url, timeout=timeout)

    def fetch_account_usage(self, *, base_url: str | None = None, api_key: str | None = None):
        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

        key = api_key or (self.resolve_api_key_secret() or ("", ""))[0]
        if not key:
            return None
        account = accounts.account_for_key(key)
        usage = accounts.subscription_usage(key, timeout=3.5)
        funds = accounts.balance(key, timeout=3.5)
        if usage is None and funds is None:
            return None

        windows = []
        active = bool(usage and usage.get("active"))
        if active:
            for field, label in (("dailyInputTokens", "Daily input tokens"),
                                 ("weeklyInputTokens", "Weekly input tokens"),
                                 ("dailyImages", "Daily images")):
                percent = _percent(usage.get(field))
                if percent is not None:
                    windows.append(AccountUsageWindow(
                        label=label, used_percent=percent, reset_at=_reset_at(usage.get(field))))
        details = []
        if funds and funds.get("usd_balance") is not None:
            details.append(f"Pay As You Go balance: ${float(funds['usd_balance']):.2f}")
        routing = usage.get("routing") if usage and isinstance(usage.get("routing"), dict) else {}
        if routing.get("recommendedMode"):
            details.append(f"NanoGPT would bill the next request to: {routing['recommendedMode']}")
        return AccountUsageSnapshot(
            provider=self.name, source="nanogpt_api", fetched_at=datetime.now(timezone.utc),
            plan="Subscription" if active else "Pay As You Go",
            identity=f"account '{account.label}'" if account else None,
            windows=tuple(windows), details=tuple(details))


nanogpt = NanoGPTProfile(
    name="nanogpt",
    aliases=("nano-gpt", "nano_gpt"),
    display_name="NanoGPT",
    description="NanoGPT — 600+ models across vendors, with switchable subscription / Pay As You Go keys",
    signup_url="https://nano-gpt.com",
    # Fallback when no account is saved; saved accounts are resolved by the profile.
    env_vars=accounts.LEGACY_ENV_VARS,
    base_url=accounts.BASE_URL,
    models_url=f"{accounts.BASE_URL}/models",
    auth_type="api_key",
    auth_handler=_auth_handler,
    # OpenAI-compatible passthrough: multimodal models accept image parts,
    # including inside tool-result messages.
    supports_vision=True,
    # Cheap, vision-capable, 1M-context model for compression / titling / vision
    # helpers: keeps background work off the (much pricier) main model.
    default_aux_model="deepseek/deepseek-v4.1-flash",
    fallback_models=(
        "anthropic/claude-sonnet-5.5",
        "openai/gpt-5.6-sol",
        "google/gemini-3.1-pro-preview",
        "deepseek/deepseek-v4.1-flash",
        "moonshotai/kimi-k3.1-pro",
        "z-ai/glm-5.3-flash",
        "minimax/minimax-m3",
        "x-ai/grok-4.7",
    ),
)

register_provider(nanogpt)
