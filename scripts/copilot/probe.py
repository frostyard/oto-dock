#!/usr/bin/env python3
"""Bounded SDK compatibility probe; never imports or starts the OtoDock app.

Default: no credentials, inference, or tools. --live --use-gh-token opts into
three small inference turns using the explicitly selected GitHub CLI identity.
This transport/policy probe is separate from sandbox_probe.py: it does not
establish OtoDock sandbox enforcement or production adapter compatibility.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import contextlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import psutil

SDK_VERSION = "1.0.13"
RUNTIME_VERSION = "1.0.83"
PROTOCOL_VERSION = 3
MARKER = "OTO_COPILOT_PROBE_OK"


def child_environment(root: Path) -> dict[str, str]:
    """Do not inherit ambient inference tokens, config paths, or repo secrets."""
    return {
        "PATH": os.defpath,
        "HOME": str(root),
        "COPILOT_HOME": str(root / "state"),
        "COPILOT_DISABLE_KEYTAR": "1",
        "COPILOT_SKIP_CLI_DOWNLOAD": "1",
    }


def selected_token() -> str:
    result = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
        check=True,
    )
    token = result.stdout.strip()
    if not token.startswith(("gho_", "ghu_", "github_pat_")):
        raise ValueError("The selected GitHub token type is unsupported by Copilot")
    return token


def stop_descendants(processes: list[psutil.Process]) -> bool:
    """Only reap descendants captured from this dedicated probe process."""
    for proc in reversed(processes):
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.terminate()
    _, alive = psutil.wait_procs(processes, timeout=2)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.kill()
    _, alive = psutil.wait_procs(alive, timeout=2)
    return not alive


async def probe(args: argparse.Namespace, report: dict) -> None:
    from copilot import CopilotClient, RuntimeConnection, ToolSet
    from copilot.rpc import PermissionDecisionReject

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
    from core.layers.copilot.translator import CopilotEventTranslator

    version = importlib.metadata.version("github-copilot-sdk")
    if version != SDK_VERSION:
        raise ValueError(f"Expected SDK {SDK_VERSION}; got {version}")
    report["sdk_version"] = version
    token = selected_token() if args.use_gh_token else None
    events: Counter = Counter()
    permissions: Counter = Counter()
    denied_files: set[Path] = set()
    observed: dict[tuple[int, float], psutil.Process] = {}
    translated: Counter = Counter()
    translation_errors: list[dict[str, str]] = []
    translators = {name: CopilotEventTranslator() for name in ("main", "denial")}

    def observe_children():
        for proc in psutil.Process().children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                observed[(proc.pid, proc.create_time())] = proc

    async def track_children():
        while True:
            observe_children()
            await asyncio.sleep(0.05)

    def deny(request, _invocation):
        permissions[str(request.kind)] += 1
        if request.kind == "write":
            denied_files.add(Path(request.file_name).resolve())
        return PermissionDecisionReject(feedback="Denied by the compatibility probe.")

    def event_handler(name):
        def event_received(event):
            kind = event.raw_type or event.type.value
            events[kind] += 1
            try:
                for common_event in translators[name].translate(event.to_dict()):
                    translated[common_event.type] += 1
            except ValueError as exc:
                translation_errors.append({"event_type": kind, "error_type": type(exc).__name__})
        return event_received

    with tempfile.TemporaryDirectory(prefix="otodock-copilot-probe-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        state = root / "state"
        state.mkdir()

        def client_factory():
            return CopilotClient(
                connection=RuntimeConnection.for_stdio(path=str(args.runtime)),
                working_directory=str(workspace), base_directory=str(state),
                env=child_environment(root), github_token=token,
                use_logged_in_user=False, mode="empty", log_level="error",
            )

        config = {
            "available_tools": [], "on_permission_request": deny,
            "enable_config_discovery": False, "enable_file_hooks": False,
            "enable_host_git_operations": False, "enable_session_store": True,
            "streaming": True, "on_event": event_handler("main"),
        }
        client = client_factory()
        tracker = asyncio.create_task(track_children())
        try:
            async with asyncio.timeout(args.timeout):
                await client.start()
                status = await client.get_status()
                report["runtime_version"] = status.version
                report["protocol_version"] = status.protocol_version
                assert status.version == RUNTIME_VERSION, "runtime version mismatch"
                assert status.protocol_version == PROTOCOL_VERSION, "protocol mismatch"
                ping = await client.ping("otodock")
                assert ping.message == "pong: otodock"
                report["ping"] = "passed"
                auth = await client.get_auth_status()
                report["authenticated"] = auth.isAuthenticated
                assert auth.isAuthenticated == bool(token), "unexpected authentication"

                session = await client.create_session(
                    session_id="oto-compatibility-probe", model=args.model, **config,
                    # Runtime 1.0.83 rejects limits below 30 credits. The probe
                    # also bounds wall time and submits only three short turns.
                    session_limits={"max_ai_credits": 30.0},
                )
                report["session_create"] = "passed"
                if args.live:
                    reply = await session.send_and_wait(
                        f"Reply with exactly {MARKER}. Do not call any tools.", timeout=45,
                    )
                    assert reply and reply.data.content.strip() == MARKER
                    report["inference"] = "passed"

                await session.disconnect()
                if args.live:
                    await client.stop()
                    client = client_factory()
                    await client.start()
                    resumed = await client.resume_session("oto-compatibility-probe", **config)
                    report["cold_resume"] = "passed"
                    reply = await resumed.send_and_wait(
                        "Repeat the exact marker from your previous reply. Do not use tools.",
                        timeout=45,
                    )
                    assert reply and reply.data.content.strip() == MARKER
                    report["resumed_history"] = "passed"
                    await resumed.disconnect()
                else:
                    # The runtime does not retain a useful resumable history
                    # until a turn occurs; no-auth startup is not a resume test.
                    report["cold_resume"] = "not_tested_without_inference"

                if args.live:
                    permissions.clear()
                    denied_files.clear()
                    deny_config = {**config, "available_tools": ToolSet().add_builtin("create"),
                                   "on_event": event_handler("denial")}
                    denied_session = await client.create_session(
                        session_id="oto-denial-probe", model=args.model, **deny_config,
                        session_limits={"max_ai_credits": 30.0},
                    )
                    target = workspace / "must-not-exist.txt"
                    await denied_session.send_and_wait(
                        f"Use the create tool exactly once to write PROBE to {target}. "
                        "If permission is denied, stop immediately and say DENIED. "
                        "Do not retry or use other tools.", timeout=45,
                    )
                    assert permissions["write"] and target.resolve() in denied_files, (
                        "target write permission not observed; denial unproven"
                    )
                    assert not target.exists(), "denied file creation executed"
                    report["native_file_denial"] = "passed"
                    await denied_session.disconnect()

                observe_children()
                children = list(observed.values())
                before = {}
                for proc in children:
                    with contextlib.suppress(psutil.NoSuchProcess):
                        cpu = proc.cpu_times()
                        before[proc.pid] = cpu.user + cpu.system
                start = time.monotonic()
                await asyncio.sleep(2)
                delta = 0.0
                for proc in children:
                    with contextlib.suppress(psutil.NoSuchProcess):
                        cpu = proc.cpu_times()
                        delta += max(0, cpu.user + cpu.system - before.get(proc.pid, 0))
                report["sampled_idle_cpu_cores"] = round(delta / (time.monotonic() - start), 3)
                assert not translation_errors, "supported runtime event failed translation"
        finally:
            try:
                await asyncio.wait_for(client.stop(), timeout=10)
            finally:
                observe_children()
                tracker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tracker
                _, remaining = psutil.wait_procs(list(observed.values()), timeout=2)
                report["sdk_cleanup"] = "passed" if not remaining else "required_force_cleanup"
                report["tracked_descendants_reaped"] = stop_descendants(remaining)
                report["event_counts"] = dict(sorted(events.items()))
                report["common_event_counts"] = dict(sorted(translated.items()))
                report["translation_errors"] = translation_errors
                report["denied_permission_kinds"] = dict(sorted(permissions.items()))
                assert report["tracked_descendants_reaped"], "tracked descendants survived cleanup"
                assert report["sdk_cleanup"] == "passed", "SDK required forced process cleanup"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--use-gh-token", action="store_true")
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if args.live != args.use_gh_token:
        parser.error("live testing requires both --live and --use-gh-token")
    if not args.runtime.is_file():
        parser.error("runtime must be an existing provisioned executable")
    if not 1 <= args.timeout <= 300:
        parser.error("timeout must be between 1 and 300 seconds")
    args.runtime = args.runtime.resolve()
    logging.disable(logging.CRITICAL)  # Reports never serialize raw RPC errors or credentials.
    report = {"live": args.live, "sandboxed": False, "result": "failed"}
    try:
        asyncio.run(probe(args, report))
        report["result"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
