"""Pending human-policy decisions participate in supervisor settlement/cleanup."""

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.requests import RequestUnavailableError  # noqa: E402
from core.layers.copilot.supervisor import (  # noqa: E402
    CopilotSessionSupervisor, RuntimeSnapshot, SessionSupervisorError,
)


class Backend:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.serial = 0
        self.disconnected = False
        self.idle = False
        self.snapshot_seen = asyncio.Event()
        self.control_hook = None
        self.interrupt_accepted = True

    def emit(self, kind, data=None):
        self.serial += 1
        self.supervisor.receive_event({"id": str(self.serial), "type": kind, "data": data or {}})

    async def send(self, _prompt, *, immediate=False):
        self.emit("user.message", {"messageId": "input"})
        self.emit("assistant.turn_start", {"turnId": "turn"})
        self.emit("assistant.message_delta", {"messageId": "reply", "deltaContent": "hello"})
        if self.idle:
            self.emit("session.idle")
        return "input"

    async def snapshot(self):
        self.snapshot_seen.set()
        return RuntimeSnapshot(False, (), frozenset(), frozenset())

    async def is_processing(self):
        return False

    async def abort(self):
        if self.control_hook:
            await self.control_hook()
        self.emit("session.idle", {"aborted": True})

    async def interrupt(self):
        if self.control_hook:
            await self.control_hook()
        if self.interrupt_accepted:
            self.emit("assistant.idle")
        return self.interrupt_accepted

    async def disconnect(self):
        self.disconnected = True


def setup(*, external=frozenset, timeout=0.05):
    closed = asyncio.Event()

    async def close_runtime():
        closed.set()

    supervisor = CopilotSessionSupervisor(
        pending_requests=external, close_runtime=close_runtime, rpc_timeout=timeout, turn_timeout=2,
    )
    backend = Backend(supervisor)
    supervisor.bind(backend)
    return supervisor, backend, closed


async def start_request(supervisor, *, resistant=False):
    started, cancelled, release = (asyncio.Event() for _ in range(3))

    async def policy():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if not resistant:
                raise
            await release.wait()
        return "allow"

    waiter = asyncio.create_task(supervisor.requests.run(policy))
    await started.wait()
    return waiter, cancelled, release


async def through_text(stream):
    while True:
        event = await anext(stream)
        if event.type == "text":
            return


async def through_done(stream):
    while True:
        event = await anext(stream)
        if event.type == "done":
            return event


@pytest.mark.asyncio
async def test_owned_request_blocks_done_even_after_native_idle_and_empty_inventory():
    supervisor, backend, _ = setup()
    backend.idle = True
    stream = supervisor.stream("start")
    waiter = next_event = None
    try:
        await through_text(stream)
        waiter, _, release = await start_request(supervisor)
        next_event = asyncio.create_task(through_done(stream))
        await asyncio.wait_for(backend.snapshot_seen.wait(), 1)
        await asyncio.sleep(0)
        assert not next_event.done() and len(supervisor.requests.pending_ids) == 1
        release.set()
        assert await waiter == "allow"
        assert (await asyncio.wait_for(next_event, 1)).type == "done"
    finally:
        await supervisor.close()
        if next_event and not next_event.done():
            await asyncio.gather(next_event, return_exceptions=True)
        await stream.aclose()
        if waiter:
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_external_inventory_is_not_replaced_by_empty_owned_registry():
    supervisor, _, _ = setup(external=lambda: None)
    try:
        assert not supervisor.requests.pending_ids
        snapshot = await supervisor._observe()
        assert snapshot.pending_permissions is None
        waiter, _, release = await start_request(supervisor)
        assert (await supervisor._observe()).pending_permissions is None
        release.set()
        await waiter
        assert (await supervisor._observe()).pending_permissions is None
    finally:
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "interrupt"])
async def test_control_pauses_request_admission_before_runtime_ack(control):
    supervisor, backend, _ = setup()
    stream = supervisor.stream("start")
    checked = asyncio.Event()

    async def forbidden():
        pytest.fail("new request reached policy while control acknowledgement was pending")

    async def check_admission():
        with pytest.raises(RequestUnavailableError):
            await supervisor.requests.run(forbidden)
        checked.set()

    backend.control_hook = check_admission
    try:
        await anext(stream)
        ack = await getattr(supervisor, control)()
        assert ack.accepted and checked.is_set()
    finally:
        await stream.aclose()
        await supervisor.close()


@pytest.mark.asyncio
async def test_refused_interrupt_reopens_admission_but_rejects_prior_late_allow():
    supervisor, backend, _ = setup()
    backend.interrupt_accepted = False
    stream = supervisor.stream("start")
    waiter = None
    try:
        await anext(stream)
        waiter, cancelled, release = await start_request(supervisor)
        ack = await supervisor.interrupt()
        assert not ack.accepted and not cancelled.is_set()
        release.set()
        with pytest.raises(RequestUnavailableError):
            await waiter

        async def fresh():
            return "new turn decision"

        assert await supervisor.requests.run(fresh) == "new turn decision"
    finally:
        await stream.aclose()
        await supervisor.close()
        if waiter:
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_accepted_abort_is_bounded_and_does_not_claim_resistant_request_stopped():
    supervisor, backend, _ = setup()
    stream = supervisor.stream("start")
    waiter = next_event = None
    try:
        await anext(stream)
        waiter, cancelled, release = await start_request(supervisor, resistant=True)
        ack = await asyncio.wait_for(supervisor.abort(), 0.5)
        assert ack.accepted and not ack.callbacks_stopped
        assert cancelled.is_set() and len(supervisor.requests.pending_ids) == 1
        next_event = asyncio.create_task(through_done(stream))
        await asyncio.wait_for(backend.snapshot_seen.wait(), 1)
        await asyncio.sleep(0)
        assert not next_event.done()
        release.set()
        with pytest.raises(RequestUnavailableError):
            await waiter
        assert (await asyncio.wait_for(next_event, 1)).type == "done"
    finally:
        if waiter and not waiter.done():
            release.set()
            await asyncio.gather(waiter, return_exceptions=True)
        await supervisor.close()
        if next_event and not next_event.done():
            await asyncio.gather(next_event, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_close_reaches_runtime_even_when_policy_cancellation_is_resisted():
    supervisor, backend, closed = setup()
    waiter, cancelled, release = await start_request(supervisor, resistant=True)
    try:
        with pytest.raises(SessionSupervisorError, match="cleanup is incomplete"):
            await asyncio.wait_for(supervisor.close(), 0.5)
        assert closed.is_set() and backend.disconnected and cancelled.is_set()
        assert len(supervisor.requests.pending_ids) == 1

        async def forbidden():
            pytest.fail("closed supervisor admitted a new policy callback")

        with pytest.raises(RequestUnavailableError):
            await supervisor.requests.run(forbidden)
    finally:
        release.set()
        with pytest.raises(RequestUnavailableError):
            await waiter
    assert not supervisor.requests.pending_ids
