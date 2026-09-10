"""Real process ownership tests with a fake SDK; no credentials or inference."""

import asyncio
import contextlib
import functools
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import traceback
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'proxy'))
from core.layers.copilot import runtime as module


def async_test(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapper


def new_runtime(tmp_path, **kwargs):
    return module.SandboxedCopilotRuntime(
        None, runtime_path=tmp_path / 'copilot-runtime', working_directory=tmp_path,
        sandbox_state_directory='/workspace/.copilot', environment={'PATH': '/usr/bin:/bin', 'HOME': '/tmp'},
        startup_timeout=1, shutdown_timeout=0.05, **kwargs,
    )


class FakeClient:
    def __init__(self, runtime, *, hang_start=False, hang_stop=False, before_handshake=False):
        self.runtime = runtime
        self.hang_start = hang_start
        self.hang_stop = hang_stop
        self.before_handshake = before_handshake
        self._state = 'disconnected'
        self._process = None
        self.spawned = asyncio.Event()
        self.stopped = False

    async def start(self):
        code = 'import time; time.sleep(60)'
        if self.hang_stop:
            code = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'
        command = [sys.executable, '-c', code]
        if not self.before_handshake:
            command = [sys.executable, str(Path(module.__file__).with_name('launcher.py')),
                       '--ownership-file', str(self.runtime._handshake), '--nonce', self.runtime._nonce,
                       '--', *command]
        self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.spawned.set()
        if self.hang_start:
            await asyncio.Event().wait()
        while not self.runtime._handshake.exists():
            await asyncio.sleep(0.005)
        self._state = 'connected'

    async def get_status(self):
        return SimpleNamespace(version='1.0.83', protocol_version=3)

    async def stop(self):
        self.stopped = True
        if self.hang_stop:
            await asyncio.Event().wait()
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            await asyncio.to_thread(self._process.wait, 1)
        self._state = 'disconnected'


def fake_runtime(monkeypatch, tmp_path, **options):
    runtime = new_runtime(tmp_path)
    client = FakeClient(runtime, **options)
    monkeypatch.setattr(runtime, '_command', lambda: ['fake-sdk-process-test-only'])
    monkeypatch.setattr(runtime, '_make_client', lambda _: client)
    return runtime, client


@pytest.fixture
def unrelated_child():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=2)


@async_test
async def test_two_concurrent_runtimes_close_only_the_selected_tree(monkeypatch, tmp_path, unrelated_child):
    first, one = fake_runtime(monkeypatch, tmp_path)
    second, two = fake_runtime(monkeypatch, tmp_path)
    try:
        await asyncio.gather(first.start(), second.start())
        assert first.alive and second.alive
        await first.close()
        assert not first.alive and one._process.poll() is not None
        assert second.alive and two._process.poll() is None
        assert unrelated_child.poll() is None
        assert not first.forced_cleanup
    finally:
        await asyncio.gather(first.close(), second.close())


@async_test
@pytest.mark.parametrize('before_handshake', [False, True])
async def test_start_cancellation_cleans_partial_child_only(monkeypatch, tmp_path, unrelated_child, before_handshake):
    runtime, client = fake_runtime(monkeypatch, tmp_path, hang_start=True, hang_stop=True,
                                   before_handshake=before_handshake)
    task = asyncio.create_task(runtime.start())
    await client.spawned.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 4)
    assert client._process.poll() is not None
    assert unrelated_child.poll() is None
    assert not runtime.alive and runtime.forced_cleanup
    assert not Path(runtime._temporary.name).exists()


@async_test
async def test_start_timeout_and_stuck_sdk_stop_are_bounded(monkeypatch, tmp_path, unrelated_child):
    runtime, client = fake_runtime(monkeypatch, tmp_path, hang_start=True, hang_stop=True)
    runtime.startup_timeout = 0.1
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runtime.start(), 4)
    assert client._process.poll() is not None
    assert unrelated_child.poll() is None


@async_test
async def test_stdio_eof_closes_live_process_without_touching_other_children(monkeypatch, tmp_path, unrelated_child):
    runtime, client = fake_runtime(monkeypatch, tmp_path, hang_stop=True)
    await runtime.start()
    client._state = 'disconnected'  # The pinned SDK's EOF callback sets this field.
    assert not runtime.alive
    async with asyncio.timeout(4):
        while runtime._close_task is None:
            await asyncio.sleep(0.025)
        await runtime.close()
    assert client._process.poll() is not None
    assert unrelated_child.poll() is None


@async_test
async def test_observation_error_still_stops_sdk_and_finalizes(monkeypatch, tmp_path, unrelated_child):
    runtime, client = fake_runtime(monkeypatch, tmp_path)
    await runtime.start()

    def fail_observe():
        raise OSError('simulated process handle exhaustion')

    monkeypatch.setattr(runtime, '_observe', fail_observe)
    with pytest.raises(RuntimeError, match='ownership observation failed'):
        await runtime.close()
    assert client.stopped and client._process.poll() is not None
    assert unrelated_child.poll() is None
    assert not Path(runtime._temporary.name).exists()
    assert runtime._watch_task.done()


def test_fast_root_exit_still_tracks_valid_handshake_session(tmp_path, unrelated_child):
    handshake, child_pid = tmp_path / 'identity.json', tmp_path / 'child.pid'
    nonce = 'isolated-test-nonce'
    code = ('import subprocess,sys; from pathlib import Path; '
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
            f'Path({str(child_pid)!r}).write_text(str(p.pid))')
    proc = subprocess.Popen([sys.executable, str(Path(module.__file__).with_name('launcher.py')),
                             '--ownership-file', str(handshake), '--nonce', nonce,
                             '--', sys.executable, '-c', code])
    proc.wait(timeout=2)  # The first observation happens only after the root exited.
    tree = module._OwnedTree()
    pid = int(child_pid.read_text())
    try:
        tree.observe(proc, handshake, nonce)
        assert tree.handshake_verified
        assert pid in {p.pid for p in tree.live()}
        tree.signal(signal.SIGKILL)
        assert unrelated_child.poll() is None
    finally:
        tree.dispose()
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize('key', ['GH_TOKEN', 'gh_token', 'COPILOT_CONNECTION_TOKEN', 'Ld_Preload',
                               'PYTHONPATH', 'PYTHONHOME', 'DYLD_INSERT_LIBRARIES', 'BASH_ENV'])
def test_host_bootstrap_rejects_ambient_credentials_and_loader_overrides(tmp_path, key):
    runtime = new_runtime(tmp_path)
    runtime.environment[key] = 'must-not-execute-or-leak'
    with pytest.raises(ValueError, match='prohibited') as exc:
        runtime._environment()
    assert 'must-not-execute-or-leak' not in str(exc.value)


def test_no_ambient_environment_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv('GH_TOKEN', 'ambient-secret')
    runtime = new_runtime(tmp_path)
    env = runtime._environment()
    assert 'GH_TOKEN' not in env and 'ambient-secret' not in env.values()
    assert env['COPILOT_DISABLE_KEYTAR'] == '1'
    assert env['COPILOT_SKIP_CLI_DOWNLOAD'] == '1'


def test_agent_writable_host_helper_path_is_rejected(tmp_path):
    runtime = new_runtime(tmp_path)
    runtime.environment['PATH'] = str(tmp_path) + ':/usr/bin'
    with pytest.raises(ValueError, match='trusted system directories'):
        runtime._environment()


def test_forged_handshake_cannot_adopt_unrelated_process(tmp_path, unrelated_child):
    owned = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    handshake = tmp_path / 'identity.json'
    unrelated = module._process(unrelated_child.pid)
    handshake.write_text(json.dumps({'nonce': 'nonce', 'pid': unrelated.pid,
                                    'session_id': unrelated.pid, 'start_ticks': unrelated.start_ticks}))
    tree = module._OwnedTree()
    try:
        tree.observe(owned, handshake, 'nonce')
        assert not tree.handshake_verified
        assert unrelated_child.pid not in {p.pid for p in tree.live()}
        tree.signal(signal.SIGKILL)
        owned.wait(timeout=2)
        assert unrelated_child.poll() is None
    finally:
        tree.dispose()
        if owned.poll() is None:
            owned.kill()
            owned.wait(timeout=2)


@async_test
async def test_repeated_close_cancellation_waits_for_owned_cleanup(monkeypatch, tmp_path, unrelated_child):
    runtime, client = fake_runtime(monkeypatch, tmp_path, hang_stop=True)
    runtime.shutdown_timeout = 0.15
    await runtime.start()
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0.02)
    closing.cancel()
    await asyncio.sleep(0.02)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 4)
    assert client._process.poll() is not None
    assert runtime._client is None and runtime._token is None
    assert unrelated_child.poll() is None


@async_test
async def test_sdk_startup_errors_are_sanitized_after_cleanup(monkeypatch, tmp_path):
    runtime, client = fake_runtime(monkeypatch, tmp_path)

    async def fail_status():
        raise ValueError('opaque-sdk-error contains ghp_do-not-expose')

    monkeypatch.setattr(client, 'get_status', fail_status)
    with pytest.raises(RuntimeError, match='Copilot runtime startup failed') as exc:
        await runtime.start()
    assert 'ghp_do-not-expose' not in ''.join(traceback.format_exception(exc.value))
    assert client._process.poll() is not None


def test_bootstrap_python_ignores_writable_cwd_and_user_site(tmp_path):
    handshake = tmp_path / 'identity.json'
    (tmp_path / 'json.py').write_text('raise RuntimeError("untrusted cwd imported")')
    command = [sys.executable, '-I', str(Path(module.__file__).with_name('launcher.py')),
               '--ownership-file', str(handshake), '--nonce', 'test', '--', sys.executable, '-c',
               'import json,os; assert os.environ["PYTHONSAFEPATH"]=="1"; '
               'assert os.environ["PYTHONNOUSERSITE"]=="1"; print("safe")']
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=2,
                            env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)}, check=True)
    assert result.stdout.strip() == 'safe'


@pytest.mark.parametrize('later_mount', [
    ['--tmpfs', '/opt'],
    ['--bind', '/another/runtime', '/opt/copilot-runtime'],
    ['--ro-bind', '/another/plugin', '/opt/copilot-runtime/builtin'],
])
def test_later_mount_cannot_shadow_verified_runtime_assets(monkeypatch, tmp_path, later_mount):
    # Replace the builder module boundary, not global application configuration;
    # this test must run in CI without the SDK or proxy dependency set.
    boundary = ModuleType('core.sandbox.sandbox')
    boundary._NETNS_LAUNCHER = tmp_path / 'oto-sandbox-net'

    class Builder:
        def build_command_prefix(self, inner):
            return [str(boundary._NETNS_LAUNCHER), '--block-private', '--forward', '1', '--',
                    'bwrap', '--unshare-pid', '--die-with-parent', '--cap-drop', 'ALL',
                    '--ro-bind', str(tmp_path), '/opt/copilot-runtime', *later_mount, '--', *inner]

    boundary.SandboxBuilder = Builder
    monkeypatch.setitem(sys.modules, 'core.sandbox.sandbox', boundary)
    runtime = new_runtime(tmp_path)
    runtime.builder = Builder()
    runtime.runtime_path.write_bytes(b'fake-runtime')
    runtime.runtime_path.chmod(0o700)
    runtime.runtime_path.with_name('runtime.node').write_bytes(b'fake-assets')
    with pytest.raises(ValueError, match='read-only sandbox mount'):
        runtime._command()


def test_module_import_has_no_sdk_or_platform_configuration_side_effects():
    proxy = str(Path(module.__file__).resolve().parents[3])
    script = (f'import sys; sys.path.insert(0,{proxy!r}); '
              'import core.layers.copilot.runtime; '
              'assert "config" not in sys.modules; assert "copilot" not in sys.modules')
    subprocess.run([sys.executable, '-I', '-c', script], check=True, timeout=2)
