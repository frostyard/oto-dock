#!/usr/bin/env python3
"""One opt-in live Copilot turn calling only the bundled stdio MCP fixture."""

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

from mcp_fixture import MARKER
from probe import SDK_VERSION, RUNTIME_VERSION, child_environment, selected_token, stop_descendants

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
from core.layers.copilot.mcp_config import INFERENCE_TOKEN_NAMES, wrap_stdio_servers  # noqa: E402
from core.layers.copilot.coordinator import (  # noqa: E402
    CopilotTurnCoordinator, SettlementObservation, TaskObservation, TaskState,
)

SERVER = "oto-fixture"


def fixture_server(audit: Path, otodock_interceptor: bool) -> dict:
    """Use the production interceptor as a separate parent of the unchanged fixture."""
    fixture_args = [str(Path(__file__).with_name("mcp_fixture.py")), "--audit", str(audit)]
    config = {
        "type": "stdio", "command": sys.executable, "args": fixture_args,
        "tools": ["marker"], "timeout": 10000,
    }
    if otodock_interceptor:
        interceptor = Path(__file__).resolve().parents[2] / "proxy/core/stdio_path_interceptor.py"
        config = wrap_stdio_servers(
            {SERVER: config}, interpreter=sys.executable, interceptor_path=str(interceptor),
        )[SERVER]
    return config


def request_shape(request) -> dict:
    """Finite classifications only: never copy runtime arguments or unknown names."""
    args = getattr(request, "args", None)
    return {
        "kind_is_mcp": request.kind == "mcp",
        "server_matches": getattr(request, "server_name", None) == SERVER,
        "tool_is_bare_marker": getattr(request, "tool_name", None) == "marker",
        "tool_is_namespaced_marker": getattr(request, "tool_name", None) == f"{SERVER}-marker",
        "args_empty_object": args == {},
        "args_missing": args is None,
        "args_empty_json": isinstance(args, str) and args.strip() == "{}",
    }


def fixture_request(request) -> bool:
    shape = request_shape(request)
    return (shape["kind_is_mcp"] and shape["server_matches"]
            and (shape["tool_is_bare_marker"] or shape["tool_is_namespaced_marker"])
            and (shape["args_empty_object"] or shape["args_missing"] or shape["args_empty_json"]))


async def run(args, report):
    from copilot import CopilotClient, RuntimeConnection, ToolSet
    from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

    assert importlib.metadata.version("github-copilot-sdk") == SDK_VERSION
    permissions = Counter()
    events = Counter()
    observed = {}
    coordinator = CopilotTurnCoordinator()
    common_events = Counter()
    open_tools = set()
    mapping_errors = Counter()
    sequence = 0

    def receive(event):
        nonlocal sequence
        sequence += 1
        raw = event.to_dict()
        kind = raw["type"]
        events[kind] += 1
        if kind == "tool.execution_start":
            open_tools.add(raw["data"]["toolCallId"])
        elif kind == "tool.execution_complete":
            open_tools.discard(raw["data"]["toolCallId"])
        try:
            common_events.update(e.type for e in coordinator.receive_event(sequence, raw))
        except (ValueError, RuntimeError, TypeError) as exc:
            mapping_errors[type(exc).__name__] += 1

    def decide(request, _invocation):
        report.setdefault("permission_shapes", []).append(request_shape(request))
        allowed = fixture_request(request)
        permissions["fixture_approved" if allowed else "other_denied"] += 1
        if allowed:
            return PermissionDecisionApproveOnce()
        return PermissionDecisionReject(feedback="Only the fixed MCP fixture is authorized.")

    def observe():
        for process in psutil.Process().children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                observed[(process.pid, process.create_time())] = process

    async def track():
        while True:
            observe()
            await asyncio.sleep(0.05)

    with tempfile.TemporaryDirectory(prefix="otodock-copilot-mcp-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        state = root / "state"
        state.mkdir()
        audit = root / "fixture-audit.jsonl"
        client = CopilotClient(
            connection=RuntimeConnection.for_stdio(path=str(args.runtime)),
            working_directory=str(workspace), base_directory=str(state),
            env=child_environment(root), github_token=selected_token(),
            use_logged_in_user=False, mode="empty", log_level="error",
        )
        tracker = asyncio.create_task(track())
        try:
            async with asyncio.timeout(args.timeout):
                await client.start()
                status = await client.get_status()
                assert status.version == RUNTIME_VERSION
                report["runtime_version"] = status.version
                report["sdk_version"] = SDK_VERSION
                session = await client.create_session(
                    model="gpt-5-mini", available_tools=ToolSet().add_mcp(f"{SERVER}-marker"),
                    on_permission_request=decide,
                    enable_config_discovery=False, enable_file_hooks=False,
                    enable_host_git_operations=False, enable_session_store=False,
                    on_event=receive,
                    session_limits={"max_ai_credits": 30.0},
                    mcp_servers={SERVER: fixture_server(audit, args.otodock_interceptor)},
                )
                reply = await session.send_and_wait(
                    "Call the oto-fixture marker MCP tool exactly once with no arguments. "
                    "Return only the text that tool returns. Do not call anything else or retry.",
                    timeout=60,
                )
                records = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
                report["fixture_call_count"] = len(records)
                report["inference_token_present_in_mcp_env"] = records[0]["token_presence"] if records else None
                if records:
                    report["credential_environment_isolation"] = (
                        "failed" if any(records[0]["token_presence"].values()) else "passed"
                    )
                report["returned_marker"] = MARKER if reply and reply.data.content.strip() == MARKER else None
                assert len(records) == 1, "fixture must run exactly once"
                assert permissions["fixture_approved"] == 1, "MCP permission not observed once"
                assert not permissions["other_denied"], "unexpected tool permission requested"
                assert report["returned_marker"] == MARKER, "fixture result did not reach final response"
                # No SDK-hosted tools or asynchronous permission callbacks are
                # exposed by this probe. Track native tool events and fetch the
                # runtime snapshots AFTER capturing the coordinator checkpoint.
                checkpoint = coordinator.begin_reconciliation()
                assert checkpoint is not None, "no current idle candidate"
                tasks = await session.rpc.tasks.list(timeout=5)
                pending = await session.rpc.permissions.pending_requests(timeout=5)
                queued = await session.rpc.queue.pending_items(timeout=5)
                processing = await session.rpc.metadata.is_processing(timeout=5)
                assert not queued.items and not queued.steering_messages, "pending native input"
                observation = SettlementObservation(
                    processing=processing.processing,
                    tasks=tuple(TaskObservation(task.id, TaskState(task.status.value)) for task in tasks.tasks),
                    pending_permissions=frozenset(item.request_id for item in pending.items),
                    pending_tools=frozenset(open_tools),
                    pending_messages=frozenset(),  # One awaited send; no local queue.
                )
                settled = coordinator.finish_reconciliation(checkpoint, observation)
                report["coordinator_done_count"] = sum(event.type == "done" for event in settled)
                report["coordinator_replayed_done_count"] = len(
                    coordinator.finish_reconciliation(checkpoint, observation),
                )
                report["coordinator_mapping_errors"] = dict(mapping_errors)
                assert not mapping_errors and report["coordinator_done_count"] == 1
                assert report["coordinator_replayed_done_count"] == 0
                if args.otodock_interceptor:
                    assert not any(records[0]["token_presence"][name] for name in INFERENCE_TOKEN_NAMES), (
                        "interceptor did not strip inference credentials"
                    )
                await session.disconnect()
        finally:
            try:
                await asyncio.wait_for(client.stop(), timeout=10)
            finally:
                observe()
                tracker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tracker
                _, remaining = psutil.wait_procs(list(observed.values()), timeout=2)
                report["sdk_cleanup"] = "passed" if not remaining else "required_force_cleanup"
                report["tracked_descendants_reaped"] = stop_descendants(remaining)
                report["permission_counts"] = dict(permissions)
                report["event_counts"] = dict(sorted(events.items()))
                report["common_event_counts"] = dict(sorted(common_events.items()))
                assert report["tracked_descendants_reaped"]
                if args.otodock_interceptor:
                    assert report["sdk_cleanup"] == "passed", "interceptor required force cleanup"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--otodock-interceptor", action="store_true",
                        help="Wrap the fixture with OtoDock's real credential/path interceptor")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    args.runtime = args.runtime.resolve()
    if not args.runtime.is_file() or not 1 <= args.timeout <= 120:
        parser.error("an existing runtime and timeout from 1 to 120 seconds are required")
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sandboxed": False, "live_turn_limit": 1,
              "otodock_interceptor": args.otodock_interceptor}
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
