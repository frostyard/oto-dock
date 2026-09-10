"""Raw model validation and isolated catalog lifecycle without SDK or inference."""

import asyncio
from copy import deepcopy
from dataclasses import make_dataclass
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot import catalog as module  # noqa: E402
from core.layers.copilot.credentials import CopilotAccountScope  # noqa: E402


def model(**changes):
    return {"id": "fixture-model", "name": "Fixture model", "capabilities": {}, **changes}


@pytest.mark.parametrize("policy,expected,available", [
    (None, "unconfigured", True), ({"state": "enabled"}, "enabled", True),
    ({"state": "unconfigured"}, "unconfigured", True), ({"state": "disabled"}, "disabled", False),
    ({"state": "future"}, "unknown", False),
])
def test_normalized_policy_is_not_an_entitlement_claim(policy, expected, available):
    rows = module.normalize_models({"models": [model(policy=policy, billing={"multiplier": 0.5},
                                                      private_token="must not project")]})
    assert rows == [{"id": "fixture-model", "name": "Fixture model", "policy": expected,
                     "available": available, "multiplier": 0.5}]


@pytest.mark.parametrize("response", [None, [], {}, {"models": None}, {"models": {}},
    {"models": [None]}, {"models": [model(), model()]},
    {"models": [model(id=str(n)) for n in range(201)]}])
def test_invalid_inventory_fails_closed(response):
    with pytest.raises(module.CopilotCatalogError, match="inventory is invalid"):
        module.normalize_models(response)


@pytest.mark.parametrize("changes", [
    {"id": 123}, {"id": True}, {"id": ""}, {"id": " model"}, {"id": "x" * 257},
    {"name": None}, {"name": ["coerced"]}, {"name": "bad\nname"},
    {"capabilities": None}, {"policy": {}}, {"policy": {"state": True}},
    {"policy": "enabled"}, {"billing": "bill"}, {"billing": {"multiplier": True}},
    {"billing": {"multiplier": "1"}}, {"billing": {"multiplier": -1}},
    {"billing": {"multiplier": float("nan")}}, {"billing": {"multiplier": float("inf")}},
    {"billing": {"multiplier": 1001}}, {"billing": {"multiplier": 10 ** 1000}},
])
def test_sdk_coercions_never_turn_malformed_data_into_rows(changes):
    with pytest.raises(module.CopilotCatalogError):
        module.normalize_models({"models": [model(**changes)]})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "states"
    root.mkdir(mode=0o700)
    selected = SimpleNamespace(platform_session_id="catalog-session", account_id="account-one",
                               scope=CopilotAccountScope.personal("human"), user_sub="human")
    context = SimpleNamespace(agent="agent", role="viewer")
    scenario = SimpleNamespace(context=context, calls=[], authenticated=True, response={"models": [model()]},
                               entered=asyncio.Event(), release=None, close_entered=asyncio.Event(),
                               close_release=None, close_error=False, suppress_cancel=False, invalid=None)
    state = ModuleType("core.session.session_state")
    state.get_session_security = lambda sid: scenario.context
    monkeypatch.setitem(sys.modules, "core.session.session_state", state)

    class Builder:
        def __init__(self, cfg):
            self.cfg = cfg

    sandbox = ModuleType("core.sandbox.sandbox")
    sandbox.SandboxBuilder = Builder
    monkeypatch.setitem(sys.modules, "core.sandbox.sandbox", sandbox)

    class Guard:
        valid = True
        credential = object()

        async def authorize(self):
            scenario.calls.append("authorize")
            if not self.valid:
                raise RuntimeError("private credential")

        async def close(self):
            scenario.calls.append("guard.close")
            self.valid = False

    guard = Guard()

    async def acquire(account, scope, on_invalid):
        assert account == selected.account_id and scope == selected.scope
        scenario.invalid = on_invalid
        scenario.calls.append("acquire")
        return guard

    class Runtime:
        alive = False
        forced_cleanup = False

        def __init__(self, builder, **options):
            scenario.runtime, scenario.builder, scenario.options = self, builder, options

        async def start(self):
            self.alive = True
            scenario.calls.append("start")
            return SimpleNamespace(get_auth_status=self.auth, _client=SimpleNamespace(request=self.request))

        async def auth(self):
            scenario.calls.append("auth")
            return SimpleNamespace(isAuthenticated=scenario.authenticated)

        async def request(self, method, params, timeout):
            assert (method, params, timeout) == ("models.list", {}, 10)
            scenario.calls.append("models.list")
            scenario.entered.set()
            if scenario.release is not None:
                try:
                    await scenario.release.wait()
                except asyncio.CancelledError:
                    if not scenario.suppress_cancel:
                        raise
            return deepcopy(scenario.response)

        async def close(self):
            scenario.calls.append("runtime.close")
            scenario.close_entered.set()
            if scenario.close_release is not None:
                await scenario.close_release.wait()
            if scenario.close_error:
                raise RuntimeError("private runtime close")
            self.alive = False

    monkeypatch.setattr(module.CopilotLeaseGuard, "acquire", acquire)
    monkeypatch.setattr(module, "SandboxedCopilotRuntime", Runtime)
    owner = module.CopilotCatalogOwner(selected, runtime_path=tmp_path / "runtime", state_root=root)
    owner.prepare()
    scenario.owner, scenario.root, scenario.guard = owner, root, guard
    fields = dict(host_agents_dir=Path("/real-agents"), host_mcps_dir=Path("/real-mcps"),
                                       extra_ro_binds=["/private"], mcp_dir_binds=["/other"],
                                       trusted_runtime_mounts=["verified-runtime"], net_forwards=["1"], net_allow_hosts=[],
                                       role="viewer", username="human", agent_name="real", is_admin_agent=False,
                                       host_claude_dir=Path("/real-home"), config_visible=False, knowledge_rw=False,
                                       mount_shared=True, external=False, external_home="", isolated_config_home=False,
                                       knowledge_libraries=[], mcp_sandbox_mounts=[])
    scenario.sandbox = make_dataclass("Config", fields.keys(), frozen=True)(**fields)
    yield scenario
    if owner._state is not None:
        owner._state.discard()


@pytest.mark.asyncio
async def test_catalog_only_calls_raw_inventory_and_removes_temporary_state_after_join(harness):
    owner = harness.owner
    await owner.start(harness.sandbox)
    assert owner.alive and owner.rows[0]["id"] == "fixture-model"
    cfg = harness.builder.cfg
    assert cfg.host_agents_dir.is_relative_to(harness.root)
    assert cfg.host_mcps_dir.is_relative_to(harness.root) and cfg.host_claude_dir.is_relative_to(harness.root)
    assert cfg.role == "viewer" and not cfg.username and not cfg.config_visible
    assert not cfg.extra_ro_binds and not cfg.mcp_dir_binds and not cfg.mcp_sandbox_mounts and not cfg.knowledge_libraries
    assert cfg.net_forwards == ["1"] and cfg.trusted_runtime_mounts == ["verified-runtime"]
    assert harness.options["credential"] is harness.guard.credential
    await owner.close()
    assert list(harness.root.iterdir()) == []
    assert harness.calls == ["acquire", "start", "auth", "authorize", "models.list", "authorize", "runtime.close", "guard.close"]
    assert owner.alive is False


@pytest.mark.asyncio
async def test_unauthenticated_runtime_does_not_query_models(harness):
    harness.authenticated = False
    with pytest.raises(module.CopilotCatalogError) as caught:
        await harness.owner.start(harness.sandbox)
    await harness.owner.close()
    assert "models.list" not in harness.calls and not list(harness.root.iterdir())
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_revocation_during_inventory_cancels_and_joins_before_discard(harness):
    harness.release = asyncio.Event()
    task = asyncio.create_task(harness.owner.start(harness.sandbox))
    await harness.entered.wait()
    harness.guard.valid = False
    harness.invalid()
    with pytest.raises(module.CopilotCatalogError):
        await task
    await harness.owner.close()
    assert not harness.runtime.alive and not list(harness.root.iterdir()) and harness.owner.rows is None


@pytest.mark.asyncio
async def test_changed_context_invalidates_idle_inventory_wait(harness):
    harness.release = asyncio.Event()
    task = asyncio.create_task(harness.owner.start(harness.sandbox))
    await harness.entered.wait()
    harness.context = deepcopy(harness.context)
    async with asyncio.timeout(1):
        with pytest.raises(module.CopilotCatalogError):
            await task
        await harness.owner.close()
    assert not harness.runtime.alive and harness.owner.rows is None


@pytest.mark.asyncio
async def test_cancelled_rpc_suppressing_cancellation_cannot_publish_rows(harness):
    harness.release, harness.suppress_cancel = asyncio.Event(), True
    task = asyncio.create_task(harness.owner.start(harness.sandbox))
    await harness.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await harness.owner.close()
    assert harness.owner.rows is None and not harness.runtime.alive


@pytest.mark.asyncio
async def test_double_cancelled_close_retains_state_until_runtime_join(harness):
    await harness.owner.start(harness.sandbox)
    harness.close_release = asyncio.Event()
    task = asyncio.create_task(harness.owner.close())
    await harness.close_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and list(harness.root.iterdir())
    harness.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(harness.root.iterdir()) and not harness.runtime.alive


@pytest.mark.asyncio
async def test_failed_runtime_cleanup_retains_private_state(harness):
    await harness.owner.start(harness.sandbox)
    harness.close_error = True
    with pytest.raises(module.CopilotCatalogError, match="cleanup is incomplete"):
        await harness.owner.close()
    assert list(harness.root.iterdir()) and harness.runtime.alive
    assert "guard.close" in harness.calls
