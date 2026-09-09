#!/usr/bin/env python3
"""Opt-in bounded lifecycle tests against the pinned SDK/runtime, outside the sandbox."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import contextlib
import importlib.metadata
import json
import logging
from pathlib import Path
import sys
import tempfile

import psutil

from probe import SDK_VERSION, RUNTIME_VERSION, child_environment, selected_token, stop_descendants

MARKER = "OTO_LIFECYCLE_READY"
FOLLOWUP = f"Reply exactly {MARKER}. Do not call any tools."
HOLD_PROMPT = "Call lifecycle_hold exactly once with no arguments. After it returns, say READY. Do not retry."


class Observations:
    """Retain event kinds, known marker matches, and in-memory message IDs only."""

    def __init__(self):
        self.counts = Counter()
        self.timeline = []
        self.delivered_ids = set()
        self.idle = asyncio.Event()
        self.marker = asyncio.Event()
        self.idle_aborted_flags = []

    def note(self, kind):
        if len(self.timeline) < 150:
            self.timeline.append(kind)

    def on_event(self, event):
        kind = event.raw_type or event.type.value
        self.counts[kind] += 1
        if kind in {"session.idle", "assistant.turn_start", "assistant.turn_end",
                    "tool.execution_start", "tool.execution_complete", "permission.requested",
                    "permission.completed", "session.compaction_start", "session.compaction_complete",
                    "abort", "agent.interrupted"}:
            self.note(kind)
        if kind == "session.idle":
            self.idle_aborted_flags.append(getattr(event.data, "aborted", None))
            self.idle.set()
        if kind == "user.message":
            self.delivered_ids.add(event.data.message_id)
            self.note("followup.delivered" if event.data.content == FOLLOWUP else "user.delivered")
        if kind == "assistant.message" and event.data.content.strip() == MARKER:
            self.marker.set()
            self.note("marker.received")


class HeldTool:
    """No host operations: pause until released, while observing SDK cancellation."""

    def __init__(self, observations):
        self.observations = observations
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def handle(self, invocation):
        assert invocation.arguments in (None, {}), "hold tool accepts no arguments"
        self.calls += 1
        self.observations.note("callback.started")
        self.started.set()
        try:
            await asyncio.wait_for(self.release.wait(), timeout=45)
            self.observations.note("callback.returned")
            from copilot.tools import ToolResult
            return ToolResult(text_result_for_llm="HOLD_RELEASED")
        except asyncio.CancelledError:
            self.cancelled = True
            self.observations.note("callback.cancelled")
            raise
        finally:
            self.finished.set()


def task_snapshot(tasks):
    """No task descriptions, results, paths, or identifiers in public evidence."""
    return [{"type": task.type.value if hasattr(task.type, "value") else task.type,
             "status": task.status.value, "sequence": getattr(task, "sequence", None)}
            for task in tasks.tasks]


async def observed_within(event, seconds):
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return False


async def exercise_control(client, scenario, result):
    from copilot import ToolSet
    from copilot.rpc import InterruptMainTurnRequest, PermissionDecisionApproveOnce, PermissionDecisionReject
    from copilot.tools import Tool

    observations = Observations()
    held = HeldTool(observations)
    permissions = Counter()

    def decide(request, _invocation):
        allowed = (request.kind == "custom-tool" and request.tool_name == "lifecycle_hold"
                   and request.args in (None, {}))
        permissions["approved" if allowed else "denied"] += 1
        return PermissionDecisionApproveOnce() if allowed else PermissionDecisionReject()

    session = await client.create_session(
        model="gpt-5-mini", tools=[Tool(
            name="lifecycle_hold", description="Wait for the compatibility test controller.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=held.handle,
        )], available_tools=ToolSet().add_custom("lifecycle_hold"),
        on_permission_request=decide, on_event=observations.on_event,
        enable_config_discovery=False, enable_file_hooks=False,
        enable_host_git_operations=False, enable_session_store=True,
        session_limits={"max_ai_credits": 30.0},
    )
    try:
        background = None
        if scenario == "interrupt":
            from copilot.rpc import TasksRegisterRequest, TaskClientType
            background = await session.rpc.tasks.register(TasksRegisterRequest(
                cancellable=False, client_task_id="oto-interrupt-background",
                description="Controlled background task metadata", type=TaskClientType.CLIENT,
            ), timeout=5)
        message_id = await session.send(HOLD_PROMPT)
        observations.note("initial.accepted")
        assert await observed_within(held.started, 45), "controlled tool never started"
        assert message_id in observations.delivered_ids
        result["tasks_while_callback_held"] = task_snapshot(await session.rpc.tasks.list(timeout=5))
        observations.idle.clear()
        if scenario == "immediate":
            followup_id = await session.send(FOLLOWUP, mode="immediate")
            result["immediate_message_accepted"] = bool(followup_id)
        elif scenario == "abort":
            await asyncio.wait_for(session.abort(), timeout=10)
        else:
            interrupted = await session.rpc.interrupt_main_turn(InterruptMainTurnRequest(), timeout=10)
            result["interrupted"] = interrupted.interrupted
        observations.note("control.acknowledged")
        result["idle_before_release"] = await observed_within(observations.idle, 2)
        result["callback_finished_before_release"] = held.finished.is_set()
        result["callback_cancelled_before_release"] = held.cancelled
        result["tasks_after_control_ack"] = task_snapshot(await session.rpc.tasks.list(timeout=5))
        if scenario == "immediate":
            result["immediate_delivered_before_release"] = followup_id in observations.delivered_ids
        held.release.set()
        observations.note("controller.released")
        assert await observed_within(held.finished, 5), "callback did not settle after release"
        if background:
            from copilot.rpc import TasksUpdateRequest, TaskClientUpdate, TaskClientUpdateKind, TasksRemoveRequest
            result["background_survived_interrupt"] = any(
                task["status"] == "running" for task in result["tasks_after_control_ack"]
            )
            assert result["background_survived_interrupt"]
            await session.rpc.tasks.update(TasksUpdateRequest(
                id=background.task.id, sequence=background.task.sequence + 1,
                update=TaskClientUpdate(kind=TaskClientUpdateKind.CANCELLED),
            ), timeout=5)
            await session.rpc.tasks.remove(TasksRemoveRequest(id=background.task.id), timeout=5)
            observations.note("background.retired")
        result["session_idle_after_release"] = await observed_within(observations.idle, 3 if background else 30)
        if background:
            processing = await session.rpc.metadata.is_processing(timeout=5)
            result["processing_after_background_retired"] = processing.processing
            result["tasks_after_background_retired"] = task_snapshot(await session.rpc.tasks.list(timeout=5))
            assert not processing.processing and not result["tasks_after_background_retired"]
        else:
            assert result["session_idle_after_release"], "session did not become idle"
        if scenario != "immediate":
            observations.idle.clear()
            followup_id = await session.send(FOLLOWUP)
            observations.note("followup.accepted")
        assert await observed_within(observations.marker, 45), "follow-up marker not observed"
        assert await observed_within(observations.idle, 20), "follow-up did not settle"
        result["followup_delivered"] = followup_id in observations.delivered_ids
        result["followup_marker_received"] = observations.marker.is_set()
        result["tasks_after_idle"] = task_snapshot(await session.rpc.tasks.list(timeout=5))
        assert held.calls == 1 and permissions["approved"] == 1 and not permissions["denied"]
        assert result["followup_delivered"]
    finally:
        held.release.set()
        result["callback_calls"] = held.calls
        result["callback_cancelled"] = held.cancelled
        result["permissions"] = dict(permissions)
        result["timeline"] = observations.timeline
        result["event_counts"] = dict(observations.counts)
        result["idle_aborted_flags"] = observations.idle_aborted_flags
        await asyncio.wait_for(session.disconnect(), timeout=10)


async def exercise_tasks(client, result):
    from copilot.rpc import (
        TaskClientType, TaskClientUpdate, TaskClientUpdateKind, TasksRegisterRequest,
        TasksUpdateRequest, TasksRemoveRequest, PermissionDecisionReject,
    )

    session = await client.create_session(
        model="gpt-5-mini", available_tools=[],
        on_permission_request=lambda _request, _invocation: PermissionDecisionReject(),
        enable_config_discovery=False, enable_file_hooks=False, enable_host_git_operations=False,
        session_limits={"max_ai_credits": 30.0},
    )
    try:
        request = TasksRegisterRequest(cancellable=False, client_task_id="oto-lifecycle-task",
                                      description="Controlled client-owned test task", type=TaskClientType.CLIENT)
        registered = await session.rpc.tasks.register(request, timeout=5)
        result["registered"] = registered.created
        result["running_snapshot"] = task_snapshot(await session.rpc.tasks.list(timeout=5))
        repeated = await session.rpc.tasks.register(request, timeout=5)
        result["duplicate_registration_same_task"] = repeated.task.id == registered.task.id and not repeated.created
        update = TasksUpdateRequest(id=registered.task.id, sequence=registered.task.sequence + 1,
                                    update=TaskClientUpdate(kind=TaskClientUpdateKind.COMPLETED))
        completed = await session.rpc.tasks.update(update, timeout=5)
        result["completion_applied"] = completed.applied
        result["completed_snapshot"] = task_snapshot(await session.rpc.tasks.list(timeout=5))
        duplicate = await session.rpc.tasks.update(update, timeout=5)
        result["duplicate_update_recognized"] = duplicate.duplicate and not duplicate.applied
        removed = await session.rpc.tasks.remove(TasksRemoveRequest(id=registered.task.id), timeout=5)
        result["removed"] = removed.removed
        result["final_snapshot"] = task_snapshot(await session.rpc.tasks.list(timeout=5))
        assert result["registered"] and result["duplicate_registration_same_task"]
        assert result["completion_applied"] and result["duplicate_update_recognized"]
        assert result["removed"] and not result["final_snapshot"]
    finally:
        await asyncio.wait_for(session.disconnect(), timeout=10)


async def exercise_compact(client, result):
    from copilot.rpc import PermissionDecisionReject, SessionHistoryCompactRequest

    observations = Observations()
    session = await client.create_session(
        model="gpt-5-mini", available_tools=[], on_event=observations.on_event,
        on_permission_request=lambda _request, _invocation: PermissionDecisionReject(),
        enable_config_discovery=False, enable_file_hooks=False, enable_host_git_operations=False,
        enable_session_store=True, session_limits={"max_ai_credits": 30.0},
    )
    try:
        reply = await session.send_and_wait(FOLLOWUP, timeout=45)
        assert reply and reply.data.content.strip() == MARKER
        compacted = await session.rpc.history.compact(
            SessionHistoryCompactRequest(custom_instructions="Preserve the exact marker from the prior reply."),
            timeout=60,
        )
        result["compact_success"] = compacted.success
        result["messages_removed"] = compacted.messages_removed
        result["tokens_removed"] = compacted.tokens_removed
        result["summary_present"] = bool(compacted.summary_content)
        reply = await session.send_and_wait(
            "Repeat the exact marker from the prior assistant reply. Do not call tools.", timeout=45,
        )
        result["post_compact_marker_received"] = bool(reply and reply.data.content.strip() == MARKER)
        assert compacted.success and result["post_compact_marker_received"]
    finally:
        result["timeline"] = observations.timeline
        result["event_counts"] = dict(observations.counts)
        await asyncio.wait_for(session.disconnect(), timeout=10)


async def run(args, report):
    from copilot import CopilotClient, RuntimeConnection

    assert importlib.metadata.version("github-copilot-sdk") == SDK_VERSION
    tracked = {}

    def observe():
        for process in psutil.Process().children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                tracked[(process.pid, process.create_time())] = process

    async def track():
        while True:
            observe()
            await asyncio.sleep(0.05)

    with tempfile.TemporaryDirectory(prefix="otodock-lifecycle-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        state = root / "state"
        state.mkdir()
        client = CopilotClient(
            connection=RuntimeConnection.for_stdio(path=str(args.runtime)),
            env=child_environment(root), github_token=selected_token(), use_logged_in_user=False,
            working_directory=str(workspace), base_directory=str(state), mode="empty", log_level="error",
        )
        tracker = asyncio.create_task(track())
        try:
            async with asyncio.timeout(args.timeout):
                await client.start()
                status = await client.get_status()
                assert status.version == RUNTIME_VERSION
                report.update(sdk_version=SDK_VERSION, runtime_version=status.version)
                operation = (exercise_tasks if args.scenario == "tasks" else
                             exercise_compact if args.scenario == "compact" else None)
                if operation:
                    await operation(client, report)
                else:
                    await exercise_control(client, args.scenario, report)
        finally:
            try:
                await asyncio.wait_for(client.stop(), timeout=10)
            finally:
                observe()
                tracker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tracker
                _, remaining = psutil.wait_procs(list(tracked.values()), timeout=2)
                report["sdk_cleanup"] = "passed" if not remaining else "required_force_cleanup"
                report["tracked_descendants_reaped"] = stop_descendants(remaining)
                assert report["sdk_cleanup"] == "passed" and report["tracked_descendants_reaped"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--scenario", choices=["abort", "interrupt", "immediate", "compact", "tasks"], required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    args.runtime = args.runtime.resolve()
    if not args.runtime.is_file() or not 1 <= args.timeout <= 240:
        parser.error("existing runtime and timeout from 1 to 240 seconds required")
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "scenario": args.scenario, "sandboxed": False}
    try:
        asyncio.run(run(args, report))
        report["result"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
