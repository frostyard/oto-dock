"""Offline event-contract tests: no Copilot install, inference, or PostgreSQL.

Run: python3 -m unittest discover -s scripts/copilot/tests -p test_translator.py
Fixtures follow the published SDK 1.0.13 wire field names; they are synthetic,
not recordings proving vendor runtime behavior.
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))

from core.layers.copilot.translator import CopilotEventTranslator


def frame(kind, data=None, event_id="event-1", **metadata):
    return {"id": event_id, "type": kind, "data": data or {}, **metadata}


def visible(events):
    return [(event.type, event.data) for event in events]


class TranslatorTests(unittest.TestCase):
    def setUp(self):
        self.translator = CopilotEventTranslator()

    def send(self, kind, data=None, event_id="event-1", **metadata):
        return self.translator.translate(frame(kind, data, event_id, **metadata))

    def test_streaming_final_emits_only_missing_suffix(self):
        a = self.send("assistant.message_delta", {"messageId": "m1", "deltaContent": "Hello"})
        b = self.send("assistant.message", {"messageId": "m1", "content": "Hello world"}, "e2")
        self.assertEqual(visible(a + b), [("text", {"content": "Hello"}), ("text", {"content": " world"})])

    def test_same_frame_replay_does_not_repeat_delta(self):
        event = frame("assistant.message_delta", {"messageId": "m1", "deltaContent": "ha"})
        self.assertEqual(len(self.translator.translate(event)), 1)
        self.assertEqual(self.translator.translate(event), [])
        # Equal text with a new event identity is a legitimate second delta.
        event["id"] = "e2"
        self.assertEqual(visible(self.translator.translate(event)), [("text", {"content": "ha"})])

    def test_final_before_delta_and_repeated_final_do_not_duplicate(self):
        self.assertEqual(len(self.send("assistant.message", {"messageId": "m1", "content": "done"})), 1)
        self.assertEqual(self.send("assistant.message_delta", {"messageId": "m1", "deltaContent": "done"}, "e2"), [])
        self.assertEqual(self.send("assistant.message", {"messageId": "m1", "content": "done"}, "e3"), [])

    def test_separate_messages_with_equal_content_survive(self):
        for index in range(2):
            result = self.send("assistant.message", {"messageId": f"m{index}", "content": "same"}, f"e{index}")
            self.assertEqual(visible(result), [("text", {"content": "same"})])

    def test_conflicting_snapshot_is_diagnostic_not_repeated_text(self):
        self.send("assistant.message_delta", {"messageId": "m1", "deltaContent": "hello"})
        events = self.send("assistant.message", {"messageId": "m1", "content": "different"}, "e2")
        self.assertEqual(events[0].type, "system")
        self.assertEqual(events[0].data["subtype"], "copilot_content_mismatch")
        self.assertNotIn("different", str(events[0].data))

    def test_reasoning_stream_and_repeated_final_emit_one_block(self):
        events = self.send("assistant.reasoning_delta", {"reasoningId": "r1", "deltaContent": "Think"})
        events += self.send("assistant.reasoning", {"reasoningId": "r1", "content": "Thinking"}, "e2")
        events += self.send("assistant.reasoning", {"reasoningId": "r1", "content": "Thinking"}, "e3")
        self.assertEqual(visible(events), [
            ("thinking", {"phase": "start"}),
            ("thinking", {"phase": "delta", "text": "Think"}),
            ("thinking", {"phase": "delta", "text": "ing"}),
            ("thinking", {"phase": "end", "text": ""}),
        ])

    def test_final_only_reasoning_has_start_delta_end_once(self):
        events = self.send("assistant.reasoning", {"reasoningId": "r1", "content": "Reason"})
        self.assertEqual([e.data["phase"] for e in events], ["start", "delta", "end"])
        self.assertEqual(self.send("assistant.reasoning_delta", {"reasoningId": "r1", "deltaContent": "Reason"}, "e2"), [])

    def test_empty_reasoning_does_not_create_empty_ui_block(self):
        self.assertEqual(self.send("assistant.reasoning_delta", {"reasoningId": "r1", "deltaContent": ""}), [])
        self.assertEqual(self.send("assistant.reasoning", {"reasoningId": "r1", "content": ""}, "e2"), [])

    def test_agent_owned_text_tools_errors_and_idle_never_reach_main(self):
        for kind, data in [
            ("assistant.message", {"messageId": "m1", "content": "child"}),
            ("tool.execution_start", {"toolCallId": "t1", "toolName": "bash"}),
            ("session.error", {"message": "child failure"}),
            ("session.idle", {}),
        ]:
            with self.subTest(kind=kind):
                self.assertEqual(self.send(kind, data, kind, agentId="child"), [])
        self.assertEqual(self.translator.settle_idle("session.idle", background_settled=True), [])

    def test_parent_tool_call_filters_but_event_parent_does_not(self):
        self.assertEqual(self.send("assistant.message", {"messageId": "m1", "content": "child", "parentToolCallId": "tool"}), [])
        events = self.send("assistant.message", {"messageId": "m2", "content": "main"}, "e2", parentId="parent-event")
        self.assertEqual(visible(events), [("text", {"content": "main"})])

    def test_tool_start_complete_keep_id_name_output_and_failure(self):
        events = self.send("tool.execution_start", {"toolCallId": "t1", "toolName": "bash", "arguments": {"command": "false"}})
        events += self.send("tool.execution_complete", {"toolCallId": "t1", "success": False, "error": {"message": "exit 1"}}, "e2")
        self.assertEqual([e.type for e in events], ["tool_use", "tool_input", "tool_result"])
        self.assertTrue(all(e.data["tool_id"] == "t1" for e in events))
        self.assertEqual(events[-1].data, {"name": "bash", "tool_id": "t1", "is_error": True, "result_content": "exit 1"})

    def test_out_of_order_tool_completion_waits_for_start(self):
        completion = {"toolCallId": "t1", "success": True, "result": {"content": "ok"}}
        self.assertEqual(self.send("tool.execution_complete", completion), [])
        events = self.send("tool.execution_start", {"toolCallId": "t1", "toolName": "read_file"}, "e2")
        self.assertEqual([e.type for e in events], ["tool_use", "tool_input", "tool_result"])
        self.assertEqual(events[-1].data["result_content"], "ok")
        self.assertEqual(self.send("tool.execution_complete", completion, "e3"), [])
        self.assertEqual(self.send("tool.execution_start", {"toolCallId": "t1", "toolName": "read_file"}, "e4"), [])

    def test_concurrent_same_name_tools_preserve_result_identity(self):
        for index in range(2):
            self.send("tool.execution_start", {"toolCallId": f"t{index}", "toolName": "bash"}, f"start-{index}")
        for index in [1, 0]:
            result = self.send("tool.execution_complete", {"toolCallId": f"t{index}", "success": True}, f"end-{index}")
            self.assertEqual(result[0].data["tool_id"], f"t{index}")

    def test_idle_requires_explicit_current_background_reconciliation(self):
        events = self.send("session.idle")
        self.assertEqual(events[0].type, "system")
        self.assertEqual(self.translator.settle_idle("event-1", background_settled=False), [])
        self.assertEqual(self.translator.settle_idle("wrong", background_settled=True), [])
        self.assertEqual([e.type for e in self.translator.settle_idle("event-1", background_settled=True)], ["done"])
        self.assertEqual(self.translator.settle_idle("event-1", background_settled=True), [])
        self.send("session.idle", event_id="another-idle")
        self.assertEqual(self.translator.settle_idle("another-idle", background_settled=True), [])

    def test_intervening_activity_invalidates_idle(self):
        for kind, data in [
            ("assistant.message_delta", {"messageId": "m", "deltaContent": "next"}),
            ("assistant.turn_start", {"turnId": "turn"}),
            ("session.background_tasks_changed", {}),
            ("permission.requested", {}),
        ]:
            with self.subTest(kind=kind):
                translator = CopilotEventTranslator()
                translator.translate(frame("session.idle"))
                translator.translate(frame(kind, data, "e2"))
                self.assertEqual(translator.settle_idle("event-1", background_settled=True), [])

    def test_filtered_subagent_activity_still_invalidates_idle_probe(self):
        self.send("session.idle")
        self.assertEqual(self.send("assistant.message_delta", {
            "messageId": "child-message", "deltaContent": "working",
        }, "child-event", agentId="child"), [])
        self.assertEqual(self.translator.settle_idle("event-1", background_settled=True), [])

    def test_later_turn_can_settle_after_previous_done(self):
        self.send("session.idle")
        self.translator.settle_idle("event-1", background_settled=True)
        self.send("assistant.turn_start", {"turnId": "next-turn"}, "e2")
        self.send("session.idle", event_id="e3")
        self.assertEqual([e.type for e in self.translator.settle_idle("e3", background_settled=True)], ["done"])

    def test_post_completion_diagnostics_do_not_manufacture_another_done(self):
        self.send("assistant.turn_start", {"turnId": "turn"})
        self.send("session.idle", event_id="idle")
        self.assertEqual(len(self.translator.settle_idle("idle", background_settled=True)), 1)
        for index, kind in enumerate(["session.usage_info", "session.background_tasks_changed"]):
            self.send(kind, event_id=f"diagnostic-{index}")
            self.send("session.idle", event_id=f"idle-{index}")
            self.assertEqual(self.translator.settle_idle(f"idle-{index}", background_settled=True), [])

    def test_late_end_of_settled_turn_does_not_manufacture_done(self):
        self.send("assistant.turn_start", {"turnId": "turn"})
        self.send("session.idle", event_id="idle")
        self.translator.settle_idle("idle", background_settled=True)
        self.send("assistant.turn_end", {"turnId": "turn"}, "late-end")
        self.send("session.idle", event_id="later-idle")
        self.assertEqual(self.translator.settle_idle("later-idle", background_settled=True), [])

    def test_semantic_replays_preserve_pending_idle_reconciliation(self):
        self.send("assistant.turn_start", {"turnId": "turn"})
        self.send("assistant.message", {"messageId": "m", "content": "done"}, "text")
        self.send("tool.execution_start", {"toolCallId": "t", "toolName": "read"}, "tool")
        self.send("tool.execution_complete", {"toolCallId": "t", "success": True}, "result")
        self.send("session.idle", event_id="idle")
        for kind, data in [
            ("assistant.turn_start", {"turnId": "turn"}),
            ("assistant.message", {"messageId": "m", "content": "done"}),
            ("tool.execution_start", {"toolCallId": "t", "toolName": "read"}),
            ("tool.execution_complete", {"toolCallId": "t", "success": True}),
        ]:
            self.assertEqual(self.send(kind, data, f"replay-{kind}"), [])
        self.assertEqual([e.type for e in self.translator.settle_idle("idle", background_settled=True)], ["done"])

    def test_orphan_completion_or_active_tool_prevents_settlement(self):
        for kind, data in [
            ("tool.execution_complete", {"toolCallId": "t1", "success": True}),
            ("tool.execution_start", {"toolCallId": "t1", "toolName": "bash"}),
        ]:
            with self.subTest(kind=kind):
                translator = CopilotEventTranslator()
                translator.translate(frame(kind, data))
                translator.translate(frame("session.idle", event_id="idle"))
                self.assertEqual(translator.settle_idle("idle", background_settled=True), [])

    def test_turn_end_and_error_do_not_emit_done(self):
        self.assertEqual([e.type for e in self.send("assistant.turn_end", {"turnId": "t"})], ["system"])
        events = self.send("session.error", {"message": "failed", "stack": "private stack"}, "e2")
        self.assertEqual(visible(events), [("error", {"message": "failed"})])

    def test_unknown_event_reports_type_without_raw_payload(self):
        events = self.send("vendor.new_event", {"secret": "do-not-forward"})
        self.assertEqual(visible(events), [("system", {"subtype": "copilot_unmapped_event", "event_type": "vendor.new_event"})])

    def test_repeated_unmapped_type_emits_only_one_diagnostic(self):
        events = []
        for index in range(1000):
            events += self.send("model.telemetry", {"private": index}, f"e{index}")
        self.assertEqual(visible(events), [("system", {
            "subtype": "copilot_unmapped_event", "event_type": "model.telemetry",
        })])
        self.assertEqual(len(self.send("model.other_telemetry", event_id="other")), 1)

    def test_suppressed_repeated_diagnostic_still_invalidates_idle(self):
        self.send("session.background_tasks_changed")
        self.send("session.idle", event_id="idle")
        self.assertEqual(self.send("session.background_tasks_changed", event_id="new-activity"), [])
        self.assertEqual(self.translator.settle_idle("idle", background_settled=True), [])

    def test_redundant_stream_progress_is_quiet_but_invalidates_idle(self):
        for kind, data in [
            ("assistant.streaming_delta", {"totalResponseSizeBytes": 123}),
            ("assistant.tool_call_delta", {"toolCallId": "t1", "inputDelta": "partial"}),
        ]:
            with self.subTest(kind=kind):
                translator = CopilotEventTranslator()
                translator.translate(frame("session.idle"))
                for index in range(100):
                    self.assertEqual(translator.translate(frame(kind, data, f"progress-{index}")), [])
                self.assertEqual(translator.settle_idle("event-1", background_settled=True), [])

    def test_malformed_supported_event_does_not_consume_id(self):
        with self.assertRaises(ValueError):
            self.send("assistant.message_delta", {"messageId": "m1"})
        events = self.send("assistant.message_delta", {"messageId": "m1", "deltaContent": "valid"})
        self.assertEqual(len(events), 1)

    def test_missing_tool_success_and_chunked_finals_fail_explicitly(self):
        with self.assertRaises(ValueError):
            self.send("tool.execution_complete", {"toolCallId": "t1"})
        with self.assertRaises(ValueError):
            self.send("assistant.message", {"messageId": "m1", "content": "one chunk", "chunkCount": 2})

    def test_interrupt_boundary_requires_work_and_preserves_generation(self):
        self.assertIsNone(self.translator.capture_interrupt_boundary())
        self.send("assistant.turn_start", {"turnId": "one"})
        boundary = self.translator.capture_interrupt_boundary()
        self.send("assistant.message_delta", {"messageId": "m", "deltaContent": "new"}, "e2")
        self.assertEqual(self.translator.reconcile_interrupted(boundary), [])
        current = self.translator.capture_interrupt_boundary()
        self.assertEqual([e.type for e in self.translator.reconcile_interrupted(current)], ["done"])
        self.assertEqual(self.translator.reconcile_interrupted(current), [])
        self.assertIsNone(self.translator.capture_interrupt_boundary())

    def test_submission_scopes_reused_native_turn_ids_without_resetting_event_replay(self):
        old = frame("assistant.turn_start", {"turnId": "reused"}, "old-start")
        self.translator.translate(old)
        self.send("session.idle", event_id="old-idle")
        self.assertEqual(len(self.translator.settle_idle("old-idle", background_settled=True)), 1)
        self.translator.begin_submission()
        self.assertEqual(self.translator.translate(old), [])
        self.assertIsNone(self.translator.capture_interrupt_boundary())
        self.assertEqual(len(self.send("assistant.turn_start", {"turnId": "reused"}, "new-start")), 1)
        self.assertEqual(self.send("assistant.turn_start", {"turnId": "reused"}, "duplicate-new-start"), [])
        self.send("session.idle", event_id="new-idle")
        self.assertEqual(len(self.translator.settle_idle("new-idle", background_settled=True)), 1)

    def test_submission_invalidates_old_idle_and_interruption_without_creating_work(self):
        self.send("assistant.turn_start", {"turnId": "one"})
        self.send("session.idle", event_id="idle")
        boundary = self.translator.capture_interrupt_boundary()
        self.translator.begin_submission()
        self.assertIsNone(self.translator.pending_idle_id)
        self.assertEqual(self.translator.settle_idle("idle", background_settled=True), [])
        self.assertEqual(self.translator.reconcile_interrupted(boundary), [])

    def test_interrupt_recapture_invalidates_old_fence(self):
        self.send("assistant.turn_start", {"turnId": "one"})
        old = self.translator.capture_interrupt_boundary()
        current = self.translator.capture_interrupt_boundary()
        self.assertEqual(self.translator.reconcile_interrupted(old), [])
        self.assertEqual(len(self.translator.reconcile_interrupted(current)), 1)

    def test_interrupt_and_native_idle_cannot_duplicate_completion(self):
        self.send("assistant.turn_start", {"turnId": "one"})
        boundary = self.translator.capture_interrupt_boundary()
        self.send("session.idle", event_id="idle")
        self.assertEqual(len(self.translator.settle_idle("idle", background_settled=True)), 1)
        self.assertEqual(self.translator.reconcile_interrupted(boundary), [])

    def test_interrupt_cancellation_is_atomic_when_other_tool_still_open(self):
        for index in range(2):
            self.send("tool.execution_start", {"toolCallId": f"t{index}", "toolName": "hold"}, f"start-{index}")
        boundary = self.translator.capture_interrupt_boundary()
        self.assertEqual(self.translator.reconcile_interrupted(boundary, cancelled_tool_ids=frozenset({"t0"})), [])
        events = self.translator.reconcile_interrupted(boundary, cancelled_tool_ids=frozenset({"t0", "t1"}))
        self.assertEqual([e.type for e in events], ["tool_result", "tool_result", "done"])
        self.assertTrue(all(e.data["is_error"] for e in events[:-1]))

    def test_interrupt_orphan_result_cannot_be_swept_as_cancellation(self):
        self.send("tool.execution_complete", {"toolCallId": "unknown", "success": False})
        self.send("assistant.turn_start", {"turnId": "one"}, "start")
        boundary = self.translator.capture_interrupt_boundary()
        self.assertEqual(self.translator.reconcile_interrupted(boundary), [])
        with self.assertRaises(ValueError):
            self.translator.reconcile_interrupted(boundary, cancelled_tool_ids=frozenset({"unknown"}))


if __name__ == "__main__":
    unittest.main()
