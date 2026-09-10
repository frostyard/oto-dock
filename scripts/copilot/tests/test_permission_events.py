"""Adversarial native request/completion correlation without SDK dependencies."""

from copy import deepcopy
from pathlib import Path
import sys
import traceback

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.permission_events import CopilotPermissionEvents


def requested(request_id="r1", tool_id="t1"):
    return {"requestId": request_id, "permissionRequest": {"kind": "custom-tool", "toolCallId": tool_id,
                                                          "args": {"secret": "ghu_private_fixture"}}}


def completed(request_id="r1", tool_id="t1", outcome="cancelled"):
    return {"requestId": request_id, "toolCallId": tool_id, "result": {"kind": outcome}}


def cancel(tracker, request_id="r1", tool_id="t1"):
    tracker.observe("permission.requested", requested(request_id, tool_id))
    tracker.observe("permission.completed", completed(request_id, tool_id))


def test_exact_native_cancelled_after_request_is_a_candidate_and_duplicates_are_idempotent():
    tracker = CopilotPermissionEvents()
    tracker.observe("tool.execution_start", {"toolCallId": "t1"})
    cancel(tracker)
    cancel(tracker)
    assert tracker.cancelled_tool_ids == frozenset({"t1"})


@pytest.mark.parametrize("outcome", ["approved", "approve-once", "reject", "denied-interactively-by-user",
                                    "denied-by-permission-request-hook", "resolved-by-hook", "unknown", None])
def test_only_native_cancelled_can_retire_an_open_tool(outcome):
    tracker = CopilotPermissionEvents()
    tracker.observe("permission.requested", requested())
    tracker.observe("permission.completed", completed(outcome=outcome))
    assert not tracker.cancelled_tool_ids


@pytest.mark.parametrize("missing", ["request", "request-id", "request-tool", "completion-tool", "result"])
def test_missing_correlation_never_becomes_proof(missing):
    tracker = CopilotPermissionEvents()
    request = requested()
    completion = completed()
    if missing == "request-id":
        request.pop("requestId")
    elif missing == "request-tool":
        request["permissionRequest"].pop("toolCallId")
    elif missing == "completion-tool":
        completion.pop("toolCallId")
    elif missing == "result":
        completion.pop("result")
    if missing != "request":
        tracker.observe("permission.requested", request)
    tracker.observe("permission.completed", completion)
    assert not tracker.cancelled_tool_ids


@pytest.mark.parametrize("invalid", [None, "", " ", 0, True, [], "x" * 1025])
def test_invalid_tool_ids_never_become_candidates(invalid):
    tracker = CopilotPermissionEvents()
    tracker.observe("permission.requested", requested(tool_id=invalid))
    tracker.observe("permission.completed", completed(tool_id=invalid))
    assert not tracker.cancelled_tool_ids


def test_unknown_completion_cannot_be_reassociated_when_request_arrives_later():
    tracker = CopilotPermissionEvents()
    tracker.observe("permission.completed", completed())
    tracker.observe("permission.requested", requested())
    tracker.observe("permission.completed", completed())
    assert not tracker.cancelled_tool_ids


def test_new_permission_for_same_tool_invalidates_prior_cancellation_and_old_replay_cannot_restore_it():
    tracker = CopilotPermissionEvents()
    cancel(tracker)
    tracker.observe("permission.requested", requested("r2"))
    assert not tracker.cancelled_tool_ids
    cancel(tracker)
    assert not tracker.cancelled_tool_ids
    tracker.observe("permission.completed", completed("r2"))
    assert tracker.cancelled_tool_ids == {"t1"}


def test_late_old_completion_cannot_cancel_the_newer_pending_permission():
    tracker = CopilotPermissionEvents()
    tracker.observe("permission.requested", requested("old"))
    tracker.observe("permission.requested", requested("new"))
    tracker.observe("permission.completed", completed("old"))
    assert not tracker.cancelled_tool_ids


@pytest.mark.parametrize("outcome", ["approved", "unknown", None])
def test_unrecognized_completion_for_same_tool_conservatively_invalidates_candidate(outcome):
    tracker = CopilotPermissionEvents()
    cancel(tracker)
    tracker.observe("permission.completed", completed("unknown", outcome=outcome))
    assert not tracker.cancelled_tool_ids


def test_late_tool_start_invalidates_candidate_and_duplicate_cancel_cannot_restore_it():
    tracker = CopilotPermissionEvents()
    cancel(tracker)
    tracker.observe("tool.execution_start", {"toolCallId": "t1"})
    assert not tracker.cancelled_tool_ids
    cancel(tracker)
    assert not tracker.cancelled_tool_ids


def test_start_after_request_but_before_completion_also_blocks_old_cancellation():
    tracker = CopilotPermissionEvents()
    tracker.observe("permission.requested", requested())
    tracker.observe("tool.execution_start", {"toolCallId": "t1"})
    tracker.observe("permission.completed", completed())
    assert not tracker.cancelled_tool_ids


def test_new_request_after_execution_start_can_produce_its_own_correlated_candidate():
    tracker = CopilotPermissionEvents()
    cancel(tracker)
    tracker.observe("tool.execution_start", {"toolCallId": "t1"})
    cancel(tracker, "r2")
    assert tracker.cancelled_tool_ids == {"t1"}


@pytest.mark.parametrize("changed", ["request-tool", "request-args", "completion-outcome", "completion-tool", "cross-envelope"])
def test_conflicting_records_fail_closed_without_exporting_payload(changed):
    tracker = CopilotPermissionEvents()
    cancel(tracker)
    if changed.startswith("request"):
        data = requested()
        data["permissionRequest"]["toolCallId" if changed == "request-tool" else "args"] = "ghu_private_other"
        kind = "permission.requested"
    else:
        data = completed()
        kind = "permission.completed"
        if changed == "completion-outcome":
            data["result"] = {"kind": "approved", "secret": "ghu_private_other"}
        else:
            data["toolCallId"] = "ghu_private_other"
        if changed == "cross-envelope":
            tracker.observe("permission.requested", requested("r2"))
            data["requestId"] = "r2"
    with pytest.raises(ValueError) as exc:
        tracker.observe(kind, data)
    assert "ghu_private" not in "".join(traceback.format_exception(exc.value))
    assert exc.value.__context__ is None and not tracker.cancelled_tool_ids
    with pytest.raises(ValueError):
        cancel(tracker, "new")


def test_raw_request_arguments_are_hashed_not_retained_or_mutation_sensitive():
    tracker = CopilotPermissionEvents()
    data = requested()
    tracker.observe("permission.requested", data)
    data["permissionRequest"]["args"]["secret"] = "changed"
    assert "ghu_private_fixture" not in repr(vars(tracker))
    assert "changed" not in repr(vars(tracker))
    tracker.observe("permission.completed", completed())
    snapshot = tracker.cancelled_tool_ids
    assert snapshot == {"t1"}
    tracker.observe("tool.execution_start", {"toolCallId": "t1"})
    assert snapshot == {"t1"} and not tracker.cancelled_tool_ids


def test_independent_tool_candidates_do_not_cross_correlate():
    tracker = CopilotPermissionEvents()
    cancel(tracker, "r1", "t1")
    cancel(tracker, "r2", "t2")
    tracker.observe("tool.execution_start", {"toolCallId": "t2"})
    assert tracker.cancelled_tool_ids == {"t1"}


def test_duplicate_reordered_keys_hash_identically():
    tracker = CopilotPermissionEvents()
    data = requested()
    tracker.observe("permission.requested", data)
    reordered = {key: deepcopy(data[key]) for key in reversed(data)}
    tracker.observe("permission.requested", reordered)
    tracker.observe("permission.completed", completed())
    assert tracker.cancelled_tool_ids == {"t1"}


@pytest.mark.parametrize("kind,data", [
    ("permission.requested", {"permissionRequest": {"toolCallId": "t1"}}),
    ("permission.completed", {"toolCallId": "t1", "result": {"kind": "approved"}}),
])
def test_missing_request_id_cannot_preserve_old_candidate_for_named_tool(kind, data):
    tracker = CopilotPermissionEvents()
    cancel(tracker)
    tracker.observe(kind, data)
    assert not tracker.cancelled_tool_ids


def test_unhashable_payload_fails_closed_without_retaining_exception_context():
    tracker = CopilotPermissionEvents()
    cancel(tracker)
    data = requested("r2")
    data["permissionRequest"]["args"] = object()
    with pytest.raises(ValueError) as exc:
        tracker.observe("permission.requested", data)
    assert exc.value.__context__ is None and not tracker.cancelled_tool_ids
