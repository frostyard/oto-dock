#!/usr/bin/env python3
"""Opt-in SDK -> real native PTY -> SDK history handoff, in disposable state.

Never changes the installed CLI or user configuration. Output contains only
bounded evidence, never raw PTY/RPC payloads or tokens. Linux PTY proof only;
this does not establish native tool policy or OtoDock terminal integration.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import tarfile
import tempfile
import termios
import time
import uuid

import psutil

CLI_SHA256 = 'ffbe1c429664b8a05efed67ecdb467123e40fcaa3c6c14ef9a98ba74da4687b7'
MARKER = 'OTO_TERMINAL_' + 'HISTORY'


def child_environment(root: Path) -> dict[str, str]:
    return {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'COPILOT_HOME': str(root / 'state'),
            'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'COPILOT_DISABLE_KEYTAR': '1',
            'COPILOT_SKIP_CLI_DOWNLOAD': '1', 'COPILOT_AUTO_UPDATE': 'false'}


def verify_cli_archive(archive: Path, executable: Path) -> None:
    """Verify both the pinned archive and the executable actually being run."""
    with archive.open('rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != CLI_SHA256:
            raise ValueError('Native CLI archive checksum mismatch')
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.name == 'copilot' and m.isfile()]
        if len(members) != 1:
            raise ValueError('Native CLI archive member mismatch')
        with tar.extractfile(members[0]) as source, executable.open('rb') as installed:
            if hashlib.file_digest(source, 'sha256').digest() != hashlib.file_digest(installed, 'sha256').digest():
                raise ValueError('Native CLI executable differs from verified archive')


def read_events(path: Path) -> list[dict]:
    """Ignore a partial final append while the native writer is running."""
    if not path.exists():
        return []
    events = []
    lines = path.read_text().splitlines()
    for index, line in enumerate(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise ValueError('Malformed completed history record') from None
    return events


def assistant_messages(events: list[dict]) -> list[str]:
    return [e.get('data', {}).get('content', '') for e in events
            if e.get('type') == 'assistant.message' and not e.get('agentId')
            and not e.get('data', {}).get('parentToolCallId')]


def require_no_writer() -> None:
    """Do not hand off while a runtime descendant is still alive."""
    for child in psutil.Process().children(recursive=True):
        with contextlib.suppress(psutil.NoSuchProcess):
            if child.status() != psutil.STATUS_ZOMBIE:
                raise RuntimeError('Previous writer still running')


def stop_tree(observed: dict, groups: set[int]) -> list[int]:
    for group in groups:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
    for child in observed.values():
        with contextlib.suppress(psutil.NoSuchProcess):
            child.kill()
    _, alive = psutil.wait_procs(list(observed.values()), timeout=2)
    result = []
    for child in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                result.append(child.pid)
    return result


def remaining_live(observed: dict) -> list[int]:
    _, alive = psutil.wait_procs(list(observed.values()), timeout=2)
    result = []
    for child in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                result.append(child.pid)
    return result


def native_turn(args, root: Path, session_id: str, token: str, report: dict) -> None:
    require_no_writer()
    history = root / 'state' / 'session-state' / session_id / 'events.jsonl'
    before = assistant_messages(read_events(history))
    if before != [MARKER]:
        raise ValueError('SDK history is not at the expected native session path')
    env = child_environment(root)
    env['COPILOT_GITHUB_TOKEN'] = token
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
    command = [str(args.cli), '--resume=' + session_id, '--model', 'gpt-5-mini',
               '--available-tools=', '--deny-tool=shell', '--deny-tool=write', '--no-ask-user',
               '--disable-builtin-mcps', '--no-custom-instructions', '--no-remote-export',
               '--no-auto-update', '--no-mouse', '--max-ai-credits', '30', '--log-level', 'none',
               '-i', 'Recall the exact marker in your previous reply. Reply only with that marker '
                     'followed immediately by -NATIVE. Do not call tools.']
    proc = subprocess.Popen(command, cwd=root / 'workspace', env=env,
                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    observed = {(proc.pid, psutil.Process(proc.pid).create_time()): psutil.Process(proc.pid)}
    deadline = time.monotonic() + 60
    byte_count = 0
    ansi = False
    resized = False
    try:
        while time.monotonic() < deadline:
            with contextlib.suppress(psutil.NoSuchProcess):
                observed.update(((p.pid, p.create_time()), p)
                                for p in psutil.Process(proc.pid).children(recursive=True))
            if not resized and byte_count:
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 100, 0, 0))
                os.killpg(proc.pid, signal.SIGWINCH)
                resized = True
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output = os.read(master, 65536)
                except OSError:
                    break
                byte_count += len(output)
                ansi = ansi or b'\x1b[' in output
            messages = assistant_messages(read_events(history))
            if messages == [MARKER, MARKER + '-NATIVE']:
                report['native_history_recalled_sdk_marker'] = True
                os.write(master, b'/exit\r')
                break
            if proc.poll() is not None:
                break
        else:
            raise TimeoutError('Native PTY turn exceeded sixty seconds')
        # Drain while exiting so the terminal cannot block on a full PTY buffer.
        exit_deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < exit_deadline:
            if select.select([master], [], [], 0.1)[0]:
                with contextlib.suppress(OSError):
                    os.read(master, 65536)
        report.update({'native_pty_output_bytes': byte_count, 'native_ansi_observed': ansi,
                       'native_resize_sent': resized, 'native_exit_code': proc.poll()})
        if not report.get('native_history_recalled_sdk_marker'):
            raise RuntimeError('Native CLI did not append expected history')
        if proc.poll() != 0:
            raise RuntimeError('Native CLI did not exit cleanly before SDK handoff')
        if any(e.get('type') == 'tool.execution_start' for e in read_events(history)):
            raise RuntimeError('Unexpected tool execution in native handoff history')
        report['native_tool_executions'] = 0
    finally:
        forced = bool(remaining_live(observed))
        report['native_cleanup'] = 'required_force_cleanup' if forced else 'passed'
        report['native_live_descendants_after_cleanup'] = stop_tree(observed, {proc.pid})
        proc.wait(timeout=2)
        os.close(master)
        if report['native_live_descendants_after_cleanup']:
            raise RuntimeError('Native descendant cleanup failed')
        if forced:
            raise RuntimeError('Native CLI required forced cleanup')
    require_no_writer()


async def sdk_turn(args, root, session_id, token, *, resume, report):
    from copilot import CopilotClient, RuntimeConnection
    from copilot.rpc import PermissionDecisionReject

    require_no_writer()
    client = CopilotClient(connection=RuntimeConnection.for_stdio(path=str(args.runtime)),
                          working_directory=str(root / 'workspace'), base_directory=str(root / 'state'),
                          env=child_environment(root), github_token=token,
                          use_logged_in_user=False, mode='empty', log_level='error')
    config = dict(available_tools=[], enable_config_discovery=False, enable_file_hooks=False,
                  enable_host_git_operations=False, enable_session_store=True,
                  on_permission_request=lambda *_: PermissionDecisionReject(feedback='Probe denies tools'))
    observed = {}

    async def track():
        while True:
            for child in psutil.Process().children(recursive=True):
                with contextlib.suppress(psutil.NoSuchProcess):
                    observed[(child.pid, child.create_time())] = child
            await asyncio.sleep(0.05)

    tracker = asyncio.create_task(track())
    try:
        async with asyncio.timeout(60):
            await client.start()
            status = await client.get_status()
            if status.version != '1.0.83' or status.protocol_version != 3:
                raise ValueError('Runtime pin mismatch')
            if resume:
                session = await client.resume_session(session_id, **config)
                prompt = 'Repeat only the exact most recent assistant reply, including its suffix. Do not use tools.'
                expected = MARKER + '-NATIVE'
            else:
                session = await client.create_session(session_id=session_id, model='gpt-5-mini',
                                                     session_limits={'max_ai_credits': 30.0}, **config)
                prompt = f'Reply only with {MARKER}. Do not call any tools.'
                expected = MARKER
            reply = await session.send_and_wait(prompt, timeout=45)
            if not reply or reply.data.content.strip() != expected:
                raise ValueError('History recall marker mismatch')
            report['sdk_resumed_native_history' if resume else 'sdk_initial_turn'] = True
            await session.disconnect()
    finally:
        try:
            await asyncio.wait_for(client.stop(), timeout=5)
        finally:
            tracker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tracker
            forced = bool(remaining_live(observed))
            report['resumed_sdk_cleanup' if resume else 'initial_sdk_cleanup'] = (
                'required_force_cleanup' if forced else 'passed'
            )
            survivors = stop_tree(observed, set())
            if survivors:
                raise RuntimeError('SDK descendants remain after cleanup')
            if forced:
                raise RuntimeError('SDK required forced cleanup before handoff')
    require_no_writer()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--cli-archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--use-gh-token', action='store_true')
    args = parser.parse_args()
    if not args.live or not args.use_gh_token:
        parser.error('Explicit --live --use-gh-token required')
    args.runtime, args.cli = args.runtime.resolve(), args.cli.resolve()
    logging.disable(logging.CRITICAL)
    report = {'result': 'failed', 'sdk_version': '1.0.13', 'cli_version': '1.0.83',
              'live': True, 'sandboxed': False, 'sequential_writer_ownership': False}
    start = time.monotonic()
    try:
        if importlib.metadata.version('github-copilot-sdk') != '1.0.13':
            raise ValueError('SDK pin mismatch')
        verify_cli_archive(args.cli_archive, args.cli)
        with tempfile.TemporaryDirectory(prefix='otodock-copilot-version-') as version_dir:
            version = subprocess.run([str(args.cli), '--version'], capture_output=True,
                                     env=child_environment(Path(version_dir)), cwd=version_dir,
                                     text=True, timeout=5, check=True).stdout
        if 'GitHub Copilot CLI 1.0.83.' not in version:
            raise ValueError('Native CLI version mismatch')
        token = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True,
                               check=True, timeout=10).stdout.strip()
        if not token.startswith(('gho_', 'ghu_', 'github_pat_')):
            raise ValueError('Unsupported token type')
        with tempfile.TemporaryDirectory(prefix='otodock-copilot-terminal-') as directory:
            root = Path(directory)
            (root / 'workspace').mkdir()
            (root / 'state').mkdir()
            (root / 'state' / 'config.json').write_text(json.dumps({
                'trustedFolders': [str(root / 'workspace')], 'autoUpdate': False,
                'banner': 'never', 'showTipsOnStartup': False, 'disableAllHooks': True,
                'memory': False, 'ide': {'autoConnect': False},
                'customAgents': {'defaultLocalOnly': True},
            }))
            session_id = str(uuid.uuid4())
            asyncio.run(sdk_turn(args, root, session_id, token, resume=False, report=report))
            native_turn(args, root, session_id, token, report)
            asyncio.run(sdk_turn(args, root, session_id, token, resume=True, report=report))
            history = root / 'state' / 'session-state' / session_id / 'events.jsonl'
            if any(e.get('type') == 'tool.execution_start' for e in read_events(history)):
                raise RuntimeError('Unexpected tool execution in complete handoff history')
            report['total_tool_executions'] = 0
            report['sequential_writer_ownership'] = True
        report['result'] = 'passed'
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    report['elapsed_seconds'] = round(time.monotonic() - start, 3)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
