"""Command handlers shared by ``hermes keys`` (terminal) and ``/keys`` (chat)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

USAGE = ("/keys [use <provider> <label> | models all|free | auto <provider|all> on|off | "
         "skip <provider> <label> on|off | providers]")
_APPLIES = ("New chats use it straight away, and so does the next message in a Desktop or messaging "
            "chat. A terminal chat that is already open keeps its current key until restarted.")
_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "model-providers" / "free-providers"


def _keys():
    """The store module that ships with the provider plugin (one copy, one cache)."""
    module = sys.modules.get("provider_keys")
    if module is None:
        if str(_PROVIDER_DIR) not in sys.path:
            sys.path.insert(0, str(_PROVIDER_DIR))
        import provider_keys as module  # type: ignore[import-not-found]
    return module


def _catalog():
    module = sys.modules.get("free_providers_catalog")
    if module is None:
        spec = importlib.util.spec_from_file_location("free_providers_catalog", _PROVIDER_DIR / "catalog.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["free_providers_catalog"] = module
        spec.loader.exec_module(module)
    return module


def supported() -> dict[str, tuple[str, str]]:
    """provider id -> (signup URL, what is free)."""
    catalog = _catalog()
    table = {name: ("", note) for name, note in catalog.WRAPPED.items()}
    table.update({entry.name: (entry.signup_url, entry.free) for entry in catalog.ADDED})
    return table


def resolve_provider(name: str) -> str:
    store, catalog = _keys(), _catalog()
    clean = (name or "").strip().lower()
    table = supported()
    if clean in table:
        return clean
    for entry in catalog.ADDED:
        if clean in entry.aliases:
            return entry.name
    raise store.KeyError_(f"'{name}' is not a provider with switchable keys. See: hermes keys providers")


def render_list() -> str:
    store = _keys()
    names = store.providers_with_keys()
    if not names:
        return ("No keys saved yet. See the providers with: hermes keys providers\n"
                "Then in a terminal: hermes keys add <provider> <label>")
    lines = ["Saved keys (* = starts first):"]
    for name in names:
        lines.append(f"  {name}" + ("" if store.auto_cycle(name) else "   [auto-cycle off]"))
        lines += [f"    {'*' if k.active else ' '} {k.label:<14} {k.masked}" + ("  (skipped by auto-cycle)" if k.skip else "")
                  for k in store.list_keys(name)]
    lines.append(f"Model lists show: {'free and paid models' if store.show_paid() else 'free models only'}"
                 "  (change with: /keys models all|free)")
    return "\n".join(lines)


def _refresh_catalogs(names) -> None:
    """Re-read the model lists Hermes caches for *names* so pickers show the change at once."""
    try:
        from hermes_cli.models import cached_provider_model_ids
    except Exception:
        return
    for name in names:
        try:
            # A failed fetch keeps the previous list: better than an empty provider row.
            cached_provider_model_ids(name, force_refresh=True)
        except Exception:
            continue  # a provider that is down keeps its old list until the next refresh


def models(mode: str) -> str:
    store, catalog = _keys(), _catalog()
    choice = (mode or "").strip().lower()
    if choice not in ("all", "free"):
        raise store.KeyError_("Use: /keys models all   or   /keys models free")
    store.set_show_paid(choice == "all")
    _refresh_catalogs([n for n in catalog.FREE_MODEL_RULES if store.list_keys(n)])
    mixed = ", ".join(sorted(n for n in catalog.FREE_MODEL_RULES if store.list_keys(n))) or "none of your providers"
    if choice == "all":
        return f"Model lists now include paid models ({mixed}). Go back with: /keys models free"
    return (f"Model lists now show only free models ({mixed}). Other providers are listed whole. "
            "Show paid ones with: /keys models all")


def auto(provider: str, state: str) -> str:
    store = _keys()
    if state not in ("on", "off"):
        raise store.KeyError_("Use: /keys auto <provider|all> on|off")
    names = store.providers_with_keys() if provider.lower() == "all" else [resolve_provider(provider)]
    for name in names:
        store.set_auto_cycle(name, state == "on")
    what = ("moves to the next saved key when one hits its limit" if state == "on"
            else "stays on the starting key and never changes key by itself")
    return f"{', '.join(names) or 'No provider'}: Hermes {what}."


def skip(provider: str, label: str, state: str = "on") -> str:
    store = _keys()
    if state not in ("on", "off"):
        raise store.KeyError_("Use: /keys skip <provider> <label> on|off")
    name = resolve_provider(provider)
    key = store.set_skip(name, label, state == "on")
    return (f"{name} key '{key.label}' is now left out of auto-cycling; it is only used when you pick it."
            if key.skip else f"{name} key '{key.label}' takes part in auto-cycling again.")


def render_providers() -> str:
    store = _keys()
    lines = ["Providers with switchable keys (free offers change; check the provider's site):"]
    for name, (signup, note) in supported().items():
        count = len(store.list_keys(name))
        saved = f"[{count} key{'s' if count != 1 else ''}]" if count else "[no key]"
        lines.append(f"  {name:<13} {saved:<9} {note}" + (f"  ({signup})" if signup else ""))
    lines.append("Add a key in a terminal: hermes keys add <provider> <label>")
    return "\n".join(lines)


def use(provider: str, label: str) -> str:
    store = _keys()
    name = resolve_provider(provider)
    key = store.use_key(name, label)
    _refresh_catalogs([name])
    return f"{name} now starts with key '{key.label}' ({key.masked}). {_APPLIES}"


def slash(raw_args: str = "") -> str:
    store = _keys()
    parts = (raw_args or "").split()
    try:
        if not parts or parts[0] in ("list", "ls"):
            return render_list()
        if parts[0] == "providers":
            return render_providers()
        if parts[0] == "use" and len(parts) == 3:
            return use(parts[1], parts[2])
        if parts[0] == "models" and len(parts) == 2:
            return models(parts[1])
        if parts[0] == "auto" and len(parts) == 3:
            return auto(parts[1], parts[2].lower())
        if parts[0] == "skip" and len(parts) in (3, 4):
            return skip(parts[1], parts[2], (parts[3] if len(parts) == 4 else "on").lower())
        if parts[0] in ("add", "remove", "rename"):
            # Keys must never be typed into a chat: the transcript would keep them.
            return f"Run that in a terminal instead: hermes keys {parts[0]} ..."
    except store.KeyError_ as exc:
        return str(exc)
    return f"Usage: {USAGE}"


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="keys_action")
    sub.add_parser("list", help="Show saved keys and which one is active per provider")
    sub.add_parser("providers", help="Show every provider with switchable keys and what it offers free")
    add = sub.add_parser("add", help="Save a key under a label (prompts for the key, hidden)")
    add.add_argument("provider")
    add.add_argument("label")
    add.add_argument("--api-key", dest="api_key", help="Pass the key instead of being prompted")
    use_p = sub.add_parser("use", help="Make a saved key the active one for its provider")
    use_p.add_argument("provider")
    use_p.add_argument("label")
    models_p = sub.add_parser("models", help="Show only free models in model lists, or paid ones too")
    models_p.add_argument("mode", choices=("all", "free"))
    auto_p = sub.add_parser("auto", help="Turn moving to the next key on a limit on or off")
    auto_p.add_argument("provider", help="A provider, or 'all'")
    auto_p.add_argument("state", choices=("on", "off"))
    skip_p = sub.add_parser("skip", help="Leave one key out of auto-cycling (e.g. a paid key)")
    skip_p.add_argument("provider")
    skip_p.add_argument("label")
    skip_p.add_argument("state", nargs="?", default="on", choices=("on", "off"))
    rename = sub.add_parser("rename", help="Rename a saved key")
    rename.add_argument("provider")
    rename.add_argument("old")
    rename.add_argument("new")
    remove = sub.add_parser("remove", help="Forget a saved key")
    remove.add_argument("provider")
    remove.add_argument("label")


def handle(args) -> int:
    store = _keys()
    action = getattr(args, "keys_action", None) or "list"
    try:
        if action == "list":
            print(render_list())
        elif action == "providers":
            print(render_providers())
        elif action == "add":
            import getpass

            name = resolve_provider(args.provider)
            secret = args.api_key or getpass.getpass(f"{name} API key (hidden): ")
            key = store.add_key(name, args.label, secret)
            print(f"Saved {name} key '{key.label}'.")
            print(render_list())
        elif action == "use":
            print(use(args.provider, args.label))
        elif action == "models":
            print(models(args.mode))
        elif action == "auto":
            print(auto(args.provider, args.state))
        elif action == "skip":
            print(skip(args.provider, args.label, args.state))
        elif action == "rename":
            name = resolve_provider(args.provider)
            print(f"Renamed to '{store.rename_key(name, args.old, args.new).label}'.")
        elif action == "remove":
            name = resolve_provider(args.provider)
            print(f"Removed {name} key '{store.remove_key(name, args.label).label}'.")
            print(render_list())
    except store.KeyError_ as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
