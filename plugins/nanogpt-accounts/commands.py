"""Command handlers shared by ``hermes nanogpt`` (terminal) and ``/nanogpt`` (chat)."""

from __future__ import annotations

import sys
from pathlib import Path

USAGE = "/nanogpt [use <label> | status]"
_APPLIES = ("New chats use it straight away, and so does the next message in a Desktop or "
            "messaging chat. A terminal chat that is already open keeps its current key until restarted.")


def _accounts():
    """The store module that ships with the provider profile (one copy, one cache)."""
    module = sys.modules.get("nanogpt_accounts")
    if module is None:
        provider_dir = Path(__file__).resolve().parent.parent / "model-providers" / "nanogpt"
        if str(provider_dir) not in sys.path:
            sys.path.insert(0, str(provider_dir))
        import nanogpt_accounts as module  # type: ignore[import-not-found]
    return module


def render_list() -> str:
    store = _accounts()
    adopted = store.adopt_existing_keys()
    rows = store.list_accounts()
    if not rows:
        return "No NanoGPT keys saved yet. In a terminal, run: hermes nanogpt add <label>"
    lines = ["NanoGPT accounts (* = active):"]
    lines += [f"  {'*' if r.active else ' '} {r.label:<12} {_describe(r.kind):<28} {r.masked}" for r in rows]
    if adopted:
        lines.append("Imported from .env: " + ", ".join(a.label for a in adopted)
                     + ". Rename with: hermes nanogpt rename <old> <new>")
    return "\n".join(lines)


def render_status(label: str | None = None) -> str:
    store = _accounts()
    account = store.get_account(label) if label else store.active_account()
    if account is None:
        return "No active NanoGPT account."
    if not account.key:
        return f"Account '{account.label}' has no key: {account.env_var} is missing from .env."
    usage, funds = store.subscription_usage(account.key), store.balance(account.key)
    if usage is None and funds is None:
        return f"Account '{account.label}': NanoGPT did not answer. Check the key and your connection."
    lines = [f"Account '{account.label}' ({account.masked})"]
    if usage is not None:
        if usage.get("active"):
            lines.append(f"  Subscription: active ({usage.get('state') or 'active'})")
            for field, name in (("dailyInputTokens", "daily tokens"), ("weeklyInputTokens", "weekly tokens")):
                window = usage.get(field)
                if isinstance(window, dict) and window.get("remaining") is not None:
                    lines.append(f"  Remaining {name}: {window['remaining']:,}")
        else:
            lines.append("  Subscription: none on this key")
        routing = usage.get("routing") if isinstance(usage.get("routing"), dict) else {}
        if routing.get("recommendedMode"):
            lines.append(f"  Next request would be billed to: {routing['recommendedMode']}")
    if funds is not None and funds.get("usd_balance") is not None:
        lines.append(f"  Pay As You Go balance: ${float(funds['usd_balance']):.2f}")
    kind = store.kind_from_usage(usage)
    if kind != store.KIND_UNKNOWN and kind != account.kind:
        store.set_kind(account.label, kind)
    return "\n".join(lines)


def use(label: str) -> str:
    store = _accounts()
    store.adopt_existing_keys()
    account = store.use_account(label)
    return f"NanoGPT now uses '{account.label}' ({_describe(account.kind)}, {account.masked}). {_APPLIES}"


def _describe(kind: str) -> str:
    return {"subscription": "subscription only", "paygo": "pay as you go",
            "mixed": "subscription, then balance"}.get(kind, "billing not detected")


# ── /nanogpt ──────────────────────────────────────────────────────────────────


def slash(raw_args: str = "") -> str:
    store = _accounts()
    parts = (raw_args or "").split()
    try:
        if not parts or parts[0] in ("list", "ls"):
            return render_list()
        if parts[0] == "use" and len(parts) == 2:
            return use(parts[1])
        if parts[0] == "status" and len(parts) <= 2:
            return render_status(parts[1] if len(parts) == 2 else None)
        if parts[0] in ("add", "remove", "rename"):
            # Keys must never be typed into a chat: the transcript would keep them.
            return f"Run that in a terminal instead: hermes nanogpt {parts[0]} ..."
    except store.AccountError as exc:
        return str(exc)
    return f"Usage: {USAGE}"


# ── hermes nanogpt ────────────────────────────────────────────────────────────


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="nanogpt_action")
    sub.add_parser("list", help="Show saved keys and which one is active")
    add = sub.add_parser("add", help="Save a key under a label (prompts for the key, hidden)")
    add.add_argument("label")
    add.add_argument("--api-key", dest="api_key", help="Pass the key instead of being prompted")
    add.add_argument("--kind", choices=("subscription", "paygo", "mixed"),
                     help="Skip auto-detection of what the key can be billed to")
    use_p = sub.add_parser("use", help="Make a saved key the active one")
    use_p.add_argument("label")
    status = sub.add_parser("status", help="Subscription quota and balance for a key")
    status.add_argument("label", nargs="?")
    rename = sub.add_parser("rename", help="Rename a saved key")
    rename.add_argument("old")
    rename.add_argument("new")
    remove = sub.add_parser("remove", help="Forget a saved key")
    remove.add_argument("label")


def handle(args) -> int:
    store = _accounts()
    action = getattr(args, "nanogpt_action", None) or "list"
    try:
        if action == "list":
            print(render_list())
        elif action == "add":
            import getpass

            store.adopt_existing_keys()
            key = args.api_key or getpass.getpass("NanoGPT API key (hidden): ")
            account = store.add_account(args.label, key, kind=args.kind)
            print(f"Saved '{account.label}' ({account.kind}).")
            print(render_list())
        elif action == "use":
            print(use(args.label))
        elif action == "status":
            store.adopt_existing_keys()
            print(render_status(args.label))
        elif action == "rename":
            store.adopt_existing_keys()
            account = store.rename_account(args.old, args.new)
            print(f"Renamed to '{account.label}'.")
        elif action == "remove":
            store.adopt_existing_keys()
            account = store.remove_account(args.label)
            print(f"Removed '{account.label}'.")
            print(render_list())
    except store.AccountError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
