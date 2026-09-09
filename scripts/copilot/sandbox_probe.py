#!/usr/bin/env python3
"""Credential-free C1 probe using OtoDock's real local sandbox launcher.

Run with the proxy's Python dependencies installed. The runtime directory is
mounted read-only after /tmp is shadowed; no host policy changes are attempted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import psutil


INNER = r'''
import errno, json, os, select, subprocess, sys, time
from pathlib import Path

results = {}
Path('/workspace/write-check').write_text('isolated workspace')
results['workspace_write'] = True
for label, path in [('knowledge', '/knowledge/write-check'),
                    ('runtime', '/opt/copilot-runtime/write-check')]:
    try:
        Path(path).write_text('must be denied')
    except OSError as exc:
        results[label + '_write_denied'] = exc.errno in (errno.EROFS, errno.EACCES)
        results[label + '_errno'] = exc.errno
    else:
        results[label + '_write_denied'] = False
print(json.dumps({'filesystem': results}), flush=True)
assert all(results[k] for k in ('workspace_write', 'knowledge_write_denied', 'runtime_write_denied'))

proc = subprocess.Popen(['/opt/copilot-runtime/copilot-runtime', '--headless', '--stdio',
                         '--no-auto-update', '--no-auto-login', '--log-level', 'error'],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=None)
try:
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping',
                       'params': {'message': 'otodock-sandbox-probe'}}).encode()
    proc.stdin.write(('Content-Length: %d\r\n\r\n' % len(body)).encode() + body)
    proc.stdin.flush()
    deadline = time.monotonic() + 5
    data = b''
    while time.monotonic() < deadline:
        if not select.select([proc.stdout], [], [], 0.1)[0]:
            if proc.poll() is not None:
                raise RuntimeError('runtime exited before ping reply: %s' % proc.returncode)
            continue
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            raise RuntimeError('runtime closed stdout before ping reply')
        data += chunk
        while b'\r\n\r\n' in data:
            header, payload = data.split(b'\r\n\r\n', 1)
            length = int(header.split(b':', 1)[1].strip())
            if len(payload) < length:
                break
            message = json.loads(payload[:length])
            data = payload[length:]
            if message.get('id') == 1:
                if 'error' in message:
                    raise RuntimeError('ping error: %s' % message['error'])
                print(json.dumps({'ping': message['result']}), flush=True)
                if '--hold-after-ping' in sys.argv:
                    time.sleep(60)  # The outer timeout must reap this live tree.
                raise SystemExit(0)
    raise TimeoutError('runtime ping exceeded five seconds')
finally:
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1)
'''


def run_bounded(command: list[str], env: dict[str, str], timeout: float) -> dict:
    """Bound startup and kill only the launched tree, including detached children."""
    start = time.monotonic()
    known: dict[int, psutil.Process] = {}
    proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    root = psutil.Process(proc.pid)
    timed_out = False
    stdout = stderr = ''
    try:
        while True:
            with contextlib.suppress(psutil.NoSuchProcess):
                known.update((p.pid, p) for p in root.children(recursive=True))
            if time.monotonic() - start > timeout:
                timed_out = True
                break
            try:
                stdout, stderr = proc.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        # The dedicated process group catches descendants created between scans;
        # tracked Process objects cover children that started their own groups.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        for child in known.values():
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
        stdout, stderr = proc.communicate(timeout=2)
        _, alive = psutil.wait_procs(list(known.values()), timeout=2)
    survivors = []
    for child in alive:
        try:
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                survivors.append(child.pid)
        except psutil.NoSuchProcess:
            pass
    return {'exit_code': proc.returncode, 'timed_out': timed_out,
            'elapsed_seconds': round(time.monotonic() - start, 3),
            'tracked_descendants': len(known), 'live_descendants_after_cleanup': survivors,
            'stdout': stdout.strip(), 'stderr': stderr.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', type=Path, required=True,
                        help='Directory containing the pinned copilot-runtime and runtime.node')
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--exercise-timeout', action='store_true',
                        help='Hold after ping and verify timeout kills the live runtime tree')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 30:
        parser.error('--timeout must be between 1 and 30 seconds')
    runtime = args.runtime_dir.resolve(strict=True)
    for name in ('copilot-runtime', 'runtime.node'):
        if not (runtime / name).is_file():
            parser.error(f'missing {name} under --runtime-dir')
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='otodock-copilot-sandbox-') as temp:
        root = Path(temp)
        os.environ['PLATFORM_DATA_DIR'] = str(root / 'data')
        os.environ['PLATFORM_CONFIG_DIR'] = str(root / 'config')
        # Keep configuration reads and any module initialization under /tmp.
        (root / 'config').mkdir()
        (root / 'config' / 'config.env').write_text('OTODOCK_STORAGE_QUOTAS=off\n')
        sys.path.insert(0, str(repo / 'proxy'))
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount

        agent = root / 'data' / 'agents' / 'probe'
        for subdir in ('workspace', 'knowledge', 'workspace/.copilot'):
            (agent / subdir).mkdir(parents=True, exist_ok=True)
        builder = SandboxBuilder(SandboxConfig(
            role='manager', username='', agent_name='probe', is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=runtime,
            host_claude_dir=agent / 'workspace' / '.claude',
            # A closed, non-service port satisfies the resolved-egress contract.
            # The probe makes no network calls and does not use the real proxy.
            net_forwards=['1'],
            mcp_sandbox_mounts=[SandboxMount(str(runtime), '/opt/copilot-runtime', 'ro')],
        ))
        inner_cmd = ['/usr/bin/python3', '-c', INNER]
        if args.exercise_timeout:
            inner_cmd.append('--hold-after-ping')
        command = builder.build_command_prefix(inner_cmd)
        assert '--block-private' in command and '--cap-drop' in command
        assert command.index('--tmpfs') < command.index('/opt/copilot-runtime')
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LANG': 'C.UTF-8',
               'COPILOT_HOME': '/workspace/.copilot', 'COPILOT_DISABLE_KEYTAR': '1',
               'COPILOT_SKIP_CLI_DOWNLOAD': '1', 'COPILOT_TELEMETRY_DISABLED': 'true'}
        result = run_bounded(command, env, args.timeout)
        result.update({'kernel': os.uname().release, 'runtime_directory': str(runtime),
                       'launcher': command[0], 'credentials_passed': False})
        print(json.dumps(result, indent=2))
        if args.exercise_timeout:
            return 0 if (result['timed_out'] and 'pong: otodock-sandbox-probe' in result['stdout']
                         and not result['live_descendants_after_cleanup']) else 1
        return 0 if result['exit_code'] == 0 and not result['live_descendants_after_cleanup'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
