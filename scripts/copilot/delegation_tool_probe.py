#!/usr/bin/env python3
"""Bounded model turns qualifying the fixed delegation tool and its ownership.

The sandbox, SDK, private history, leases and callback ownership are real.
Authority answers, account storage and child work are controlled fixtures.
This does not launch scheduler workers or prove main-chat delegation delivery.
"""

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
import uuid

from probe import selected_token


async def run(args, report):
    with tempfile.TemporaryDirectory(prefix="otodock-delegation-tool-") as directory:
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
        from core.layers.copilot.permissions import CopilotPermissionBridge
        from core.layers.copilot.provisioning import load, check_sdk
        from core.layers.copilot.session_records import CopilotSessionRecords

        provisioned = load(args.provisioned_root)
        check_sdk()
        netns_preflight()
        runtime_dir = provisioned.runtime_path.parent
        agent = root / "data/agents/delegation-tool-probe"
        for name in ("workspace", "knowledge"):
            (agent / name).mkdir(parents=True)
        homes = root / "scratch"
        homes.mkdir(mode=0o700)
        records_root, state_root = root / "records", root / "states"
        records_root.mkdir(mode=0o700)
        state_root.mkdir(mode=0o700)
        records = CopilotSessionRecords(records_root, state_root=state_root)
        builder = SandboxBuilder(SandboxConfig(
            role="manager", username="", agent_name=agent.name, is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=root / "empty-mcps", host_claude_dir=homes,
            config_visible=False, isolated_config_home=True, net_forwards=["1"],
            trusted_runtime_mounts=[SandboxMount(str(runtime_dir), "/opt/copilot-runtime", "ro")],
        ))
        platform_id = str(uuid.uuid4())
        context = SecurityContext(role="manager", username="", agent=agent.name,
                                  is_admin_agent=False, session_scope="agent", config_visible=False)
        register_session_state(platform_id, "default", context)
        scope = CopilotAccountScope.personal("delegation-probe-user")
        current = CopilotCredential("delegation-probe-account", "synthetic-principal", "revision-one",
                                    CredentialKind.USER_TOKEN, selected_token())
        config = module.CopilotLocalSessionConfig(
            platform_id, current.account_id, scope, scope.user_sub, "gpt-5-mini", frozenset({"view"}),
            "Follow requests exactly. Use oto_delegate when asked; its result is the worker's report. "
            "Call it once per request, and never retry a failed delegation.",
            delegation_targets=("repo", "qa"),
        )
        runtimes, guards, owners, sessions = [], [], [], []
        counts, traces = Counter(), []
        phase = "allow"
        approval_waiting, approval_release, denial_delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        platform_ids = [platform_id]
        worker_waiting, worker_joined = asyncio.Event(), asyncio.Event()
        original_runtime = module.SandboxedCopilotRuntime
        original_policy = module.CopilotNativeToolPolicy
        original_bind = module.bind_platform_authority
        original_acquire = CopilotLeaseGuard.__dict__["acquire"]
        marker = "OTO_DELEGATION_" + secrets.token_hex(12).upper()
        turn = None

        class ObservedRuntime(original_runtime):
            def __init__(self, *values, **options):
                super().__init__(*values, **options)
                runtimes.append(self)

        class ObservedPolicy(original_policy):
            async def _open_session(self, *values, **options):
                session = await super()._open_session(*values, **options)
                sessions.append(session)
                return session

            async def on_pre_tool_use(self, payload, invocation):
                counts[phase + "_native_hooks"] += 1
                traces.append(phase + ":native_hook")
                return await super().on_pre_tool_use(payload, invocation)

            async def on_permission_request(self, request, invocation):
                counts[phase + "_native_permissions"] += 1
                traces.append(phase + ":native_permission")
                return await super().on_permission_request(request, invocation)

        async def read(account_id, payer_scope):
            assert account_id == config.account_id and payer_scope == scope
            counts["credential_reads"] += 1
            return current

        async def acquire(cls, account_id, payer_scope, *, on_invalid, **_):
            guard = cls(await read(account_id, payer_scope), payer_scope, read_credential=read,
                        on_invalid=on_invalid, check_interval=.1, read_timeout=5)
            guards.append(guard)
            await guard.start()
            return guard

        async def decide(name, values):
            assert name == "mcp__delegation-mcp__delegate" and values["agent"] == "repo"
            counts[phase + "_host_authority"] += 1
            traces.append(phase + ":host_authority")
            if phase == "deny":
                approval_waiting.set()
                await approval_release.wait()
                denial_delivered.set()
                return {"decision": "deny"}
            return {"decision": "allow"}

        def bind(_session_id, requests, *, working_directory, owner_valid, **_):
            return CopilotPermissionBridge(requests, decide=decide, context_valid=owner_valid,
                                           working_directory=working_directory)

        async def worker(call_id, values):
            assert call_id and values["agent"] == "repo"
            counts[phase + "_worker_calls"] += 1
            traces.append(phase + ":worker")
            if phase == "cancel":
                worker_waiting.set()
                try:
                    await asyncio.Future()
                finally:
                    worker_joined.set()
                    traces.append("cancel:worker_joined")
            return marker

        async def collect(owner, prompt):
            counts["model_turns"] += 1
            assert counts["model_turns"] <= 2
            events = [event async for event in owner.stream(prompt)]
            return (sum(event.type == "done" for event in events),
                    "".join(event.data.get("content", "") for event in events if event.type == "text"))

        def prompt(label):
            return ('Call oto_delegate exactly once with agent="repo", name="' + label
                    + '", prompt="Return the requested fixture report". '
                    'Then return only its report. If it fails, say DENIED and do not retry.')

        module.SandboxedCopilotRuntime = ObservedRuntime
        module.CopilotNativeToolPolicy = ObservedPolicy
        module.bind_platform_authority = bind
        CopilotLeaseGuard.acquire = classmethod(acquire)
        try:
            async with asyncio.timeout(180):
                if args.phase == "history":
                    report["phase"] = "open"
                    owner = await module.CopilotLocalSession.open(
                        config, builder=builder, runtime_path=provisioned.runtime_path, records=records,
                        delegate_handler=worker, turn_timeout=45, on_owner=owners.append,
                    )
                    report["phase"] = "allow"
                    done, text = await collect(owner, prompt("Report"))
                    assert done == 1 and marker in text and counts["allow_worker_calls"] == 1
                    assert counts["allow_host_authority"] == 1 and counts["allow_native_hooks"] == 1
                    assert traces.index("allow:native_hook") < traces.index("allow:host_authority") < traces.index("allow:worker")
                    report["native_model_tool_success"] = True
                    await owner.close()
                    assert owner.usage_source_closed and not runtimes[-1].forced_cleanup
                    records = CopilotSessionRecords(records_root, state_root=state_root)
                    before = len(runtimes)
                    try:
                        await module.CopilotLocalSession.open(
                            replace(config, delegation_targets=("qa",)), builder=builder,
                            runtime_path=provisioned.runtime_path, records=records, resume=True,
                            delegate_handler=worker, on_owner=owners.append,
                        )
                    except module.CopilotLocalSessionError:
                        report["changed_roster_rejected_before_runtime"] = len(runtimes) == before
                    assert report.get("changed_roster_rejected_before_runtime")
                    current = replace(current, revision="revision-two")
                    phase = "resume"
                    report["phase"] = phase
                    owner = await module.CopilotLocalSession.open(
                        config, builder=builder, runtime_path=provisioned.runtime_path, records=records,
                        resume=True, delegate_handler=worker, turn_timeout=45, on_owner=owners.append,
                    )
                    done, text = await collect(owner, "Reply only with the exact OTO_DELEGATION marker the successful worker returned earlier. Do not call tools.")
                    assert done == 1 and marker in text and counts["resume_worker_calls"] == 0
                    report["cold_resume_remembers_worker_result"] = True
                    await owner.close()
                    assert len(runtimes) == 2
                else:
                    for phase in ("deny", "cancel"):
                        report["phase"] = phase
                        sid = str(uuid.uuid4())
                        platform_ids.append(sid)
                        register_session_state(sid, "default", context)
                        owner = await module.CopilotLocalSession.open(
                            replace(config, platform_session_id=sid), builder=builder,
                            runtime_path=provisioned.runtime_path, records=records,
                            delegate_handler=worker, turn_timeout=45, on_owner=owners.append,
                        )
                        turn = asyncio.create_task(collect(owner, prompt("Controlled")))
                        if phase == "deny":
                            await asyncio.wait_for(approval_waiting.wait(), 35)
                            assert counts["deny_worker_calls"] == 0 and not turn.done()
                            approval_release.set()
                            await asyncio.wait_for(denial_delivered.wait(), 5)
                            async with asyncio.timeout(5):
                                while owner._supervisor.callbacks.pending_ids:
                                    await asyncio.sleep(.01)
                            assert counts["deny_host_authority"] >= 1 and counts["deny_worker_calls"] == 0
                            report["held_approval_denied_without_dispatch"] = True
                        else:
                            await asyncio.wait_for(worker_waiting.wait(), 35)
                            assert counts["cancel_worker_calls"] == 1
                        # Do not rely on a model obeying no-retry instructions:
                        # rejection/cancellation qualify side-effect prevention,
                        # then the owner explicitly ends the controlled turn.
                        await owner.close()
                        await asyncio.gather(turn, return_exceptions=True)
                        turn = None
                        assert owner.usage_source_closed and not runtimes[-1].alive
                        if phase == "cancel":
                            assert worker_joined.is_set() and not owner._supervisor.callbacks.pending_ids
                            report["held_worker_cancelled_and_joined"] = True
                    assert len(runtimes) == 2
        finally:
            approval_release.set()
            failed = False
            for owner in owners:
                try:
                    await owner.close()
                except Exception:
                    failed = True
            if turn is not None:
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            for runtime in runtimes:
                try:
                    await runtime.close()
                except Exception:
                    failed = True
            for guard in guards:
                await guard.close()
            module.SandboxedCopilotRuntime = original_runtime
            module.CopilotNativeToolPolicy = original_policy
            module.bind_platform_authority = original_bind
            CopilotLeaseGuard.acquire = original_acquire
            for sid in platform_ids:
                cleanup_session_permission_state(sid)
            report["counts"] = dict(counts)
            report["callback_sequence"] = traces
            report["runtime_count"] = len(runtimes)
            report["normal_cleanup"] = not failed and all(not value.alive and not value.forced_cleanup for value in runtimes)
            report["credential_observers_closed"] = all(not guard.valid for guard in guards)
            assert report["normal_cleanup"] and report["credential_observers_closed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provisioned-root", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("history", "controls"), required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "sandboxed": True, "model_turn_limit": 2, "qualification_phase": args.phase, "deadline_seconds": 180,
              "authority": "controlled answers with real owner-context and lease checks",
              "worker": "controlled async host callback; no scheduler child",
              "credential_store": "in-memory scoped fixture; explicit existing GitHub token"}
    started = time.monotonic()
    try:
        asyncio.run(run(args, report))
        report["result"] = "passed"
    except Exception as error:
        report["error_type"] = type(error).__name__
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
