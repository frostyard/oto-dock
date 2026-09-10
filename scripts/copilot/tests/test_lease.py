"""Pinned identity, rotation and paused-consumer revocation without an SDK."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import time
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'proxy'))
from core.layers.copilot.credentials import (  # noqa: E402
    CopilotAccountScope, CopilotCredential, CredentialKind, CredentialUnavailableError,
)
from core.layers.copilot.lease import CopilotLeaseGuard  # noqa: E402
from core.layers.copilot.supervisor import (  # noqa: E402
    CopilotSessionSupervisor, RuntimeSnapshot, SessionSupervisorError,
)


def credential(**changes):
    return replace(CopilotCredential('account-a', 'github-user-a', 'generation-a',
                                    CredentialKind.USER_TOKEN, 'gho_testcredential', None), **changes)


def guard_for(current=None, **options):
    initial = current or credential()
    source = [initial]
    invalidated = asyncio.Event()
    calls = []

    async def read(account_id, scope):
        calls.append((account_id, scope))
        if isinstance(source[0], Exception):
            raise source[0]
        return source[0]

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=invalidated.set,
                              check_interval=0.01, read_timeout=0.1, **options)
    return guard, source, invalidated, calls


@pytest.mark.asyncio
async def test_acquisition_and_every_submission_preserve_explicit_scope():
    guard, _, invalidated, calls = guard_for()
    await guard.start()
    try:
        await guard.authorize()
        await guard.authorize()
        assert calls == [('account-a', CopilotAccountScope.personal('alice'))] * 3
        assert guard.valid and not invalidated.is_set()
    finally:
        await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [
    credential(revision='generation-b'), credential(principal_id='another-payer'),
    credential(token='gho_replacedcredential'), credential(account_id='account-b'),
    CredentialUnavailableError('disabled, deleted or scope removed'),
])
async def test_rotation_and_revocation_invalidate_without_consumer(replacement):
    guard, source, invalidated, _ = guard_for()
    await guard.start()
    source[0] = replacement
    try:
        await asyncio.wait_for(invalidated.wait(), 1)
        assert not guard.valid
        source[0] = credential()  # Re-enabling never silently resurrects this runtime.
        with pytest.raises(CredentialUnavailableError):
            await guard.authorize()
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_expiry_timer_fires_while_database_observation_is_stalled():
    expires = time.time() + 0.06
    initial = credential(expires_at=expires)
    blocked = asyncio.Event()
    invalidated = asyncio.Event()
    calls = 0

    async def read(*_):
        nonlocal calls
        calls += 1
        if calls > 1:
            blocked.set()
            await asyncio.Event().wait()
        return initial

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=invalidated.set,
                              check_interval=0.01, read_timeout=1)
    await guard.start()
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        await asyncio.wait_for(invalidated.wait(), 0.5)
        assert not guard.valid
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_initial_store_failure_is_sanitized_and_never_starts_observer():
    guard, source, invalidated, _ = guard_for()
    source[0] = RuntimeError('private token gho_secret')
    with pytest.raises(CredentialUnavailableError) as error:
        await guard.start()
    assert error.value.__context__ is None
    assert 'secret' not in str(error.value) and invalidated.is_set()
    await guard.close()
    with pytest.raises(CredentialUnavailableError):
        await guard.start()


@pytest.mark.asyncio
async def test_close_before_start_is_idempotent_and_does_not_revoke_owner():
    guard, _, invalidated, _ = guard_for()
    await guard.close()
    await guard.close()
    assert not invalidated.is_set() and not guard.valid


@pytest.mark.asyncio
async def test_authorization_failure_prevents_any_sdk_submission_and_closes_runtime():
    calls = []

    class Backend:
        async def send(self, *_, **__):
            calls.append('send')
            return 'message'

        async def disconnect(self):
            calls.append('disconnect')

    async def close():
        calls.append('close')

    async def denied():
        raise CredentialUnavailableError('gho_private_account_failure')

    supervisor = CopilotSessionSupervisor(pending_requests=frozenset, close_runtime=close,
                                           authorize_submission=denied)
    supervisor.bind(Backend())
    with pytest.raises(SessionSupervisorError, match='authorization changed') as error:
        async for _ in supervisor.stream('must never be sent'):
            pass
    assert calls == ['disconnect', 'close']
    assert error.value.__context__ is None
    assert 'private_account' not in str(error.value)


@pytest.mark.asyncio
async def test_store_revocation_closes_supervisor_while_consumer_is_paused():
    initial = credential()
    source = [initial]
    closed = asyncio.Event()
    supervisor = None

    async def read(*_):
        return source[0]

    def invalidate():
        supervisor.invalidate_credentials()

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=invalidate,
                              check_interval=0.01)

    async def close():
        await guard.close()
        closed.set()

    supervisor = CopilotSessionSupervisor(pending_requests=frozenset, close_runtime=close,
                                           authorize_submission=guard.authorize)

    class Backend:
        async def send(self, *_args, **_kwargs):
            for index, kind, data in [
                (1, 'assistant.turn_start', {'turnId': 'turn'}),
                (2, 'assistant.message_delta', {'messageId': 'message', 'deltaContent': 'hello'}),
            ]:
                supervisor.receive_event({'id': str(index), 'type': kind, 'data': data})
            return 'accepted'

        async def disconnect(self):
            pass

    supervisor.bind(Backend())
    await guard.start()
    stream = supervisor.stream('start')
    try:
        await anext(stream)
        source[0] = credential(revision='replacement')
        await asyncio.wait_for(closed.wait(), 1)
        assert not guard.valid
        with pytest.raises(SessionSupervisorError):
            await anext(stream)
    finally:
        await stream.aclose()
        await supervisor.close()


@pytest.mark.asyncio
async def test_close_during_initial_read_never_publishes_credential_or_starts_watcher():
    reading = asyncio.Event()
    initial = credential()

    async def read(*_):
        reading.set()
        await asyncio.Event().wait()
        return initial

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=lambda: None, read_timeout=0.05)
    starting = asyncio.create_task(guard.start())
    await reading.wait()
    assert not guard.valid
    with pytest.raises(CredentialUnavailableError):
        _ = guard.credential
    with pytest.raises(CredentialUnavailableError):
        await guard.start()
    await guard.close()
    with pytest.raises((asyncio.CancelledError, CredentialUnavailableError)):
        await starting
    assert guard._watch_task is None
    assert not guard.valid


@pytest.mark.asyncio
async def test_stubborn_read_cannot_defeat_expiry_or_bounded_cleanup():
    initial = credential(expires_at=time.time() + 0.15)
    reading, cancelled, release, invalidated = (asyncio.Event() for _ in range(4))
    calls = 0

    async def read(*_):
        nonlocal calls
        calls += 1
        if calls > 1:
            reading.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        return initial

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=invalidated.set,
                              check_interval=0.01, read_timeout=0.3)
    await guard.start()
    try:
        await asyncio.wait_for(reading.wait(), 1)
        await asyncio.wait_for(invalidated.wait(), 0.4)
        assert not guard.valid
        with pytest.raises(CredentialUnavailableError, match='cleanup is incomplete'):
            await asyncio.wait_for(guard.close(), 0.7)
        assert cancelled.is_set() and guard._reads
        with pytest.raises(CredentialUnavailableError):
            _ = guard.credential
    finally:
        release.set()
        if guard._reads:
            await asyncio.gather(*guard._reads)
        await asyncio.sleep(0)
    assert not guard._reads


@pytest.mark.asyncio
async def test_read_timeout_does_not_accept_reader_that_suppresses_cancellation():
    initial = credential()
    release = asyncio.Event()
    calls = 0
    invalidated = asyncio.Event()

    async def read(*_):
        nonlocal calls
        calls += 1
        if calls > 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
        return initial

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=invalidated.set,
                              check_interval=1, read_timeout=0.03)
    await guard.start()
    try:
        with pytest.raises(CredentialUnavailableError):
            await asyncio.wait_for(guard.authorize(), 0.3)
        assert invalidated.is_set()
        release.set()
        await guard.close()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_repeated_close_cancellation_waits_for_same_owned_cleanup():
    initial = credential()
    reading, cancelled, release = (asyncio.Event() for _ in range(3))
    calls = 0

    async def read(*_):
        nonlocal calls
        calls += 1
        if calls > 1:
            reading.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        return initial

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=lambda: None,
                              check_interval=0.01, read_timeout=0.5)
    await guard.start()
    await reading.wait()
    closing = asyncio.create_task(guard.close())
    await cancelled.wait()
    cleanup = guard._close_task
    for _ in range(2):
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
    assert not guard.valid and guard._close_task is cleanup
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert cleanup.done()
    await guard.close()


@pytest.mark.asyncio
async def test_acquire_bridge_reads_explicit_account_off_loop_and_scope_cannot_change(monkeypatch):
    import threading
    initial = credential()
    calls = []
    loop_thread = threading.get_ident()
    fake = ModuleType('storage.copilot_account_store')

    def read(account_id, scope):
        calls.append((account_id, scope, threading.get_ident()))
        return initial

    fake.read_credential = read
    monkeypatch.setitem(sys.modules, 'storage.copilot_account_store', fake)
    scope = CopilotAccountScope.personal('alice')
    guard = await CopilotLeaseGuard.acquire('account-a', scope, on_invalid=lambda: None)
    try:
        assert guard.credential == initial
        assert len(calls) == 2
        assert all(account_id == 'account-a' and observed == scope and thread != loop_thread
                   for account_id, observed, thread in calls)
        with pytest.raises(AttributeError):
            guard.scope = CopilotAccountScope.platform()
    finally:
        await guard.close()
    with pytest.raises(CredentialUnavailableError):
        _ = guard.credential


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['error', 'wrong_account', 'startup_failure'])
async def test_acquire_bridge_failure_is_sanitized_and_never_falls_back(monkeypatch, outcome):
    fake = ModuleType('storage.copilot_account_store')
    calls = []

    def read(account_id, scope):
        calls.append((account_id, scope))
        if outcome == 'wrong_account':
            return credential(account_id='other')
        if outcome == 'startup_failure' and len(calls) == 1:
            return credential()
        raise RuntimeError('gho_private_token_from_database')

    fake.read_credential = read
    monkeypatch.setitem(sys.modules, 'storage.copilot_account_store', fake)
    scope = CopilotAccountScope.platform()
    with pytest.raises(CredentialUnavailableError) as error:
        await CopilotLeaseGuard.acquire('account-a', scope, on_invalid=lambda: None)
    assert 'private_token' not in str(error.value)
    assert error.value.__context__ is None
    assert all(call == ('account-a', scope) for call in calls)


class LeaseBackend:
    def __init__(self, supervisor, *, finish):
        self.supervisor = supervisor
        self.finish = finish
        self.messages = 0

    async def send(self, *_args, **_kwargs):
        self.messages += 1
        events = [
            ('user.message', {'messageId': 'input'}),
            ('assistant.turn_start', {'turnId': 'turn'}),
            ('assistant.message_delta', {'messageId': 'message', 'deltaContent': 'hello'}),
        ]
        if self.finish:
            events.append(('session.idle', {}))
        for index, (kind, data) in enumerate(events):
            self.supervisor.receive_event({'id': str(index), 'type': kind, 'data': data})
        return 'input'

    async def snapshot(self):
        return RuntimeSnapshot(False, (), frozenset(), frozenset())

    async def disconnect(self):
        pass


@pytest.mark.asyncio
async def test_revocation_closes_warm_runtime_after_completed_stream():
    initial = credential()
    source = [initial]
    closed = asyncio.Event()

    async def read(*_):
        return source[0]

    def invalidate():
        supervisor.invalidate_credentials()

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=invalidate, check_interval=0.01)

    async def close():
        await guard.close()
        closed.set()

    supervisor = CopilotSessionSupervisor(pending_requests=frozenset, close_runtime=close,
                                           authorize_submission=guard.authorize)
    backend = LeaseBackend(supervisor, finish=True)
    supervisor.bind(backend)
    await guard.start()
    outputs = [event async for event in supervisor.stream('first')]
    assert outputs[-1].type == 'done' and not closed.is_set()
    source[0] = credential(token='gho_generic_unversioned_replacement')
    await asyncio.wait_for(closed.wait(), 1)
    with pytest.raises(SessionSupervisorError):
        await anext(supervisor.stream('must not start'))
    assert backend.messages == 1
    await supervisor.close()


@pytest.mark.asyncio
async def test_steering_revalidates_after_writer_wait_and_never_sends_changed_account():
    initial = credential()
    source = [initial]
    writer_checks = []
    closed = asyncio.Event()
    checking_submissions = False

    async def read(*_):
        if checking_submissions:
            writer_checks.append(supervisor.coordinator._writer_lock.locked())
        return source[0]

    def invalidate():
        supervisor.invalidate_credentials()

    guard = CopilotLeaseGuard(initial, CopilotAccountScope.personal('alice'),
                              read_credential=read, on_invalid=invalidate, check_interval=60)

    async def close():
        await guard.close()
        closed.set()

    supervisor = CopilotSessionSupervisor(pending_requests=frozenset, close_runtime=close,
                                           authorize_submission=guard.authorize)
    backend = LeaseBackend(supervisor, finish=False)
    supervisor.bind(backend)
    await guard.start()
    checking_submissions = True
    stream = supervisor.stream('first')
    try:
        await anext(stream)
        async with supervisor.coordinator.writer():
            steering = asyncio.create_task(supervisor.steer('must not send'))
            await asyncio.sleep(0)
            assert not steering.done()
            source[0] = credential(revision='changed-during-lock-wait')
        with pytest.raises(SessionSupervisorError):
            await steering
        await asyncio.wait_for(closed.wait(), 1)
        assert writer_checks == [True, True]
        assert backend.messages == 1
    finally:
        await stream.aclose()
        await supervisor.close()


@pytest.mark.asyncio
async def test_acquire_initial_database_read_has_independent_deadline(monkeypatch):
    import threading
    reading, release, finished = (threading.Event() for _ in range(3))
    fake = ModuleType('storage.copilot_account_store')

    def read(*_):
        reading.set()
        try:
            release.wait(timeout=2)
            return credential()
        finally:
            finished.set()

    fake.read_credential = read
    monkeypatch.setitem(sys.modules, 'storage.copilot_account_store', fake)
    try:
        with pytest.raises(CredentialUnavailableError):
            await asyncio.wait_for(CopilotLeaseGuard.acquire(
                'account-a', CopilotAccountScope.personal('alice'), on_invalid=lambda: None,
                read_timeout=0.05,
            ), 0.5)
        assert reading.is_set() and not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
