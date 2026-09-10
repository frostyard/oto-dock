"""Host policy request ownership and stale decision races without SDK/network."""

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.requests import CopilotRequestRegistry, RequestUnavailableError  # noqa: E402


@pytest.mark.asyncio
async def test_request_inventory_invalidates_before_registration_and_completion():
    snapshots = []
    registry = CopilotRequestRegistry(lambda: snapshots.append(registry.pending_ids))
    decision = object()

    async def policy():
        assert len(registry.pending_ids) == 1
        return decision

    assert await registry.run(policy) is decision
    assert registry.pending_ids == frozenset()
    assert snapshots[0] == frozenset()
    # Registration observes absence; every later callback observes ownership
    # before its removal, including the shielded answer-delivery continuation.
    assert snapshots[1:]
    assert all(len(snapshot) == 1 for snapshot in snapshots[1:])


@pytest.mark.asyncio
async def test_completed_policy_remains_owned_until_shielded_answer_is_delivered():
    registry = CopilotRequestRegistry(lambda: None)
    observations = []

    def before_waiter_resumes(_task):
        observations.append((registry.pending_ids, registry._registry.pending_ids, waiter.done()))

    async def policy():
        # Registered after the registry/shield done callbacks. This executes
        # after policy-task removal but before the shielded waiter is resumed.
        asyncio.current_task().add_done_callback(before_waiter_resumes)
        return "allow"

    waiter = asyncio.create_task(registry.run(policy))
    assert await waiter == "allow"
    assert len(observations) == 1
    pending, policies, delivered = observations[0]
    assert len(pending) == 1 and policies == frozenset() and delivered is False
    assert registry.pending_ids == frozenset()


@pytest.mark.asyncio
async def test_sdk_waiter_cancellation_keeps_policy_owned_until_joined():
    registry = CopilotRequestRegistry(lambda: None)
    started = asyncio.Event()

    async def policy():
        started.set()
        await asyncio.Event().wait()

    waiter = asyncio.create_task(registry.run(policy))
    await started.wait()
    pending = registry.pending_ids
    assert len(pending) == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert registry.pending_ids == pending
    await registry.cancel_all(0.5)
    assert not registry.pending_ids


@pytest.mark.asyncio
async def test_pause_resume_rejects_old_allow_without_cancelling_policy():
    registry = CopilotRequestRegistry(lambda: None)
    started, release = asyncio.Event(), asyncio.Event()

    async def policy():
        started.set()
        await release.wait()
        return "allow"

    waiter = asyncio.create_task(registry.run(policy))
    await started.wait()
    registry.pause_admissions()
    registry.resume_admissions()
    release.set()
    with pytest.raises(RequestUnavailableError):
        await waiter
    assert not registry.pending_ids

    async def fresh():
        return "fresh decision"

    assert await registry.run(fresh) == "fresh decision"


@pytest.mark.asyncio
async def test_resistant_policy_stays_pending_and_cancellation_is_requested_once():
    registry = CopilotRequestRegistry(lambda: None)
    started, cancelled, release = (asyncio.Event() for _ in range(3))
    cancellations = []

    async def policy():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations.append(True)
            cancelled.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append(True)
                raise
        return "late allow"

    waiter = asyncio.create_task(registry.run(policy))
    await started.wait()
    pending = registry.pending_ids
    registry.pause_admissions()
    try:
        await asyncio.wait_for(registry.cancel_all(0.01), 0.3)
        assert cancelled.is_set() and registry.pending_ids == pending
        await asyncio.wait_for(registry.cancel_all(0.01), 0.3)
        assert cancellations == [True] and not waiter.done()
        registry.resume_admissions()
    finally:
        release.set()
    with pytest.raises(RequestUnavailableError):
        await waiter
    assert not registry.pending_ids


@pytest.mark.asyncio
async def test_cancelled_join_wait_does_not_abandon_policy_or_repeat_cancellation():
    registry = CopilotRequestRegistry(lambda: None)
    started, cancelled, release = (asyncio.Event() for _ in range(3))

    async def policy():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    waiter = asyncio.create_task(registry.run(policy))
    await started.wait()
    shutdown = asyncio.create_task(registry.cancel_all(0.5))
    await cancelled.wait()
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert len(registry.pending_ids) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await registry.cancel_all(0)
    assert not registry.pending_ids


@pytest.mark.asyncio
async def test_paused_and_closed_requests_never_invoke_factory():
    registry = CopilotRequestRegistry(lambda: None)

    async def forbidden():
        pytest.fail("policy executed after admission was closed")

    registry.pause_admissions()
    with pytest.raises(RequestUnavailableError):
        await registry.run(forbidden)
    registry.close_admissions()
    registry.close_admissions()
    with pytest.raises(RequestUnavailableError):
        registry.resume_admissions()
    with pytest.raises(RequestUnavailableError):
        await registry.run(forbidden)
    assert not registry.pending_ids


@pytest.mark.asyncio
async def test_policy_failure_is_sanitized_without_retaining_exception_context():
    registry = CopilotRequestRegistry(lambda: None)

    async def policy():
        raise RuntimeError("sensitive-request-arguments-and-token")

    with pytest.raises(RequestUnavailableError) as error:
        await registry.run(policy)
    assert "sensitive" not in str(error.value)
    assert error.value.__context__ is None
    assert not registry.pending_ids


@pytest.mark.asyncio
async def test_abandoned_sdk_waiter_does_not_leave_unobserved_policy_failure():
    registry = CopilotRequestRegistry(lambda: None)
    started, release, finished = (asyncio.Event() for _ in range(3))
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def policy():
        started.set()
        await release.wait()
        finished.set()
        raise RuntimeError("private-request-error")

    waiter = asyncio.create_task(registry.run(policy))
    try:
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await finished.wait()
        await asyncio.sleep(0)
        assert not registry.pending_ids
        assert not unhandled
    finally:
        release.set()
        await registry.cancel_all(0.5)
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_concurrent_policy_requests_have_distinct_pending_ids():
    registry = CopilotRequestRegistry(lambda: None)
    started = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def policy():
        nonlocal count
        count += 1
        if count == 2:
            started.set()
        await release.wait()
        return "allow"

    waiters = [asyncio.create_task(registry.run(policy)) for _ in range(2)]
    await started.wait()
    assert len(registry.pending_ids) == 2
    release.set()
    assert await asyncio.gather(*waiters) == ["allow", "allow"]
    assert not registry.pending_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True, "1"])
async def test_invalid_cancellation_deadline_is_rejected(timeout):
    registry = CopilotRequestRegistry(lambda: None)
    with pytest.raises(ValueError):
        await registry.cancel_all(timeout)

    async def policy():
        return "still admitted"

    assert await registry.run(policy) == "still admitted"
