"""Server-kick rescue at WS close (ws/dashboard._extract_server_kicks).

A chat's server-owned first turn rides the per-connection notify queue; the
queue dies with the connection, so a kick waiting behind a streaming turn was
silently lost on refresh/blip ("my first message never got answered"). The
close handler now drains the queue through this helper and runs every rescued
kick headless. These tests lock the extraction semantics: only `_server_kick`
items are kept (in order), everything else is dropped, the queue ends empty.

The helper is async since 2026-09-04: the queue drain itself is still one
synchronous step, only the delegate-result parking writes await the DB
executor (storage/pg.py's event-loop rule).
"""

import asyncio

import pytest

from ws.dashboard import _extract_server_kicks


def _kick(cid: str) -> dict:
    return {"type": "_server_kick", "chat_id": cid, "session_id": f"s-{cid}",
            "text": "hello", "images": [], "files": []}


@pytest.mark.asyncio
async def test_extracts_only_kicks_in_order():
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"type": "notification", "data": {}})
    q.put_nowait(_kick("c1"))
    q.put_nowait({"type": "bg_nudge", "chat_id": "x"})
    q.put_nowait(_kick("c2"))
    q.put_nowait("garbage-non-dict")

    kicks = await _extract_server_kicks(q)

    assert [k["chat_id"] for k in kicks] == ["c1", "c2"]
    assert all(k["type"] == "_server_kick" for k in kicks)
    assert q.empty()  # non-kick items dropped, exactly as a dead queue did


@pytest.mark.asyncio
async def test_empty_queue():
    q: asyncio.Queue = asyncio.Queue()
    assert await _extract_server_kicks(q) == []
    assert q.empty()


@pytest.mark.asyncio
async def test_kick_payload_preserved():
    q: asyncio.Queue = asyncio.Queue()
    payload = {"type": "_server_kick", "chat_id": "c9", "session_id": "s9",
               "text": "first prompt", "images": [{"x": 1}], "files": [{"f": 2}]}
    q.put_nowait(payload)
    kicks = await _extract_server_kicks(q)
    assert kicks == [payload]


@pytest.mark.asyncio
async def test_task_result_prompt_is_parked_off_loop(monkeypatch):
    """An undrained delegate result is persisted (event row + durable wake)
    through the DB executor, never on the loop thread."""
    import threading
    from storage import database as task_store

    loop_ident = threading.get_ident()
    seen: dict[str, object] = {}

    def _add(cid, role, text, **kw):
        seen["add_thread"] = threading.get_ident()
        seen["event_type"] = kw.get("event_type")
        return 1

    def _park(cid, prompt):
        seen["park_thread"] = threading.get_ident()
        seen["park"] = (cid, prompt)
        return True

    monkeypatch.setattr(task_store, "add_chat_message", _add)
    monkeypatch.setattr(task_store, "append_pending_delegate_wake", _park)

    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"type": "task_result_prompt", "chat_id": "c1",
                  "result_prompt": "result!", "task_id": "t", "task_name": "n",
                  "delegate_agent": "a", "output_text": "o", "status": "completed"})
    q.put_nowait(_kick("c2"))

    kicks = await _extract_server_kicks(q)

    assert [k["chat_id"] for k in kicks] == ["c2"]
    assert seen["event_type"] == "delegate_result"
    assert seen["park"] == ("c1", "result!")
    assert seen["add_thread"] != loop_ident
    assert seen["park_thread"] != loop_ident
