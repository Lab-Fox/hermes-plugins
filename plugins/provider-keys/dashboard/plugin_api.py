"""Routes for the Desktop key menu, at /api/plugins/provider-keys/.

No route returns or accepts key material: keys are named by label and shown
masked. Handlers are plain ``def`` so FastAPI runs them off the event loop.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import commands  # noqa: E402 -- the same handlers `hermes keys` and /keys use


def _pool_state(provider: str) -> dict[str, dict[str, Any]]:
    """label -> why Hermes is resting that key, for keys its pool has benched."""
    try:
        from agent.credential_pool import load_pool

        entries = load_pool(provider).entries()
    except Exception:
        return {}
    try:
        from agent.credential_pool import _exhausted_until  # when Hermes will try the key again
    except Exception:
        _exhausted_until = None
    state = {}
    now = time.time()
    for entry in entries:
        if getattr(entry, "last_status", None) != "exhausted":
            continue
        until = None
        if _exhausted_until is not None:
            try:
                until = _exhausted_until(entry, sole_credential=len(entries) == 1)
            except Exception:
                until = None
        if until is not None and until <= now:
            continue  # the rest is over; Hermes uses the key again on its next pick
        state[entry.label] = {"resting": True, "code": getattr(entry, "last_error_code", None),
                              "since": getattr(entry, "last_status_at", None), "until": until}
    return state


def _session_provider(session_id: str) -> str:
    if not session_id:
        return ""
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            resolved = db.resolve_session_id(session_id) or session_id
            row = db.get_session(db.get_compression_tip(resolved) or resolved) or {}
        finally:
            db.close()
        return str(row.get("billing_provider") or "").strip().lower()
    except Exception:
        return ""


def _default_provider() -> str:
    try:
        from hermes_cli.config import load_config

        return str((load_config().get("model") or {}).get("provider") or "").strip().lower()
    except Exception:
        return ""


def build_state(session_id: str = "") -> dict[str, Any]:
    store = commands._keys()
    listing = []
    for name in store.providers_with_keys():
        resting = _pool_state(name)
        # The key requests go out on now: the first one in cycle order that Hermes is not resting.
        using = next((k.label for k in store.cycle_order(name) if k.label not in resting), "")
        listing.append({
            "name": name, "auto": store.auto_cycle(name), "using": using,
            "keys": [{"label": k.label, "masked": k.masked, "active": k.active, "skip": k.skip,
                      **resting.get(k.label, {"resting": False})} for k in store.list_keys(name)],
        })
    current = _session_provider(session_id) or _default_provider()
    known = {p["name"] for p in listing}
    if current not in known:
        try:  # a session records "token-harbor"-style aliases as typed; map them to the provider id
            current = commands.resolve_provider(current)
        except Exception:
            current = ""
    return {"current": current if current in known else "", "providers": listing, "now": time.time()}


class UseBody(BaseModel):
    provider: str
    label: str


class AutoBody(BaseModel):
    provider: str
    on: bool


def _guard(fn, *args):
    try:
        return fn(*args)
    except commands._keys().KeyError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/state")
def state(session: str = Query("", max_length=200)):
    return build_state(session)


@router.post("/use")
def use(body: UseBody):
    return {"message": _guard(commands.use, body.provider, body.label), **build_state()}


@router.post("/auto")
def auto(body: AutoBody):
    return {"message": _guard(commands.auto, body.provider, "on" if body.on else "off"), **build_state()}
