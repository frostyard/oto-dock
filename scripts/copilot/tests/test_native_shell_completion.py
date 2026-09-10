"""Native Bash display settlement requires separate controlled process-exit proof."""

from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.coordinator import (  # noqa: E402
    AbortState, CopilotTurnCoordinator, InterruptState, SettlementObservation, TaskObservation, TaskState,
)

EMPTY = SettlementObservation(False, (), frozenset(), frozenset(), pending_messages=frozenset())
SHELL = replace(EMPTY, cancelled_native_shell_tool_ids=frozenset({"bash-one"}))


def event(coordinator, kind, data=None, **metadata):
    sequence = coordinator.last_sequence + 1
    return coordinator.receive_event(sequence, {
        "id": f"event-{sequence}", "type": kind, "data": data or {}, **metadata,
    })


def start(*extra_tools):
    coordinator = CopilotTurnCoordinator()
    event(coordinator, "assistant.turn_start", {"turnId": "turn-one"})
    for identity, name in (("bash-one", "bash"), *extra_tools):
        event(coordinator, "tool.execution_start", {"toolCallId": identity, "toolName": name})
    return coordinator


def abort(coordinator, *, accepted=True, aborted=True):
    ticket = coordinator.request_abort()
    if accepted is not None:
        coordinator.acknowledge_abort(ticket, accepted=accepted)
    event(coordinator, "session.idle", {"aborted": aborted})
    return coordinator.begin_reconciliation()


def interrupted(coordinator):
    ticket = coordinator.request_interrupt()
    coordinator.acknowledge_interrupt(ticket, accepted=True)
    return coordinator.begin_interrupt_reconciliation(ticket)


def finish_interrupt(coordinator, checkpoint, first=SHELL, second=SHELL, *, barrier=False):
    return coordinator.finish_interrupt_reconciliation(
        checkpoint, first, second, processing_barrier=barrier,
    )


def assert_stopped(events):
    assert [item.type for item in events] == ["tool_result", "done"]
    assert events[0].data["tool_id"] == "bash-one"
    assert events[0].data["is_error"] is True
    assert "Native shell stopped after control and owned-process settlement." in str(events[0].data)
    assert "success" not in str(events[0].data).lower()


def test_accepted_abort_requires_all_proofs_then_emits_one_error_and_done():
    coordinator = start()
    checkpoint = abort(coordinator)
    assert coordinator.finish_reconciliation(checkpoint, EMPTY) == []
    assert_stopped(coordinator.finish_reconciliation(checkpoint, SHELL))
    assert coordinator.abort_state is AbortState.SETTLED_UNVERIFIED
    assert coordinator.finish_reconciliation(checkpoint, SHELL) == []
    assert event(coordinator, "tool.execution_complete", {
        "toolCallId": "bash-one", "success": True, "result": {"content": "late success"},
    }) == []


@pytest.mark.parametrize("accepted,aborted", [(None, True), (False, True), (True, False)])
def test_acknowledgement_and_matching_aborted_idle_are_both_required(accepted, aborted):
    coordinator = start()
    checkpoint = abort(coordinator, accepted=accepted, aborted=aborted)
    assert coordinator.finish_reconciliation(checkpoint, SHELL) == []


def test_ordinary_idle_without_control_cannot_use_native_shell_proof():
    coordinator = start()
    event(coordinator, "session.idle", {})
    assert coordinator.finish_reconciliation(coordinator.begin_reconciliation(), SHELL) == []


def test_accepted_abort_without_idle_cannot_manufacture_idle_from_process_exit():
    coordinator = start()
    ticket = coordinator.request_abort()
    coordinator.acknowledge_abort(ticket, accepted=True)
    assert coordinator.begin_reconciliation() is None
    assert coordinator.finish_reconciliation(None, SHELL) == []


@pytest.mark.parametrize("changes", [
    {"processing": True}, {"processing": None}, {"tasks": None},
    {"tasks": (TaskObservation("shell", TaskState.UNKNOWN),)},
    {"tasks": (TaskObservation("shell", TaskState.RUNNING),)},
    {"pending_tools": frozenset({"host-callback"})}, {"pending_tools": None},
    {"pending_permissions": frozenset({"permission"})}, {"pending_permissions": None},
    {"pending_messages": frozenset({"queued-input"})}, {"pending_messages": None},
])
def test_native_shell_proof_cannot_bypass_unresolved_inventory(changes):
    coordinator = start()
    checkpoint = abort(coordinator)
    assert coordinator.finish_reconciliation(checkpoint, replace(SHELL, **changes)) == []
    assert_stopped(coordinator.finish_reconciliation(checkpoint, SHELL))


def test_retired_task_is_not_success_and_still_requires_separate_stop_proof():
    coordinator = start()
    checkpoint = abort(coordinator)
    retired = replace(EMPTY, tasks=(TaskObservation("native-shell", TaskState.RETIRED),))
    assert coordinator.finish_reconciliation(checkpoint, retired) == []
    assert_stopped(coordinator.finish_reconciliation(checkpoint, replace(
        retired, cancelled_native_shell_tool_ids=frozenset({"bash-one"}),
    )))


@pytest.mark.parametrize("proof", [None, set(), ["bash-one"], frozenset({0}), frozenset({True}),
    frozenset({None}), frozenset({""}), frozenset({" padded"}), frozenset({"line\nbreak"}),
    frozenset({"bash-one", "unknown"})])
@pytest.mark.parametrize("control", ["abort", "interrupt"])
def test_invalid_native_shell_proof_batch_has_no_partial_effects(proof, control):
    coordinator = start()
    invalid = replace(SHELL, cancelled_native_shell_tool_ids=proof)
    if control == "abort":
        checkpoint = abort(coordinator)
        with pytest.raises(ValueError):
            coordinator.finish_reconciliation(checkpoint, invalid)
        assert_stopped(coordinator.finish_reconciliation(checkpoint, SHELL))
    else:
        checkpoint = interrupted(coordinator)
        with pytest.raises(ValueError):
            finish_interrupt(coordinator, checkpoint, invalid, invalid)
        assert_stopped(finish_interrupt(coordinator, checkpoint))


@pytest.mark.parametrize("pair", ["host-native", "permission-native", "host-permission"])
@pytest.mark.parametrize("control", ["abort", "interrupt"])
def test_all_three_stop_proofs_must_be_pairwise_disjoint(pair, control):
    coordinator = start()
    fields = {
        "cancelled_tool_ids": frozenset({"bash-one"}) if "host" in pair else frozenset(),
        "cancelled_permission_tool_ids": frozenset({"bash-one"}) if "permission" in pair else frozenset(),
        "cancelled_native_shell_tool_ids": frozenset({"bash-one"}) if "native" in pair else frozenset(),
    }
    invalid = replace(EMPTY, **fields)
    checkpoint = abort(coordinator) if control == "abort" else interrupted(coordinator)
    with pytest.raises(ValueError):
        if control == "abort":
            coordinator.finish_reconciliation(checkpoint, invalid)
        else:
            finish_interrupt(coordinator, checkpoint, invalid, invalid)
    if control == "abort":
        assert_stopped(coordinator.finish_reconciliation(checkpoint, SHELL))
    else:
        assert_stopped(finish_interrupt(coordinator, checkpoint))


def test_native_shell_proof_only_applies_to_known_open_bash_not_other_tools_or_children():
    coordinator = start(("read-one", "view"))
    event(coordinator, "tool.execution_start", {"toolCallId": "child-bash", "toolName": "bash"},
          agentId="child")
    checkpoint = abort(coordinator)
    for identity in ("read-one", "child-bash"):
        with pytest.raises(ValueError):
            coordinator.finish_reconciliation(checkpoint, replace(
                SHELL, cancelled_native_shell_tool_ids=frozenset({"bash-one", identity}),
            ))
    # An independently unresolved ordinary tool still blocks DONE.
    events = coordinator.finish_reconciliation(checkpoint, SHELL)
    assert [item.type for item in events] == ["tool_result"]
    assert events[0].data["is_error"] is True


def test_three_distinct_proof_sources_preserve_distinct_error_meanings():
    coordinator = start(("host", "fixture"), ("permission", "view"))
    checkpoint = abort(coordinator)
    proof = replace(SHELL, cancelled_tool_ids=frozenset({"host"}),
                    cancelled_permission_tool_ids=frozenset({"permission"}))
    events = coordinator.finish_reconciliation(checkpoint, proof)
    assert [item.type for item in events] == ["tool_result", "tool_result", "tool_result", "done"]
    by_id = {item.data["tool_id"]: item.data for item in events[:-1]}
    assert all(data["is_error"] is True for data in by_id.values())
    assert "owned-process settlement" in str(by_id["bash-one"])
    assert "Tool execution cancelled." in str(by_id["host"])
    assert "permission request was cancelled" in str(by_id["permission"])


def test_interrupt_requires_identical_native_shell_proof_in_both_snapshots():
    coordinator = start()
    checkpoint = interrupted(coordinator)
    assert finish_interrupt(coordinator, checkpoint, EMPTY, SHELL) == []
    assert finish_interrupt(coordinator, checkpoint, SHELL, EMPTY) == []
    assert finish_interrupt(coordinator, checkpoint, EMPTY, EMPTY) == []
    assert_stopped(finish_interrupt(coordinator, checkpoint))
    assert coordinator.interrupt_state is InterruptState.SETTLED
    assert finish_interrupt(coordinator, checkpoint) == []


@pytest.mark.parametrize("barrier", [True, None, 0])
def test_interrupt_native_shell_proof_cannot_bypass_processing_barrier(barrier):
    coordinator = start()
    checkpoint = interrupted(coordinator)
    assert finish_interrupt(coordinator, checkpoint, barrier=barrier) == []
    assert_stopped(finish_interrupt(coordinator, checkpoint))


def test_interrupt_host_mutation_between_snapshots_invalidates_native_shell_proof():
    coordinator = start()
    checkpoint = interrupted(coordinator)
    coordinator.invalidate_observation()
    assert finish_interrupt(coordinator, checkpoint) == []


def test_interrupt_rejected_or_unacknowledged_cannot_begin_native_shell_reconciliation():
    coordinator = start()
    ticket = coordinator.request_interrupt()
    assert coordinator.begin_interrupt_reconciliation(ticket) is None
    coordinator.acknowledge_interrupt(ticket, accepted=False)
    assert coordinator.begin_interrupt_reconciliation(ticket) is None


@pytest.mark.parametrize("field", ["cancelled_tool_ids", "cancelled_permission_tool_ids"])
def test_interrupt_requires_other_proof_sources_stable_alongside_native_shells(field):
    coordinator = start(("other-tool", "fixture"))
    checkpoint = interrupted(coordinator)
    combined = replace(SHELL, **{field: frozenset({"other-tool"})})
    assert finish_interrupt(coordinator, checkpoint, SHELL, combined) == []
    events = finish_interrupt(coordinator, checkpoint, combined, combined)
    assert [item.type for item in events] == ["tool_result", "tool_result", "done"]
    assert all(item.data["is_error"] is True for item in events[:-1])


def test_aborted_idle_from_before_control_cannot_authorize_native_shell_stop():
    coordinator = start()
    event(coordinator, "session.idle", {"aborted": True})
    ticket = coordinator.request_abort()
    coordinator.acknowledge_abort(ticket, accepted=True)
    assert coordinator.finish_reconciliation(coordinator.begin_reconciliation(), SHELL) == []
    event(coordinator, "session.idle", {"aborted": True})
    assert_stopped(coordinator.finish_reconciliation(coordinator.begin_reconciliation(), SHELL))
