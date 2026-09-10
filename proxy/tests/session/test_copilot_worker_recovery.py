"""A restarted process may observe receipts but cannot infer child cleanup."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.session import worker_ownership
from services.delegation import recovery
from ws import dashboard


@pytest.fixture
def receipt(monkeypatch):
    monkeypatch.setattr(recovery, '_by_session', {})
    monkeypatch.setattr(recovery, '_by_chat', {})
    monkeypatch.setattr(worker_ownership, '_owners', {})
    return {'receipt_id': 'receipt-one', 'conversation_id': 'conversation-one',
            'tool_id': 'call-one', 'session_id': 'child-one', 'chat_id': 'task-run-one',
            'generation': 'original-generation'}


@pytest.mark.asyncio
async def test_restored_receipt_blocks_terminal_lane_without_native_owner(receipt, monkeypatch):
    reader = Mock(side_effect=AssertionError('Terminal run state is not cleanup evidence'))
    monkeypatch.setattr(dashboard.task_store, 'get_run', reader)
    await recovery.restore(store=SimpleNamespace(list_unsettled=lambda: [receipt]))
    assert not worker_ownership._owners
    assert worker_ownership.is_owned_worker(receipt['session_id'])
    assert dashboard.task_run_active(receipt['chat_id'])
    assert await dashboard.task_run_active_async(receipt['chat_id'])
    assert dashboard.owned_worker_message_blocked(receipt['chat_id'], {'type': 'chat'})
    assert not dashboard.owned_worker_message_blocked(receipt['chat_id'], {'type': 'resume_chat'})
    reader.assert_not_called()


def test_only_exact_committed_receipt_can_release_quarantine(receipt):
    recovery.quarantine(receipt)
    recovery.release({**receipt, 'generation': 'later-generation'})
    assert recovery.chat_quarantined(receipt['chat_id'])
    recovery.release(receipt)
    assert not recovery.chat_quarantined(receipt['chat_id'])
    assert not recovery.session_quarantined(receipt['session_id'])


@pytest.mark.asyncio
async def test_restore_rejects_conflicting_records_without_partial_publication(receipt):
    conflict = {**receipt, 'receipt_id': 'different-receipt', 'session_id': 'different-child'}
    with pytest.raises(ValueError):
        await recovery.restore(store=SimpleNamespace(list_unsettled=lambda: [receipt, conflict]))
    assert not recovery._by_session and not recovery._by_chat


@pytest.mark.asyncio
async def test_empty_restore_never_erases_existing_unresolved_owner(receipt):
    recovery.quarantine(receipt)
    await recovery.restore(store=SimpleNamespace(list_unsettled=lambda: []))
    assert recovery.chat_quarantined(receipt['chat_id'])
    # Mutating the caller's object cannot rewrite an already-captured receipt.
    original = deepcopy(receipt)
    receipt['generation'] = 'mutated'
    recovery.release(receipt)
    assert recovery.chat_quarantined(receipt['chat_id'])
    recovery.release(original)
    assert not recovery._by_chat


@pytest.mark.asyncio
async def test_recovery_read_failure_and_overflow_are_not_treated_as_empty(receipt, monkeypatch):
    def fail():
        raise RuntimeError('storage unavailable')
    with pytest.raises(RuntimeError):
        await recovery.restore(store=SimpleNamespace(list_unsettled=fail))
    monkeypatch.setattr(recovery, '_MAX_UNSETTLED', 1)
    with pytest.raises(ValueError):
        await recovery.restore(store=SimpleNamespace(list_unsettled=lambda: [receipt, receipt]))
    assert not recovery._by_session and not recovery._by_chat
