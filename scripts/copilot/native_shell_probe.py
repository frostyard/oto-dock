#!/usr/bin/env python3
"""No-auth native shell lifecycle discovery inside an owned disposable sandbox.

Uses only fixed fixture Python sleeps, never sends a model prompt, and records
shapes/counters instead of command text, process IDs, session IDs or raw errors.
This diagnostic deliberately admits shell controls not yet enabled in production.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return type(value).__name__


async def run(args, report):
    import psutil
    from copilot import ToolSet
    from copilot._jsonrpc import JsonRpcError
    from copilot.rpc import AbortRequest, InterruptMainTurnRequest, TasksCancelRequest, TasksGetProgressRequest, TasksPromoteToBackgroundRequest, ToolsExecuteRequest

    with tempfile.TemporaryDirectory(prefix="otodock-native-shell-") as directory:
        root = Path(directory)
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount, netns_preflight
        from core.layers.copilot.runtime import SandboxedCopilotRuntime
        from core.layers.copilot.session_state import PrivateCopilotSessionState

        netns_preflight()
        agent = root / "data/agents/native-shell-probe"
        workspace = agent / "workspace"
        for subdir in ("workspace", "knowledge"):
            (agent / subdir).mkdir(parents=True)
        (root / "private").mkdir(mode=0o700)
        state = PrivateCopilotSessionState.create(root / "private")
        assets = args.runtime_dir
        builder = SandboxBuilder(SandboxConfig(
            role="manager", username="", agent_name="native-shell-probe", is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=assets,
            host_claude_dir=workspace / ".claude", net_forwards=["1"],
            mcp_sandbox_mounts=[SandboxMount(str(assets), "/opt/copilot-runtime", "ro")],
        ))
        runtime = SandboxedCopilotRuntime(
            builder, runtime_path=assets / "copilot-runtime", working_directory=workspace,
            sandbox_state_directory=state.sandbox_destination, session_state=state,
            environment={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
            startup_timeout=20, shutdown_timeout=5,
        )
        events = Counter()
        event_shapes = {}
        hook_counts = Counter()
        processes = []
        report["cases"] = []
        launch = None

        def receive(event):
            kind = event.raw_type or event.type.value
            events[kind] += 1
            event_shapes[kind] = shape(event.to_dict().get("data"))

        async def hook(payload, invocation):
            hook_counts[payload.get("toolName", "unknown")] += 1
            return {"permissionDecision": "allow"}

        async def execute(name, arguments):
            result = await session.rpc.tools.execute(
                ToolsExecuteRequest(name=name, arguments=arguments), timeout=60,
            )
            report.setdefault("tool_result_shapes", {})[name] = shape(result)
            return result

        def matching_processes(script):
            found = []
            for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
                try:
                    argv = proc.info["cmdline"] or []
                    if len(argv) >= 3 and Path(argv[0]).name.startswith("python") and argv[1:3] == ["-c", script]:
                        found.append((proc.pid, proc.info["create_time"]))
                except (psutil.Error, OSError):
                    continue
            return found

        def live(identities):
            total = 0
            for pid, created in identities:
                try:
                    proc = psutil.Process(pid)
                    total += proc.create_time() == created and proc.status() != psutil.STATUS_ZOMBIE
                except psutil.Error:
                    pass
            return total

        def pid_relation(task, identities):
            native_pid = getattr(task, "pid", None)
            relation = {"pid_present": type(native_pid) is int and native_pid > 0,
                        "matches_host_fixture_pid": False, "matches_host_ancestor_pid": False,
                        "matches_fixture_namespace_pid": False, "matches_ancestor_namespace_pid": False}
            for pid, _ in identities:
                try:
                    proc = psutil.Process(pid)
                    candidates = [proc, *proc.parents()]
                    for index, candidate in enumerate(candidates):
                        role = "fixture" if index == 0 else "ancestor"
                        relation[f"matches_host_{role}_pid"] |= candidate.pid == native_pid
                        status = Path(f"/proc/{candidate.pid}/status").read_text()
                        ids = next((line.split()[1:] for line in status.splitlines()
                                    if line.startswith("NSpid:")), [])
                        if len(ids) > 1 and int(ids[-1]) == native_pid:
                            relation[f"matches_{role}_namespace_pid"] = True
                except (psutil.Error, OSError):
                    continue
            return relation

        async def inventory(shell_id):
            result = await session.rpc.tasks.list(timeout=5)
            report["tasks_shape"] = shape(result.to_dict())
            return [{"type": task.type, "status": getattr(task.status, "value", task.status),
                     "attachment_mode": getattr(getattr(task, "attachment_mode", None), "value",
                                                getattr(task, "attachment_mode", None)),
                     "execution_mode": getattr(getattr(task, "execution_mode", None), "value",
                                               getattr(task, "execution_mode", None)),
                     "id_matches_shell_id": task.id == shell_id}
                    for task in result.tasks]

        try:
            async with asyncio.timeout(110):
                client = await runtime.start()
                report["unauthenticated"] = not (await client.get_auth_status()).isAuthenticated
                assert report["unauthenticated"]
                session = await client.create_session(
                    model="gpt-5-mini", available_tools=ToolSet().add_builtin(
                        ["bash", "read_bash", "list_bash", "stop_bash"]),
                    tools=[], hooks={"on_pre_tool_use": hook}, on_event=receive,
                    enable_config_discovery=False, enable_file_hooks=False,
                    enable_host_git_operations=False, enable_session_store=False,
                )
                for mode, control in (("sync", "natural"), ("sync", "tasks_cancel"), ("sync", "promote_then_cancel"), ("sync", "stop_bash"),
                                      ("async", "interrupt"), ("async", "abort")):
                    shell_id = f"fixture-{uuid.uuid4().hex}" if mode == "async" else None
                    duration = 2 if control == "natural" else 45
                    script = f"import time; marker='{uuid.uuid4().hex}'; print('FIXTURE_READY',flush=True); time.sleep({duration})"
                    arguments = {"command": f"python3 -c \"{script}\"",
                                 "description": "Bounded inert shell fixture", "mode": mode, "initial_wait": 10}
                    if shell_id is not None:
                        arguments["shellId"] = shell_id
                    started = time.monotonic()
                    launch = asyncio.create_task(execute("bash", arguments))
                    await asyncio.sleep(0.25 if control == "natural" else 12 if mode == "sync" else 1)
                    identities = matching_processes(script)
                    processes.extend(identities)
                    assert len(identities) == 1
                    raw_tasks = await session.rpc.tasks.list(timeout=5)
                    active = [task for task in raw_tasks.tasks
                              if getattr(task.status, "value", task.status) in {"running", "pending"}]
                    row = {"mode": mode, "control": control, "caller_supplied_shell_id": shell_id is not None,
                           "fixture_processes": len(identities), "launch_returned_before_control": launch.done(),
                           "active_task_count": len(active), "tasks_before": await inventory(shell_id)}
                    report["cases"].append(row)
                    if len(active) != 1:
                        raise ValueError("Expected one active isolated native shell task")
                    task_id = active[0].id
                    row["pid_relation"] = pid_relation(active[0], identities)
                    progress = await session.rpc.tasks.get_progress(TasksGetProgressRequest(id=task_id), timeout=5)
                    row["progress_shape"] = shape(progress.to_dict())
                    if shell_id is None:
                        shell_id = task_id
                    row["task_id_matches_shell_id"] = task_id == shell_id
                    listed = await execute("list_bash", {})
                    row["list_contains_shell_id"] = shell_id in str(listed)
                    if mode == "async":
                        read = await execute("read_bash", {"shellId": shell_id, "delay": 0})
                        row["read_contains_fixture_output"] = "FIXTURE_READY" in str(read)
                    if control == "natural":
                        await asyncio.wait_for(asyncio.shield(launch), timeout=5)
                    elif control == "promote_then_cancel":
                        promoted = await session.rpc.tasks.promote_to_background(
                            TasksPromoteToBackgroundRequest(id=task_id), timeout=5,
                        )
                        row["promote_result"] = promoted.to_dict()
                        row["tasks_after_promotion"] = await inventory(shell_id)
                        await asyncio.sleep(0.1)
                        row["launch_returned_after_promotion"] = launch.done()
                        stopped = await session.rpc.tasks.cancel(TasksCancelRequest(id=task_id), timeout=5)
                        row["tasks_cancel_result"] = stopped.to_dict()
                    elif control == "tasks_cancel":
                        stopped = await session.rpc.tasks.cancel(TasksCancelRequest(id=task_id), timeout=5)
                        row["tasks_cancel_result"] = stopped.to_dict()
                    elif control == "stop_bash":
                        stopped = await execute("stop_bash", {"shellId": shell_id})
                        row["stop_result_shape"] = shape(stopped)
                    elif control == "interrupt":
                        interrupted = await session.rpc.interrupt_main_turn(InterruptMainTurnRequest(), timeout=5)
                        row["interrupt_result"] = interrupted.to_dict()
                    else:
                        aborted = await session.rpc.abort(AbortRequest(), timeout=5)
                        row["abort_result_shape"] = shape(aborted.to_dict())
                        row["abort_success"] = aborted.success
                    row["live_immediately_after_control"] = live(identities)
                    row["tasks_immediately_after_control"] = await inventory(shell_id)
                    await asyncio.sleep(0.25)
                    row["live_after_quarter_second"] = live(identities)
                    row["tasks_after_quarter_second"] = await inventory(shell_id)
                    if live(identities):
                        stopped = await session.rpc.tasks.cancel(TasksCancelRequest(id=task_id), timeout=5)
                        row["fallback_tasks_cancel_result"] = stopped.to_dict()
                    row["live_after_stop"] = live(identities)
                    row["tasks_after_stop"] = await inventory(shell_id)
                    result = await asyncio.wait_for(launch, timeout=10)
                    row["launch_seconds"] = round(time.monotonic() - started, 3)
                    row["launch_contains_shell_id"] = shell_id in str(result)
                    row["launch_contains_fixture_output"] = "FIXTURE_READY" in str(result)
                await session.disconnect()
        finally:
            report["live_fixtures_before_runtime_close"] = live(processes)
            await runtime.close()
            if launch is not None:
                launch.cancel()
                await asyncio.gather(launch, return_exceptions=True)
            report["runtime_closed"] = not runtime.alive
            report["normal_cleanup"] = not runtime.forced_cleanup
            report["fixture_survivors_after_cleanup"] = live(processes)
            report["events"] = dict(events)
            report["event_shapes"] = event_shapes
            report["hook_counts"] = dict(hook_counts)
            state.discard()
            assert report["runtime_closed"] and report["normal_cleanup"]
            assert report["fixture_survivors_after_cleanup"] == 0

        # One separate runtime exercises the guarded adapter after the raw
        # contract discovery. Its fence baseline precedes all SDK sessions.
        from core.layers.copilot.native_shells import CopilotNativeShellSession
        from core.layers.copilot.native_tool_policy import CopilotNativeToolPolicy
        from core.layers.copilot.permissions import CopilotPermissionBridge
        from core.layers.copilot.requests import CopilotRequestRegistry

        guarded_state = PrivateCopilotSessionState.create(root / "private")
        guarded_runtime = SandboxedCopilotRuntime(
            builder, runtime_path=assets / "copilot-runtime", working_directory=workspace,
            sandbox_state_directory=guarded_state.sandbox_destination, session_state=guarded_state,
            environment={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
            startup_timeout=20, shutdown_timeout=5,
        )
        requests = CopilotRequestRegistry(on_change=lambda: None)
        report["guarded_cases"] = []
        guarded_launch = None

        async def decide(name, arguments):
            return {"decision": "allow"}

        bridge = CopilotPermissionBridge(
            requests, decide=decide, context_valid=lambda: True, working_directory="/workspace",
        )
        policy = CopilotNativeToolPolicy(bridge, enabled_tools=frozenset({"bash"}))
        try:
            async with asyncio.timeout(40):
                guarded_client = await guarded_runtime.start()
                fence = guarded_runtime.capture_process_fence()
                guarded_session = await policy.create_session(
                    guarded_client, model="gpt-5-mini", enable_session_store=False,
                )
                adapter = CopilotNativeShellSession(guarded_session, processes_settled=fence.is_settled)
                for control in ("natural", "abort"):
                    duration = 2 if control == "natural" else 45
                    script = f"import time; marker='{uuid.uuid4().hex}'; time.sleep({duration})"
                    guarded_launch = asyncio.create_task(guarded_session.rpc.tools.execute(
                        ToolsExecuteRequest(name="bash", arguments={
                            "command": f"python3 -c \"{script}\"", "description": "Inert fenced fixture", "mode": "sync",
                        }), timeout=15,
                    ))
                    await asyncio.sleep(0.25)
                    identities = matching_processes(script)
                    processes.extend(identities)
                    assert len(identities) == 1
                    before = await adapter.snapshot()
                    row = {"control": control, "fence_blocks_while_running": not fence.is_settled(),
                           "snapshot_has_unsettled_work": any(
                               task.state.value not in {"completed", "failed", "cancelled", "retired"} for task in before.tasks)}
                    report["guarded_cases"].append(row)
                    assert row["fence_blocks_while_running"] and row["snapshot_has_unsettled_work"]
                    if control == "abort":
                        tasks = await guarded_session.rpc.tasks.list(timeout=5)
                        active = [task for task in tasks.tasks
                                  if getattr(task.status, "value", task.status) == "running"]
                        assert len(active) == 1
                        task_id = active[0].id
                        promoted = await guarded_session.rpc.tasks.promote_to_background(
                            TasksPromoteToBackgroundRequest(id=task_id), timeout=5,
                        )
                        row["promoted_to_background"] = promoted.promoted is True
                        tasks = await guarded_session.rpc.tasks.list(timeout=5)
                        promoted_tasks = [task for task in tasks.tasks if task.id == task_id]
                        assert len(promoted_tasks) == 1
                        mode = getattr(promoted_tasks[0], "execution_mode", None)
                        row["native_execution_mode_background"] = getattr(mode, "value", mode) == "background"
                        await asyncio.wait_for(asyncio.shield(guarded_launch), timeout=5)
                        row["tool_rpc_returned_while_process_alive"] = live(identities) == 1
                        row["fence_blocks_after_tool_rpc_returned"] = not fence.is_settled()
                        assert row["promoted_to_background"] and row["native_execution_mode_background"]
                        assert row["tool_rpc_returned_while_process_alive"] and row["fence_blocks_after_tool_rpc_returned"]
                        await adapter.abort()
                        row["adapter_abort_returned"] = True
                    try:
                        await asyncio.wait_for(guarded_launch, timeout=5)
                    except JsonRpcError:
                        # Abort can reject a direct tools.execute waiter. That is
                        # not process-exit proof; the fence/snapshot checks below are.
                        if control != "abort":
                            raise
                        row["native_tool_rpc_interrupted"] = True
                    async with asyncio.timeout(5):
                        while True:
                            snapshot = await adapter.snapshot()
                            if all(task.state.value in {"completed", "failed", "cancelled", "retired"} for task in snapshot.tasks):
                                break
                            await asyncio.sleep(0.05)
                    row["fence_settled_after_control"] = fence.is_settled()
                    row["fixture_live_after_control"] = live(identities)
                    row["snapshot_settled_after_control"] = all(
                        task.state.value in {"completed", "failed", "cancelled", "retired"} for task in snapshot.tasks)
                    assert row["fence_settled_after_control"] and row["fixture_live_after_control"] == 0
                    assert row["snapshot_settled_after_control"]
                await guarded_session.disconnect()
        finally:
            requests.close_admissions()
            await requests.cancel_all(timeout=5)
            await guarded_runtime.close()
            if guarded_launch is not None:
                guarded_launch.cancel()
                await asyncio.gather(guarded_launch, return_exceptions=True)
            report["guarded_runtime_closed"] = not guarded_runtime.alive
            report["guarded_normal_cleanup"] = not guarded_runtime.forced_cleanup
            report["fixture_survivors_after_all_cleanup"] = live(processes)
            guarded_state.discard()
            assert report["guarded_runtime_closed"] and report["guarded_normal_cleanup"]
            assert report["fixture_survivors_after_all_cleanup"] == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir = args.runtime_dir.resolve()
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "credentials_supplied": False, "model_prompts": 0, "sandboxed": True,
              "dispatch": "session.tools.execute", "phase": "native shell lifecycle (RPC only)"}
    started = time.monotonic()
    try:
        asyncio.run(run(args, report))
        report["result"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
