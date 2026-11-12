"""Read-only session details for the Desktop Context tab, at /api/plugins/session-context/.

Handlers are plain ``def`` so FastAPI runs them in a worker thread: a large
transcript must not hold up the gateway's event loop.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_TOKEN_FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write")


def _tokens(row: dict[str, Any]) -> dict[str, int]:
    return {name: int(row.get(f"{name}_tokens") or 0) for name in _TOKEN_FIELDS}


def _cost(row: dict[str, Any]) -> float:
    """What was billed when the provider reported it, else Hermes's estimate."""
    return float(row.get("actual_cost_usd") or row.get("estimated_cost_usd") or 0.0)


def _lineage(db, session_id: str) -> list[dict[str, Any]]:
    """Every stored row of one conversation, oldest first. Compressing a long
    chat continues it under a new id; its usage is spread over those rows."""
    resolved = db.resolve_session_id(session_id) or session_id
    ids = db.get_compression_lineage(db.get_compression_tip(resolved) or resolved)
    rows = [row for row in (db.get_session(sid) for sid in ids) if row]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No stored session '{session_id}'")
    return rows


def _model_usage(db, ids: list[str]) -> list[dict[str, Any]]:
    """Per-model totals: shows what a mid-chat model switch or a side task cost."""
    marks = ",".join("?" * len(ids))
    rows = db._read_all(  # noqa: SLF001 - no public reader for this table
        f"""SELECT model, billing_provider, task, SUM(api_call_count), SUM(input_tokens), SUM(output_tokens),
                   SUM(reasoning_tokens), SUM(cache_read_tokens), SUM(cache_write_tokens),
                   SUM(COALESCE(NULLIF(actual_cost_usd, 0), estimated_cost_usd, 0))
              FROM session_model_usage WHERE session_id IN ({marks})
             GROUP BY model, billing_provider, task ORDER BY MIN(first_seen)""", ids)
    return [{"model": r[0] or "", "provider": r[1] or "", "task": r[2] or "", "calls": int(r[3] or 0),
             "tokens": {"input": int(r[4] or 0), "output": int(r[5] or 0), "reasoning": int(r[6] or 0),
                        "cache_read": int(r[7] or 0), "cache_write": int(r[8] or 0)},
             "cost_usd": float(r[9] or 0.0)} for r in rows]


def build_summary(db, session_id: str) -> dict[str, Any]:
    rows = _lineage(db, session_id)
    first, tip = rows[0], rows[-1]
    ids = [row["id"] for row in rows]

    tokens = {name: sum(_tokens(row)[name] for row in rows) for name in _TOKEN_FIELDS}
    # Hermes stores input without the cached part, so the four add up to what was processed.
    tokens["total"] = tokens["input"] + tokens["output"] + tokens["cache_read"] + tokens["cache_write"]

    messages = db.get_messages(tip["id"], include_ancestors=True)
    counts: dict[str, int] = {}
    listing = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        counts[role] = counts.get(role, 0) + 1
        listing.append({"id": message.get("id"), "session_id": message.get("session_id"), "role": role,
                        "timestamp": message.get("timestamp"), "tool_name": message.get("tool_name") or "",
                        "chars": len(message.get("content") or "")})
    stamps = [m["timestamp"] for m in listing if m["timestamp"]]

    try:
        models = _model_usage(db, ids)
    except Exception:  # an older store without the per-model table still gets the totals
        models = []
    return {
        "session": {"id": tip["id"], "root_id": first["id"], "segments": len(rows), "title": tip.get("title") or "",
                    "provider": tip.get("billing_provider") or "", "model": tip.get("model") or "",
                    "source": first.get("source") or "", "cwd": tip.get("cwd") or "",
                    "created_at": first.get("started_at"),
                    "last_activity_at": max([tip.get("last_activity_at") or 0, *stamps]) or None},
        "tokens": tokens,
        "cost": {"usd": sum(_cost(row) for row in rows), "status": tip.get("cost_status") or "",
                 "billed": any(row.get("actual_cost_usd") for row in rows)},
        "api_calls": sum(int(row.get("api_call_count") or 0) for row in rows),
        "counts": {"total": len(listing), **counts},
        "system_prompt": tip.get("system_prompt") or "",
        "models": models,
        "messages": listing,
    }


def message_detail(db, session_id: str, message_id: int) -> dict[str, Any]:
    tip = _lineage(db, session_id)[-1]
    for message in db.get_messages(tip["id"], include_ancestors=True):
        if message.get("id") == message_id:
            return {"id": message_id, "role": message.get("role"), "content": message.get("content") or "",
                    "reasoning": message.get("reasoning") or message.get("reasoning_content") or "",
                    "tool_name": message.get("tool_name") or "", "tool_calls": message.get("tool_calls"),
                    "finish_reason": message.get("finish_reason") or "", "timestamp": message.get("timestamp")}
    raise HTTPException(status_code=404, detail=f"No message {message_id} in session '{session_id}'")


def _with_db(fn, *args):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        return fn(db, *args)
    finally:
        db.close()


@router.get("/session")
def session(id: str = Query(..., min_length=1, max_length=200)):  # noqa: A002 - the query parameter name
    return _with_db(build_summary, id)


@router.get("/message")
def message(session: str = Query(..., min_length=1, max_length=200), id: int = Query(...)):  # noqa: A002
    return _with_db(message_detail, session, id)
