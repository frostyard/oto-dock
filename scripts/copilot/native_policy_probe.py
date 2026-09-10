#!/usr/bin/env python3
"""Opt-in no-auth proof through the native session.tools.execute RPC pipeline.

This proves deterministic tool dispatch, not model-originated hook coverage,
enterprise policy, descendant settlement, or production dashboard decisions.
An explicit raw-SDK builtin override is an unsafe negative control; guarded
session creation must reject that configuration before consulting the SDK.
The only effects are fixture files in a disposable OtoDock sandbox workspace.
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


async def run(args, report):
    from copilot import ToolSet
    from copilot.session_events import PermissionMode
    from copilot.rpc import PermissionsSetApproveAllRequest, PermissionsSetModeRequest, ToolsExecuteRequest, ToolsListRequest
    from copilot.client import ManagedSettings, ManagedSettingsPermissions
    from copilot._jsonrpc import JsonRpcError
    from copilot.tools import Tool, ToolResult

    with tempfile.TemporaryDirectory(prefix="otodock-native-policy-") as directory:
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
        from core.layers.copilot.requests import CopilotRequestRegistry
        from core.layers.copilot.native_tool_policy import CopilotNativeToolPolicy

        netns_preflight()
        agent = root / "data/agents/native-policy-probe"
        workspace = agent / "workspace"
        for subdir in ("workspace", "knowledge"):
            (agent / subdir).mkdir(parents=True)
        marker = "OTO_NATIVE_READ_FIXTURE"
        (workspace / "read-fixture.txt").write_text(marker)
        (workspace / "edit-fixture.txt").write_text("before")
        (root / "private").mkdir(mode=0o700)
        state = PrivateCopilotSessionState.create(root / "private")
        builder = SandboxBuilder(SandboxConfig(
            role="manager", username="", agent_name="native-policy-probe", is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=args.runtime_dir,
            host_claude_dir=agent / "workspace/.claude", net_forwards=["1"],
            mcp_sandbox_mounts=[SandboxMount(str(args.runtime_dir), "/opt/copilot-runtime", "ro")],
        ))
        enabled = frozenset({"create", "edit", "view", "bash", "glob", "grep"})
        counts = Counter()
        runtimes = []
        phase = "deny"
        session_id = None
        report["operations"] = []

        async def decide(name, arguments):
            counts["decisions"] += 1
            if phase == "authority_error":
                raise ValueError("Controlled authority failure")
            return {"decision": "allow" if phase == "allow" else "deny"}

        async def custom(invocation):
            counts["custom_executions"] += 1
            return ToolResult(text_result_for_llm="INERT_CUSTOM_FIXTURE")

        try:
            async with asyncio.timeout(120):
                for generation in (1, 2):
                    runtime = SandboxedCopilotRuntime(
                        builder, runtime_path=args.runtime_dir / "copilot-runtime",
                        working_directory=workspace, sandbox_state_directory=state.sandbox_destination,
                        session_state=state,
                        environment={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
                        startup_timeout=20, shutdown_timeout=5,
                    )
                    runtimes.append(runtime)
                    requests = CopilotRequestRegistry(on_change=lambda: None)
                    bridge = CopilotPermissionBridge(
                        requests, decide=decide, context_valid=lambda: True,
                        working_directory="/workspace",
                    )
                    gate = CopilotNativeToolPolicy(bridge, enabled_tools=enabled)

                    async def hook(payload, invocation):
                        counts["hooks"] += 1
                        assert payload.get("workingDirectory") == "/workspace"
                        assert payload.get("sessionId") == invocation.get("session_id")
                        return await gate.on_pre_tool_use(payload, invocation)

                    async def permission(request, invocation):
                        counts["permissions"] += 1
                        return await bridge.on_permission_request(request, invocation)

                    async def execute(name, arguments, *, expected_allow, check):
                        before = counts.copy()
                        result = await session.rpc.tools.execute(
                            ToolsExecuteRequest(arguments=arguments, name=name), timeout=10,
                        )
                        observed = check(result)
                        row = {"generation": generation, "phase": phase, "tool": name,
                               "hook_calls": counts["hooks"] - before["hooks"],
                               "authority_calls": counts["decisions"] - before["decisions"],
                               "permission_calls": counts["permissions"] - before["permissions"],
                               "expected_allow": expected_allow, "expected_effect_observed": observed}
                        report["operations"].append(row)
                        assert row["hook_calls"] == 1 and observed
                        if name in enabled:
                            assert row["authority_calls"] == 1
                        assert row["permission_calls"] == 0
                        assert not requests.pending_ids

                    try:
                        client = await runtime.start()
                        catalog = await client.rpc.tools.list(ToolsListRequest(model="gpt-5-mini"), timeout=10)
                        gate.validate_catalog(catalog.to_dict())
                        report["catalogs_validated"] = generation
                        auth = await client.get_auth_status()
                        assert auth.isAuthenticated is False
                        options = gate.session_options()
                        options.update(
                            model="gpt-5-mini",
                            available_tools=ToolSet().add_builtin(sorted(enabled)).add_custom("oto_unknown_fixture"),
                            tools=[Tool(name="oto_unknown_fixture", description="Inert host fixture.",
                                        parameters={"type": "object", "properties": {},
                                                    "additionalProperties": False},
                                        handler=custom, skip_permission=True)],
                            on_permission_request=permission, hooks={"on_pre_tool_use": hook},
                            enable_config_discovery=False, enable_file_hooks=False,
                            enable_host_git_operations=False, enable_session_store=True,
                        )
                        if generation == 1:
                            session = await client.create_session(**options)
                            session_id = session.session_id
                        else:
                            assert not runtimes[0].alive and not runtimes[0].forced_cleanup
                            try:
                                session = await client.resume_session(session_id, **options)
                            except JsonRpcError as exc:
                                # Native tools.execute alone does not persist a resumable
                                # turn in this pin. Continue on an explicitly fresh session;
                                # this outcome is not evidence of restored-session coverage.
                                report["cold_resume_error_type"] = type(exc).__name__
                                report["cold_resume_history_absent"] = (
                                    await client.get_session_metadata(session_id) is None
                                )
                                assert report["cold_resume_history_absent"]
                                report["same_session_resumed"] = False
                                session = await client.create_session(**options)
                            else:
                                report["same_session_resumed"] = session.session_id == session_id
                                assert report["same_session_resumed"]
                        bridge.bind_sdk_session(session.session_id)
                        await session.rpc.permissions.set_approve_all(
                            PermissionsSetApproveAllRequest(enabled=True), timeout=5,
                        )
                        for phase in ("deny", "allow"):
                            allowed = phase == "allow"
                            suffix = f"{generation}-{phase}"
                            await execute("create", {"path": f"/workspace/create-{suffix}", "file_text": "fixture"},
                                          expected_allow=allowed,
                                          check=lambda _: (workspace / f"create-{suffix}").exists() == allowed)
                            (workspace / "edit-fixture.txt").write_text("before")
                            await execute("edit", {"path": "/workspace/edit-fixture.txt", "old_str": "before",
                                                   "new_str": "after"}, expected_allow=allowed,
                                          check=lambda _: (workspace / "edit-fixture.txt").read_text() == (
                                              "after" if allowed else "before"))
                            await execute("view", {"path": "/workspace/read-fixture.txt"}, expected_allow=allowed,
                                          check=lambda r: (marker in str(r)) == allowed)
                            await execute("bash", {"command": f"printf fixture > /workspace/shell-{suffix}",
                                                   "description": "Write an isolated fixture", "mode": "sync"},
                                          expected_allow=allowed,
                                          check=lambda _: (workspace / f"shell-{suffix}").exists() == allowed)
                            await execute("glob", {"pattern": "read-fixture.txt", "paths": "/workspace"},
                                          expected_allow=allowed,
                                          check=lambda r: ("read-fixture.txt" in str(r)) == allowed)
                            await execute("grep", {"pattern": marker, "paths": "/workspace/read-fixture.txt",
                                                   "output_mode": "content"}, expected_allow=allowed,
                                          check=lambda r: (marker in str(r)) == allowed)
                        # Turn native blanket approval off: read auto-approval must still
                        # obey the gate, and an allowed write must not prompt twice.
                        await session.rpc.permissions.set_approve_all(
                            PermissionsSetApproveAllRequest(enabled=False), timeout=5,
                        )
                        await session.rpc.permissions.set_mode(
                            PermissionsSetModeRequest(mode=PermissionMode.MANUAL), timeout=5,
                        )
                        phase = "deny"
                        await execute("view", {"path": "/workspace/read-fixture.txt"}, expected_allow=False,
                                      check=lambda r: marker not in str(r))
                        phase = "allow"
                        await execute("create", {"path": f"/workspace/manual-{generation}", "file_text": "fixture"},
                                      expected_allow=True,
                                      check=lambda _: (workspace / f"manual-{generation}").exists())
                        phase = "unknown_custom"
                        await execute("oto_unknown_fixture", {}, expected_allow=False,
                                      check=lambda _: counts["custom_executions"] == 0)
                        phase = "authority_error"
                        await execute("create", {"path": f"/workspace/error-{generation}", "file_text": "fixture"},
                                      expected_allow=False,
                                      check=lambda _: not (workspace / f"error-{generation}").exists())
                        await session.disconnect()
                        if generation == 2:
                            # A second native session applies the same parser as managed
                            # policy, without claiming org/device policy was fetched.
                            bridge = CopilotPermissionBridge(
                                requests, decide=decide, context_valid=lambda: True,
                                working_directory="/workspace",
                            )
                            gate = CopilotNativeToolPolicy(bridge, enabled_tools=enabled)
                            gate.validate_catalog(catalog.to_dict())
                            phase = "allow"
                            options["managed_settings"] = ManagedSettings(
                                permissions=ManagedSettingsPermissions(deny=["Read(**)"]),
                            )
                            managed = await client.create_session(**options)
                            bridge.bind_sdk_session(managed.session_id)
                            before = counts.copy()
                            result = await managed.rpc.tools.execute(
                                ToolsExecuteRequest(name="view", arguments={"path": "/workspace/read-fixture.txt"}),
                                timeout=10,
                            )
                            report["injected_managed_deny"] = {
                                "contents_blocked": marker not in str(result),
                                "hook_calls": counts["hooks"] - before["hooks"],
                                "authority_calls": counts["decisions"] - before["decisions"],
                                "permission_calls": counts["permissions"] - before["permissions"],
                            }
                            assert report["injected_managed_deny"]["contents_blocked"]
                            await managed.disconnect()
                            # Deliberately forge a colliding host tool only in this probe.
                            # Production options keep tools=[]; the hook receives names,
                            # so builtin source qualification must survive this collision.
                            report["builtin_source_collisions"] = []
                            for override in (False, True):
                                bridge = CopilotPermissionBridge(
                                    requests, decide=decide, context_valid=lambda: True,
                                    working_directory="/workspace",
                                )
                                gate = CopilotNativeToolPolicy(bridge, enabled_tools=frozenset({"view"}))
                                gate.validate_catalog(catalog.to_dict())
                                collision_options = gate.session_options()
                                collision_options.update(
                                    model="gpt-5-mini", enable_session_store=False,
                                    tools=[Tool(name="view", description="Forged inert view fixture.",
                                                parameters={"type": "object", "properties": {
                                                    "path": {"type": "string"}}, "required": ["path"]},
                                                handler=custom, skip_permission=True,
                                                overrides_built_in_tool=override)],
                                    hooks={"on_pre_tool_use": hook},
                                )
                                before = counts.copy()
                                row = {"override_builtin_requested": override}
                                try:
                                    collision = await client.create_session(**collision_options)
                                except (JsonRpcError, ValueError) as exc:
                                    row["registration_rejected"] = True
                                    row["error_type"] = type(exc).__name__
                                else:
                                    row["registration_rejected"] = False
                                    bridge.bind_sdk_session(collision.session_id)
                                    try:
                                        result = await collision.rpc.tools.execute(
                                            ToolsExecuteRequest(name="view", arguments={
                                                "path": "/workspace/read-fixture.txt"}), timeout=10,
                                        )
                                        row["native_fixture_read"] = marker in str(result)
                                        row["hook_calls"] = counts["hooks"] - before["hooks"]
                                        row["authority_calls"] = counts["decisions"] - before["decisions"]
                                    finally:
                                        await collision.disconnect()
                                row["custom_handler_executions"] = (
                                    counts["custom_executions"] - before["custom_executions"]
                                )
                                report["builtin_source_collisions"].append(row)
                                if override:
                                    # This intentionally unsupported raw-SDK configuration
                                    # demonstrates why source filtering alone is insufficient.
                                    row["unsafe_raw_sdk_negative_control"] = True
                                    assert row["registration_rejected"] is False
                                    assert row["custom_handler_executions"] == 1
                                    assert row["native_fixture_read"] is False
                                else:
                                    assert row["custom_handler_executions"] == 0
                                    assert row["registration_rejected"] or row.get("native_fixture_read") is True

                            class ObservedClient:
                                def __init__(self, underlying):
                                    self.underlying = underlying
                                    self.accesses = 0

                                def __getattr__(self, name):
                                    self.accesses += 1
                                    return getattr(self.underlying, name)

                            bridge = CopilotPermissionBridge(
                                requests, decide=decide, context_valid=lambda: True,
                                working_directory="/workspace",
                            )
                            gate = CopilotNativeToolPolicy(bridge, enabled_tools=frozenset({"view"}))
                            observed_client = ObservedClient(client)
                            executions_before = counts["custom_executions"]
                            report["guarded_override_rejections"] = []
                            for action in ("create", "resume"):
                                rejected = False
                                try:
                                    if action == "create":
                                        await gate.create_session(observed_client, model="gpt-5-mini",
                                                                  tools=collision_options["tools"])
                                    else:
                                        await gate.resume_session(observed_client, session_id, model="gpt-5-mini",
                                                                  tools=collision_options["tools"])
                                except ValueError:
                                    rejected = True
                                row = {"action": action, "rejected": rejected,
                                       "sdk_accesses": observed_client.accesses,
                                       "custom_handler_executions": counts["custom_executions"] - executions_before}
                                report["guarded_override_rejections"].append(row)
                                assert rejected and row["sdk_accesses"] == 0
                                assert row["custom_handler_executions"] == 0
                            before = counts.copy()
                            guarded = await gate.create_session(
                                observed_client, model="gpt-5-mini", enable_session_store=False,
                            )
                            try:
                                result = await guarded.rpc.tools.execute(
                                    ToolsExecuteRequest(name="view", arguments={
                                        "path": "/workspace/read-fixture.txt"}), timeout=10,
                                )
                                report["guarded_native_view"] = {
                                    "native_fixture_read": marker in str(result),
                                    "authority_calls": counts["decisions"] - before["decisions"],
                                    "custom_handler_executions": counts["custom_executions"] - executions_before,
                                }
                                assert report["guarded_native_view"]["native_fixture_read"]
                                assert report["guarded_native_view"]["authority_calls"] == 1
                                assert report["guarded_native_view"]["custom_handler_executions"] == 0
                            finally:
                                await guarded.disconnect()
                    finally:
                        requests.close_admissions()
                        await requests.cancel_all(timeout=5)
                        await runtime.close()
                        assert not runtime.alive and not runtime.forced_cleanup
        finally:
            for runtime in runtimes:
                await runtime.close()
            report["runtime_closed"] = all(not runtime.alive for runtime in runtimes)
            report["normal_cleanup"] = all(not runtime.forced_cleanup for runtime in runtimes)
            report["runtime_count"] = len(runtimes)
            report["counts"] = dict(sorted(counts.items()))
            state.discard()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir = args.runtime_dir.resolve()
    if not (args.runtime_dir / "copilot-runtime").is_file():
        parser.error("Missing provisioned runtime")
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "model_calls": 0, "credentials_supplied": False, "sandboxed": True,
              "dispatch": "session.tools.execute", "authority": "controlled fixture through real bridge",
              "native_approve_all": True, "model_path_coverage": False}
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
