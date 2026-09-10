#!/usr/bin/env python3
"""Bounded live session-supervisor checks through the real Linux sandbox."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time

from probe import selected_token

MARKER = "OTO_SUPERVISOR_READY"


async def run(args, report):
    from copilot import ToolSet
    from copilot.tools import Tool
    from copilot.rpc import (
        PermissionDecisionApproveOnce, PermissionDecisionReject,
        TasksRegisterRequest, TaskClientType, TasksUpdateRequest,
        TaskClientUpdate, TaskClientUpdateKind, TasksRemoveRequest,
    )

    with tempfile.TemporaryDirectory(prefix="otodock-supervisor-") as directory:
        root = Path(directory)
        # Only this disposable development harness sets platform paths, before
        # importing config. The runtime/supervisor components never do so.
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount, netns_preflight
        from core.layers.copilot.runtime import SandboxedCopilotRuntime
        from core.layers.copilot.supervisor import CopilotSessionSupervisor
        from core.layers.copilot.sdk_session import CopilotSdkSession

        netns_preflight()
        agent = root / "data/agents/supervisor-probe"
        for subdir in ("workspace/.copilot", "knowledge"):
            (agent / subdir).mkdir(parents=True, exist_ok=True)
        builder = SandboxBuilder(SandboxConfig(
            role="manager", username="", agent_name="supervisor-probe", is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=args.runtime_dir,
            host_claude_dir=agent / "workspace/.claude", net_forwards=["1"],
            mcp_sandbox_mounts=[SandboxMount(str(args.runtime_dir), "/opt/copilot-runtime", "ro")],
        ))
        runtime = SandboxedCopilotRuntime(
            builder, runtime_path=args.runtime_dir / "copilot-runtime",
            working_directory=agent / "workspace", sandbox_state_directory="/workspace/.copilot",
            environment={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
            github_token=selected_token(), startup_timeout=20, shutdown_timeout=5,
        )
        supervisor = CopilotSessionSupervisor(
            pending_requests=frozenset, close_runtime=runtime.close,
            turn_timeout=60, rpc_timeout=10,
        )
        events = Counter()
        permissions = Counter()
        started = asyncio.Event()
        cancelled = asyncio.Event()
        callback_calls = 0
        producer = None
        stage = "startup"
        operation_failed = False
        turn_ids = set()
        turn_repeats = []

        def receive(event):
            kind = event.raw_type or event.type.value
            events[kind] += 1
            if kind == "assistant.turn_start":
                turn_id = event.to_dict()["data"].get("turnId")
                turn_repeats.append(turn_id in turn_ids)
                turn_ids.add(turn_id)
            supervisor.receive_event(event)

        def permission(request, _invocation):
            allowed = (request.kind == "custom-tool" and request.tool_name == "supervisor_hold"
                       and request.args in (None, {}))
            permissions["approved" if allowed else "denied"] += 1
            return PermissionDecisionApproveOnce() if allowed else PermissionDecisionReject()

        async def held():
            nonlocal callback_calls
            callback_calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def owned(invocation):
            assert invocation.arguments in (None, {})
            return await supervisor.callbacks.run(invocation.tool_call_id, held)

        async def collect(prompt):
            result = []
            async for event in supervisor.stream(prompt):
                result.append(event)
            return result

        try:
            async with asyncio.timeout(180):
                client = await runtime.start()
                report["runtime_alive_after_start"] = runtime.alive
                session = await client.create_session(
                    model="gpt-5-mini", available_tools=ToolSet().add_custom("supervisor_hold"),
                    tools=[Tool(name="supervisor_hold", description="Wait for the test controller.",
                                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                                handler=owned)],
                    on_permission_request=permission, on_event=receive,
                    enable_config_discovery=False, enable_file_hooks=False,
                    enable_host_git_operations=False, enable_session_store=True,
                    streaming=True, session_limits={"max_ai_credits": 30.0},
                )
                supervisor.bind(CopilotSdkSession(session))
                stage = "normal_turn"
                result = await collect(f"Reply only with {MARKER}. Do not call tools.")
                report["normal_turn"] = {
                    "marker_matched": "".join(e.data["content"] for e in result if e.type == "text").strip() == MARKER,
                    "done_count": sum(e.type == "done" for e in result),
                }
                assert report["normal_turn"] == {"marker_matched": True, "done_count": 1}
                for scenario in ("abort", "interrupt"):
                    stage = scenario
                    started.clear()
                    cancelled.clear()
                    background = None
                    if scenario == "interrupt":
                        background = await session.rpc.tasks.register(TasksRegisterRequest(
                            cancellable=False, client_task_id="supervisor-background",
                            description="Controlled metadata only", type=TaskClientType.CLIENT,
                        ), timeout=5)
                    before_idle = events["session.idle"]
                    producer = asyncio.create_task(collect(
                        "Call supervisor_hold exactly once with no arguments. After it returns say READY. Do not retry.",
                    ))
                    await asyncio.wait_for(started.wait(), timeout=45)
                    report.setdefault("control_has_open_turn", {})[scenario] = supervisor.coordinator._turn_open
                    acknowledgement = await getattr(supervisor, scenario)()
                    details = {"accepted": acknowledgement.accepted,
                               "callbacks_stopped": acknowledgement.callbacks_stopped,
                               "callback_observed_cancellation": cancelled.is_set()}
                    if background is not None:
                        await asyncio.sleep(0.2)
                        details["completion_blocked_by_background"] = not producer.done()
                        assert not producer.done()
                        await session.rpc.tasks.update(TasksUpdateRequest(
                            id=background.task.id, sequence=background.task.sequence + 1,
                            update=TaskClientUpdate(kind=TaskClientUpdateKind.CANCELLED),
                        ), timeout=5)
                        await session.rpc.tasks.remove(TasksRemoveRequest(id=background.task.id), timeout=5)
                    result = await asyncio.wait_for(producer, timeout=20)
                    producer = None
                    details["done_count"] = sum(e.type == "done" for e in result)
                    details["cancelled_tool_results"] = sum(
                        e.type == "tool_result" and e.data.get("is_error") is True for e in result
                    )
                    details["new_native_idle_count"] = events["session.idle"] - before_idle
                    assert details["accepted"] and details["callbacks_stopped"] and cancelled.is_set()
                    assert details["done_count"] == 1 and details["cancelled_tool_results"] == 1
                    if scenario == "interrupt":
                        assert details["new_native_idle_count"] == 0
                    report[scenario] = details
                assert callback_calls == 2 and permissions == {"approved": 2}
        except Exception as exc:
            operation_failed = True
            report["failure_stage"] = stage
            report["operation_error_type"] = type(exc).__name__
            raise
        finally:
            if producer is not None:
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await producer
            await supervisor.close()
            report["normal_runtime_cleanup"] = not runtime.forced_cleanup
            report["runtime_alive_after_close"] = runtime.alive
            report["callback_calls"] = callback_calls
            report["pending_host_callbacks"] = len(supervisor.callbacks.pending_ids)
            report["permissions"] = dict(permissions)
            report["native_event_counts"] = dict(sorted(events.items()))
            report["model_turn_id_reused"] = turn_repeats
            if not operation_failed:
                assert not runtime.alive and not runtime.forced_cleanup and not supervisor.callbacks.pending_ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir = args.runtime_dir.resolve()
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sandboxed": True, "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "live_turn_limit": 3, "host_callback_has_external_side_effects": False}
    start = time.monotonic()
    try:
        asyncio.run(run(args, report))
        report["result"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    report["elapsed_seconds"] = round(time.monotonic() - start, 3)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
