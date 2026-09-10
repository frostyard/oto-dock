#!/usr/bin/env python3
"""Bounded real-sandbox credential-generation restart and private history proof."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time

from probe import selected_token


async def run(args, report):
    from copilot import ToolSet
    from copilot.rpc import PermissionDecisionReject

    with tempfile.TemporaryDirectory(prefix="otodock-account-") as directory:
        root = Path(directory)
        # Disposable development harness only: production adapters never alter
        # process-global platform config or inherit a credential environment.
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount, netns_preflight
        from core.layers.copilot.credentials import CopilotCredential, CopilotAccountScope, CredentialKind
        from core.layers.copilot.lease import CopilotLeaseGuard
        from core.layers.copilot.runtime import SandboxedCopilotRuntime
        from core.layers.copilot.session_state import PrivateCopilotSessionState
        from core.layers.copilot.supervisor import CopilotSessionSupervisor
        from core.layers.copilot.sdk_session import CopilotSdkSession

        netns_preflight()
        agent = root / "data/agents/account-probe"
        for subdir in ("workspace", "knowledge"):
            (agent / subdir).mkdir(parents=True, exist_ok=True)
        trusted_state = root / "private-state"
        trusted_state.mkdir(mode=0o700)
        state = PrivateCopilotSessionState.create(trusted_state)
        state_path = state.path
        builder = SandboxBuilder(SandboxConfig(
            role="manager", username="", agent_name="account-probe", is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=args.runtime_dir,
            host_claude_dir=agent / "workspace/.claude", net_forwards=["1"],
            mcp_sandbox_mounts=[SandboxMount(str(args.runtime_dir), "/opt/copilot-runtime", "ro")],
        ))
        scope = CopilotAccountScope.personal("local-probe-scope")
        current = None
        reads = 0
        token = None
        session_id = None
        login = None
        marker = "OTO_ACCOUNT_" + secrets.token_hex(12).upper()
        events = Counter()
        permissions = 0
        runtimes = []
        guards = []
        supervisors = []
        report["private_state_outside_workspace"] = not state_path.is_relative_to(agent)

        async def read_credential(account_id, payer_scope):
            nonlocal reads
            reads += 1
            assert account_id == "local-probe-account" and payer_scope == scope
            return current

        def make_runtime(selected):
            runtime = SandboxedCopilotRuntime(
                builder, runtime_path=args.runtime_dir / "copilot-runtime",
                working_directory=agent / "workspace", sandbox_state_directory=state.sandbox_destination,
                environment={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
                credential=selected, session_state=state, startup_timeout=20, shutdown_timeout=5,
            )
            runtimes.append(runtime)
            return runtime

        def permission(_request, _invocation):
            nonlocal permissions
            permissions += 1
            return PermissionDecisionReject()

        try:
            async with asyncio.timeout(180):
                for generation in (1, 2):
                    report["stage"] = generation
                    acquired = selected_token()
                    if token is not None:
                        report["same_user_token_reacquired"] = acquired == token
                        assert report["same_user_token_reacquired"]
                    token = acquired
                    # Synthetic repository revisions; unknown actual OAuth
                    # expiry is preserved rather than invented for the probe.
                    current = CopilotCredential(
                        "local-probe-account", "local-probe-principal", str(generation),
                        CredentialKind.USER_TOKEN, token,
                    )
                    runtime = make_runtime(current)
                    supervisor = None

                    def invalidate():
                        supervisor.invalidate_credentials()

                    guard = CopilotLeaseGuard(
                        current, scope, read_credential=read_credential, on_invalid=invalidate,
                        check_interval=0.25, read_timeout=5,
                    )
                    guards.append(guard)
                    supervisor = CopilotSessionSupervisor(
                        pending_requests=frozenset, close_runtime=runtime.close,
                        turn_timeout=60, rpc_timeout=10, authorize_submission=guard.authorize,
                    )
                    supervisors.append(supervisor)

                    def receive(event):
                        events[event.raw_type or event.type.value] += 1
                        supervisor.receive_event(event)

                    await guard.start()
                    client = await runtime.start()
                    auth = await client.get_auth_status()
                    assert auth.isAuthenticated
                    if generation == 1:
                        login = auth.login
                    else:
                        report["runtime_login_available"] = bool(login) and bool(auth.login)
                        report["same_runtime_login"] = bool(login) and auth.login == login
                        if report["runtime_login_available"]:
                            assert report["same_runtime_login"]
                    models = await client.list_models()
                    allowed = any(m.id == "gpt-5-mini" and (m.policy is None or m.policy.state != "disabled")
                                  for m in models)
                    assert allowed
                    options = dict(
                        model="gpt-5-mini", available_tools=ToolSet(), tools=[],
                        on_permission_request=permission, on_event=receive,
                        enable_config_discovery=False, enable_file_hooks=False,
                        enable_host_git_operations=False, enable_session_store=True,
                        streaming=True, session_limits={"max_ai_credits": 30.0},
                    )
                    if generation == 1:
                        session = await client.create_session(**options)
                        session_id = session.session_id
                    else:
                        assert not runtimes[0].alive and not runtimes[0].forced_cleanup
                        session = await client.resume_session(session_id, **options)
                        history = await session.get_events()
                        report["history_marker_retained"] = any(
                            e.type.value == "assistant.message" and getattr(e.data, "content", None) == marker
                            for e in history
                        )
                        assert report["history_marker_retained"]
                    supervisor.bind(CopilotSdkSession(session))
                    before_reads = reads
                    prompt = (f"Remember this marker and reply with only it: {marker}. Do not use tools."
                              if generation == 1 else
                              "Reply with only the marker from my previous message. Do not use tools.")
                    result = [event async for event in supervisor.stream(prompt)]
                    matched = "".join(e.data["content"] for e in result if e.type == "text").strip() == marker
                    done_count = sum(e.type == "done" for e in result)
                    assert matched and done_count == 1 and reads > before_reads
                    report.setdefault("turns", []).append({
                        "marker_matched": matched, "done_count": done_count,
                        "source_revalidated": reads > before_reads, "model_available": allowed,
                    })
                    if generation == 2:
                        current = replace(current, revision="3")
                        async with asyncio.timeout(10):
                            while runtime._close_task is None or not runtime._close_task.done():
                                await asyncio.sleep(0.05)
                        report["idle_generation_change_closed_runtime"] = not runtime.alive and not guard.valid
                        assert report["idle_generation_change_closed_runtime"]
                    await supervisor.close()
                    await guard.close()
                    assert not runtime.alive and not runtime.forced_cleanup

                report["stage"] = 3
                noauth = make_runtime(None)
                client = await noauth.start()
                auth = await client.get_auth_status()
                report["noauth_restart_has_no_fallback"] = not auth.isAuthenticated
                assert report["noauth_restart_has_no_fallback"]
                await noauth.close()
                assert not noauth.forced_cleanup
                assert permissions == 0 and events["tool.execution_start"] == 0
        finally:
            # Cleanup owners before private state disposal even after cancellation.
            cleanup_failed = False
            for supervisor in supervisors:
                try:
                    await supervisor.close()
                except Exception:
                    cleanup_failed = True
            for guard in guards:
                try:
                    await guard.close()
                except Exception:
                    cleanup_failed = True
            for runtime in runtimes:
                try:
                    await runtime.close()
                except Exception:
                    cleanup_failed = True
            report["all_runtimes_closed"] = all(not runtime.alive for runtime in runtimes)
            report["normal_cleanup"] = all(not runtime.forced_cleanup for runtime in runtimes)
            report["runtime_count"] = len(runtimes)
            report["source_read_count"] = reads
            report["tool_execution_count"] = events["tool.execution_start"]
            report["permission_request_count"] = permissions
            if not cleanup_failed:
                state.discard()
            report["private_state_discarded"] = not state_path.exists()
            current = token = None
            if cleanup_failed:
                raise RuntimeError("Account probe cleanup failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir = args.runtime_dir.resolve()
    logging.disable(logging.CRITICAL)
    report = {
        "result": "failed", "sandboxed": True, "sdk_version": "1.0.13", "runtime_version": "1.0.83",
        "live_turn_limit": 2, "real_oauth_refresh": False, "installation_token_tested": False,
        "database_account_store_tested": False, "distinct_live_accounts_tested": False,
    }
    start = time.monotonic()
    try:
        asyncio.run(run(args, report))
        report["result"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        trace = exc.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == __file__:
                report["failure_probe_line"] = trace.tb_lineno
            trace = trace.tb_next
    report["elapsed_seconds"] = round(time.monotonic() - start, 3)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
