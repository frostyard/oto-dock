"""Pinned SDK subprocess transport through the mandatory OtoDock Linux sandbox.

No SDK dependency is imported until start(). Configuration and mounts belong to
the caller; this adapter never changes global platform environment/configuration.
The 1.0.13 private Popen/state fields are confined here because its public stdio
transport provides neither subprocess ownership nor a disconnect-state property.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import contextlib
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import signal
import subprocess
import sys
import tempfile
import time

from .credentials import CopilotCredential
from .session_state import PrivateCopilotSessionState, SANDBOX_STATE_DIRECTORY

SDK_VERSION = '1.0.13'
RUNTIME_VERSION = '1.0.83'
PROTOCOL_VERSION = 3


@dataclass(frozen=True)
class _Process:
    pid: int
    start_ticks: int
    parent_pid: int
    session_id: int
    state: str


def _process(pid: int) -> _Process | None:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return _Process(pid, int(fields[19]), int(fields[1]), int(fields[3]), fields[0])
    except (OSError, ValueError, IndexError):
        return None


class _OwnedTree:
    """Track only the exact SDK child and its isolated session/descendants.

    pidfds bind signals to process identities, avoiding PID-reuse races. Reading
    /proc does not confer ownership: only the verified root's session or ancestry
    permits adopting a child. No proxy-wide children are signalled.
    """

    def __init__(self):
        self.root: _Process | None = None
        self.session_id: int | None = None
        self.handshake_verified = False
        self.owned: dict[tuple[int, int], tuple[_Process, int]] = {}

    def _adopt(self, process: _Process):
        key = (process.pid, process.start_ticks)
        if key in self.owned or process.state == 'Z':
            return
        try:
            fd = os.pidfd_open(process.pid)
        except ProcessLookupError:
            return
        current = _process(process.pid)
        if current is None or current.start_ticks != process.start_ticks:
            os.close(fd)
            return
        self.owned[key] = (process, fd)

    def observe(self, popen, handshake: Path, nonce: str, *, full_scan: bool = True):
        record = None
        try:
            candidate = json.loads(handshake.read_text())
            if (isinstance(candidate, dict) and isinstance(popen, subprocess.Popen)
                    and candidate.get('nonce') == nonce and candidate.get('pid') == popen.pid
                    and candidate.get('session_id') == popen.pid
                    and type(candidate.get('start_ticks')) is int):
                record = candidate
        except (OSError, ValueError):
            pass  # A partial launcher write is retried on the next observation.
        if self.root is None and isinstance(popen, subprocess.Popen) and popen.poll() is None:
            process = _process(popen.pid)
            if process is not None and process.parent_pid == os.getpid():
                self.root = process
                self._adopt(process)
        if self.root is None and record is not None:
            # A fast bootstrap failure can exit and be reaped before the first
            # observation. The private nonce record still identifies its session.
            current = _process(popen.pid)
            if current is None or current.start_ticks == record['start_ticks']:
                self.root = _Process(popen.pid, record['start_ticks'], os.getpid(), popen.pid, 'Z')
        if self.root is None:
            return
        if not self.handshake_verified and record is not None and record['start_ticks'] == self.root.start_ticks:
            self.session_id = self.root.pid
            self.handshake_verified = True
        if not full_scan:
            return
        known_pids = {p.pid for p, _ in self.owned.values() if self.is_live(p)}
        processes = [p for path in Path('/proc').iterdir() if path.name.isdecimal()
                     if (p := _process(int(path.name))) is not None]
        # Multiple passes discover descendants regardless of /proc iteration order.
        changed = True
        while changed:
            changed = False
            for process in processes:
                if process.pid in known_pids:
                    continue
                if ((self.session_id is not None and process.session_id == self.session_id)
                        or process.parent_pid in known_pids):
                    self._adopt(process)
                    if self.is_live(process):
                        known_pids.add(process.pid)
                        changed = True

    @staticmethod
    def is_live(process: _Process) -> bool:
        current = _process(process.pid)
        return bool(current and current.start_ticks == process.start_ticks and current.state != 'Z')

    @property
    def alive(self) -> bool:
        return self.root is not None and self.is_live(self.root)

    def live(self) -> list[_Process]:
        return [process for process, _ in self.owned.values() if self.is_live(process)]

    def signal(self, sig: int):
        for _, fd in reversed(list(self.owned.values())):
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(fd, sig)

    def dispose(self):
        for _, fd in self.owned.values():
            os.close(fd)
        self.owned.clear()


class SandboxedCopilotRuntime:
    """Single-use, cancellation-safe owner of one SDK runtime process tree.

    ``builder`` must already expose runtime_path.parent read-only at the parent
    of sandbox_runtime_path, including runtime.node and all adjacent assets.
    environment is an explicit caller-curated dictionary, never os.environ.
    Inference authentication comes from an explicit credential. github_token is
    retained for development probes; repository/MCP credentials use their
    separate broker rather than an ambient GitHub token here.
    """

    def __init__(self, builder, *, runtime_path: Path, working_directory: Path,
                 sandbox_state_directory: str, environment: Mapping[str, str],
                 sandbox_runtime_path: str = '/opt/copilot-runtime/copilot-runtime',
                 github_token: str | None = None, credential: CopilotCredential | None = None,
                 session_state: PrivateCopilotSessionState | None = None,
                 startup_timeout: float = 15,
                 shutdown_timeout: float = 5):
        if credential is not None and not isinstance(credential, CopilotCredential):
            raise TypeError('Invalid Copilot runtime credential')
        if credential is not None and github_token is not None:
            raise ValueError('Copilot credential and legacy token are mutually exclusive')
        if session_state is not None and not isinstance(session_state, PrivateCopilotSessionState):
            raise TypeError('Invalid Copilot private session state')
        self.builder = builder
        self.runtime_path = Path(runtime_path).resolve()
        self.working_directory = Path(working_directory).resolve()
        self.sandbox_runtime_path = sandbox_runtime_path
        self.sandbox_state_directory = sandbox_state_directory
        self.environment = dict(environment)
        self._token = github_token
        self._credential = credential
        self._session_state = session_state
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self._client = None
        self._popen = None
        self._start_task = None
        self._close_task = None
        self._watch_task = None
        self._temporary = None
        self._handshake = None
        self._nonce = secrets.token_hex(32)
        self._tree = _OwnedTree()
        self._started = False
        self.forced_cleanup = False

    @property
    def alive(self) -> bool:
        return (self._started and self._close_task is None and self._tree.alive
                and getattr(self._client, '_state', None) == 'connected')

    def _command(self):
        from core.sandbox.sandbox import SandboxBuilder, _NETNS_LAUNCHER

        if self._credential is not None and self._session_state is None:
            raise ValueError('Copilot credentials require private session state')
        state_path = None
        if self._session_state is not None:
            if self.sandbox_state_directory != SANDBOX_STATE_DIRECTORY:
                raise ValueError('Copilot private state requires its fixed sandbox destination')
            state_path = self._session_state.path  # Revalidates host ownership and path identity.
            if (state_path.is_relative_to(self.runtime_path.parent)
                    or self.runtime_path.parent.is_relative_to(state_path)
                    or PurePosixPath(self.sandbox_runtime_path).is_relative_to(SANDBOX_STATE_DIRECTORY)
                    or PurePosixPath(SANDBOX_STATE_DIRECTORY).is_relative_to(
                        PurePosixPath(self.sandbox_runtime_path).parent)):
                raise ValueError('Copilot private state must not overlap runtime assets')
        if not isinstance(self.builder, SandboxBuilder):
            raise TypeError('Copilot requires an actual SandboxBuilder')
        if (not sys.platform.startswith('linux') or not hasattr(os, 'pidfd_open')
                or not hasattr(signal, 'pidfd_send_signal')):
            raise RuntimeError('Copilot sandbox ownership requires Linux pidfd support')
        # Fail before launch if host policy prevents acquiring process handles.
        fd = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
        if not 0 < self.startup_timeout <= 120 or not 0 < self.shutdown_timeout <= 30:
            raise ValueError('Invalid bounded Copilot lifecycle timeout')
        if (not self.runtime_path.is_file() or not os.access(self.runtime_path, os.X_OK)
                or not self.runtime_path.with_name('runtime.node').is_file()):
            raise ValueError('Copilot runtime and adjacent assets must be provisioned')
        if not self.working_directory.is_dir():
            raise ValueError('Copilot host working directory does not exist')
        for path in (self.sandbox_runtime_path, self.sandbox_state_directory):
            if not path.startswith('/') or '..' in PurePosixPath(path).parts:
                raise ValueError('Copilot sandbox paths must be absolute and normalized')
        self._environment()
        command = self.builder.build_command_prefix([self.sandbox_runtime_path])
        if command[0] != str(_NETNS_LAUNCHER) or '--block-private' not in command:
            raise ValueError('Copilot requires the mandatory sandbox network launcher')
        outer = command.index('--')
        inner = command.index('--', outer + 1)
        flags = command[outer + 2:inner]
        if (command[outer + 1] != 'bwrap' or command[inner + 1:] != [self.sandbox_runtime_path]
                or '--die-with-parent' not in flags or '--unshare-pid' not in flags
                or not any(flags[i:i + 2] == ['--cap-drop', 'ALL'] for i in range(len(flags) - 1))):
            raise ValueError('Copilot requires the mandatory bubblewrap process isolation')
        expected_mount = ['--ro-bind', str(self.runtime_path.parent), str(PurePosixPath(self.sandbox_runtime_path).parent)]
        runtime_dir = PurePosixPath(self.sandbox_runtime_path).parent
        last_overlap = None
        for index, option in enumerate(flags):
            count = 3 if option in {'--ro-bind', '--bind'} else 2 if option in {'--tmpfs', '--dev', '--proc'} else 0
            if count and index + count <= len(flags):
                destination = PurePosixPath(flags[index + count - 1])
                if (destination == runtime_dir or destination in runtime_dir.parents
                        or runtime_dir in destination.parents):
                    last_overlap = flags[index:index + count]
        if last_overlap != expected_mount:
            raise ValueError('Copilot runtime assets require an explicit read-only sandbox mount')
        if state_path is not None:
            # Trusted internal state is deliberately outside MCP/agent trees.
            # Inject only this validated allocation after community mounts;
            # keep SandboxBuilder's community manifest allowlist unchanged.
            command[inner:inner] = ['--bind', str(state_path), SANDBOX_STATE_DIRECTORY]
        return command

    def _environment(self):
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.environment.items()):
            raise ValueError('Copilot environment requires string keys and values')
        forbidden = {'GH_TOKEN', 'GITHUB_TOKEN', 'COPILOT_GITHUB_TOKEN', 'COPILOT_SDK_AUTH_TOKEN',
                     'COPILOT_CONNECTION_TOKEN', 'GITHUB_COPILOT_API_TOKEN', 'COPILOT_API_URL',
                     'BASH_ENV', 'ENV', 'GCONV_PATH'}
        if any(key.upper() in forbidden or key.upper().startswith(('PYTHON', 'LD_', 'DYLD_'))
               for key in self.environment):
            raise ValueError('Ambient credentials and host interpreter overrides are prohibited')
        # The network launcher resolves host helpers before bwrap enters its
        # mount namespace. User/agent-writable PATH entries cannot be admitted.
        if any(path not in {'/usr/bin', '/bin', '/usr/local/bin', '/usr/sbin', '/sbin'}
               for path in self.environment.get('PATH', '/usr/bin:/bin').split(':')):
            raise ValueError('Copilot bootstrap PATH must contain only trusted system directories')
        env = dict(self.environment)
        env.setdefault('PATH', '/usr/bin:/bin')
        env.update({'COPILOT_DISABLE_KEYTAR': '1', 'COPILOT_SKIP_CLI_DOWNLOAD': '1',
                    'COPILOT_AUTO_UPDATE': 'false', 'COPILOT_HOME': self.sandbox_state_directory})
        return env

    def _make_client(self, command):
        if self._credential is not None:
            self._credential.ensure_usable(time.time())
        # Reject caller-supplied authentication before injecting the credential's
        # selected channel. Installation tokens must never use SDK github_token.
        env = self._environment()
        github_token = self._token
        if self._credential is not None:
            github_token, credential_environment = self._credential.runtime_auth()
            env.update(credential_environment)
        if importlib.metadata.version('github-copilot-sdk') != SDK_VERSION:
            raise RuntimeError('Unsupported Copilot SDK version')
        from copilot import CopilotClient, RuntimeConnection

        args = ['-I', str(Path(__file__).with_name('launcher.py')), '--ownership-file', str(self._handshake),
                '--nonce', self._nonce, '--', *command]
        return CopilotClient(
            connection=RuntimeConnection.for_stdio(path=sys.executable, args=args),
            working_directory=str(self.working_directory), base_directory=self.sandbox_state_directory,
            env=env, github_token=github_token, use_logged_in_user=False, mode='empty', log_level='error',
        )

    def _observe(self, *, full_scan: bool = True):
        if self._client is not None and self._handshake is not None:
            if self._popen is None:
                self._popen = getattr(self._client, '_process', None)
            self._tree.observe(self._popen, self._handshake, self._nonce, full_scan=full_scan)

    async def _watch(self):
        try:
            while True:
                # Once connected, the SDK's EOF state and the owned root are
                # sufficient liveness signals. Avoid a proxy-wide /proc census
                # every tick; rescan exact ancestry/session at startup and close.
                self._observe(full_scan=not self._started)
                if self._started and (not self._tree.alive or getattr(self._client, '_state', None) != 'connected'):
                    self._begin_close()
                    return
                await asyncio.sleep(0.25 if self._started else 0.05)
        except Exception:
            self._begin_close()

    async def start(self):
        if self._close_task is not None:
            raise RuntimeError('Copilot runtime cannot restart after close')
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start())
        try:
            return await asyncio.shield(self._start_task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.close()
            raise
        except TimeoutError:
            await self.close()
            raise TimeoutError('Copilot runtime startup timed out') from None
        except Exception:
            await self.close()
            raise RuntimeError('Copilot runtime startup failed') from None

    async def _start(self):
        if self._credential is not None:
            self._credential.ensure_usable(time.time())
        command = self._command()
        self._temporary = tempfile.TemporaryDirectory(prefix='otodock-copilot-owner-')
        self._handshake = Path(self._temporary.name) / 'identity.json'
        self._client = self._make_client(command)
        self._watch_task = asyncio.create_task(self._watch())
        async with asyncio.timeout(self.startup_timeout):
            await self._client.start()
            self._observe()
            if not self._tree.handshake_verified:
                raise RuntimeError('Copilot process ownership handshake failed')
            status = await self._client.get_status()
            if status.version != RUNTIME_VERSION or status.protocol_version != PROTOCOL_VERSION:
                raise RuntimeError('Unsupported Copilot runtime/protocol version')
            self._started = True
            if not self.alive:
                raise RuntimeError('Copilot transport closed during startup')
            return self._client

    def _begin_close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(self._observe_close_result)
        return self._close_task

    @staticmethod
    def _observe_close_result(task):
        if not task.cancelled():
            task.exception()

    async def close(self):
        task = self._begin_close()
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise RuntimeError('Copilot runtime cleanup was cancelled') from None
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self):
        self._started = False
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._start_task
        observation_error = None

        def observe_for_cleanup():
            nonlocal observation_error
            try:
                self._observe()
            except Exception as exc:
                observation_error = exc

        def owned_pending():
            return bool(self._tree.live()) or (
                isinstance(self._popen, subprocess.Popen) and self._popen.poll() is None
            )

        try:
            observe_for_cleanup()
            if self._client is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._client.stop(), self.shutdown_timeout)
            observe_for_cleanup()
            # SDK.stop owns only its immediate process (the outer launcher).
            # Gracefully stop remaining namespace/runtime helpers ourselves
            # before escalating to SIGKILL; ownership is still exact pidfds.
            if self._tree.live():
                self._tree.signal(signal.SIGTERM)
            deadline = asyncio.get_running_loop().time() + min(self.shutdown_timeout, 2)
            while owned_pending() and asyncio.get_running_loop().time() < deadline:
                observe_for_cleanup()
                await asyncio.sleep(0.025)
            self.forced_cleanup = owned_pending()
            if self.forced_cleanup:
                self._tree.signal(signal.SIGKILL)
                # Resource exhaustion may prevent even the first pidfd. The
                # pinned SDK's exact Popen handle still owns that direct child.
                if isinstance(self._popen, subprocess.Popen) and self._popen.poll() is None:
                    self._popen.kill()
                deadline = asyncio.get_running_loop().time() + 2
                while owned_pending() and asyncio.get_running_loop().time() < deadline:
                    observe_for_cleanup()
                    self._tree.signal(signal.SIGKILL)
                    await asyncio.sleep(0.025)
            if owned_pending():
                raise RuntimeError('Owned Copilot processes survived shutdown')
            if observation_error is not None:
                raise RuntimeError('Copilot process ownership observation failed') from None
        finally:
            if self._watch_task is not None:
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._watch_task
            self._tree.dispose()
            if self._temporary is not None:
                self._temporary.cleanup()
            self._token = None
            self._credential = None
            self._client = None
            self._start_task = None
            self._popen = None
