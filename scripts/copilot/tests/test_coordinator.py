"""Offline coordinator races; no Copilot runtime, DB, or credentials required."""

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))

from core.layers.copilot.coordinator import (
    AbortState, CopilotTurnCoordinator, EventSequenceError,
    SettlementObservation, TaskObservation, TaskState,
)


def frame(kind, event_id, data=None, **metadata):
    return {"type": kind, "id": event_id, "data": data or {}, **metadata}


SETTLED = SettlementObservation(False, (), frozenset(), frozenset(), pending_messages=frozenset())


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = CopilotTurnCoordinator()

    def receive(self, kind, data=None, **metadata):
        seq = self.coordinator.last_sequence + 1
        return self.coordinator.receive_event(seq, frame(kind, f"event-{seq}", data, **metadata))

    def idle(self, *, aborted=False):
        self.receive("session.idle", {"aborted": aborted})
        return self.coordinator.begin_reconciliation()

    def start(self, turn_id="turn"):
        self.receive("assistant.turn_start", {"turnId": turn_id})

    def test_complete_only_with_current_full_snapshot_once(self):
        self.start()
        checkpoint = self.idle()
        self.assertIsNotNone(checkpoint)
        done = self.coordinator.finish_reconciliation(checkpoint, SETTLED)
        self.assertEqual([e.type for e in done], ["done"])
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])
        self.assertEqual(self.coordinator.finish_reconciliation(self.idle(), SETTLED), [])

    def test_missing_or_pending_snapshot_fails_closed(self):
        checkpoint = self.idle()
        for observation in [
            replace(SETTLED, processing=None), replace(SETTLED, processing=True),
            replace(SETTLED, tasks=None), replace(SETTLED, pending_permissions=None),
            replace(SETTLED, pending_tools=None),
            replace(SETTLED, pending_messages=None),
            replace(SETTLED, pending_messages=frozenset({"queued-send"})),
            replace(SETTLED, pending_permissions=frozenset({"permission"})),
            replace(SETTLED, pending_tools=frozenset({"tool"})),
        ]:
            with self.subTest(observation=observation):
                self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, observation), [])
        self.assertEqual(len(self.coordinator.finish_reconciliation(checkpoint, SETTLED)), 1)

    def test_unknown_idle_or_orphaned_tasks_are_not_complete(self):
        for state in [TaskState.UNKNOWN, TaskState.IDLE, TaskState.ORPHANED, TaskState.RUNNING, "new-vendor-state"]:
            with self.subTest(state=state):
                self.assertFalse(replace(SETTLED, tasks=(TaskObservation("task", state),)).is_settled())

    def test_terminal_tasks_are_settled_not_necessarily_successful(self):
        tasks = tuple(TaskObservation(str(i), state) for i, state in enumerate(
            [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED]))
        self.assertTrue(replace(SETTLED, tasks=tasks).is_settled())

    def test_duplicate_or_missing_task_identity_invalidates_snapshot(self):
        self.assertFalse(replace(SETTLED, tasks=(TaskObservation("", TaskState.COMPLETED),)).is_settled())
        task = TaskObservation("one", TaskState.COMPLETED)
        self.assertFalse(replace(SETTLED, tasks=(task, task)).is_settled())

    def test_background_permission_and_filtered_child_race_invalidates_snapshot(self):
        for kind, metadata in [
            ("session.background_tasks_changed", {}), ("permission.requested", {}),
            ("assistant.streaming_delta", {"agentId": "child"}),
        ]:
            with self.subTest(kind=kind):
                checkpoint = self.idle()
                self.receive(kind, **metadata)
                self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])
                self.assertIsNone(self.coordinator.begin_reconciliation())

    def test_unknown_activity_still_invalidates_when_diagnostic_suppressed(self):
        self.receive("new.vendor.event")
        checkpoint = self.idle()
        self.assertEqual(self.receive("new.vendor.event"), [])
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])

    def test_new_idle_can_reconcile_after_background_change(self):
        old = self.idle()
        self.receive("session.background_tasks_changed")
        current = self.idle()
        self.assertEqual(self.coordinator.finish_reconciliation(old, SETTLED), [])
        self.assertEqual(len(self.coordinator.finish_reconciliation(current, SETTLED)), 1)

    def test_open_ordinary_tool_blocks_even_incorrect_empty_snapshot(self):
        self.receive("tool.execution_start", {"toolCallId": "t", "toolName": "read"})
        self.assertEqual(self.coordinator.finish_reconciliation(self.idle(), SETTLED), [])
        self.receive("tool.execution_complete", {"toolCallId": "t", "success": True})
        self.assertEqual(len(self.coordinator.finish_reconciliation(self.idle(), SETTLED)), 1)

    def test_duplicate_delivery_does_not_replay_or_invalidate_snapshot(self):
        event = frame("session.idle", "idle")
        self.coordinator.receive_event(1, event)
        checkpoint = self.coordinator.begin_reconciliation()
        self.assertEqual(self.coordinator.receive_event(1, event), [])
        self.assertEqual(self.coordinator.receive_event(2, event), [])
        self.assertEqual(self.coordinator.last_sequence, 2)
        self.assertEqual(len(self.coordinator.finish_reconciliation(checkpoint, SETTLED)), 1)

    def test_changed_payload_same_id_poisoned_at_old_and_new_sequence(self):
        for sequence in (1, 2):
            with self.subTest(sequence=sequence):
                coordinator = CopilotTurnCoordinator()
                coordinator.receive_event(1, frame("session.idle", "same-id"))
                checkpoint = coordinator.begin_reconciliation()
                changed = frame("assistant.turn_start", "same-id", {"turnId": "new-turn"})
                with self.assertRaises(EventSequenceError):
                    coordinator.receive_event(sequence, changed)
                self.assertEqual(coordinator.finish_reconciliation(checkpoint, SETTLED), [])
                self.assertIsNone(coordinator.begin_reconciliation())

    def test_malformed_old_sequence_frame_is_validated_before_dedup(self):
        for malformed in ({"id": "same-id"}, {"id": "same-id", "type": "session.idle", "data": []}):
            with self.subTest(malformed=malformed):
                coordinator = CopilotTurnCoordinator()
                coordinator.receive_event(1, frame("session.idle", "same-id"))
                checkpoint = coordinator.begin_reconciliation()
                with self.assertRaises(ValueError):
                    coordinator.receive_event(1, malformed)
                self.assertEqual(coordinator.finish_reconciliation(checkpoint, SETTLED), [])

    def test_reordered_dictionary_keys_still_deduplicate(self):
        event = {"id": "idle", "type": "session.idle", "data": {"aborted": False, "mode": "interactive"}}
        self.coordinator.receive_event(1, event)
        checkpoint = self.coordinator.begin_reconciliation()
        reordered = {"data": {"mode": "interactive", "aborted": False}, "type": "session.idle", "id": "idle"}
        self.assertEqual(self.coordinator.receive_event(1, reordered), [])
        self.assertEqual(self.coordinator.receive_event(2, reordered), [])
        self.assertEqual(len(self.coordinator.finish_reconciliation(checkpoint, SETTLED)), 1)

    def test_nested_payload_mutation_is_not_hidden_by_reused_input_object(self):
        event = frame("session.idle", "idle", {"aborted": False, "extra": {"nested": [1]}})
        self.coordinator.receive_event(1, event)
        checkpoint = self.coordinator.begin_reconciliation()
        event["data"]["extra"]["nested"].append(2)
        with self.assertRaises(EventSequenceError):
            self.coordinator.receive_event(2, event)
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])

    def test_non_json_payloads_rejected_without_exposing_values(self):
        for invalid in (float("nan"), float("inf"), {1: "private-value"}, ("private-value",), object()):
            with self.subTest(value_type=type(invalid).__name__):
                coordinator = CopilotTurnCoordinator()
                coordinator.receive_event(1, frame("session.idle", "idle"))
                checkpoint = coordinator.begin_reconciliation()
                with self.assertRaises(ValueError) as error:
                    coordinator.receive_event(2, frame("vendor.event", "bad", {"payload": invalid}))
                self.assertNotIn("private-value", str(error.exception))
                self.assertEqual(coordinator.finish_reconciliation(checkpoint, SETTLED), [])

    def test_gap_conflict_and_transport_loss_prevent_settlement(self):
        for sequence, event in [(3, frame("session.info", "gap")), (1, frame("session.idle", "conflict"))]:
            with self.subTest(sequence=sequence):
                coordinator = CopilotTurnCoordinator()
                coordinator.receive_event(1, frame("session.idle", "idle"))
                checkpoint = coordinator.begin_reconciliation()
                with self.assertRaises(EventSequenceError):
                    coordinator.receive_event(sequence, event)
                self.assertEqual(coordinator.finish_reconciliation(checkpoint, SETTLED), [])
                with self.assertRaises(EventSequenceError):
                    coordinator.receive_event(2, frame("session.idle", "retry"))
        checkpoint = self.idle()
        self.coordinator.transport_lost()
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])

    def test_malformed_supported_event_poisoned_not_skipped(self):
        checkpoint = self.idle()
        with self.assertRaises(ValueError):
            self.receive("tool.execution_complete", {"toolCallId": "t"})
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])

    def test_abort_ack_alone_is_not_graceful(self):
        self.start()
        ticket = self.coordinator.request_abort()
        self.assertEqual(self.coordinator.abort_state, AbortState.REQUESTED)
        self.assertTrue(self.coordinator.acknowledge_abort(ticket, accepted=True))
        self.assertEqual(self.coordinator.abort_state, AbortState.ACKNOWLEDGED)
        self.coordinator.finish_reconciliation(self.idle(aborted=True), SETTLED)
        self.assertEqual(self.coordinator.abort_state, AbortState.SETTLED_UNVERIFIED)

    def test_graceful_abort_requires_aborted_idle_and_history_evidence(self):
        self.start()
        ticket = self.coordinator.request_abort()
        checkpoint = self.idle(aborted=True)
        self.coordinator.finish_reconciliation(checkpoint, replace(SETTLED, history_preserved=True))
        self.assertEqual(self.coordinator.abort_state, AbortState.GRACEFUL)
        # A delayed RPC ack cannot downgrade observed completion.
        self.assertFalse(self.coordinator.acknowledge_abort(ticket, accepted=True))
        self.assertEqual(self.coordinator.abort_state, AbortState.GRACEFUL)
        with self.assertRaises(RuntimeError):
            self.coordinator.request_abort()

    def test_normal_idle_with_history_is_not_graceful_abort(self):
        self.start()
        self.coordinator.request_abort()
        self.coordinator.finish_reconciliation(self.idle(), replace(SETTLED, history_preserved=True))
        self.assertEqual(self.coordinator.abort_state, AbortState.SETTLED_UNVERIFIED)

    def test_abort_rejection_and_old_turn_ack_never_become_graceful(self):
        self.start()
        old = self.coordinator.request_abort()
        self.coordinator.acknowledge_abort(old, accepted=False)
        self.coordinator.finish_reconciliation(self.idle(aborted=True), replace(SETTLED, history_preserved=True))
        self.assertEqual(self.coordinator.abort_state, AbortState.REJECTED)
        self.start("new-turn")
        self.assertFalse(self.coordinator.acknowledge_abort(old, accepted=True))
        self.assertEqual(self.coordinator.abort_state, AbortState.NONE)

    def test_abort_control_invalidates_outstanding_snapshot(self):
        self.start()
        checkpoint = self.idle()
        self.coordinator.request_abort()
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])

    def test_model_iteration_does_not_discard_active_abort(self):
        self.start("iteration-one")
        ticket = self.coordinator.request_abort()
        self.start("iteration-two")
        self.assertEqual(self.coordinator.abort_state, AbortState.REQUESTED)
        self.assertTrue(self.coordinator.acknowledge_abort(ticket, accepted=True))

    def test_idle_before_abort_request_cannot_prove_that_abort_graceful(self):
        self.start()
        self.idle(aborted=True)
        self.coordinator.request_abort()
        checkpoint = self.coordinator.begin_reconciliation()
        self.coordinator.finish_reconciliation(checkpoint, replace(SETTLED, history_preserved=True))
        self.assertEqual(self.coordinator.abort_state, AbortState.SETTLED_UNVERIFIED)

    def test_host_only_callback_mutation_invalidates_snapshot(self):
        old = self.idle()
        self.coordinator.invalidate_observation()
        self.assertEqual(self.coordinator.finish_reconciliation(old, SETTLED), [])
        current = self.coordinator.begin_reconciliation()
        pending = replace(SETTLED, pending_tools=frozenset({"host-callback"}))
        self.assertEqual(self.coordinator.finish_reconciliation(current, pending), [])

    def test_abort_does_not_close_surviving_callback_without_join_proof(self):
        self.start()
        self.receive("tool.execution_start", {"toolCallId": "tool", "toolName": "hold"})
        ticket = self.coordinator.request_abort()
        self.coordinator.acknowledge_abort(ticket, accepted=True)
        checkpoint = self.idle(aborted=True)
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])
        pending = replace(SETTLED, pending_tools=frozenset({"tool"}), cancelled_tool_ids=frozenset({"tool"}))
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, pending), [])
        self.coordinator.invalidate_observation()  # Callback cancelled and joined.
        proof = replace(SETTLED, cancelled_tool_ids=frozenset({"tool"}))
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, proof), [])
        events = self.coordinator.finish_reconciliation(self.coordinator.begin_reconciliation(), proof)
        self.assertEqual([event.type for event in events], ["tool_result", "done"])
        self.assertTrue(events[0].data["is_error"])
        self.assertIn("cancelled", events[0].data["result_content"])

    def test_mismatched_cancellation_batch_does_not_clear_known_tool(self):
        self.start()
        self.receive("tool.execution_start", {"toolCallId": "tool", "toolName": "hold"})
        self.coordinator.request_abort()
        checkpoint = self.idle(aborted=True)
        invalid = replace(SETTLED, cancelled_tool_ids=frozenset({"tool", "unknown"}))
        with self.assertRaises(ValueError):
            self.coordinator.finish_reconciliation(checkpoint, invalid)
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, SETTLED), [])

    def test_cancellation_without_abort_intent_does_not_clear_tool(self):
        self.start()
        self.receive("tool.execution_start", {"toolCallId": "tool", "toolName": "hold"})
        checkpoint = self.idle()
        proof = replace(SETTLED, cancelled_tool_ids=frozenset({"tool"}))
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, proof), [])

    def test_rejected_abort_cannot_use_cancelled_tool_proof(self):
        self.start()
        self.receive("tool.execution_start", {"toolCallId": "tool", "toolName": "hold"})
        ticket = self.coordinator.request_abort()
        self.coordinator.acknowledge_abort(ticket, accepted=False)
        checkpoint = self.idle(aborted=True)
        proof = replace(SETTLED, cancelled_tool_ids=frozenset({"tool"}))
        self.assertEqual(self.coordinator.finish_reconciliation(checkpoint, proof), [])


class CoordinatorConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_serializes_sends_without_blocking_event_progress(self):
        coordinator = CopilotTurnCoordinator()
        acquired = asyncio.Event()
        release = asyncio.Event()
        second_acquired = asyncio.Event()

        async def first():
            async with coordinator.writer():
                acquired.set()
                await release.wait()

        async def second():
            async with coordinator.writer():
                second_acquired.set()

        one = asyncio.create_task(first())
        await acquired.wait()
        two = asyncio.create_task(second())
        await asyncio.sleep(0)
        self.assertFalse(second_acquired.is_set())
        # Inbound events and reconciliation do not need the outbound lock.
        coordinator.receive_event(1, frame("session.idle", "idle"))
        self.assertEqual(len(coordinator.finish_reconciliation(coordinator.begin_reconciliation(), SETTLED)), 1)
        release.set()
        await asyncio.wait_for(asyncio.gather(one, two), 1)
        self.assertTrue(second_acquired.is_set())

    async def test_cancelled_writer_releases_lock_and_waiter_cancellation_is_safe(self):
        coordinator = CopilotTurnCoordinator()
        acquired = asyncio.Event()

        async def hold():
            async with coordinator.writer():
                acquired.set()
                await asyncio.Event().wait()

        holder = asyncio.create_task(hold())
        await acquired.wait()
        waiter = asyncio.create_task(hold())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        holder.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await holder
        async with asyncio.timeout(1):
            async with coordinator.writer():
                pass

    async def test_reentrant_writer_fails_immediately(self):
        coordinator = CopilotTurnCoordinator()
        async with coordinator.writer():
            with self.assertRaises(RuntimeError):
                async with coordinator.writer():
                    self.fail("reentrant lock acquired")


if __name__ == "__main__":
    unittest.main()
