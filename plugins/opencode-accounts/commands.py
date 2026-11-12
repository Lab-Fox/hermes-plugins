"""Command handlers shared by ``hermes opencode`` (terminal) and ``/opencode`` (chat)."""

from __future__ import annotations

import sys
from pathlib import Path

USAGE = "/opencode [use <label>]"
_APPLIES = ("It applies to both opencode-zen and opencode-go. New chats use it straight away, and so "
            "does the next message in a Desktop or messaging chat. A terminal chat that is already "
            "open keeps its current key until restarted.")


def _accounts():
    """The store module that ships with the provider override (one copy, one cache)."""
    module = sys.modules.get("opencode_accounts")
    if module is None:
        provider_dir = Path(__file__).resolve().parent.parent / "model-providers" / "opencode-accounts"
        if str(provider_dir) not in sys.path:
            sys.path.insert(0, str(provider_dir))
        import opencode_accounts as module  # type: ignore[import-not-found]
    return module


def render_list() -> str:
    store = _accounts()
    adopted = store.adopt_existing_keys()
    rows = store.list_accounts()
    if not rows:
        return ("No OpenCode accounts saved yet. Create an API key at https://opencode.ai/console, "
                "then in a terminal run: hermes opencode add <label>")
    lines = ["OpenCode accounts (* = active):"]
    lines += [f"  {'*' if r.active else ' '} {r.label:<14} {r.masked}" for r in rows]
    if adopted:
        lines.append("Imported from .env: " + ", ".join(a.label for a in adopted)
                     + ". Rename with: hermes opencode rename <old> <new>")
    return "\n".join(lines)


def use(label: str) -> str:
    store = _accounts()
    store.adopt_existing_keys()
    account = store.use_account(label)
    return f"OpenCode now uses '{account.label}' ({account.masked}). {_APPLIES}"


def slash(raw_args: str = "") -> str:
    store = _accounts()
    parts = (raw_args or "").split()
    try:
        if not parts or parts[0] in ("list", "ls"):
            return render_list()
        if parts[0] == "use" and len(parts) == 2:
            return use(parts[1])
        if parts[0] in ("add", "remove", "rename"):
            # Keys must never be typed into a chat: the transcript would keep them.
            return f"Run that in a terminal instead: hermes opencode {parts[0]} ..."
    except store.AccountError as exc:
        return str(exc)
    return f"Usage: {USAGE}"


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="opencode_action")
    sub.add_parser("list", help="Show saved accounts and which one is active")
    add = sub.add_parser("add", help="Save an API key under a label (prompts for the key, hidden)")
    add.add_argument("label")
    add.add_argument("--api-key", dest="api_key", help="Pass the key instead of being prompted")
    use_p = sub.add_parser("use", help="Make a saved account the active one")
    use_p.add_argument("label")
    rename = sub.add_parser("rename", help="Rename a saved account")
    rename.add_argument("old")
    rename.add_argument("new")
    remove = sub.add_parser("remove", help="Forget a saved account")
    remove.add_argument("label")


def handle(args) -> int:
    store = _accounts()
    action = getattr(args, "opencode_action", None) or "list"
    try:
        if action == "list":
            print(render_list())
        elif action == "add":
            import getpass

            store.adopt_existing_keys()
            key = args.api_key or getpass.getpass("OpenCode API key (hidden): ")
            account = store.add_account(args.label, key)
            print(f"Saved '{account.label}'.")
            print(render_list())
        elif action == "use":
            print(use(args.label))
        elif action == "rename":
            store.adopt_existing_keys()
            print(f"Renamed to '{store.rename_account(args.old, args.new).label}'.")
        elif action == "remove":
            store.adopt_existing_keys()
            print(f"Removed '{store.remove_account(args.label).label}'.")
            print(render_list())
    except store.AccountError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
