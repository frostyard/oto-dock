#!/usr/bin/env python3
"""Opt-in, one-turn Copilot SDK inference through the real OtoDock sandbox.

Requires the proxy dependencies and github-copilot-sdk==1.0.13. Credentials are
selected explicitly from gh, never printed or persisted by the probe. All tool
surfaces are disabled. This is compatibility evidence, not a production engine.
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
import signal
import subprocess
import sys
import tempfile
import time

import psutil

MARKER = 'OTO_SANDBOX_SDK_OK'


async def run(args, report):
    from copilot import CopilotClient, RuntimeConnection
    from copilot.rpc import PermissionDecisionReject

    if importlib.metadata.version('github-copilot-sdk') != '1.0.13':
        raise ValueError('Expected github-copilot-sdk==1.0.13')
    token = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True,
                           check=True, timeout=10).stdout.strip()
    if not token.startswith(('gho_', 'ghu_', 'github_pat_')):
        raise ValueError('Unsupported selected token type')
    events = Counter()
    observed = {}
    groups = set()

    def observe():
        for child in psutil.Process().children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                observed[(child.pid, child.create_time())] = child
                if child.ppid() == os.getpid() and os.getpgid(child.pid) == child.pid:
                    groups.add(child.pid)

    async def track():
        while True:
            observe()
            await asyncio.sleep(0.05)

    def event_received(event):
        events[event.raw_type or event.type.value] += 1

    def deny(_request, _invocation):
        report['unexpected_permission_requests'] += 1
        return PermissionDecisionReject(feedback='All tools disabled in this probe')

    with tempfile.TemporaryDirectory(prefix='otodock-copilot-sandbox-sdk-') as directory:
        root = Path(directory)
        os.environ['PLATFORM_DATA_DIR'] = str(root / 'data')
        os.environ['PLATFORM_CONFIG_DIR'] = str(root / 'config')
        (root / 'config').mkdir()
        (root / 'config' / 'config.env').write_text('OTODOCK_STORAGE_QUOTAS=off\n')
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'proxy'))
        from core.sandbox.sandbox import (
            SandboxBuilder, SandboxConfig, SandboxMount, netns_preflight, netns_resolv_path,
        )

        # Existing preflight writes its DNS swap only inside PLATFORM_DATA_DIR.
        netns_preflight()
        report['dns_swap_prepared'] = netns_resolv_path().exists()
        agent = root / 'data' / 'agents' / 'probe'
        for subdir in ('workspace/.copilot', 'knowledge'):
            (agent / subdir).mkdir(parents=True, exist_ok=True)
        builder = SandboxBuilder(SandboxConfig(
            role='manager', username='', agent_name='probe', is_admin_agent=False,
            host_agents_dir=agent.parent, host_mcps_dir=args.runtime_dir,
            host_claude_dir=agent / 'workspace' / '.claude', net_forwards=['1'],
            mcp_sandbox_mounts=[SandboxMount(str(args.runtime_dir), '/opt/copilot-runtime', 'ro')],
        ))
        command = builder.build_command_prefix(['/opt/copilot-runtime/copilot-runtime'])
        # SDK Popen has no start_new_session option. A tiny credential-free
        # wrapper creates our own process group then execs the real launcher.
        wrapper = root / 'launch'
        wrapper.write_text('#!/usr/bin/python3\nimport os,sys\nos.setsid()\nos.execv(sys.argv[1],sys.argv[1:])\n')
        wrapper.chmod(0o700)
        client = CopilotClient(
            connection=RuntimeConnection.for_stdio(path=str(wrapper), args=command),
            working_directory=str(agent / 'workspace'), base_directory='/workspace/.copilot',
            env={'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LANG': 'C.UTF-8',
                 'COPILOT_HOME': '/workspace/.copilot', 'COPILOT_DISABLE_KEYTAR': '1',
                 'COPILOT_SKIP_CLI_DOWNLOAD': '1'},
            github_token=token, use_logged_in_user=False, mode='empty', log_level='error',
        )
        tracker = asyncio.create_task(track())
        try:
            async with asyncio.timeout(60):
                await client.start()
                status = await client.get_status()
                if status.version != '1.0.83' or status.protocol_version != 3:
                    raise ValueError('Unexpected runtime/protocol version')
                report['runtime_version'] = status.version
                report['protocol_version'] = status.protocol_version
                report['sdk_start_inside_sandbox'] = 'passed'
                session = await client.create_session(
                    session_id='oto-sandbox-sdk-probe', model='gpt-5-mini',
                    available_tools=[], on_permission_request=deny,
                    enable_config_discovery=False, enable_file_hooks=False,
                    enable_host_git_operations=False, enable_session_store=True,
                    streaming=True, on_event=event_received,
                    session_limits={'max_ai_credits': 30.0},
                )
                reply = await session.send_and_wait(
                    f'Reply with exactly {MARKER}. Do not call any tools.', timeout=45,
                )
                if not reply or reply.data.content.strip() != MARKER:
                    raise ValueError('Unexpected marker response')
                if report['unexpected_permission_requests'] or events['tool.execution_start']:
                    raise ValueError('Unexpected tool activity in a no-tool probe')
                report['sandboxed_inference'] = 'passed'
                await session.disconnect()
        finally:
            observe()
            try:
                await asyncio.wait_for(client.stop(), timeout=5)
            finally:
                observe()
                tracker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tracker
                for group in groups:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(group, signal.SIGKILL)
                for child in observed.values():
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.kill()
                _, alive = psutil.wait_procs(list(observed.values()), timeout=2)
                survivors = []
                for child in alive:
                    with contextlib.suppress(psutil.NoSuchProcess):
                        if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                            survivors.append(child.pid)
                report['tracked_descendants'] = len(observed)
                report['live_descendants_after_cleanup'] = survivors
                report['event_counts'] = dict(sorted(events.items()))
                if survivors:
                    raise RuntimeError('Probe descendant cleanup failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--use-gh-token', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live or not args.use_gh_token:
        parser.error('Requires explicit --live --use-gh-token')
    args.runtime_dir = args.runtime_dir.resolve()
    if not (args.runtime_dir / 'copilot-runtime').is_file():
        parser.error('Missing provisioned copilot-runtime')
    logging.disable(logging.CRITICAL)
    report = {'result': 'failed', 'sdk_version': '1.0.13', 'model': 'gpt-5-mini',
              'live': True, 'sandboxed': True, 'unexpected_permission_requests': 0}
    start = time.monotonic()
    try:
        asyncio.run(run(args, report))
        report['result'] = 'passed'
    except Exception as exc:
        report['error_type'] = type(exc).__name__  # Never serialize raw credential-bearing errors.
    report['elapsed_seconds'] = round(time.monotonic() - start, 3)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
