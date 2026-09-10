#!/usr/bin/env python3
"""Three bounded model turns qualifying attached native shell completion and controls."""

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

from probe import selected_token

async def run(args, report):
    from copilot.rpc import PermissionsSetApproveAllRequest
    import psutil

    with tempfile.TemporaryDirectory(prefix="otodock-native-model-") as directory:
        root = Path(directory)
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount, netns_preflight
        from core.layers.copilot.runtime import SandboxedCopilotRuntime
        from core.layers.copilot.session_state import PrivateCopilotSessionState
        from core.layers.copilot.permissions import CopilotPermissionBridge
        from core.layers.copilot.native_tool_policy import CopilotNativeToolPolicy
        from core.layers.copilot.supervisor import CopilotSessionSupervisor
        from core.layers.copilot.native_shells import CopilotNativeShellSession

        netns_preflight()
        agent = root / "data/agents/native-model-probe"
        for subdir in ("workspace", "knowledge"):
            (agent / subdir).mkdir(parents=True, exist_ok=True)
        trusted_state = root / "private"
        trusted_state.mkdir(mode=0o700)
        state = PrivateCopilotSessionState.create(trusted_state)
        builder = SandboxBuilder(SandboxConfig(
            role="manager", username="", agent_name="native-model-probe", is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=args.runtime_dir,
            host_claude_dir=agent / "workspace/.claude", net_forwards=["1"],
            mcp_sandbox_mounts=[SandboxMount(str(args.runtime_dir), "/opt/copilot-runtime", "ro")],
        ))
        token = selected_token()
        counts = Counter()
        events = Counter()
        runtimes = []
        supervisors = []
        consumers = []
        try:
            async with asyncio.timeout(240):
                for generation, control in enumerate(("normal", "abort", "interrupt"), 1):
                    runtime = SandboxedCopilotRuntime(
                        builder, runtime_path=args.runtime_dir / "copilot-runtime",
                        working_directory=agent / "workspace", sandbox_state_directory=state.sandbox_destination,
                        environment={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
                        github_token=token, session_state=state, startup_timeout=20, shutdown_timeout=5,
                    )
                    runtimes.append(runtime)
                    supervisor = CopilotSessionSupervisor(
                        pending_requests=frozenset, close_runtime=runtime.close, rpc_timeout=15, turn_timeout=65,
                    )
                    supervisors.append(supervisor)

                    duration = 12 if control == "normal" else 45
                    script = (f"import time; marker='{control}-{time.monotonic_ns()}'; "
                              f"print('OTO_SHELL_STARTED',flush=True); time.sleep({duration}); "
                              "print('OTO_SHELL_FINISHED',flush=True)")
                    command = f'python3 -c "{script}"'
                    expected = {"command": command, "description": "Bounded shell fixture",
                                "mode": "sync", "initial_wait": 10, "cwd": "/workspace"}

                    async def decide(name, arguments):
                        counts[f"generation_{generation}_decisions"] += 1
                        allow = (name == "Bash" and arguments == expected
                                 and counts[f"generation_{generation}_decisions"] == 1)
                        counts["allows" if allow else "denials"] += 1
                        return {"decision": "allow" if allow else "deny"}

                    bridge = CopilotPermissionBridge(
                        supervisor.requests, decide=decide, context_valid=lambda: True,
                        working_directory="/workspace",
                    )
                    policy = CopilotNativeToolPolicy(bridge, enabled_tools=frozenset({"bash"}))

                    def receive(event):
                        events[event.raw_type or event.type.value] += 1
                        supervisor.receive_event(event)

                    client = await runtime.start()
                    process_fence = runtime.capture_process_fence()
                    options = {
                        "model": "gpt-5-mini", "on_event": receive,
                        "enable_session_store": True, "streaming": True,
                        "session_limits": {"max_ai_credits": 30.0},
                    }
                    session = await policy.create_session(client, **options)
                    backend = CopilotNativeShellSession(
                        session, processes_settled=process_fence.is_settled, rpc_timeout=5, cancel_timeout=5,
                    )
                    supervisor.bind(backend)
                    await session.rpc.permissions.set_approve_all(
                        PermissionsSetApproveAllRequest(enabled=True), timeout=5,
                    )
                    prompt = (f'Run bash exactly once with these arguments: '
                              f'{json.dumps({k: v for k, v in expected.items() if k != "cwd"})}. '
                              'Do not run any other tool or retry. After the command finishes reply DONE.')
                    outputs = []

                    async def consume():
                        async for event in supervisor.stream(prompt):
                            outputs.append(event)

                    consumer = asyncio.create_task(consume())
                    consumers.append(consumer)
                    identities = []
                    seen_running = False
                    async with asyncio.timeout(45):
                        while not (seen_running and identities):
                            if consumer.done():
                                await consumer
                                raise AssertionError("No native running shell was observed")
                            snapshot = await backend.snapshot()
                            seen_running = any(task.state.value == "running" for task in snapshot.tasks)
                            if seen_running:
                                for process in psutil.process_iter(["pid", "cmdline", "create_time"]):
                                    try:
                                        argv = process.info["cmdline"] or []
                                        if len(argv) >= 3 and argv[1:3] == ["-c", script]:
                                            identities.append((process.pid, process.info["create_time"]))
                                    except psutil.Error:
                                        pass
                            if not identities:
                                await asyncio.sleep(0.1)
                    assert identities
                    row = {"control": control, "running_task_observed": seen_running,
                           "fixture_processes": len(identities)}
                    report.setdefault("turns", []).append(row)
                    if control != "normal":
                        ack = await getattr(supervisor, control)()
                        row["control_accepted"] = ack.accepted
                        assert ack.accepted and ack.callbacks_stopped
                    await consumer
                    row["done_count"] = sum(event.type == "done" for event in outputs)
                    assert row["done_count"] == 1
                    live = 0
                    for pid, created in identities:
                        try:
                            process = psutil.Process(pid)
                            live += (process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE)
                        except psutil.Error:
                            pass
                    row["fixture_survivors_at_done"] = live
                    assert live == 0
                    row["pending_host_requests"] = len(supervisor.requests.pending_ids)
                    assert not supervisor.requests.pending_ids
                    await supervisor.close()
                    assert not runtime.alive and not runtime.forced_cleanup
                assert counts["allows"] == 3 and counts["denials"] == 0
        finally:
            cleanup_failed = False
            for supervisor in supervisors:
                try:
                    await supervisor.close()
                except Exception:
                    cleanup_failed = True
            for runtime in runtimes:
                try:
                    await runtime.close()
                except Exception:
                    cleanup_failed = True
            for consumer in consumers:
                if not consumer.done():
                    consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            state.discard()
            report["counts"] = dict(sorted(counts.items()))
            report["event_counts"] = dict(sorted(events.items()))
            report["normal_cleanup"] = (not cleanup_failed and all(
                not runtime.alive and not runtime.forced_cleanup for runtime in runtimes
            ))
            assert report["normal_cleanup"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir = args.runtime_dir.resolve()
    if not (args.runtime_dir / "copilot-runtime").is_file():
        parser.error("Missing provisioned runtime")
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "live_turn_limit": 3, "sandboxed": True, "authority": "controlled fixture"}
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
