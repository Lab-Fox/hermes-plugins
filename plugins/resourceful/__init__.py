"""resourceful: nudge the agent to use what is installed and to look things up.

* A standing rule set in the system prompt (cache-safe section, frozen per session).
* Per message: the installed skills and tools that fit the task, connectors when an app is
  named, and a reminder to search when the message needs outside information (appended to
  the user message, which is how Hermes keeps the prompt cache intact).
* Per tool result: when a tool fails the same way again, a note suggesting a search for the
  exact error (added to that result only).

Everything is local word matching: no model calls, no network, nothing stored on disk.
"""

from __future__ import annotations

import logging
import threading
import time

from . import resourceful_core as core

logger = logging.getLogger(__name__)

_WEB_TOOL_ORDER = ("net_search", "web_search")
# Always in view and used constantly: suggesting them is noise.
_PLAIN_TOOLS = frozenset({"terminal", "read_file", "write_file", "patch", "search_files", "memory", "clarify",
                          "todo", "todo_list", "skills_list", "skill_view", "skill_manage", "web_search",
                          "web_extract", "tool_search", "tool_describe", "tool_call", "send_message",
                          "manage_connections"})
# Toolsets that are app plumbing (desktop helpers, onboarding) or always in view: never suggested.
_QUIET_TOOLSETS = frozenset({"desktop_ui", "onboarding", "project", "catalog", "clarify", "memory", "todo", "skills",
                             "file", "session_search"})
_CATALOG_TTL = 300  # seconds: new skills and plugins show up within five minutes

_catalog_lock = threading.Lock()
_catalog: dict = {"at": 0.0, "index": None, "features": core.Features(), "extra_tools": []}
_state_lock = threading.Lock()  # hooks for different chats, and parallel tool calls, run on different threads
_hinter = core.TurnHinter()
_tracker = core.StuckTracker()


def _config() -> dict:
    try:
        from hermes_cli.config import load_config

        return load_config() or {}
    except Exception:  # noqa: BLE001
        return {}


def _off(value) -> bool:
    return value is False or str(value).strip().lower() in ("false", "off", "0", "no", "disabled")


def _tool_is_usable(registry, entry) -> bool:
    """The tool's own availability check, not just its toolset's."""
    try:
        return bool(registry.get_definitions({entry.name}, quiet=True))
    except Exception:  # noqa: BLE001
        return False


def _load_catalog() -> dict:
    """Skills, tools and features, rebuilt at most every few minutes."""
    with _catalog_lock:
        if _catalog["index"] is not None and time.monotonic() - _catalog["at"] < _CATALOG_TTL:
            return _catalog
    items: list[core.Item] = []
    try:
        from tools.skills_tool import find_all_skills

        for skill in find_all_skills(skip_disabled=True):
            if skill.get("name"):
                items.append(core.Item("skill", str(skill["name"]), str(skill.get("description") or ""),
                                       str(skill.get("category") or "")))
    except Exception as exc:  # noqa: BLE001 -- hints are optional; never break a turn
        logger.debug("resourceful: skill catalog unavailable: %s", exc)

    tools: list[core.Item] = []
    extra_tools: list[core.Item] = []  # plugin and MCP tools: the ones most often deferred and forgotten
    names: set[str] = set()
    try:
        from tools.registry import registry

        for entry in registry.get_all_entries():
            if not _tool_is_usable(registry, entry):
                continue
            names.add(entry.name)
            if entry.name in _PLAIN_TOOLS or entry.toolset in _QUIET_TOOLSETS:
                continue
            item = core.Item("tool", entry.name,
                             entry.description or str((entry.schema or {}).get("description") or ""))
            tools.append(item)
            try:
                scope = registry.plugin_scope_for_callable(entry.handler)
            except Exception:  # noqa: BLE001
                scope = None
            if scope or entry.name.startswith("mcp_"):
                extra_tools.append(item)
    except Exception as exc:  # noqa: BLE001
        logger.debug("resourceful: tool registry unavailable: %s", exc)

    config = _config()
    tool_cfg = config.get("tools") or {}
    tool_search = not _off((tool_cfg.get("tool_search") or {}).get("enabled", "auto"))
    connectors = ("manage_connections" in names
                  and not _off((tool_cfg.get("connectors") or {}).get("enabled", True))
                  and not _off((config.get("nous") or {}).get("guest", True)))
    features = core.Features(web_tools=tuple(n for n in _WEB_TOOL_ORDER if n in names),
                             tool_search=tool_search, connectors=connectors)
    index = core.Index(items + tools)
    with _catalog_lock:
        _catalog.update(at=time.monotonic(), index=index, features=features, extra_tools=extra_tools)
        return _catalog


def _rules(_session_info) -> str:
    catalog = _load_catalog()
    return core.standing_rules(catalog["features"], catalog["extra_tools"])


def _on_turn(session_id: str = "", user_message="", **_kwargs):
    if not isinstance(user_message, str):
        # A multimodal turn: hint on its text parts.
        parts = user_message if isinstance(user_message, list) else []
        user_message = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
    try:
        catalog = _load_catalog()
        with _state_lock:
            text = _hinter.hint(session_id or "", user_message, catalog["index"], catalog["features"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("resourceful: turn hint failed: %s", exc)
        return None
    return {"context": text} if text else None


def _on_tool_result(tool_name: str = "", args=None, result="", conversation_id: str = "", session_id: str = "",
                    status: str = "", error_message: str = "", **_kwargs):
    if not isinstance(result, str):
        return None
    try:
        with _state_lock:
            nudge = _tracker.observe(conversation_id or session_id or "", tool_name or "",
                                     args if isinstance(args, dict) else {}, result, status or "",
                                     error_message or "", _catalog["features"].web_tools)
    except Exception as exc:  # noqa: BLE001
        logger.debug("resourceful: stuck check failed: %s", exc)
        return None
    return core.attach(result, nudge) if nudge else None


def _on_post_tool_call(tool_name: str = "", conversation_id: str = "", session_id: str = "", **_kwargs):
    # tool_search and tool_describe skip transform_tool_result, so the "looked it up" reset happens here.
    if tool_name in core.RESEARCH_TOOLS:
        with _state_lock:
            _tracker.reset(conversation_id or session_id or "")


def register(ctx) -> None:
    ctx.register_system_prompt_section("resourceful.rules", _rules, position="after_memory", max_chars=3000)
    ctx.register_hook("pre_llm_call", _on_turn)
    ctx.register_hook("transform_tool_result", _on_tool_result)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
