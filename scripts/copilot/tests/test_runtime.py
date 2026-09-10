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
from core.layers.copilot.credentials import CopilotCredential, CredentialKind, CredentialUnavailableError


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
        code = getattr(self, 'fixture_code', 'import time; time.sleep(60)')
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
        # Real SDK readiness follows launcher exec, which happens only after
        # the ownership record is fully written. Path existence alone races
        # that write and announces fake readiness too early.
        while True:
            try:
                json.loads(self.runtime._handshake.read_text())
                break
            except (FileNotFoundError, json.JSONDecodeError):
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


def credential(kind=CredentialKind.USER_TOKEN, expires_at=200):
    token = 'ghu_privatefixture' if kind is CredentialKind.USER_TOKEN else 'ghs_privatefixture'
    return CopilotCredential('account', 'principal', 'revision', kind, token, expires_at)


def capture_sdk(monkeypatch):
    sdk = ModuleType('copilot')
    sdk.CopilotClient = SimpleNamespace
    sdk.RuntimeConnection = SimpleNamespace(for_stdio=lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, 'copilot', sdk)
    monkeypatch.setattr(module.importlib.metadata, 'version', lambda _: module.SDK_VERSION)
    return sdk


@pytest.mark.parametrize('kind', [CredentialKind.USER_TOKEN, CredentialKind.INSTALLATION_TOKEN, None])
def test_explicit_credential_selects_exact_sdk_channel(monkeypatch, tmp_path, kind):
    capture_sdk(monkeypatch)
    monkeypatch.setattr(module.time, 'time', lambda: 100)
    selected = credential(kind) if kind else None
    runtime = new_runtime(tmp_path, credential=selected)
    client = runtime._make_client(['fake-sandbox'])
    expected_user = selected.token if kind is CredentialKind.USER_TOKEN else None
    assert client.github_token == expected_user
    if kind is CredentialKind.INSTALLATION_TOKEN:
        assert client.env['COPILOT_GITHUB_TOKEN'] == selected.token
    else:
        assert 'COPILOT_GITHUB_TOKEN' not in client.env
    assert 'COPILOT_SDK_AUTH_TOKEN' not in client.env
    assert client.use_logged_in_user is False and client.mode == 'empty'
    if selected:
        assert selected.token not in repr(runtime)
        assert selected.token not in repr(selected)
        assert selected.token not in repr(client.connection)
    assert 'COPILOT_GITHUB_TOKEN' not in runtime.environment


@async_test
async def test_expired_credential_fails_before_sandbox_or_sdk_start(monkeypatch, tmp_path):
    monkeypatch.setattr(module.time, 'time', lambda: 200)
    runtime = new_runtime(tmp_path, credential=credential())

    def forbidden_command():
        pytest.fail('Expired credential reached sandbox construction')

    monkeypatch.setattr(runtime, '_command', forbidden_command)
    with pytest.raises(RuntimeError, match='Copilot runtime startup failed') as exc:
        await runtime.start()
    assert 'privatefixture' not in ''.join(traceback.format_exception(exc.value))
    assert runtime._temporary is None and runtime._client is None
    assert runtime._credential is None and runtime._token is None


def test_credential_expiry_rechecked_before_client_construction(monkeypatch, tmp_path):
    monkeypatch.setattr(module.time, 'time', lambda: 200)
    runtime = new_runtime(tmp_path, credential=credential())
    with pytest.raises(CredentialUnavailableError):
        runtime._make_client(['fake-sandbox'])


def test_explicit_credential_cannot_mix_with_legacy_token(tmp_path):
    for legacy in ('ghu_otherfixture', ''):
        with pytest.raises(ValueError, match='mutually exclusive') as exc:
            new_runtime(tmp_path, credential=credential(), github_token=legacy)
        assert 'privatefixture' not in str(exc.value) and 'otherfixture' not in str(exc.value)


@pytest.mark.parametrize('variable', [
    'GITHUB_COPILOT_API_TOKEN', 'github_copilot_api_token', 'COPILOT_API_URL', 'copilot_api_url',
    'COPILOT_GITHUB_TOKEN',
])
def test_installation_channel_cannot_bypass_caller_environment_rejection(monkeypatch, tmp_path, variable):
    monkeypatch.setattr(module.time, 'time', lambda: 100)
    runtime = new_runtime(tmp_path, credential=credential(CredentialKind.INSTALLATION_TOKEN))
    runtime.environment[variable] = 'private-override'
    with pytest.raises(ValueError, match='Ambient credentials') as exc:
        runtime._make_client(['fake-sandbox'])
    assert 'private-override' not in str(exc.value)


@async_test
async def test_close_drops_live_credential_reference(monkeypatch, tmp_path):
    monkeypatch.setattr(module.time, 'time', lambda: 100)
    runtime, client = fake_runtime(monkeypatch, tmp_path)
    runtime._credential = credential()
    await runtime.start()
    await runtime.close()
    assert client.stopped and runtime._credential is None


def state_mount_runtime(monkeypatch, tmp_path, *, with_state=True):
    boundary = ModuleType('core.sandbox.sandbox')
    boundary._NETNS_LAUNCHER = tmp_path / 'oto-sandbox-net'
    assets = tmp_path / 'assets'
    assets.mkdir()

    class Builder:
        def build_command_prefix(self, inner):
            # Community mounts cannot expose the private host state root. Its
            # absence models the actual SandboxBuilder allowlist faithfully.
            return [str(boundary._NETNS_LAUNCHER), '--block-private', '--forward', '1', '--',
                    'bwrap', '--unshare-pid', '--die-with-parent', '--cap-drop', 'ALL',
                    '--ro-bind', str(assets), '/opt/copilot-runtime', '--tmpfs', '/var', '--', *inner]

    boundary.SandboxBuilder = Builder
    monkeypatch.setitem(sys.modules, 'core.sandbox.sandbox', boundary)
    state = module.PrivateCopilotSessionState.create(tmp_path) if with_state else None
    runtime = new_runtime(tmp_path, credential=credential(), session_state=state)
    runtime.sandbox_state_directory = module.SANDBOX_STATE_DIRECTORY
    runtime.runtime_path = assets / 'copilot-runtime'
    runtime.runtime_path.write_bytes(b'fake-runtime')
    runtime.runtime_path.chmod(0o700)
    runtime.runtime_path.with_name('runtime.node').write_bytes(b'fake-assets')
    runtime.builder = Builder()
    return runtime, state


@async_test
async def test_trusted_state_mount_is_last_and_survives_runtime_close(monkeypatch, tmp_path):
    runtime, state = state_mount_runtime(monkeypatch, tmp_path)
    try:
        path = state.path
        command = runtime._command()
        assert command[-5:] == ['--bind', str(path), module.SANDBOX_STATE_DIRECTORY,
                               '--', runtime.sandbox_runtime_path]
        assert command.index('--tmpfs') < command.index('--bind')
        await runtime.close()
        assert state.path == path and path.is_dir()
    finally:
        state.discard()


def test_typed_launch_requires_private_state(monkeypatch, tmp_path):
    runtime, _ = state_mount_runtime(monkeypatch, tmp_path, with_state=False)
    with pytest.raises(ValueError, match='require private session state'):
        runtime._command()


@pytest.mark.parametrize('failure', ['discarded', 'wrong-destination', 'overlapping-assets'])
def test_private_state_cannot_be_replaced_or_overlay_runtime(monkeypatch, tmp_path, failure):
    runtime, state = state_mount_runtime(monkeypatch, tmp_path)
    try:
        if failure == 'discarded':
            state.discard()
        elif failure == 'wrong-destination':
            runtime.sandbox_state_directory = '/workspace/.copilot'
        else:
            runtime.runtime_path = state.path / 'copilot-runtime'
        with pytest.raises((ValueError, RuntimeError)):
            runtime._command()
    finally:
        state.discard()


@async_test
async def test_process_fence_capture_requires_started_runtime_and_precedes_sessions(monkeypatch, tmp_path):
    runtime, client = fake_runtime(monkeypatch, tmp_path)
    client._sessions = {}
    with pytest.raises(RuntimeError, match='baseline must precede'):
        runtime.capture_process_fence()
    try:
        await runtime.start()
        fence = runtime.capture_process_fence()
        with pytest.raises(RuntimeError, match='baseline must precede'):
            runtime.capture_process_fence()
        client._sessions['owned'] = SimpleNamespace(session_id='owned')
        assert fence.is_settled() is True
    finally:
        await runtime.close()
    with pytest.raises(RuntimeError, match='settlement is unavailable'):
        fence.is_settled()


@async_test
async def test_process_fence_refuses_capture_after_session_creation(monkeypatch, tmp_path):
    runtime, client = fake_runtime(monkeypatch, tmp_path)
    client._sessions = {'already-created': SimpleNamespace(session_id='already-created')}
    try:
        await runtime.start()
        with pytest.raises(RuntimeError, match='baseline must precede'):
            runtime.capture_process_fence()
    finally:
        await runtime.close()


def fence_fixture():
    root = module._Process(100, 1000, 1, 100, 'S')
    processes = [root]
    client = SimpleNamespace(_sessions={'owned': SimpleNamespace(session_id='owned')})
    runtime = SimpleNamespace(
        alive=True, _client=client,
        _tree=SimpleNamespace(handshake_verified=True, live=lambda: list(processes)),
        _observe=lambda: None,
    )
    return module._RuntimeProcessFence(runtime), runtime, processes


@pytest.mark.parametrize('sessions', [{}, {'one': SimpleNamespace(session_id='one'),
    'two': SimpleNamespace(session_id='two')}, None, [], {'owned': None},
    {'wrong-key': SimpleNamespace(session_id='owned')}])
def test_process_fence_requires_one_consistent_session_and_failure_is_sticky(sessions):
    fence, runtime, _ = fence_fixture()
    runtime._client._sessions = sessions
    with pytest.raises(RuntimeError, match='settlement is unavailable'):
        fence.is_settled()
    runtime._client._sessions = {'owned': SimpleNamespace(session_id='owned')}
    with pytest.raises(RuntimeError, match='settlement is unavailable'):
        fence.is_settled()


@pytest.mark.parametrize('change', ['object', 'key', 'id', 'client'])
def test_process_fence_rejects_session_or_client_identity_replacement(change):
    fence, runtime, _ = fence_fixture()
    assert fence.is_settled() is True
    current = runtime._client._sessions['owned']
    if change == 'object':
        runtime._client._sessions['owned'] = SimpleNamespace(session_id='owned')
    elif change == 'key':
        runtime._client._sessions = {'different': current}
    elif change == 'id':
        current.session_id = 'different'
    else:
        runtime._client = SimpleNamespace(_sessions={'owned': current})
    with pytest.raises(RuntimeError, match='settlement is unavailable'):
        fence.is_settled()


@pytest.mark.parametrize('change', ['session', 'disconnect', 'handshake'])
def test_process_fence_revalidates_ownership_after_census(change):
    fence, runtime, _ = fence_fixture()

    def observe():
        if change == 'session':
            runtime._client._sessions['owned'] = SimpleNamespace(session_id='owned')
        elif change == 'disconnect':
            runtime.alive = False
        else:
            runtime._tree.handshake_verified = False

    runtime._observe = observe
    with pytest.raises(RuntimeError, match='settlement is unavailable'):
        fence.is_settled()


def test_process_fence_matches_pid_and_start_ticks_not_pid_alone():
    fence, _runtime, processes = fence_fixture()
    assert fence.is_settled() is True
    processes[:] = [module._Process(100, 2000, 1, 100, 'S')]
    assert fence.is_settled() is False
    processes[:] = [module._Process(100, 1000, 1, 100, 'S')]
    assert fence.is_settled() is True


def test_process_fence_second_census_catches_late_owned_process():
    fence, runtime, processes = fence_fixture()
    calls = []

    def observe():
        calls.append(True)
        if len(calls) == 2:
            processes.append(module._Process(101, 2000, 100, 100, 'S'))

    runtime._observe = observe
    assert fence.is_settled() is False
    assert len(calls) == 2


def test_process_fence_census_error_is_sanitized_and_sticky():
    fence, runtime, _ = fence_fixture()

    def observe():
        raise OSError('private process identity payload')

    runtime._observe = observe
    with pytest.raises(RuntimeError) as error:
        fence.is_settled()
    assert str(error.value) == 'Copilot owned process settlement is unavailable'
    assert error.value.__context__ is None
    runtime._observe = lambda: None
    with pytest.raises(RuntimeError):
        fence.is_settled()


@async_test
async def test_real_owned_child_after_baseline_blocks_but_unrelated_child_does_not(
    monkeypatch, tmp_path, unrelated_child,
):
    runtime, client = fake_runtime(monkeypatch, tmp_path)
    client._sessions = {}
    trigger = tmp_path / 'spawn-child'
    pid_file = tmp_path / 'owned-child.pid'
    stop = tmp_path / 'stop-child'
    child_code = (
        'import time; from pathlib import Path; '
        f'p=Path({str(stop)!r}); '
        '\nwhile not p.exists(): time.sleep(0.005)'
    )
    client.fixture_code = (
        'import subprocess,sys,time; from pathlib import Path\n'
        f'while not Path({str(trigger)!r}).exists(): time.sleep(0.005)\n'
        f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}], start_new_session=True)\n'
        f'Path({str(pid_file)!r}).write_text(str(child.pid))\n'
        'child.wait()\n'
        'time.sleep(60)\n'
    )
    try:
        await runtime.start()
        fence = runtime.capture_process_fence()
        client._sessions['owned'] = SimpleNamespace(session_id='owned')
        assert fence.is_settled() is True
        assert unrelated_child.poll() is None
        trigger.write_text('spawn')
        async with asyncio.timeout(2):
            while not pid_file.exists():
                await asyncio.sleep(0.005)
        child_pid = int(pid_file.read_text())
        assert fence.is_settled() is False
        assert child_pid in {process.pid for process in runtime._tree.live()}
        assert unrelated_child.pid not in {process.pid for process in runtime._tree.live()}
        stop.write_text('stop')
        async with asyncio.timeout(2):
            while not fence.is_settled():
                await asyncio.sleep(0.005)
        assert unrelated_child.poll() is None
    finally:
        stop.write_text('stop')
        await runtime.close()
    assert unrelated_child.poll() is None
