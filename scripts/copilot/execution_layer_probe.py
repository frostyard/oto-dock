#!/usr/bin/env python3
"""Three bounded live turns through the unregistered local Copilot execution layer.

Uses actual sandbox resolution, private storage, platform permission state and
credential observers. Only account storage and database-backed sandbox metadata
are controlled fixtures. The selected gh credential remains in memory; this is
not OAuth refresh, PostgreSQL, account UI, or production registration evidence.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
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
    with tempfile.TemporaryDirectory(prefix="otodock-execution-layer-") as directory:
        root = Path(directory)
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        from auth.path_policy import SecurityContext
        from core.layers.copilot import local_session as factory
        from core.layers.copilot.credentials import CopilotAccountScope, CopilotCredential, CredentialKind
        from core.layers.copilot.layer import CopilotAgentConfig, CopilotExecutionLayer, CopilotLayerError
        from core.layers.copilot.lease import CopilotLeaseGuard
        from core.layers.copilot.sandbox_home import CopilotSandboxHomes
        from core.layers.copilot.session_records import CopilotSessionRecords
        from core.sandbox.sandbox import netns_preflight
        from core.session import session_state as state
        from services.mcp import mcp_registry
        from storage import db_knowledge_libraries

        netns_preflight()
        agent = root / "data/agents/execution-layer-probe"
        for name in ("workspace", "knowledge"):
            (agent / name).mkdir(parents=True)
        records_root, state_root, homes_root = (root / name for name in ("records", "states", "homes"))
        for path in (records_root, state_root, homes_root):
            path.mkdir(mode=0o700)
        scope = CopilotAccountScope.personal("execution-layer-user")
        current = CopilotCredential(
            "execution-layer-account", "synthetic-principal", "revision-one",
            CredentialKind.USER_TOKEN, selected_token(),
        )
        config = CopilotAgentConfig(
            agent_name=agent.name, user_sub=scope.user_sub,
            account_id=current.account_id, account_scope=scope,
            client_type="dashboard", model="gpt-5-mini", enabled_tools=frozenset({"view"}),
            permission_mode="dontAsk",
            system_prompt="Follow the current request. Do not use tools for this conversation.",
            security_context=SecurityContext(
                role="manager", username="", agent=agent.name, is_admin_agent=False,
                session_scope="agent", available_scopes=("agent",), config_visible=False,
                principal="user", target_kind="local",
            ),
        )
        platform_id, aborted_id = str(uuid.uuid4()), str(uuid.uuid4())
        runtimes, guards, homes, owners, installations = [], [], [], [], []
        reads = 0
        waiter = stream = None
        original_runtime = factory.SandboxedCopilotRuntime
        original_acquire = CopilotLeaseGuard.__dict__["acquire"]
        original_egress = mcp_registry.resolve_sandbox_egress
        original_attachments = db_knowledge_libraries.attachments_for_consumer

        class ObservedRuntime(original_runtime):
            """Instrumentation delegates all sandbox/process work unchanged."""

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
            try:
                await guard.start()
            except BaseException:
                await guard.close()
                raise
            return guard

        async def new_layer():
            if args.provisioned_root is not None:
                from core.layers.copilot.provisioned_layer import open_provisioned_layer
                installation = open_provisioned_layer(args.provisioned_root)
                layer = await installation.__aenter__()
                installations.append(installation)
                return layer
            scratch = CopilotSandboxHomes(homes_root)
            homes.append(scratch)
            return CopilotExecutionLayer(
                runtime_path=args.runtime_dir / "copilot-runtime",
                records=CopilotSessionRecords(records_root, state_root=state_root), homes=scratch,
            )

        async def start(layer, session_id, *, resume=False):
            # Include failed attempts in final idempotent cleanup as well.
            owners.append((layer, session_id))
            await layer.start_session(session_id, replace(config, resume=resume))
            from core.session.owned_sessions import get_owned_session
            from core.session.session_manager import is_session_registered
            claim = get_owned_session(session_id)
            assert claim is not None and claim.active and claim.engine == "copilot-cli"
            assert is_session_registered(session_id)

        async def clean_closed(layer, session_id):
            async with asyncio.timeout(15):
                while (not await layer.is_session_process_dead(session_id)
                       or state.get_session_security(session_id) is not None):
                    await asyncio.sleep(0.05)
            assert not await layer.is_session_alive(session_id)
            from core.session.owned_sessions import get_owned_session
            assert get_owned_session(session_id) is None

        async def marker_turn(layer, prompt, *, generation):
            async with asyncio.timeout(60), layer.session_lock(platform_id):
                events = [event async for event in layer.send_message(platform_id, prompt)]
            done = sum(event.type == "done" for event in events)
            tools = sum(event.type == "tool_use" for event in events)
            remembered = marker in "".join(
                event.data.get("content", "") for event in events if event.type == "text"
            )
            report.setdefault("turns", []).append({
                "generation": generation, "resumed": generation == 2,
                "marker_present": remembered, "done_count": done, "tool_events": tools,
            })
            assert remembered and done == 1 and tools == 0

        async def rejected_resume(session_id):
            candidate = await new_layer()
            before = len(runtimes)
            rejected = False
            try:
                await start(candidate, session_id, resume=True)
            except CopilotLayerError:
                rejected = True
            assert rejected and len(runtimes) == before
            await clean_closed(candidate, session_id)

        factory.SandboxedCopilotRuntime = ObservedRuntime
        CopilotLeaseGuard.acquire = classmethod(acquire)
        mcp_registry.resolve_sandbox_egress = lambda *_values, **_options: (["1"], [])
        db_knowledge_libraries.attachments_for_consumer = lambda *_values, **_options: []
        marker = "OTO_EXECUTION_" + secrets.token_hex(12).upper()
        try:
            async with asyncio.timeout(180):
                first = await new_layer()
                await start(first, platform_id)
                registered = state.get_session_security(platform_id)
                assert registered is not None
                assert await first.is_session_alive(platform_id)
                assert not await first.is_session_process_dead(platform_id)
                duplicate = await new_layer()
                before = len(runtimes)
                try:
                    await start(duplicate, platform_id)
                except CopilotLayerError:
                    report["duplicate_rejected_before_runtime"] = len(runtimes) == before
                assert report.get("duplicate_rejected_before_runtime")
                report["duplicate_preserved_context"] = state.get_session_security(platform_id) is registered
                assert report["duplicate_preserved_context"]
                await marker_turn(
                    first, f"Remember this marker exactly: {marker}. Reply with only that marker. Do not use tools.",
                    generation=1,
                )
                from core.session.owned_sessions import get_owned_session
                assert await get_owned_session(platform_id).close()
                await clean_closed(first, platform_id)
                assert not runtimes[-1].alive and not runtimes[-1].forced_cleanup
                report["normal_close_removed_context_and_joined"] = True
                report["normal_close_through_owned_registry"] = True

                current = replace(current, revision="revision-two")
                second = await new_layer()
                await start(second, platform_id, resume=True)
                await marker_turn(
                    second, "Reply with only the exact marker I asked you to remember. Do not use tools.",
                    generation=2,
                )
                request_id = str(uuid.uuid4())
                waiter = asyncio.create_task(state.wait_for_permission(request_id, platform_id, timeout=30))
                async with asyncio.timeout(5):
                    while state.get_permission_request_session(request_id) != platform_id:
                        await asyncio.sleep(0)
                assert not waiter.done()
                current = replace(current, revision="revision-revoked")
                await clean_closed(second, platform_id)
                report["idle_revocation_removed_context_and_joined"] = not runtimes[-1].alive
                report["idle_revocation_denied_platform_waiter"] = await asyncio.wait_for(waiter, 5) is False
                assert report["idle_revocation_removed_context_and_joined"]
                assert report["idle_revocation_denied_platform_waiter"]
                await rejected_resume(platform_id)
                report["revoked_history_rejected_before_runtime"] = True

                current = replace(current, revision="revision-three")
                third = await new_layer()
                await start(third, aborted_id)
                async with asyncio.timeout(60), third.session_lock(aborted_id):
                    stream = third.send_message(
                        aborted_id,
                        "Without using tools, write 100 numbered short sentences explaining simple arithmetic. "
                        "Start immediately with sentence one and continue until all 100 are written.",
                    )
                    first_text = False
                    done = tools = 0
                    async for event in stream:
                        done += event.type == "done"
                        tools += event.type == "tool_use"
                        if event.type == "text" and event.data.get("content"):
                            first_text = True
                            break
                    assert first_text and done == 0 and tools == 0
                    assert await third.is_session_alive(aborted_id)
                    report["paused_producer_first_text_received"] = True
                    # Intentionally leave the producer iterator paused with its
                    # public session lock held throughout the hard stop.
                    if args.provisioned_root is not None:
                        installation = installations[-1]
                        await asyncio.wait_for(installation.__aexit__(None, None, None), 20)
                        installations.pop()
                        report["context_exit_joined_paused_active_session"] = True
                    else:
                        result = await asyncio.wait_for(third.abort(aborted_id), 20)
                        assert result is False
                        report["paused_producer_abort_returned_false"] = True
                    await clean_closed(third, aborted_id)
                    assert not runtimes[-1].alive and not runtimes[-1].forced_cleanup
                    await stream.aclose()
                    stream = None
                report["turns"].append({
                    "generation": 3, "resumed": False, "first_text_received": first_text,
                    "done_before_abort": done, "tool_events": tools,
                })
                report["paused_producer_hard_stop_joined_and_removed_context"] = True
                await rejected_resume(aborted_id)
                report["aborted_history_rejected_before_runtime"] = True
                assert len(runtimes) == 3
        finally:
            cleanup_failed = False
            for layer, session_id in owners:
                try:
                    await layer.close_session(session_id)
                except Exception:
                    cleanup_failed = True
            if stream is not None:
                with suppress(Exception):
                    await stream.aclose()
            if waiter is not None and not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            # Capture layer results before safety cleanup can mask an ownership
            # leak. Instrumented runtimes still perform their real close path.
            report["layer_cleanup_joined_all_runtimes"] = all(not runtime.alive for runtime in runtimes)
            report["layer_cleanup_removed_all_contexts"] = all(
                state.get_session_security(session_id) is None for session_id in (platform_id, aborted_id)
            )
            report["layer_cleanup_released_all_claims"] = all([
                await layer.is_session_process_dead(session_id) for layer, session_id in owners
            ])
            report["layer_cleanup_stopped_all_credential_observers"] = all(
                not guard.valid and (guard._watch_task is None or guard._watch_task.done()) for guard in guards
            )
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
            for installation in reversed(installations):
                try:
                    await installation.__aexit__(None, None, None)
                except Exception:
                    cleanup_failed = True
            report["provisioned_layer_contexts_closed"] = bool(installations) and not cleanup_failed
            for scratch in homes:
                scratch.close()
            factory.SandboxedCopilotRuntime = original_runtime
            CopilotLeaseGuard.acquire = original_acquire
            mcp_registry.resolve_sandbox_egress = original_egress
            db_knowledge_libraries.attachments_for_consumer = original_attachments
            report["scoped_store_reads"] = reads
            report["runtime_count"] = len(runtimes)
            report["normal_cleanup"] = not cleanup_failed and all(
                not runtime.alive and not runtime.forced_cleanup for runtime in runtimes
            )
            assert all(report[key] for key in (
                "normal_cleanup", "layer_cleanup_joined_all_runtimes", "layer_cleanup_removed_all_contexts",
                "layer_cleanup_released_all_claims", "layer_cleanup_stopped_all_credential_observers",
            ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--runtime-dir", type=Path)
    source.add_argument("--provisioned-root", type=Path,
                        help="Verify and open an installation made by provision_local.py")
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runtime_dir is not None:
        args.runtime_dir = args.runtime_dir.resolve()
    logging.disable(logging.CRITICAL)
    report = {
        "result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
        "live_turn_limit": 3, "turn_deadline_seconds": 60, "flow_deadline_seconds": 180,
        "per_session_credit_limit": 30, "sandboxed": True, "actual_execution_layer": True,
        "actual_sandbox_resolver": True, "platform_registration": "actual layer-owned context",
        "actual_owned_registry": True,
        "verified_provisioning": args.provisioned_root is not None,
        "credential_store": "controlled scoped fixture", "oauth_refresh": False,
        "postgresql_account_store": False,
    }
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
