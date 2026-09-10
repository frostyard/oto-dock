#!/usr/bin/env python3
"""Opt-in sandbox proof of owned Copilot approval, rejection and abort waits.

Only a trusted, inert Python fixture is exposed. Policy answers are controlled
fixtures, not proof of every native tool or the production dashboard authority.
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

from probe import selected_token


async def run(args, report):
    from copilot import ToolSet
    from copilot.tools import Tool, ToolResult

    with tempfile.TemporaryDirectory(prefix="otodock-permission-") as directory:
        root = Path(directory)
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount, netns_preflight
        from core.layers.copilot.runtime import SandboxedCopilotRuntime
        from core.layers.copilot.supervisor import CopilotSessionSupervisor
        from core.layers.copilot.sdk_session import CopilotSdkSession
        from core.layers.copilot.permissions import CopilotPermissionBridge

        netns_preflight()
        agent = root / "data/agents/permission-probe"
        for subdir in ("workspace/.copilot", "knowledge"):
            (agent / subdir).mkdir(parents=True, exist_ok=True)
        builder = SandboxBuilder(SandboxConfig(
            role="manager", username="", agent_name="permission-probe", is_admin_agent=False,
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
            pending_requests=frozenset, close_runtime=runtime.close, turn_timeout=45, rpc_timeout=5,
        )
        phase = "deny"
        counts = Counter()
        waiting = asyncio.Event()
        cancelled = asyncio.Event()
        events = Counter()
        permission_outcomes = Counter()

        async def decide(name, arguments):
            assert name == "CopilotCustomTool" and arguments == {}
            counts[f"{phase}_decisions"] += 1
            if phase == "hold":
                waiting.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return {"decision": "allow" if phase == "allow" and counts["allow_decisions"] == 1 else "deny"}

        bridge = CopilotPermissionBridge(
            supervisor.requests, decide=decide, context_valid=lambda: True,
            working_directory="/workspace",
            custom_tools={"oto_permission_probe": "CopilotCustomTool"},
        )

        async def fixture(invocation):
            async def execute():
                assert invocation.arguments == {}
                counts[f"{phase}_executions"] += 1
                return ToolResult(text_result_for_llm="OTO_PERMISSION_FIXTURE_OK. The fixture succeeded; reply DONE.")
            return await supervisor.callbacks.run(invocation.tool_call_id, execute)

        def receive(event):
            events[event.raw_type or event.type.value] += 1
            raw = event.to_dict()
            if raw["type"] == "permission.completed":
                data = raw["data"]
                outcome = data.get("result", {}).get("kind")
                if isinstance(outcome, str):
                    permission_outcomes[outcome] += 1
                counts["permission_completion_with_tool_id"] += bool(data.get("toolCallId"))
            supervisor.receive_event(event)

        async def collect():
            return [event async for event in supervisor.stream(
                "Call oto_permission_probe exactly once with empty arguments. "
                "Do not retry if it is denied. Then reply DONE."
            )]

        turn = None
        try:
            async with asyncio.timeout(150):
                client = await runtime.start()
                session = await client.create_session(
                    model="gpt-5-mini", available_tools=ToolSet().add_custom("oto_permission_probe"),
                    tools=[Tool(name="oto_permission_probe", description="Inert approval test fixture.",
                                parameters={"type": "object", "properties": {},
                                            "additionalProperties": False}, handler=fixture)],
                    on_permission_request=bridge.on_permission_request, on_event=receive,
                    enable_config_discovery=False, enable_file_hooks=False,
                    enable_host_git_operations=False, enable_session_store=False,
                    streaming=True, session_limits={"max_ai_credits": 30.0},
                )
                supervisor.bind(CopilotSdkSession(session))
                bridge.bind_sdk_session(session.session_id)
                for phase in ("deny", "allow"):
                    result = await collect()
                    assert counts[f"{phase}_decisions"] >= 1
                    assert counts[f"{phase}_executions"] == (1 if phase == "allow" else 0)
                    assert sum(event.type == "done" for event in result) == 1
                    assert not supervisor.requests.pending_ids
                phase = "hold"
                turn = asyncio.create_task(collect())
                await asyncio.wait_for(waiting.wait(), timeout=35)
                report["approval_wait_owned"] = len(supervisor.requests.pending_ids) == 1
                assert report["approval_wait_owned"] and not turn.done()
                acknowledgement = await supervisor.abort()
                report["abort_accepted"] = acknowledgement.accepted
                report["host_requests_joined"] = acknowledgement.callbacks_stopped and cancelled.is_set()
                result = await asyncio.wait_for(turn, timeout=15)
                report["abort_done_count"] = sum(event.type == "done" for event in result)
                report["no_execution_after_abort"] = counts["hold_executions"] == 0
                assert report["host_requests_joined"] and report["no_execution_after_abort"]
                assert report["abort_accepted"] and report["abort_done_count"] == 1
                assert not supervisor.requests.pending_ids
        finally:
            try:
                await supervisor.close()
            finally:
                if turn is not None:
                    await asyncio.gather(turn, return_exceptions=True)
                await runtime.close()
                report["counts"] = dict(sorted(counts.items()))
                report["event_counts"] = dict(sorted(events.items()))
                report["permission_outcomes"] = dict(sorted(permission_outcomes.items()))
                report["runtime_closed"] = not runtime.alive
                report["normal_cleanup"] = not runtime.forced_cleanup
                assert report["runtime_closed"] and report["normal_cleanup"]


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
              "live_turn_limit": 3, "model": "gpt-5-mini", "sandboxed": True,
              "authority": "controlled inert fixture"}
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
