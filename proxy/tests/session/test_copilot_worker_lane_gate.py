"""A terminal run remains observation-only until its exact worker owner exits."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import WebSocketDisconnect
import pytest

from core.session import worker_ownership
from ws import dashboard, dashboard_chat, dashboard_dispatch

CHAT = 'task-owned-run'


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(worker_ownership, '_owners', {})
    owner = SimpleNamespace(chat_id=CHAT, closing=True)
    worker_ownership.claim_worker('owned-session', owner)
    return owner


@pytest.mark.asyncio
async def test_retained_claim_precedes_database_and_terminal_run_until_exact_release(owner, monkeypatch):
    read = Mock(side_effect=AssertionError('Owned lane must not query run state'))
    monkeypatch.setattr(dashboard.task_store, 'get_run', read)
    assert dashboard.task_run_active(CHAT)
    assert await dashboard.task_run_active_async(CHAT)
    worker_ownership.release_worker('owned-session', object())
    assert dashboard.task_run_active(CHAT)
    worker_ownership.release_worker('owned-session', owner)
    read.side_effect = None
    read.return_value = {'status': 'completed'}
    assert not dashboard.task_run_active(CHAT)
    read.assert_called_once_with('owned-run')


MUTATIONS = ['pre_warmup', 'warmup', 'chat', 'artifact_interaction', 'app_action',
             'permission_response', 'question_response', 'location_response', 'plan_review_response',
             'mode_change', 'model_change', 'execution_mode_change', 'execution_mode_switch',
             'compact_context', 'move_chat', 'switch_engine', 'implement_plan', 'abort',
             'cancel_queued', 'cancel_all_queued', 'pty_takeover', 'pty_input', 'pty_attachments']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', MUTATIONS)
async def test_idle_dispatch_cannot_mutate_or_queue_into_retained_worker(owner, kind):
    conn = SimpleNamespace(chat_id=CHAT, _send_error=AsyncMock())
    # A forged unrelated chat_id cannot bypass handlers that use bound state.
    await dashboard_dispatch.ClientMessageDispatcher._dispatch_client_message(
        conn, {'type': kind, 'chat_id': 'other-chat', 'text': 'injected', 'approved': True})
    conn._send_error.assert_awaited_once()
    assert 'still owned' in conn._send_error.call_args.args[0]
    assert dashboard.owned_worker_message_blocked(None, {'type': kind, 'chat_id': CHAT})


@pytest.mark.parametrize('kind', ['resume_chat', 'chat_read', 'probe_liveness', 'client_info', 'user_active', 'user_idle', 'ping', 'close'])
def test_read_only_navigation_and_connection_lifecycle_remain_available(owner, kind):
    assert not dashboard.owned_worker_message_blocked(CHAT, {'type': kind, 'chat_id': CHAT})


@pytest.mark.asyncio
async def test_dispatch_resumes_normal_behavior_only_after_owner_release(owner):
    handler = AsyncMock()
    conn = SimpleNamespace(chat_id=CHAT, _send_error=AsyncMock(), _handle_model_change=handler)
    msg = {'type': 'model_change', 'model': 'other-model'}
    await dashboard_dispatch.ClientMessageDispatcher._dispatch_client_message(conn, msg)
    handler.assert_not_awaited()
    worker_ownership.release_worker('owned-session', owner)
    await dashboard_dispatch.ClientMessageDispatcher._dispatch_client_message(conn, msg)
    handler.assert_awaited_once_with(msg)


@pytest.mark.asyncio
async def test_continue_recheck_blocks_warmup_send_and_permission_paths_before_db(owner, monkeypatch):
    monkeypatch.setattr(dashboard_chat, 'run_db', Mock(side_effect=AssertionError('No DB on retained claim')))
    conn = SimpleNamespace(_send_error=AsyncMock())
    assert await dashboard_chat.ChatController._deny_task_continue(conn, CHAT)
    conn._send_error.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chat', 'mode_change', 'permission_response', 'abort'])
async def test_attached_live_pump_cannot_bypass_the_owned_lane_gate(owner, monkeypatch, kind):
    monkeypatch.setattr(dashboard_chat, '_chat_streaming_state', {})
    queue = asyncio.Queue()
    pump = SimpleNamespace(chat_id=CHAT, attach=Mock(return_value=queue), detach=Mock())
    socket = SimpleNamespace(receive_text=AsyncMock(side_effect=[json.dumps({'type': kind, 'text': 'inject'}), WebSocketDisconnect()]))
    conn = SimpleNamespace(chat_id=CHAT, websocket=socket, _send=AsyncMock(), _send_error=AsyncMock())
    result = await dashboard_chat.ChatController._stream_via_pump(conn, pump)
    assert result['detached'] is True
    conn._send_error.assert_awaited_once()
    pump.detach.assert_called_with(queue)
