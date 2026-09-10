#!/usr/bin/env python3
"""Two bounded model turns through the account-bound factory and durable resume.

Uses one real token, a synthetic scoped credential store and actual platform
security registration. This is not PostgreSQL/account-UI or OAuth-refresh proof.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import uuid

from probe import selected_token


async def run(args, report):
    with tempfile.TemporaryDirectory(prefix="otodock-local-factory-") as directory:
        root = Path(directory)
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        from auth.path_policy import SecurityContext
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount, netns_preflight
        from core.session.session_state import register_session_state, cleanup_session_permission_state
        from core.layers.copilot import local_session as module
        from core.layers.copilot.credentials import CopilotCredential, CopilotAccountScope, CredentialKind
        from core.layers.copilot.lease import CopilotLeaseGuard
        from core.layers.copilot.session_records import CopilotSessionRecords

        netns_preflight()
        agent = root / "data/agents/local-factory-probe"
        for name in ("workspace", "knowledge"):
            (agent / name).mkdir(parents=True)
        records_root, state_root = root / "records", root / "states"
        records_root.mkdir(mode=0o700)
        state_root.mkdir(mode=0o700)
        records = CopilotSessionRecords(records_root, state_root=state_root)
        builder = SandboxBuilder(SandboxConfig(
            role="manager", username="", agent_name=agent.name, is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=args.runtime_dir,
            host_claude_dir=agent / "workspace/.claude", config_visible=False,
            net_forwards=["1"], mcp_sandbox_mounts=[SandboxMount(
                str(args.runtime_dir), "/opt/copilot-runtime", "ro",
            )],
        ))
        platform_id = str(uuid.uuid4())
        context = SecurityContext(role="manager", username="", agent=agent.name,
                                  is_admin_agent=False, session_scope="agent", config_visible=False)
        register_session_state(platform_id, "auto", context)
        scope = CopilotAccountScope.personal("local-factory-user")
        current = CopilotCredential("local-factory-account", "synthetic-principal", "revision-one",
                                    CredentialKind.USER_TOKEN, selected_token())
        config = module.CopilotLocalSessionConfig(
            platform_id, current.account_id, scope, scope.user_sub, "gpt-5-mini", frozenset({"view"}),
            "Follow the current request. Do not use tools for this conversation.",
        )
        runtimes, guards, owners = [], [], []
        reads = 0
        original_runtime = module.SandboxedCopilotRuntime
        original_acquire = CopilotLeaseGuard.__dict__["acquire"]

        class ObservedRuntime(original_runtime):
            def __init__(self, *values, **options):
                super().__init__(*values, **options)
                runtimes.append(self)

        async def read(account_id, payer_scope):
            nonlocal reads
            reads += 1
            assert account_id == config.account_id and payer_scope == scope
            return current

        async def acquire(cls, account_id, payer_scope, *, on_invalid, **_options):
            credential = await read(account_id, payer_scope)
            guard = cls(credential, payer_scope, read_credential=read, on_invalid=on_invalid,
                        check_interval=0.1, read_timeout=5)
            guards.append(guard)
            await guard.start()
            return guard

        module.SandboxedCopilotRuntime = ObservedRuntime
        CopilotLeaseGuard.acquire = classmethod(acquire)
        marker = "OTO_FACTORY_" + secrets.token_hex(12).upper()
        try:
            async with asyncio.timeout(180):
                for generation in (1, 2):
                    owner = await module.CopilotLocalSession.open(
                        config, builder=builder, runtime_path=args.runtime_dir / "copilot-runtime",
                        records=records, resume=generation == 2, turn_timeout=60,
                    )
                    owners.append(owner)
                    if generation == 1:
                        # Reusing a currently held platform-session record cannot
                        # create a second runtime, even with the same payer.
                        before = len(runtimes)
                        try:
                            await module.CopilotLocalSession.open(
                                config, builder=builder, runtime_path=args.runtime_dir / "copilot-runtime",
                                records=records, resume=True,
                            )
                        except module.CopilotLocalSessionError:
                            report["concurrent_writer_rejected_before_runtime"] = len(runtimes) == before
                        assert report.get("concurrent_writer_rejected_before_runtime")
                    prompt = (f"Remember this marker exactly: {marker}. Reply with only that marker. Do not use tools."
                              if generation == 1 else "Reply with only the exact marker I asked you to remember. Do not use tools.")
                    events = [event async for event in owner.stream(prompt)]
                    assert sum(event.type == "done" for event in events) == 1
                    # CommonEvent text payloads are inspected only in memory.
                    remembered = marker in "".join(event.data.get("content", "") for event in events if event.type == "text")
                    assert remembered
                    report.setdefault("turns", []).append({
                        "generation": generation, "resumed": generation == 2,
                        "marker_present": remembered, "done_count": 1,
                        "tool_events": sum(event.type == "tool_use" for event in events),
                    })
                    assert report["turns"][-1]["tool_events"] == 0
                    if generation == 1:
                        await owner.close()
                        assert not runtimes[-1].alive and not runtimes[-1].forced_cleanup
                        # Fresh store object must reopen the durable inode-bound
                        # state; the old owner/record handle is no longer used.
                        records = CopilotSessionRecords(records_root, state_root=state_root)
                        before = len(runtimes)
                        try:
                            await module.CopilotLocalSession.open(
                                replace(config, model="different-model"), builder=builder,
                                runtime_path=args.runtime_dir / "copilot-runtime", records=records, resume=True,
                            )
                        except module.CopilotLocalSessionError:
                            report["profile_change_rejected_before_runtime"] = len(runtimes) == before
                        assert report.get("profile_change_rejected_before_runtime")
                        current = replace(current, revision="revision-two")
                    else:
                        current = replace(current, revision="revision-revoked")
                        async with asyncio.timeout(5):
                            while runtimes[-1].alive:
                                await asyncio.sleep(0.05)
                        await owner.close()
                        report["idle_credential_change_closed_runtime"] = True
                        before = len(runtimes)
                        try:
                            await module.CopilotLocalSession.open(
                                config, builder=builder, runtime_path=args.runtime_dir / "copilot-runtime",
                                records=records, resume=True,
                            )
                        except module.CopilotLocalSessionError:
                            report["uncertain_history_rejected_before_runtime"] = len(runtimes) == before
                        assert report.get("uncertain_history_rejected_before_runtime")
                assert len(runtimes) == 2
        finally:
            cleanup_failed = False
            for owner in owners:
                try:
                    await owner.close()
                except Exception:
                    cleanup_failed = True
            for runtime in runtimes:
                try:
                    await runtime.close()
                except Exception:
                    cleanup_failed = True
            for guard in guards:
                try:
                    await guard.close()
                except Exception:
                    cleanup_failed = True
            module.SandboxedCopilotRuntime = original_runtime
            CopilotLeaseGuard.acquire = original_acquire
            cleanup_session_permission_state(platform_id)
            report["scoped_store_reads"] = reads
            report["runtime_count"] = len(runtimes)
            report["normal_cleanup"] = (not cleanup_failed and all(
                not runtime.alive and not runtime.forced_cleanup for runtime in runtimes
            ))
            report["all_credential_observers_closed"] = all(not guard.valid for guard in guards)
            assert report["normal_cleanup"] and report["all_credential_observers_closed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir = args.runtime_dir.resolve()
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "live_turn_limit": 2, "sandboxed": True, "credential_store": "controlled scoped fixture",
              "platform_authority": "actual registered SecurityContext", "oauth_refresh": False}
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
