"""Real platform registration/permission state around an inert local owner."""

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import uuid

import pytest
import pytest_asyncio

from auth.path_policy import SecurityContext
from core.events.common_events import CommonEvent
from core.execution_layer import AgentConfig
from core.layers.copilot import layer as module
from core.layers.copilot.credentials import CopilotAccountScope
from core.layers.copilot.sandbox_home import CopilotSandboxHomes
from core.layers.copilot.session_records import CopilotSessionRecords
from core.sandbox.sandbox import SandboxConfig
from core.session import session_state as state


class Owner:
    def __init__(self):
        self.alive = True
        self.closed = False
        self.ended = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.close_release = None
        self.close_error = False
        self.stream_release = None
        self.messages = []
        self.calls = []

    async def close(self):
        if self.closed:
            return
        self.alive = False
        self.close_entered.set()
        if self.close_release:
            await self.close_release.wait()
        if self.close_error:
            raise RuntimeError("private cleanup error")
        self.closed = True
        self.ended.set()

    async def wait_closed(self):
        await self.ended.wait()

    async def stream(self, prompt):
        self.messages.append(prompt)
        completed = False
        try:
            yield CommonEvent("text", {"text": "fixture"})
            if self.stream_release:
                await self.stream_release.wait()
            completed = True
            yield CommonEvent("done")
        finally:
            if not completed:
                await self.close()

    async def steer(self, *args):
        self.calls.append(("steer", args))
        return True

    async def interrupt(self, *args):
        self.calls.append(("interrupt", args))
        return True

    async def abort(self, *args):
        self.calls.append(("abort", args))
        return True


@pytest_asyncio.fixture
async def harness(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "_save_session_security", lambda: None)
    monkeypatch.setattr(state, "_save_sessions", lambda: None)
    registry = ModuleType("core.session.session_manager")
    registered = set()
    registry.is_session_registered = registered.__contains__
    monkeypatch.setitem(sys.modules, "core.session.session_manager", registry)
    roots = [tmp_path / name for name in ("records", "state", "homes")]
    for root in roots:
        root.mkdir(mode=0o700)
    records = CopilotSessionRecords(roots[0], state_root=roots[1])
    homes = CopilotSandboxHomes(roots[2])
    assets = tmp_path / "mcps/runtime"
    assets.mkdir(parents=True)
    runtime = assets / "copilot-runtime"
    runtime.write_text("inert runtime")
    scenario = SimpleNamespace(
        registered=registered, owners=[], opens=[], resolve_calls=[], layers=[], ids=[], open_hook=None,
        records=records, homes=homes, runtime=runtime,
    )

    def resolve(**options):
        scenario.resolve_calls.append(options)
        return SandboxConfig(
            role=options["role"], username=options["username"], agent_name=options["agent_name"],
            is_admin_agent=options["is_admin_agent"], host_agents_dir=tmp_path / "agents",
            host_mcps_dir=tmp_path / "mcps", host_claude_dir=options["host_claude_dir"],
            config_visible=options["config_visible"], mount_shared=options["mount_shared"],
            knowledge_rw=options["knowledge_rw"], trusted_runtime_mounts=options["trusted_runtime_mounts"],
            net_forwards=["8400"], isolated_config_home=options["isolated_config_home"],
        )

    async def open_owner(config, **options):
        # Test real registration occurs first, including UUID session stamping.
        context = state.get_session_security(config.platform_session_id)
        assert context is not None and context.cli_session_id == config.platform_session_id
        scenario.opens.append((config, options))
        owner = Owner()
        scenario.owners.append(owner)
        if scenario.open_hook:
            await scenario.open_hook(owner)
        return owner

    monkeypatch.setattr(module, "resolve_sandbox_config", resolve)
    monkeypatch.setattr(module.CopilotLocalSession, "open", open_owner)

    def layer():
        value = module.CopilotExecutionLayer(runtime_path=runtime, records=records, homes=homes)
        scenario.layers.append(value)
        return value

    def identity():
        value = str(uuid.uuid4())
        scenario.ids.append(value)
        return value

    scenario.layer, scenario.identity = layer, identity
    yield scenario
    for owner in scenario.owners:
        if owner.close_release:
            owner.close_release.set()
        if owner.stream_release:
            owner.stream_release.set()
        owner.close_error = False
        await owner.close()
    for value in scenario.layers:
        for sid in list(value._sessions):
            with suppress(module.CopilotLayerError):
                await value.close_session(sid)
    # Failed cleanup deliberately retains claims; fixture releases only its own IDs.
    for sid in scenario.ids:
        module._claims.pop(sid, None)
        state.cleanup_session_permission_state(sid)
        state._sessions.pop(sid, None)
    homes.close()


def config(**changes):
    return module.CopilotAgentConfig(**{
        "agent_name": "demo", "user_sub": "alice", "client_type": "dashboard", "model": "fixture-model",
        "permission_mode": "default", "account_id": "account-one",
        "account_scope": CopilotAccountScope.personal("alice"), "enabled_tools": frozenset({"view", "bash"}),
        "security_context": SecurityContext(role="viewer", username="alice", agent="demo", is_admin_agent=False,
                                            config_visible=False), **changes,
    })


async def start(harness, **changes):
    layer, sid = harness.layer(), harness.identity()
    await layer.start_session(sid, config(**changes))
    return layer, sid, harness.owners[-1]


async def pending_permission(sid):
    request = str(uuid.uuid4())
    entered = asyncio.Event()

    async def wait():
        entered.set()
        return await state.wait_for_permission(request, session_id=sid, timeout=3)

    waiting = asyncio.create_task(wait())
    await asyncio.wait_for(entered.wait(), 1)
    assert state.get_permission_request_session(request) == sid
    return request, waiting


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"mcp_config_path": "private-config"}, {"credential_env": {"TOKEN": "secret"}},
    {"extra_env": {"TOKEN": "secret"}}, {"mcp_secret_bundles": {"server": {"token": "secret"}}},
    {"effort": "high"}, {"subscription_id": "generic-payer"}, {"subscription_user_sub": "alice"},
    {"sandbox_host_claude_dir": "/untrusted"}, {"codex_thread_id": "other-engine"},
    {"use_native_permissions": True}, {"interactive": True}, {"interactive_first_prompt": "unexpected"},
    {"work_cwd": "/outside"}, {"default_execution_mode": "interactive"}, {"term": "xterm"},
    {"multi_value_envs": {"PATH": ":"}}, {"fallback_reason": "wrong-engine"},
    {"execution_target": "remote"}, {"execution_path": "codex-cli"}, {"client_type": "task"},
    {"permission_mode": "auto"}, {"permission_mode": "bypassPermissions"},
    {"chat_id": "other-engine-history"}, {"interactive_theme": "dark"},
    {"model": ""}, {"resume": "true"}, {"account_scope": None}, {"enabled_tools": frozenset({"web_fetch"})},
])
async def test_unsupported_config_rejected_before_registration_or_factory(harness, changes):
    layer, sid = harness.layer(), harness.identity()
    with pytest.raises(module.CopilotLayerError) as error:
        await layer.start_session(sid, config(**changes))
    assert error.value.__context__ is None
    assert state.get_session_security(sid) is None and sid not in state._session_modes
    assert harness.opens == [] and sid not in module._claims


@pytest.mark.asyncio
async def test_plain_agent_config_cannot_implicitly_select_copilot_payer(harness):
    layer, sid = harness.layer(), harness.identity()
    with pytest.raises(module.CopilotLayerError):
        await layer.start_session(sid, AgentConfig(agent_name="demo"))
    assert state.get_session_security(sid) is None and harness.opens == []


@pytest.mark.asyncio
async def test_registration_stamps_context_and_uses_private_home_and_pinned_readonly_assets(harness):
    layer, sid, owner = await start(harness)
    registered = state.get_session_security(sid)
    assert registered.cli_session_id == sid
    assert state.get_session_client_type(sid) == "dashboard"
    assert state.get_session_mode(sid) == "default"
    options = harness.resolve_calls[0]
    assert Path(options["host_claude_dir"]).parent == harness.homes.root
    assert options["mcp_dir_binds"] == []
    assert options["isolated_config_home"] is True
    builder = harness.opens[0][1]["builder"]
    scratch_mounts = [mount for mount in builder.workspace_mount_table()
                      if Path(mount.host) == options["host_claude_dir"]]
    assert len(scratch_mounts) == 1 and scratch_mounts[0].rw is True
    assert scratch_mounts[0].sandbox == builder.get_cwd() + "/.claude"
    mount = options["trusted_runtime_mounts"][0]
    assert mount.mode == "ro" and mount.sandbox == "/opt/copilot-runtime"
    assert await layer.get_session(sid) is owner
    assert await layer.is_session_alive(sid) is True
    assert await layer.is_session_process_dead(sid) is False
    await layer.close_session(sid)
    assert state.get_session_security(sid) is None
    assert await layer.is_session_process_dead(sid) is True


@pytest.mark.asyncio
async def test_duplicate_across_layer_instances_preserves_first_registration(harness):
    first, sid, owner = await start(harness)
    context = state.get_session_security(sid)
    with pytest.raises(module.CopilotLayerError):
        await harness.layer().start_session(sid, config())
    assert state.get_session_security(sid) is context
    assert owner.alive and await first.is_session_alive(sid)
    assert len(harness.opens) == 1


@pytest.mark.asyncio
async def test_concurrent_startup_reserves_before_factory_await_and_never_reports_dead(harness):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(_owner):
        entered.set()
        await release.wait()

    harness.open_hook = hold
    first, second, sid = harness.layer(), harness.layer(), harness.identity()
    pending = asyncio.create_task(first.start_session(sid, config()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        original = state.get_session_security(sid)
        assert await first.is_session_process_dead(sid) is False
        with pytest.raises(module.CopilotLayerError):
            await second.start_session(sid, config())
        assert state.get_session_security(sid) is original
        release.set()
        await pending
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["context", "registry"])
async def test_startup_scheduling_gap_rechecks_foreign_ownership_before_registration(harness, monkeypatch, foreign):
    layer, sid = harness.layer(), harness.identity()
    original = module.CopilotExecutionLayer._start
    replacement = replace(config().security_context, display_name="foreign owner")

    async def intercept(self, entry, local):
        if foreign == "context":
            state.register_session_state(sid, "plan", replacement)
        else:
            harness.registered.add(sid)
        return await original(self, entry, local)

    monkeypatch.setattr(module.CopilotExecutionLayer, "_start", intercept)
    with pytest.raises(module.CopilotLayerError):
        await layer.start_session(sid, config())
    assert harness.opens == []
    if foreign == "context":
        assert state.get_session_security(sid).display_name == "foreign owner"
        assert state.get_session_mode(sid) == "plan"


@pytest.mark.asyncio
async def test_cancelled_startup_closes_late_owner_before_releasing_context(harness):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def late(_owner):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    harness.open_hook = late
    layer, sid = harness.layer(), harness.identity()
    pending = asyncio.create_task(layer.start_session(sid, config()))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    await asyncio.wait_for(cancelled.wait(), 1)
    assert await layer.is_session_process_dead(sid) is False
    assert state.get_session_security(sid) is not None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert harness.owners[0].closed
    assert state.get_session_security(sid) is None
    assert sid not in module._claims


@pytest.mark.asyncio
async def test_repeated_close_cancellation_keeps_ownership_until_runtime_cleanup_finishes(harness):
    layer, sid, owner = await start(harness)
    owner.close_release = asyncio.Event()
    closing = asyncio.create_task(layer.close_session(sid))
    try:
        await owner.close_entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        assert await layer.is_session_process_dead(sid) is False
        assert state.get_session_security(sid) is not None
        assert sid in module._claims
        owner.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert owner.closed and sid not in module._claims
        assert state.get_session_security(sid) is None
    finally:
        owner.close_release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_independent_owner_death_reaps_owned_context_and_denies_pending_permission(harness):
    layer, sid, owner = await start(harness)
    _request, waiting = await pending_permission(sid)
    await owner.close()
    assert await asyncio.wait_for(waiting, 1) is False
    async with asyncio.timeout(1):
        while await layer.get_session(sid) is not None:
            await asyncio.sleep(0)
    assert state.get_session_security(sid) is None and sid not in module._claims


@pytest.mark.asyncio
async def test_replacement_context_and_its_permission_waiter_survive_old_owner_reaper(harness):
    layer, sid, owner = await start(harness)
    replacement = replace(config().security_context, display_name="replacement")
    state.register_session_state(sid, "plan", replacement)
    actual = state.get_session_security(sid)
    request, waiting = await pending_permission(sid)
    try:
        await owner.close()
        async with asyncio.timeout(1):
            while await layer.get_session(sid) is not None:
                await asyncio.sleep(0)
        assert state.get_session_security(sid) is actual
        assert state.get_session_mode(sid) == "plan"
        assert not waiting.done()
        state.resolve_permission(request, False)
        assert await waiting is False
    finally:
        state.resolve_permission(request, False)
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_hard_abort_is_not_blocked_by_held_producer_lock(harness):
    layer, sid, owner = await start(harness)
    async with layer.session_lock(sid):
        assert await asyncio.wait_for(layer.abort(sid), 1) is False
    assert owner.closed and owner.calls == []


@pytest.mark.asyncio
async def test_permission_response_cannot_approve_another_session(harness):
    first, first_id, _ = await start(harness)
    second, second_id, _ = await start(harness)
    request, waiting = await pending_permission(second_id)
    try:
        with pytest.raises(module.CopilotLayerError):
            await first.respond_permission(first_id, request, True)
        assert not waiting.done()
        with pytest.raises(module.CopilotLayerError):
            await second.respond_permission(second_id, request, 1)
        await second.respond_permission(second_id, request, False)
        assert await waiting is False
    finally:
        state.resolve_permission(request, False)
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_unsupported_live_features_do_not_dispatch_and_capability_mutation_does_not_persist(harness):
    layer, sid, owner = await start(harness)
    assert await layer.steer(sid, "new input") is False
    assert await layer.interrupt_for_queued(sid) is False
    assert await layer.can_resume_session(sid, agent_name="demo", username="alice") is False
    assert owner.calls == []
    caps = layer.capabilities
    assert not caps.supports_resume and not caps.supports_mcps and not caps.supports_subagents
    caps.permission_modes.append("bypassPermissions")
    caps.supports_mcps = True
    assert "bypassPermissions" not in layer.capabilities.permission_modes
    assert layer.capabilities.supports_mcps is False
    assert "error" in await layer.send_control_request(sid, "unknown")
    with pytest.raises(module.CopilotLayerError):
        await layer.change_model(sid, "different-model")
    with pytest.raises(module.CopilotLayerError):
        await layer.change_mode(sid, "auto")
    await layer.change_model(sid, "fixture-model")
    await layer.change_mode(sid, "default")


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_explicit_resume_and_typed_payer_are_forwarded_without_generic_discovery(harness, resume):
    layer, sid, _ = await start(harness, resume=resume)
    local, options = harness.opens[-1]
    assert options["resume"] is resume
    assert local.platform_session_id == sid and local.account_id == "account-one"
    assert local.scope == CopilotAccountScope.personal("alice")
    assert await layer.can_resume_session(sid) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"inject_time": True}, {"inject_time": 0}, {"attachments": []}, {"model": "other"}])
async def test_unsupported_message_options_reject_before_owner_dispatch(harness, options):
    layer, sid, owner = await start(harness)
    with pytest.raises(module.CopilotLayerError):
        _ = [event async for event in layer.send_message(sid, "private prompt", **options)]
    assert owner.messages == []


@pytest.mark.asyncio
async def test_abandoned_stream_closes_owner_then_reaps_platform_state(harness):
    layer, sid, owner = await start(harness)
    stream = layer.send_message(sid, "fixture", inject_time=False)
    assert (await anext(stream)).type == "text"
    await stream.aclose()
    assert owner.closed
    async with asyncio.timeout(1):
        while await layer.get_session(sid) is not None:
            await asyncio.sleep(0)
    assert state.get_session_security(sid) is None


@pytest.mark.asyncio
async def test_failed_cleanup_retains_unknown_process_tombstone_and_cross_instance_claim(harness):
    layer, sid, owner = await start(harness)
    owner.close_error = True
    with pytest.raises(module.CopilotLayerError) as error:
        await layer.close_session(sid)
    assert "private" not in str(error.value) and error.value.__context__ is None
    assert await layer.is_session_alive(sid) is False
    assert await layer.is_session_process_dead(sid) is False
    assert sid in module._claims
    with pytest.raises(module.CopilotLayerError):
        await harness.layer().start_session(sid, config())


@pytest.mark.asyncio
async def test_stale_producer_lock_cannot_dispatch_into_replacement_same_id(harness):
    layer, sid, first = await start(harness)
    async with layer.session_lock(sid):
        assert await layer.abort(sid) is False
        await layer.start_session(sid, config())
        replacement = harness.owners[-1]
        assert replacement is not first
        with pytest.raises(module.CopilotLayerError):
            _ = [event async for event in layer.send_message(sid, "stale producer")]
        assert replacement.messages == []
    async with layer.session_lock(sid):
        events = [event async for event in layer.send_message(sid, "fresh producer")]
    assert [event.type for event in events] == ["text", "done"]
    assert replacement.messages == ["fresh producer"]


@pytest.mark.asyncio
async def test_same_mode_request_detects_out_of_band_mode_change(harness):
    layer, sid, _ = await start(harness)
    state.set_session_mode(sid, "plan")
    with pytest.raises(module.CopilotLayerError):
        await layer.change_mode(sid, "default")
    assert state.get_session_mode(sid) == "plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"session_allowed_roots": ("/outside",)}, {"work_cwd": "/outside"},
    {"target_kind": "admin_remote"}, {"external_channel": "phone"},
    {"cli_session_id": "00000000-0000-4000-8000-000000000001"},
])
async def test_unsupported_context_scope_is_rejected_before_registration(harness, changes):
    layer, sid = harness.layer(), harness.identity()
    context = replace(config().security_context, **changes)
    with pytest.raises(module.CopilotLayerError):
        await layer.start_session(sid, config(security_context=context))
    assert state.get_session_security(sid) is None and harness.opens == []


@pytest.mark.asyncio
async def test_registry_owned_session_without_context_is_not_cleaned_by_failed_start(harness, monkeypatch):
    layer, sid = harness.layer(), harness.identity()
    original = module.CopilotExecutionLayer._start
    request, waiting = await pending_permission(sid)
    state.set_session_mode(sid, "plan")

    async def intercept(self, entry, local):
        harness.registered.add(sid)
        return await original(self, entry, local)

    monkeypatch.setattr(module.CopilotExecutionLayer, "_start", intercept)
    try:
        with pytest.raises(module.CopilotLayerError):
            await layer.start_session(sid, config())
        assert state.get_session_security(sid) is None
        assert state.get_session_mode(sid) == "plan"
        assert not waiting.done()
        state.resolve_permission(request, False)
        assert await waiting is False
    finally:
        state.resolve_permission(request, False)
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_partial_owned_registration_failure_cleans_mode_without_claiming_foreign_context(harness, monkeypatch):
    layer, sid = harness.layer(), harness.identity()

    def fail_security(*_args):
        raise RuntimeError("private registration persistence failure")

    monkeypatch.setattr(state, "set_session_security", fail_security)
    with pytest.raises(module.CopilotLayerError) as error:
        await layer.start_session(sid, config())
    assert error.value.__context__ is None and "private" not in str(error.value)
    assert sid not in state._session_modes and state.get_session_security(sid) is None
    assert sid not in module._claims and harness.opens == []


@pytest.mark.asyncio
async def test_preexisting_platform_registration_is_never_overwritten_or_cleaned(harness):
    layer, sid = harness.layer(), harness.identity()
    state.register_session_state(sid, "plan", replace(config().security_context, display_name="original"))
    original = state.get_session_security(sid)
    with pytest.raises(module.CopilotLayerError):
        await layer.start_session(sid, config())
    assert state.get_session_security(sid) is original
    assert state.get_session_mode(sid) == "plan"
    assert harness.opens == []


@pytest.mark.asyncio
async def test_config_snapshot_cannot_be_changed_by_caller_during_startup(harness):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(_owner):
        entered.set()
        await release.wait()

    harness.open_hook = hold
    layer, sid = harness.layer(), harness.identity()
    selected = config()
    pending = asyncio.create_task(layer.start_session(sid, selected))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        selected.model = "changed-model"
        selected.account_id = "changed-account"
        selected.permission_mode = "auto"
        release.set()
        await pending
        local, _ = harness.opens[0]
        assert local.model == "fixture-model" and local.account_id == "account-one"
        assert state.get_session_mode(sid) == "default"
        await layer.change_model(sid, "fixture-model")
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
