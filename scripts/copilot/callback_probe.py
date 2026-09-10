#!/usr/bin/env python3
"""One opt-in live turn proving source callback ownership and explicit abort cleanup."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import logging
from pathlib import Path
import sys
import tempfile

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))

from core.layers.copilot.callbacks import CallbackRegistry
from lifecycle_probe import HeldTool, Observations, HOLD_PROMPT, observed_within
from probe import SDK_VERSION, RUNTIME_VERSION, child_environment, selected_token, stop_descendants


async def probe(args, report):
    from copilot import CopilotClient, RuntimeConnection, ToolSet
    from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
    from copilot.tools import Tool

    assert importlib.metadata.version("github-copilot-sdk") == SDK_VERSION
    observations = Observations()
    held = HeldTool(observations)
    changes = []
    registry = CallbackRegistry(lambda: changes.append(len(registry.pending_ids)))
    permissions = {"approved": 0, "denied": 0}
    tracked = {}

    def observe():
        for process in psutil.Process().children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                tracked[(process.pid, process.create_time())] = process

    async def track():
        while True:
            observe()
            await asyncio.sleep(0.05)

    def decide(request, _invocation):
        allowed = (request.kind == "custom-tool" and request.tool_name == "lifecycle_hold"
                   and request.args in (None, {}))
        permissions["approved" if allowed else "denied"] += 1
        return PermissionDecisionApproveOnce() if allowed else PermissionDecisionReject()

    async def owned_handler(invocation):
        return await registry.run(invocation.tool_call_id, lambda: held.handle(invocation))

    with tempfile.TemporaryDirectory(prefix="otodock-callback-owner-") as directory:
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
                session = await client.create_session(
                    model="gpt-5-mini", tools=[Tool(
                        name="lifecycle_hold", description="Wait for the compatibility test controller.",
                        parameters={"type": "object", "properties": {}, "additionalProperties": False},
                        handler=owned_handler,
                    )], available_tools=ToolSet().add_custom("lifecycle_hold"),
                    on_permission_request=decide, on_event=observations.on_event,
                    enable_config_discovery=False, enable_file_hooks=False,
                    enable_host_git_operations=False, enable_session_store=False,
                    session_limits={"max_ai_credits": 30.0},
                )
                await session.send(HOLD_PROMPT)
                assert await observed_within(held.started, 45), "controlled callback never started"
                owned_ids = registry.pending_ids
                assert len(owned_ids) == 1
                observations.idle.clear()
                await asyncio.wait_for(session.abort(), timeout=10)
                assert await observed_within(observations.idle, 5), "abort did not produce idle"
                report["aborted_idle"] = observations.idle_aborted_flags[-1] is True
                report["callback_still_owned_after_runtime_abort"] = registry.pending_ids == owned_ids
                report["native_tasks_empty_after_abort"] = not (await session.rpc.tasks.list(timeout=5)).tasks
                report["callback_finished_after_runtime_abort"] = held.finished.is_set()
                assert report["aborted_idle"] and report["callback_still_owned_after_runtime_abort"]
                assert not held.finished.is_set()
                confirmed = await registry.cancel_all(timeout=2)
                report["cancelled_ids_match_owned_ids"] = confirmed == owned_ids
                report["pending_after_registry_join"] = len(registry.pending_ids)
                report["callback_observed_cancellation"] = held.cancelled
                report["callback_finished_after_registry_join"] = held.finished.is_set()
                assert confirmed == owned_ids and not registry.pending_ids
                assert held.cancelled and held.finished.is_set()
                assert permissions == {"approved": 1, "denied": 0}
                await asyncio.wait_for(session.disconnect(), timeout=10)
        finally:
            try:
                await registry.cancel_all(timeout=2)
                await asyncio.wait_for(client.stop(), timeout=10)
            finally:
                observe()
                tracker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tracker
                _, remaining = psutil.wait_procs(list(tracked.values()), timeout=2)
                report["sdk_cleanup"] = "passed" if not remaining else "required_force_cleanup"
                report["tracked_descendants_reaped"] = stop_descendants(remaining)
                report["ownership_sizes_before_change"] = changes
                report["permissions"] = permissions
                report["timeline"] = observations.timeline
                report["event_counts"] = dict(observations.counts)
                assert report["sdk_cleanup"] == "passed" and report["tracked_descendants_reaped"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    args.runtime = args.runtime.resolve()
    if not args.runtime.is_file() or not 1 <= args.timeout <= 120:
        parser.error("existing runtime and timeout from 1 to 120 seconds required")
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "live_turn_limit": 1, "sandboxed": False}
    try:
        asyncio.run(probe(args, report))
        report["result"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
