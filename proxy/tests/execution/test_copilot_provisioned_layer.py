"""A service lifetime verifies installation before owning any session resources."""

import asyncio
from pathlib import Path

import pytest

import config
from core.layers.copilot import provisioned_layer as module
from core.layers.copilot.provisioning import CopilotProvisioningError, ProvisionedCopilotPaths
from core.layers.copilot.sandbox_home import SandboxHomeError


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "installation"
    root.mkdir(mode=0o700)
    for name in ("records", "state", "homes", "runtime"):
        (root / name).mkdir(mode=0o700)
    runtime = root / "runtime/copilot-runtime"
    runtime.write_text("inert runtime fixture")
    paths = ProvisionedCopilotPaths(root, runtime, root / "records", root / "state", root / "homes")
    calls = []

    def load(candidate, *, forbidden_roots):
        calls.append((candidate, forbidden_roots))
        return paths

    monkeypatch.setattr(module, "load", load)
    monkeypatch.setattr(module, "check_sdk", lambda: None)
    return paths, calls


@pytest.mark.asyncio
async def test_lifetime_excludes_platform_mounts_and_releases_only_descriptors(installation):
    paths, calls = installation
    extra = Path("/extra-mount")
    async with module.open_provisioned_layer(paths.root, forbidden_roots=(extra,)) as layer:
        homes = layer._homes
        child = homes.get("session-fixture")
        (child / "preserved").write_text("history")
        assert layer._runtime_path == paths.runtime_path
    assert calls == [(paths.root, (config.AGENTS_DIR, config.MCPS_DIR, config.PLATFORM_CONFIG_DIR, extra))]
    with pytest.raises(SandboxHomeError):
        homes.get("new-session")
    assert (child / "preserved").read_text() == "history"
    assert layer._closing.done()


@pytest.mark.asyncio
async def test_body_failure_still_seals_layer_and_closes_homes(installation):
    paths, _ = installation
    with pytest.raises(ValueError, match="body fixture"):
        async with module.open_provisioned_layer(paths.root) as layer:
            raise ValueError("body fixture")
    assert layer._closing.done()
    with pytest.raises(SandboxHomeError):
        _ = layer._homes.root


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "check_sdk"])
async def test_failed_preflight_does_not_open_homes(installation, monkeypatch, operation):
    paths, _ = installation

    def fail(*args, **kwargs):
        raise CopilotProvisioningError("fixture preflight failure")

    def unexpected(*args, **kwargs):
        pytest.fail("No homes should be opened before preflight succeeds")

    monkeypatch.setattr(module, operation, fail)
    monkeypatch.setattr(module, "CopilotSandboxHomes", unexpected)
    with pytest.raises(CopilotProvisioningError):
        async with module.open_provisioned_layer(paths.root):
            pytest.fail("Failed installation was admitted")


@pytest.mark.asyncio
async def test_cancelled_body_joins_layer_cleanup_and_closes_homes(installation):
    paths, _ = installation
    entered = asyncio.Event()
    holder = []

    async def service():
        async with module.open_provisioned_layer(paths.root) as layer:
            holder.append(layer)
            entered.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(service())
    await asyncio.wait_for(entered.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert holder[0]._closing.done()
    with pytest.raises(SandboxHomeError):
        _ = holder[0]._homes.root
