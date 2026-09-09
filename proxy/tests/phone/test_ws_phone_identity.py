"""The phone WebSocket warmup and the call's identity: identity resolved
first, stamped for the call log, reuse requests validated, ephemeral caller
trees pruned at hangup.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import ws.phone as ws_phone
from core.session import external_identity
from services.phone.phone_identity import RouteIdentity
from tests.phone import test_ws_phone_turns as turns

SID = "11111111-2222-4333-8444-555555555555"
_run_handler = turns._run_handler
phone_ws_env = turns.phone_ws_env      # re-registers the fixture in this module


async def _warmup_frame(ws, **fields):
    msg = {"type": "warmup", "model": "unified", "llm_mode": "direct", "phone_mode": True}
    msg.update(fields)
    ws.push(msg)
    return await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))


@pytest.mark.asyncio
async def test_identity_is_stamped_for_the_call_log(phone_ws_env, monkeypatch):
    ws, layer = phone_ws_env
    seen = {}
    ident = external_identity.resolve("phone", "+302101234567", session_id=SID)
    monkeypatch.setattr(
        ws_phone, "resolve_route_identity",
        lambda route, **kw: seen.update(kw) or RouteIdentity(mode="caller", role="viewer", external=ident),
    )
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, caller_phone="+30 210 1234567", pin_verified=True,
                                phone_route_id="r1")
    assert frame["type"] == "warmup_ready"
    sid = frame["data"]["session_id"]
    assert seen["caller_phone"] == "+30 210 1234567" and seen["pin_verified"] is True
    assert seen["session_id"] == sid
    assert ws_phone.pop_call_identity(sid) == "caller:+302101234567"
    assert ws_phone.pop_call_identity(sid) == ""     # consumed once
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_reuse_request_is_validated(phone_ws_env, monkeypatch):
    """A non-UUID / non-phone / other-agent session id is never attached."""
    ws, layer = phone_ws_env
    checked = {}
    monkeypatch.setattr(
        ws_phone, "_validated_reuse_sid",
        lambda sid, agent: checked.update(sid=sid, agent=agent) or "",
    )
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, session_id="not-a-phone-session")
    assert frame["type"] == "warmup_ready"
    assert checked == {"sid": "not-a-phone-session", "agent": "unified"}
    assert frame["data"]["session_id"] != "not-a-phone-session"
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)


def test_validated_reuse_sid_rules(temp_db, monkeypatch):
    from storage import database as task_store
    assert ws_phone._validated_reuse_sid("", "a") == ""
    assert ws_phone._validated_reuse_sid("nope", "a") == ""
    sid = str(uuid.uuid4())
    monkeypatch.setattr(task_store, "get_chat_by_session",
                        lambda s: {"source_type": "dashboard", "agent": "a"})
    assert ws_phone._validated_reuse_sid(sid, "a") == ""
    monkeypatch.setattr(task_store, "get_chat_by_session",
                        lambda s: {"source_type": "phone", "agent": "b"})
    assert ws_phone._validated_reuse_sid(sid, "a") == ""
    monkeypatch.setattr(task_store, "get_chat_by_session",
                        lambda s: {"source_type": "phone", "agent": "a"})
    assert ws_phone._validated_reuse_sid(sid, "a") == sid


@pytest.mark.asyncio
async def test_ephemeral_tree_is_pruned_at_hangup(phone_ws_env, monkeypatch, tmp_path):
    ws, layer = phone_ws_env
    agents = tmp_path / "agents"
    monkeypatch.setattr(ws_phone.config, "AGENTS_DIR", agents)
    monkeypatch.setattr(ws_phone.config, "get_agent_dir", lambda a: agents / a)
    pruned = []

    def _resolve(route, **kw):
        ident = external_identity.resolve("phone", "anonymous", session_id=kw["session_id"])
        home = external_identity.external_home(agents / "unified", ident)
        (home / "workspace").mkdir(parents=True)
        return RouteIdentity(mode="caller", role="viewer", external=ident)
    monkeypatch.setattr(ws_phone, "resolve_route_identity", _resolve)
    monkeypatch.setattr(ws_phone.external_identity, "prune_ephemeral",
                        lambda home: pruned.append(str(home)) or True)
    task = await _run_handler(ws)
    frame = await _warmup_frame(ws, caller_phone="anonymous")
    sid = frame["data"]["session_id"]
    ws.push({"type": "close"})
    await asyncio.wait_for(task, 5)
    assert pruned == [str(agents / "unified" / "externals" / "phone" / "_ephemeral" / sid)]
