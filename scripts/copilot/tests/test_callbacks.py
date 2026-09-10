"""Real asyncio callback ownership races; no SDK, database, or inference."""

import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))

from core.layers.copilot.callbacks import (
    CallbackAdmissionError, CallbackExecutionError, CallbackRegistry, DuplicateCallbackError,
)


class CallbackRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_rejects_factory_and_refused_id_cannot_replay_after_resume(self):
        registry = CallbackRegistry(lambda: None)
        calls = []

        async def callback():
            calls.append("performed")

        registry.pause_admissions()
        with self.assertRaises(CallbackAdmissionError):
            await registry.run("old-turn", callback)
        registry.resume_admissions()
        with self.assertRaises(DuplicateCallbackError):
            await registry.run("old-turn", callback)
        await registry.run("new-turn", callback)
        self.assertEqual(calls, ["performed"])

    async def test_permanent_close_cannot_resume_or_invoke_factory(self):
        registry = CallbackRegistry(lambda: None)
        registry.close_admissions()
        registry.close_admissions()
        with self.assertRaises(CallbackAdmissionError):
            registry.resume_admissions()
        with self.assertRaises(CallbackAdmissionError):
            await registry.run("closed", lambda: self.fail("Factory ran after close"))
        self.assertEqual(registry.pending_ids, frozenset())

    async def test_success_and_invalidation_before_mutations(self):
        snapshots = []
        registry = CallbackRegistry(lambda: snapshots.append(registry.pending_ids))

        async def callback():
            self.assertEqual(registry.pending_ids, frozenset({"one"}))
            return "value"

        self.assertEqual(await registry.run("one", callback), "value")
        self.assertEqual(registry.pending_ids, frozenset())
        self.assertEqual(snapshots, [frozenset(), frozenset({"one"})])
        self.assertEqual(await registry.cancel_all(0), frozenset())

    async def test_duplicate_never_repeats_side_effect_even_after_completion(self):
        registry = CallbackRegistry(lambda: None)
        calls = []

        async def callback():
            calls.append("performed")

        await registry.run("unique", callback)
        with self.assertRaises(DuplicateCallbackError):
            await registry.run("unique", callback)
        self.assertEqual(calls, ["performed"])

    async def test_sdk_awaiter_cancellation_preserves_owned_callback_until_joined(self):
        registry = CallbackRegistry(lambda: None)
        started = asyncio.Event()

        async def callback():
            started.set()
            await asyncio.Event().wait()

        waiter = asyncio.create_task(registry.run("tool", callback))
        await started.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(registry.pending_ids, frozenset({"tool"}))
        self.assertEqual(await registry.cancel_all(1), frozenset({"tool"}))
        self.assertEqual(registry.pending_ids, frozenset())
        with self.assertRaises(DuplicateCallbackError):
            await registry.run("tool", callback)

    async def test_cancellation_resistant_callback_remains_owned_and_not_proven_cancelled(self):
        registry = CallbackRegistry(lambda: None)
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def callback():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return "completed despite cancellation"

        waiter = asyncio.create_task(registry.run("resistant", callback))
        await started.wait()
        try:
            self.assertEqual(await registry.cancel_all(0.01), frozenset())
            self.assertTrue(cancelled.is_set())
            self.assertEqual(registry.pending_ids, frozenset({"resistant"}))
            # A second request must not interrupt the callback's cleanup await.
            self.assertEqual(await registry.cancel_all(0.01), frozenset())
            self.assertFalse(waiter.done())
        finally:
            release.set()
        self.assertEqual(await waiter, "completed despite cancellation")
        self.assertEqual(await registry.cancel_all(0), frozenset())

    async def test_late_join_is_not_lost_after_cancellation_deadline(self):
        registry = CallbackRegistry(lambda: None)
        started, release = asyncio.Event(), asyncio.Event()

        async def callback():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

        waiter = asyncio.create_task(registry.run("late", callback))
        await started.wait()
        self.assertEqual(await registry.cancel_all(0.01), frozenset())
        self.assertEqual(registry.pending_ids, frozenset({"late"}))
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(await registry.cancel_all(0), frozenset({"late"}))

    async def test_callback_failure_is_sanitized_and_id_remains_consumed(self):
        registry = CallbackRegistry(lambda: None)

        async def callback():
            raise RuntimeError("secret-token-value")

        with self.assertRaises(CallbackExecutionError) as captured:
            await registry.run("failed", callback)
        self.assertEqual(captured.exception.error_type, "RuntimeError")
        self.assertNotIn("secret-token-value", str(captured.exception))
        self.assertIsNone(captured.exception.__context__)
        self.assertEqual(registry.pending_ids, frozenset())
        with self.assertRaises(DuplicateCallbackError):
            await registry.run("failed", callback)

    async def test_concurrent_duplicate_does_not_start_second_callback(self):
        registry = CallbackRegistry(lambda: None)
        started, release = asyncio.Event(), asyncio.Event()

        async def callback():
            started.set()
            await release.wait()

        waiter = asyncio.create_task(registry.run("same", callback))
        await started.wait()
        try:
            with self.assertRaises(DuplicateCallbackError):
                await registry.run("same", callback)
        finally:
            release.set()
            await waiter

    async def test_invalid_deadlines_do_not_request_cancellation(self):
        registry = CallbackRegistry(lambda: None)
        for timeout in (-1, float("inf"), float("nan"), "1"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await registry.cancel_all(timeout)

    async def test_cancelled_shutdown_wait_does_not_drop_resistant_callback(self):
        registry = CallbackRegistry(lambda: None)
        started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def callback():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()

        waiter = asyncio.create_task(registry.run("shutdown-race", callback))
        await started.wait()
        shutdown = asyncio.create_task(registry.cancel_all(1))
        await cancelling.wait()
        shutdown.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await shutdown
        self.assertEqual(registry.pending_ids, frozenset({"shutdown-race"}))
        release.set()
        await waiter
        self.assertEqual(registry.pending_ids, frozenset())
        self.assertEqual(await registry.cancel_all(0), frozenset())

    async def test_empty_or_invalid_id_never_invokes_factory(self):
        registry = CallbackRegistry(lambda: None)

        async def unexpected():
            self.fail("invalid callback ID executed")

        for identity in ("", None, 1):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                await registry.run(identity, unexpected)


if __name__ == "__main__":
    unittest.main()
