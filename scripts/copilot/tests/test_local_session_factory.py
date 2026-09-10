"""Local factory composition with real leases/policy and an inert SDK transport."""

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.credentials import (  # noqa: E402
    CopilotAccountScope, CopilotCredential, CredentialKind,
)
from core.layers.copilot.lease import CopilotLeaseGuard  # noqa: E402
from core.layers.copilot.native_tool_policy import _SCHEMAS  # noqa: E402
from core.layers.copilot.permissions import CopilotPermissionBridge  # noqa: E402


@dataclass
class ToolsListRequest:
    model: str | None = None


class FakeSession:
    def __init__(self, client, options):
        self.client = client
        self.options = options
        self.session_id = options["session_id"]
        self.messages = []
        self.serial = 0
        self.disconnected = False
        self.rpc = SimpleNamespace(
            tasks=SimpleNamespace(list=self.tasks, refresh=self.refresh),
            permissions=SimpleNamespace(pending_requests=self.permissions),
            queue=SimpleNamespace(pending_items=self.queue),
            metadata=SimpleNamespace(is_processing=self.processing),
        )

    async def tasks(self, **_):
        return SimpleNamespace(tasks=[])

    async def refresh(self, **_):
        return SimpleNamespace()

    async def permissions(self, **_):
        return SimpleNamespace(items=[])

    async def queue(self, **_):
        return SimpleNamespace(items=[], steering_messages=[])

    async def processing(self, **_):
        return SimpleNamespace(processing=False)

    def emit(self, kind, data=None):
        self.serial += 1
        self.options["on_event"]({"id": f"event-{self.serial}", "type": kind, "data": data or {}})

    async def send(self, prompt, *, mode):
        await self.client.scenario.checkpoint("sdk.send")
        self.messages.append((prompt, mode))
        message_id = f"message-{len(self.messages)}"
        self.emit("user.message", {"messageId": message_id, "content": prompt})
        self.emit("assistant.turn_start", {"turnId": f"turn-{len(self.messages)}"})
        self.emit("assistant.message", {"messageId": f"reply-{len(self.messages)}", "content": "fixture reply"})
        self.emit("session.idle")
        return message_id

    async def abort(self):
        self.emit("session.idle", {"aborted": True})

    async def disconnect(self):
        if not self.disconnected:
            self.client.scenario.log.append("sdk.disconnect")
            self.disconnected = True
            self.client._sessions.pop(self.session_id, None)


class FakeClient:
    def __init__(self, scenario):
        self.scenario = scenario
        self._sessions = {}
        self._client = SimpleNamespace(request=self.raw_request)
        self.opened = []
        self.rpc = SimpleNamespace(tools=SimpleNamespace(list=self.catalog))

    async def catalog(self, request, **_):
        await self.scenario.checkpoint("catalog")
        return SimpleNamespace(to_dict=lambda: {"tools": [
            {"name": name, "parameters": schema} for name, schema in _SCHEMAS.items()
        ]})

    async def create_session(self, **options):
        self.scenario.log.append("sdk.create.begin")
        session = FakeSession(self, options)
        self._sessions[session.session_id] = session
        self.opened.append(session)
        await self.scenario.checkpoint("sdk.create")
        return session

    async def resume_session(self, session_id, **options):
        options["session_id"] = session_id
        session = FakeSession(self, options)
        self._sessions[session_id] = session
        self.opened.append(session)
        await self.scenario.checkpoint("sdk.resume")
        return session

    async def get_auth_status(self):
        await self.scenario.checkpoint("auth")
        return SimpleNamespace(isAuthenticated=getattr(self.scenario, "authenticated", True), login="fixture-login")

    async def raw_request(self, method, params, *, timeout):
        assert method == "models.list" and params == {} and timeout == 10
        await self.scenario.checkpoint("models")
        if hasattr(self.scenario, "raw_models"):
            return {"models": self.scenario.raw_models}
        models = getattr(self.scenario, "models", [SimpleNamespace(id="gpt-5-mini", policy=None)])
        if not isinstance(models, list):
            return {"models": models}
        return {"models": [{"id": item.id, "name": "Fixture model", "capabilities": {},
                            "policy": vars(item.policy) if isinstance(item.policy, SimpleNamespace) else item.policy}
                           for item in models]}

    async def list_models(self):
        raise AssertionError("Lossy SDK model conversion must not run")


class FakeRuntime:
    def __init__(self, scenario, builder, **options):
        self.scenario = scenario
        self.builder = builder
        self.options = options
        self.alive = False
        self.closed = False
        self.forced_cleanup = False
        self.client = FakeClient(scenario)
        self.closed_event = asyncio.Event()
        scenario.runtimes.append(self)
        scenario.log.append("runtime.construct")

    async def start(self):
        self.alive = True  # Partial startup has resources that still require cleanup.
        await self.scenario.checkpoint("runtime.start")
        return self.client

    def capture_process_fence(self):
        assert self.alive and not self.client._sessions
        self.scenario.log.append("runtime.fence")
        return SimpleNamespace(is_settled=lambda: self.alive)

    async def close(self):
        if self.closed:
            return
        await self.scenario.checkpoint("runtime.close")
        self.alive = False
        self.closed = True
        self.closed_event.set()


class Scenario:
    def __init__(self):
        self.log = []
        self.runtimes = []
        self.stages = {}
        self.entered = {}
        self.reads = []
        self.guards = []
        self.bindings = []
        self.current = {
            "account-a": CopilotCredential("account-a", "github:user:1", "generation-a",
                                           CredentialKind.USER_TOKEN, "gho_fixture_a", None),
            "account-b": CopilotCredential("account-b", "github:user:2", "generation-b",
                                           CredentialKind.USER_TOKEN, "gho_fixture_b", None),
        }

    async def checkpoint(self, stage):
        self.log.append(stage)
        self.entered.setdefault(stage, asyncio.Event()).set()
        if stage in self.stages:
            await self.stages[stage]()

    async def read(self, account_id, scope):
        self.reads.append((account_id, scope))
        await self.checkpoint("lease.read")
        value = self.current[account_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def acquire(self, account_id, scope, *, on_invalid, **options):
        initial = await self.read(account_id, scope)
        guard = CopilotLeaseGuard(initial, scope, read_credential=self.read, on_invalid=on_invalid,
                                  check_interval=0.01, read_timeout=0.2)
        self.guards.append(guard)
        try:
            await guard.start()
        except BaseException:
            await guard.close()
            raise
        return guard

    def bind(self, session_id, requests, *, working_directory, **options):
        self.bindings.append((session_id, requests, working_directory))

        async def decide(name, arguments):
            return {"decision": "allow"}

        owner_valid = options.pop("owner_valid", lambda: True)
        return CopilotPermissionBridge(requests, decide=decide, context_valid=owner_valid,
                                       working_directory=working_directory, **options)


def install_fake_sdk(monkeypatch):
    sdk = ModuleType("copilot")
    rpc = ModuleType("copilot.rpc")
    rpc.ToolsListRequest = ToolsListRequest
    sdk.rpc = rpc
    sdk_tools = ModuleType("copilot.tools")
    sdk_tools.Tool = SimpleNamespace
    sdk_tools.ToolResult = SimpleNamespace
    monkeypatch.setitem(sys.modules, "copilot.tools", sdk_tools)
    monkeypatch.setitem(sys.modules, "copilot", sdk)
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)


@dataclass(frozen=True)
class SecurityContext:
    role: str = "manager"
    agent: str = "fixture"
    username: str = ""
    is_admin_agent: bool = False
    target_kind: str = "local"
    session_scope: str = "agent"
    principal: str = "user"
    external_home: str = ""
    work_cwd: str = ""
    config_visible: bool = False
    available_scopes: tuple = ("agent",)
    knowledge_rw: bool = False
    knowledge_libraries: tuple = ()


@dataclass
class SandboxConfig:
    host_agents_dir: Path
    runtime_directory: Path
    role: str = "manager"
    agent_name: str = "fixture"
    username: str = ""
    is_admin_agent: bool = False
    external: bool = False
    external_home: str = ""
    config_visible: bool = False
    mount_shared: bool = True
    knowledge_rw: bool = False
    knowledge_libraries: tuple = ()


class SandboxBuilder:
    def __init__(self, cfg):
        self.cfg = cfg

    def get_cwd(self):
        return "/workspace"

    def workspace_mount_table(self):
        return [SimpleNamespace(host=str(self.cfg.host_agents_dir / self.cfg.agent_name / "workspace"),
                                sandbox="/workspace", rw=True)]

    def build_command_prefix(self, args):
        return ["bwrap", "--bind", self.workspace_mount_table()[0].host, "/workspace",
                "--ro-bind", str(self.cfg.runtime_directory), "/opt/copilot-runtime", "--", *args]


@pytest.fixture
def harness(monkeypatch, tmp_path):
    install_fake_sdk(monkeypatch)
    scenario = Scenario()
    scenario.contexts = {"platform-a": SecurityContext(), "platform-b": SecurityContext()}
    policy = ModuleType("auth.path_policy")
    policy.SecurityContext = SecurityContext
    sandbox = ModuleType("core.sandbox.sandbox")
    sandbox.SandboxBuilder = SandboxBuilder
    state = ModuleType("core.session.session_state")
    state.get_session_security = scenario.contexts.get
    scenario.mode = "default"
    state.get_session_mode = lambda sid: scenario.mode
    monkeypatch.setitem(sys.modules, "auth.path_policy", policy)
    monkeypatch.setitem(sys.modules, "core.sandbox.sandbox", sandbox)
    monkeypatch.setitem(sys.modules, "core.session.session_state", state)
    from core.layers.copilot import local_session as module
    from core.layers.copilot.session_records import CopilotSessionRecords

    monkeypatch.setattr(module, "SandboxedCopilotRuntime",
                        lambda builder, **options: FakeRuntime(scenario, builder, **options))
    monkeypatch.setattr(module, "bind_platform_authority", scenario.bind)
    monkeypatch.setattr(module.CopilotLeaseGuard, "acquire", scenario.acquire)
    workspace = tmp_path / "agents/fixture/workspace"
    workspace.mkdir(parents=True)
    assets = tmp_path / "runtime"
    assets.mkdir()
    runtime_path = assets / "copilot-runtime"
    runtime_path.write_text("inert")
    record_root, state_root = tmp_path / "records", tmp_path / "state"
    record_root.mkdir(mode=0o700)
    state_root.mkdir(mode=0o700)
    records = CopilotSessionRecords(record_root, state_root=state_root)
    scenario.module = module
    scenario.builder = SandboxBuilder(SandboxConfig(workspace.parents[1], assets))
    scenario.runtime_path = runtime_path
    scenario.records = records
    scenario.record_root = record_root
    scenario.state_root = state_root
    return scenario


def config(harness, **changes):
    values = dict(platform_session_id="platform-a", account_id="account-a",
                  scope=CopilotAccountScope.personal("alice"), user_sub="alice",
                  model="gpt-5-mini", enabled_tools=frozenset({"view", "bash"}))
    values.update(changes)
    return harness.module.CopilotLocalSessionConfig(**values)


async def open_session(harness, selected=None, **options):
    return await harness.module.CopilotLocalSession.open(
        selected or config(harness), builder=harness.builder, runtime_path=harness.runtime_path,
        records=harness.records, turn_timeout=2, **options,
    )


def documents(harness):
    import json
    return [json.loads(path.read_text()) for path in harness.record_root.glob("*.json")]


async def clean(harness):
    harness.stages.clear()
    for runtime in harness.runtimes:
        await runtime.close()
    for guard in harness.guards:
        await guard.close()


@pytest.mark.asyncio
async def test_selected_account_scope_and_private_runtime_profile_are_exact(harness, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gho_wrong_ambient_account")
    owner = await open_session(harness)
    try:
        runtime = harness.runtimes[0]
        selected = runtime.options["credential"]
        assert selected == harness.current["account-a"]
        assert all(pair == ("account-a", CopilotAccountScope.personal("alice")) for pair in harness.reads)
        assert runtime.options["environment"] == {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
        assert runtime.options["working_directory"] == harness.builder.cfg.host_agents_dir / "fixture/workspace"
        assert runtime.options["session_state"].path.parent == harness.state_root
        assert harness.bindings[0][0] == "platform-a" and harness.bindings[0][2] == "/workspace"
        assert harness.log.index("runtime.fence") < harness.log.index("sdk.create.begin")
        options = runtime.client.opened[0].options
        assert options["available_tools"] == ["builtin:bash", "builtin:view"]
        assert options["tools"] == [] and options["mcp_servers"] == {}
        assert options["enable_config_discovery"] is False and options["enable_file_hooks"] is False
        assert options["enable_host_git_operations"] is False and options["enable_session_store"] is True
        assert not any(hasattr(owner, name) for name in ("client", "runtime", "session", "supervisor"))
        assert "gho_" not in repr(owner)
    finally:
        await owner.close()
        await clean(harness)
    assert documents(harness)[0]["status"] == "active"  # No completed turn to resume.


@pytest.mark.asyncio
async def test_completed_turn_reauthorizes_and_commits_resume_only_after_runtime_close(harness):
    owner = await open_session(harness)
    before = len(harness.reads)
    try:
        events = [event async for event in owner.stream("fixture prompt")]
        assert sum(event.type == "done" for event in events) == 1
        assert len(harness.reads) > before
        assert documents(harness)[0]["status"] == "active"
    finally:
        await owner.close()
        await clean(harness)
    assert harness.runtimes[0].closed and not harness.guards[0].valid
    assert documents(harness)[0]["status"] == "ready"
    assert len(list(harness.state_root.iterdir())) == 1  # History survives owner close.


@pytest.mark.asyncio
async def test_two_accounts_and_scopes_have_isolated_idle_revocation(harness):
    first = await open_session(harness)
    second = await open_session(harness, config(
        harness, platform_session_id="platform-b", account_id="account-b",
        scope=CopilotAccountScope.platform(), user_sub="bob",
    ))
    try:
        harness.current["account-a"] = replace(harness.current["account-a"], revision="revoked-generation")
        await asyncio.wait_for(harness.runtimes[0].closed_event.wait(), 1)
        assert not harness.runtimes[1].closed and harness.guards[1].valid
        events = [event async for event in second.stream("other account remains usable")]
        assert sum(event.type == "done" for event in events) == 1
        assert set(harness.reads) == {
            ("account-a", CopilotAccountScope.personal("alice")),
            ("account-b", CopilotAccountScope.platform()),
        }
    finally:
        await first.close()
        await second.close()
        await clean(harness)
    assert next(doc for doc in documents(harness) if doc["profile"]["account_id"] == "account-a")["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["runtime.start", "auth", "models", "catalog", "sdk.create"])
async def test_startup_failures_close_partial_runtime_lease_and_leave_record_unresumable(harness, stage):
    async def fail():
        raise RuntimeError("gho_private_fixture_failure")

    harness.stages[stage] = fail
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError) as caught:
            await open_session(harness)
        assert "gho_" not in str(caught.value) and caught.value.__context__ is None
        assert all(runtime.closed for runtime in harness.runtimes)
        assert all(not guard.valid for guard in harness.guards)
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["runtime.start", "auth", "models", "catalog", "sdk.create"])
async def test_cancellation_during_startup_joins_every_acquired_resource(harness, stage):
    entered = asyncio.Event()

    async def block():
        entered.set()
        await asyncio.Event().wait()

    harness.stages[stage] = block
    task = asyncio.create_task(open_session(harness))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert all(runtime.closed for runtime in harness.runtimes)
        assert all(not guard.valid for guard in harness.guards)
        assert documents(harness)[0]["status"] == "active"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await clean(harness)


@pytest.mark.asyncio
async def test_credential_rotation_during_runtime_start_cannot_return_ready_owner(harness):
    async def rotate():
        harness.current["account-a"] = replace(harness.current["account-a"], revision="changed")
        await asyncio.sleep(0.03)

    harness.stages["runtime.start"] = rotate
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness)
        assert harness.runtimes[0].closed and not harness.guards[0].valid
        assert not harness.runtimes[0].client.opened
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_repeated_close_cancellation_cannot_release_record_before_runtime_stops(harness):
    owner = await open_session(harness)
    stream = owner.stream("fixture prompt")
    await anext(stream)  # Abandoning a stream cannot mark history ready.
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_close():
        entered.set()
        await release.wait()

    harness.stages["runtime.close"] = held_close
    task = asyncio.create_task(owner.close())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and harness.runtimes[0].alive
        assert documents(harness)[0]["status"] == "active"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.runtimes[0].closed and not harness.guards[0].valid
        await owner.close()
        await stream.aclose()
    finally:
        release.set()
        await stream.aclose()
        await asyncio.gather(task, return_exceptions=True)
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "role", "remote", "scope"])
async def test_mismatched_live_authority_rejects_before_any_account_or_runtime_access(harness, mutation):
    if mutation == "missing":
        harness.contexts.pop("platform-a")
    else:
        field, value = {"role": ("role", "viewer"), "remote": ("target_kind", "user_remote"),
                        "scope": ("available_scopes", ())}[mutation]
        harness.contexts["platform-a"] = replace(harness.contexts["platform-a"], **{field: value})
    with pytest.raises(harness.module.CopilotLocalSessionError):
        await open_session(harness)
    assert not harness.reads and not harness.runtimes and not documents(harness)


@pytest.mark.asyncio
async def test_clean_resume_keeps_history_native_identity_and_selects_new_credential_generation(harness):
    first = await open_session(harness)
    try:
        events = [event async for event in first.stream("first fixture")]
        assert any(event.type == "done" for event in events)
    finally:
        await first.close()
    record = documents(harness)[0]
    harness.current["account-a"] = replace(harness.current["account-a"], revision="new-generation",
                                           token="gho_new_fixture_credential")
    resumed = await open_session(harness, resume=True)
    try:
        runtime = harness.runtimes[1]
        assert runtime.options["credential"].revision == "new-generation"
        assert runtime.client.opened[0].session_id == record["native_session_id"]
        assert runtime.client.opened[0].options["continue_pending_work"] is False
        assert documents(harness)[0]["allocation"] == record["allocation"]
        assert "sdk.resume" in harness.log
        events = [event async for event in resumed.stream("second fixture")]
        assert sum(event.type == "done" for event in events) == 1
    finally:
        await resumed.close()
        await clean(harness)
    assert documents(harness)[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_duplicate_writer_refusal_does_not_close_first_owner_or_choose_another_session(harness):
    first = await open_session(harness)
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness)
        assert len(harness.runtimes) == 1 and harness.runtimes[0].alive
        assert harness.guards[0].valid and not harness.guards[1].valid
        events = [event async for event in first.stream("still owned")]
        assert sum(event.type == "done" for event in events) == 1
        assert len(documents(harness)) == 1
    finally:
        await first.close()
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"system_prompt": "different profile"}, {"enabled_tools": frozenset({"view"})}])
async def test_resume_profile_mismatch_never_starts_runtime_or_falls_back_to_create(harness, changes):
    first = await open_session(harness)
    try:
        events = [event async for event in first.stream("persist fixture")]
        assert any(event.type == "done" for event in events)
    finally:
        await first.close()
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness, config(harness, **changes), resume=True)
        assert len(harness.runtimes) == 1
        assert documents(harness)[0]["status"] == "ready"
        assert not harness.guards[-1].valid
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_context_replacement_denies_native_hook_immediately_and_closes_idle_owner(harness):
    owner = await open_session(harness)
    runtime = harness.runtimes[0]
    session = runtime.client.opened[0]
    harness.contexts["platform-a"] = replace(harness.contexts["platform-a"])
    try:
        decision = await session.options["hooks"]["on_pre_tool_use"]({
            "sessionId": session.session_id, "workingDirectory": "/workspace", "timestamp": 1,
            "toolName": "view", "toolArgs": {"path": "/workspace/fixture"},
        }, {"session_id": session.session_id})
        assert decision["permissionDecision"] == "deny"
        await asyncio.wait_for(runtime.closed_event.wait(), 1)
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await anext(owner.stream("must not dispatch"))
        assert session.messages == []
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["unauthenticated", "missing_model", "disabled_model", "duplicate_model"])
async def test_authentication_or_model_refusal_closes_runtime_before_sdk_session(harness, condition):
    if condition == "unauthenticated":
        harness.authenticated = False
    elif condition == "missing_model":
        harness.models = []
    elif condition == "disabled_model":
        harness.models = [SimpleNamespace(id="gpt-5-mini", policy=SimpleNamespace(state="disabled"))]
    else:
        harness.models = [SimpleNamespace(id="gpt-5-mini", policy=None)] * 2
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness)
        assert harness.runtimes[0].closed and not harness.runtimes[0].client.opened
        assert not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_runtime_cleanup_error_still_closes_lease_and_forbids_ready_commit(harness):
    owner = await open_session(harness)
    events = [event async for event in owner.stream("completed before cleanup failure")]
    assert any(event.type == "done" for event in events)
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("gho_private_shutdown_error")

    harness.stages["runtime.close"] = fail_once
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError) as caught:
            await owner.close()
        assert "gho_" not in str(caught.value) and caught.value.__context__ is None
        assert not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_cancelled_startup_cannot_return_owner_when_sdk_swallows_cancellation(harness):
    entered = asyncio.Event()

    async def late_sdk_result():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return  # Adversarial SDK returns an acquired resource after cancellation.

    harness.stages["sdk.create"] = late_sdk_result
    task = asyncio.create_task(open_session(harness))
    result = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert harness.runtimes[0].closed and not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        if isinstance(result, harness.module.CopilotLocalSession):
            await result.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await clean(harness)


@pytest.mark.asyncio
async def test_stream_failure_is_sanitized_and_prevents_prior_success_marking_record_ready(harness):
    owner = await open_session(harness)
    first = [event async for event in owner.stream("first turn completed")]
    assert any(event.type == "done" for event in first)

    async def fail():
        raise RuntimeError("gho_private_sdk_dispatch_failure")

    harness.stages["sdk.send"] = fail
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError) as caught:
            _ = [event async for event in owner.stream("second turn failed")]
        assert "gho_" not in str(caught.value) and caught.value.__context__ is None
        assert harness.runtimes[0].closed and not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [
    pytest.param(SimpleNamespace(state="future-unknown-state"), id="unknown-state"),
    pytest.param(SimpleNamespace(), id="missing-state"),
    pytest.param(SimpleNamespace(state=["enabled"]), id="malformed-state"),
    pytest.param("enabled", id="malformed-policy"),
])
async def test_unknown_or_malformed_model_policy_never_opens_native_session(harness, policy):
    harness.models = [SimpleNamespace(id="gpt-5-mini", policy=policy)]
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError) as caught:
            await open_session(harness)
        assert caught.value.__context__ is None
        assert harness.runtimes[0].closed and not harness.runtimes[0].client.opened
        assert not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("inventory", [None, {}, (SimpleNamespace(id="gpt-5-mini", policy=None),)])
async def test_nonlist_model_inventory_is_unavailable_without_fallback(harness, inventory):
    harness.models = inventory
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness)
        assert harness.runtimes[0].closed and not harness.runtimes[0].client.opened
        assert not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["enabled", "unconfigured"])
async def test_known_model_policy_states_allow_the_explicit_selected_model(harness, state):
    harness.models = [SimpleNamespace(id="gpt-5-mini", policy=SimpleNamespace(state=state))]
    owner = await open_session(harness)
    try:
        assert harness.runtimes[0].client.opened[0].options["model"] == "gpt-5-mini"
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_late_sdk_startup_result_cannot_escape_independent_deadline(harness, monkeypatch):
    original = harness.module.CopilotLocalSession._open

    async def expiring_open(owner, *arguments):
        async def late_result():
            assert asyncio.current_task().cancelling() == 0
            owner._startup_deadline = asyncio.get_running_loop().time() - 1

        harness.stages["sdk.create"] = late_result
        return await original(owner, *arguments)

    monkeypatch.setattr(harness.module.CopilotLocalSession, "_open", expiring_open)
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError) as caught:
            await open_session(harness)
        assert caught.value.__context__ is None
        assert len(harness.runtimes[0].client.opened) == 1  # The late SDK resource existed.
        assert harness.runtimes[0].closed and not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_resume_permission_mode_change_rejects_before_new_runtime(harness):
    owner = await open_session(harness)
    _ = [event async for event in owner.stream("complete")]
    await owner.close()
    count = len(harness.runtimes)
    harness.mode = "acceptEdits"
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness, resume=True)
        assert len(harness.runtimes) == count
    finally:
        await clean(harness)


def reasoning_model(levels=None):
    return {"id": "gpt-5-mini", "name": "Fixture model", "capabilities": {"supports": {"reasoningEffort": True}},
            "supportedReasoningEfforts": levels if levels is not None else ["low", "high"],
            "defaultReasoningEffort": "low"}


@pytest.mark.asyncio
async def test_explicit_reasoning_effort_reaches_guarded_create_and_cold_resume(harness):
    harness.raw_models = [reasoning_model()]
    selected = config(harness, reasoning_effort="high")
    first = await open_session(harness, selected)
    _ = [event async for event in first.stream("complete")]
    await first.close()
    resumed = await open_session(harness, selected, resume=True)
    try:
        assert harness.runtimes[0].client.opened[0].options["reasoning_effort"] == "high"
        assert harness.runtimes[1].client.opened[0].options["reasoning_effort"] == "high"
    finally:
        await resumed.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_provider_default_preserves_old_reference_digest_and_omits_sdk_option(harness):
    import hashlib
    import json

    selected = config(harness)
    owner = await open_session(harness, selected)
    try:
        old_reference = {
            "sandbox": harness.builder.cfg, "context": harness.contexts["platform-a"],
            "system_prompt": selected.system_prompt, "runtime_path": harness.runtime_path.resolve(),
            "cwd": harness.builder.get_cwd(), "permission_mode": "default",
        }
        old_bytes = json.dumps(harness.module._canonical(old_reference), sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode()
        assert documents(harness)[0]["profile"]["config_digest"] == hashlib.sha256(old_bytes).hexdigest()
        assert "reasoning_effort" not in harness.runtimes[0].client.opened[0].options
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("before,after", [(None, "high"), ("high", None), ("high", "low")])
async def test_resume_effort_change_rejects_before_starting_another_runtime(harness, before, after):
    harness.raw_models = [reasoning_model()]
    first = await open_session(harness, config(harness, reasoning_effort=before))
    _ = [event async for event in first.stream("complete")]
    await first.close()
    count = len(harness.runtimes)
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness, config(harness, reasoning_effort=after), resume=True)
        assert len(harness.runtimes) == count
    finally:
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [
    reasoning_model(["low"]),
    {"id": "gpt-5-mini", "name": "Fixture", "capabilities": {}},
    {**reasoning_model(), "supportedReasoningEfforts": ["low", True]},
])
async def test_unadvertised_or_malformed_effort_never_creates_native_session(harness, model):
    harness.raw_models = [model]
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness, config(harness, reasoning_effort="high"))
        assert harness.runtimes[0].closed and not harness.runtimes[0].client.opened
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_resume_rechecks_current_model_effort_advertisement(harness):
    harness.raw_models = [reasoning_model()]
    selected = config(harness, reasoning_effort="high")
    first = await open_session(harness, selected)
    _ = [event async for event in first.stream("complete")]
    await first.close()
    harness.raw_models = [reasoning_model(["low"])]
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness, selected, resume=True)
        assert harness.runtimes[1].closed and not harness.runtimes[1].client.opened
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


def usage_event(**data):
    import uuid
    return {"id": str(uuid.uuid4()), "type": "assistant.usage", "data": {"model": "reported-model", **data}}


@pytest.mark.asyncio
async def test_usage_observed_before_create_returns_after_done_and_during_cleanup(harness):
    received = []

    async def emit_early():
        harness.runtimes[0].client.opened[0].options["on_event"](usage_event(inputTokens=1))

    harness.stages["sdk.create"] = emit_early
    owner = await open_session(harness, usage_observer=received.append)
    sdk = harness.runtimes[0].client.opened[0]
    try:
        first = [item async for item in owner.stream("first")]
        assert first[-1].type == "done"
        queued = list(owner._supervisor._events)
        late = usage_event(outputTokens=2)
        sdk.options["on_event"](late)
        sdk.options["on_event"](late)
        assert list(owner._supervisor._events) == queued
        second = [item async for item in owner.stream("second")]
        assert [item.type for item in second if item.type != "system"] == [
            item.type for item in first if item.type != "system"]
        assert not any("assistant.usage" in str(item.data) for item in second)
        assert all(item.type != "usage" for item in first + second)
        assert len(received) == 2

        async def emit_closing():
            sdk.options["on_event"](usage_event(cacheReadTokens=3))

        harness.stages["runtime.close"] = emit_closing
        await owner.close()
        assert len(received) == 3 and received[-1]["cache_read_tokens"] == 3
        assert owner._usage is None and documents(harness)[0]["status"] == "ready"
    finally:
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["idle", "runtime.close"])
async def test_usage_callback_failure_joins_owner_and_keeps_history_active(harness, phase):
    def fail(frame):
        raise RuntimeError("gho_private_callback_error")

    owner = await open_session(harness, usage_observer=fail)
    assert owner.usage_source_closed is False
    _ = [item async for item in owner.stream("completed")]
    sdk = harness.runtimes[0].client.opened[0]

    async def emit():
        sdk.options["on_event"](usage_event(inputTokens=1))

    if phase == "idle":
        await emit()
    else:
        harness.stages[phase] = emit
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError) as caught:
            await owner.close()
        assert "gho_" not in str(caught.value) and caught.value.__context__ is None
        assert harness.runtimes[0].closed and not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
        assert owner.usage_source_closed is True
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_usage_without_observer_drops_without_changing_next_turn(harness):
    owner = await open_session(harness)
    try:
        first = [item async for item in owner.stream("first")]
        sdk = harness.runtimes[0].client.opened[0]
        sdk.options["on_event"]({"type": "assistant.usage", "data": {"model": None}})
        second = [item async for item in owner.stream("second")]
        assert [item.type for item in second if item.type != "system"] == [
            item.type for item in first if item.type != "system"]
        assert not any("assistant.usage" in str(item.data) for item in second)
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("reports_alive", [True, False])
async def test_failed_runtime_close_cannot_prove_usage_source_joined(harness, reports_alive):
    owner = await open_session(harness, usage_observer=lambda frame: None)

    async def fail():
        harness.runtimes[0].alive = reports_alive
        raise RuntimeError("private shutdown failure")

    harness.stages["runtime.close"] = fail
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await owner.close()
        assert owner.usage_source_closed is False
        assert owner._usage is not None
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [True, False])
async def test_startup_observer_retains_initialized_owner_when_runtime_rollback_fails(harness, rollback_fails):
    captured = []

    def capture(owner):
        assert harness.runtimes == [] and harness.guards == []
        assert owner.usage_source_closed is False and owner.closed is False
        captured.append(owner)

    async def fail_startup():
        raise RuntimeError("private startup failure")

    async def fail_close():
        raise RuntimeError("private rollback failure")

    harness.stages["sdk.create"] = fail_startup
    if rollback_fails:
        harness.stages["runtime.close"] = fail_close
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError) as caught:
            await open_session(harness, on_owner=capture)
        assert caught.value.__context__ is None and "private" not in str(caught.value)
        assert len(captured) == 1
        assert captured[0].usage_source_closed is (not rollback_fails)
        if rollback_fails:
            with pytest.raises(harness.module.CopilotLocalSessionError):
                await captured[0].close()
            assert harness.runtimes[0].alive is True
        else:
            await captured[0].close()
            assert harness.runtimes[0].closed is True
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_owner_observer_precedes_usage_observer_validation(harness):
    captured = []
    with pytest.raises(harness.module.CopilotLocalSessionError):
        await open_session(harness, on_owner=captured.append, usage_observer="invalid")
    assert len(captured) == 1 and captured[0].usage_source_closed is True
    assert harness.runtimes == [] and harness.guards == []


@pytest.mark.asyncio
async def test_delegation_callback_bound_before_create_and_reattached_on_resume(harness):
    calls = []

    async def handler(call_id, arguments):
        calls.append((call_id, arguments))
        return "child result"

    selected = config(harness, delegation_targets=("repo", "qa"))
    first = await open_session(harness, selected, delegate_handler=handler)
    try:
        options = harness.runtimes[0].client.opened[0].options
        assert options["available_tools"] == ["builtin:bash", "builtin:view", "custom:oto_delegate"]
        assert len(options["tools"]) == 1 and options["tools"][0].name == "oto_delegate"
        assert options["mcp_servers"] == {}
        _ = [item async for item in first.stream("complete")]
        await first.close()
        resumed = await open_session(harness, selected, resume=True, delegate_handler=handler)
        try:
            old_tool = options["tools"][0]
            new_tool = harness.runtimes[1].client.opened[0].options["tools"][0]
            assert old_tool is not new_tool and old_tool.handler != new_tool.handler
            assert calls == []  # Registration/resume never executes delegation.
        finally:
            await resumed.close()
    finally:
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("before,after", [((), ("repo",)), (("repo",), ()), (("repo",), ("qa",))])
async def test_delegation_profile_change_cannot_resume_or_launch_runtime(harness, before, after):
    async def handler(*_):
        return "result"

    first = await open_session(harness, config(harness, delegation_targets=before),
                               delegate_handler=handler if before else None)
    _ = [item async for item in first.stream("complete")]
    await first.close()
    count = len(harness.runtimes)
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness, config(harness, delegation_targets=after), resume=True,
                               delegate_handler=handler if after else None)
        assert len(harness.runtimes) == count
    finally:
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("targets,handler", [((), True), (("repo",), None), (("repo",), "not-callable")])
async def test_delegation_handler_and_profile_must_agree_before_resources(harness, targets, handler):
    with pytest.raises(harness.module.CopilotLocalSessionError):
        await open_session(harness, config(harness, delegation_targets=targets), delegate_handler=handler)
    assert harness.runtimes == [] and harness.guards == []
