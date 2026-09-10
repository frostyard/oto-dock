"""Explicit host runtime assets do not broaden the MCP manifest mount policy."""

from dataclasses import replace

import pytest

from core.sandbox import sandbox
from core.sandbox.sandbox import SandboxBuilder, SandboxConfig, SandboxMount, resolve_sandbox_config


@pytest.fixture
def paths(tmp_path):
    agents, mcps, assets = (tmp_path / name for name in ("agents", "mcps", "runtime-assets"))
    for directory in (agents / "demo/workspace", agents / "demo/knowledge", mcps, assets):
        directory.mkdir(parents=True)
    home = tmp_path / "scratch"
    home.mkdir()
    cfg = SandboxConfig(
        role="manager", username="", agent_name="demo", is_admin_agent=False,
        host_agents_dir=agents, host_mcps_dir=mcps, host_claude_dir=home, net_forwards=["8400"],
    )
    return cfg, assets


def test_external_assets_rejected_as_mcp_but_explicitly_admitted_read_only(paths):
    cfg, assets = paths
    mount = SandboxMount(str(assets), "/opt/copilot-runtime", "ro")
    assert SandboxBuilder(replace(cfg, mcp_sandbox_mounts=[mount]))._conditional_mcp_mounts() == []
    trusted = SandboxBuilder(replace(cfg, trusted_runtime_mounts=[mount]))
    expected = ["--ro-bind", str(assets.resolve()), "/opt/copilot-runtime"]
    assert trusted._trusted_runtime_mounts() == expected
    command = trusted.build_command_prefix(["/opt/copilot-runtime/copilot-runtime"])
    position = command.index(str(assets.resolve()))
    assert command[position - 1:position + 2] == expected


def test_default_empty_runtime_mounts_preserve_command(paths):
    cfg, _ = paths
    before = SandboxBuilder(cfg).build_command_prefix(["fixture"])
    assert SandboxBuilder(cfg)._trusted_runtime_mounts() == []
    assert before == SandboxBuilder(replace(cfg, trusted_runtime_mounts=[])).build_command_prefix(["fixture"])
    assert cfg.trusted_runtime_mounts is not replace(cfg, trusted_runtime_mounts=[]).trusted_runtime_mounts


@pytest.mark.parametrize("destination", [
    "/", "/workspace", "/users", "/etc", "/etc/runtime", "/proc/runtime", "/usr/bin/runtime",
    "/var/lib/runtime", "/config/runtime", "/knowledge/runtime", "/tmp/runtime",
    "/workspace/.claude/runtime", "/users/alice/.codex/runtime", "/opt/.ssh/runtime",
    "relative", "/opt/../opt/runtime", "/opt/./runtime", "/opt//runtime", "//opt/runtime",
    "/opt/runtime/", "/opt/runtime\x00", "/opt\\runtime",
])
def test_protected_or_noncanonical_destination_raises(paths, destination):
    cfg, assets = paths
    cfg = replace(cfg, trusted_runtime_mounts=[SandboxMount(str(assets), destination, "ro")])
    with pytest.raises(ValueError, match="Invalid trusted runtime sandbox mount"):
        SandboxBuilder(cfg).build_command_prefix(["fixture"])


@pytest.mark.parametrize("mode", ["rw", "", "RO", None, True])
def test_non_read_only_mode_rejected(paths, mode):
    cfg, assets = paths
    cfg = replace(cfg, trusted_runtime_mounts=[SandboxMount(str(assets), "/opt/runtime", mode)])
    with pytest.raises(ValueError, match="Invalid trusted runtime sandbox mount"):
        SandboxBuilder(cfg)._trusted_runtime_mounts()


@pytest.mark.parametrize("kind", ["missing", "file", "relative", "traversal", "nul"])
def test_invalid_source_rejected(paths, kind):
    cfg, assets = paths
    source = str(assets)
    if kind == "missing":
        source = str(assets / "missing")
    elif kind == "file":
        path = assets / "runtime-file"
        path.write_text("fixture")
        source = str(path)
    elif kind == "relative":
        source = "relative/assets"
    elif kind == "traversal":
        source = str(assets / "../runtime-assets")
    else:
        source += "\x00"
    cfg = replace(cfg, trusted_runtime_mounts=[SandboxMount(source, "/opt/runtime", "ro")])
    with pytest.raises(ValueError, match="Invalid trusted runtime sandbox mount"):
        SandboxBuilder(cfg)._trusted_runtime_mounts()


@pytest.mark.parametrize("mounts", [None, (), {}, [None], [{"host": "/tmp", "sandbox": "/opt/runtime", "mode": "ro"}]])
def test_manifest_dict_or_invalid_collection_cannot_enter_trusted_channel(paths, mounts):
    cfg, _ = paths
    with pytest.raises(ValueError, match="Invalid trusted runtime sandbox mount"):
        SandboxBuilder(replace(cfg, trusted_runtime_mounts=mounts))._trusted_runtime_mounts()


@pytest.mark.parametrize("destination", ["/opt/runtime", "/opt/runtime/subdir", "/opt", "//opt/runtime"])
def test_later_mcp_cannot_shadow_trusted_runtime(paths, destination):
    cfg, assets = paths
    manifest_source = cfg.host_mcps_dir / "replacement"
    manifest_source.mkdir()
    cfg = replace(cfg,
                  trusted_runtime_mounts=[SandboxMount(str(assets), "/opt/runtime", "ro")],
                  mcp_sandbox_mounts=[SandboxMount(str(manifest_source), destination, "rw")])
    builder = SandboxBuilder(cfg)
    assert builder._conditional_mcp_mounts() == []
    assert builder._trusted_runtime_mounts() == ["--ro-bind", str(assets), "/opt/runtime"]


def test_duplicate_or_overlapping_trusted_mount_destinations_rejected(paths):
    cfg, assets = paths
    for destination in ("/opt/runtime", "/opt/runtime/child", "/opt"):
        cfg = replace(cfg, trusted_runtime_mounts=[
            SandboxMount(str(assets), "/opt/runtime", "ro"),
            SandboxMount(str(assets), destination, "ro"),
        ])
        with pytest.raises(ValueError, match="Invalid trusted runtime sandbox mount"):
            SandboxBuilder(cfg)._trusted_runtime_mounts()


@pytest.mark.parametrize("destination", ["//etc", "//workspace", "//users", "//proc/runtime"])
def test_double_slash_cannot_alias_protected_mcp_destination(paths, destination):
    cfg, _ = paths
    source = cfg.host_mcps_dir / "fixture"
    source.mkdir()
    cfg = replace(cfg, mcp_sandbox_mounts=[SandboxMount(str(source), destination, "ro")])
    assert SandboxBuilder(cfg)._conditional_mcp_mounts() == []


def test_actual_resolver_preserves_explicit_channel_without_changing_mcp_roots(paths, monkeypatch):
    from storage import db_knowledge_libraries

    cfg, assets = paths
    monkeypatch.setattr(sandbox.app_config, "AGENTS_DIR", cfg.host_agents_dir)
    monkeypatch.setattr(sandbox.app_config, "MCPS_DIR", cfg.host_mcps_dir)
    monkeypatch.setattr(db_knowledge_libraries, "attachments_for_consumer", lambda _: [])
    mount = SandboxMount(str(assets), "/opt/runtime", "ro")
    selected = [mount]
    resolved = resolve_sandbox_config(
        cfg.role, cfg.username, cfg.agent_name, cfg.is_admin_agent, cfg.host_claude_dir,
        net_forwards=["8400"], mcp_dir_binds=[], trusted_runtime_mounts=selected,
    )
    selected.clear()
    assert resolved.trusted_runtime_mounts == [mount]
    assert resolved.host_mcps_dir == cfg.host_mcps_dir
    assert resolved.mcp_sandbox_mounts == []
    assert SandboxBuilder(resolved)._trusted_runtime_mounts() == ["--ro-bind", str(assets), "/opt/runtime"]


@pytest.mark.parametrize("username", ["", "alice"])
def test_isolated_home_is_last_mount_and_shadows_only_selected_claude_home(paths, username):
    cfg, _ = paths
    cfg.host_claude_dir.chmod(0o700)
    cwd = cfg.host_agents_dir / "demo" / (f"users/{username}" if username else "workspace")
    legacy = cwd / ".claude"
    legacy.mkdir(parents=True)
    (legacy / "legacy-credential").write_text("private fixture")
    cfg = replace(cfg, username=username)
    default = SandboxBuilder(cfg).workspace_mount_table()
    builder = SandboxBuilder(replace(cfg, isolated_config_home=True))
    isolated = builder.workspace_mount_table()
    assert isolated[:-1] == default
    assert isolated[-1] == sandbox.Mount(str(cfg.host_claude_dir), builder.get_cwd() + "/.claude", True)
    assert (legacy / "legacy-credential").read_text() == "private fixture"
    assert not list(cfg.host_claude_dir.iterdir())
    command = builder._workspace_mounts()
    assert command[-3:] == ["--bind", str(cfg.host_claude_dir), builder.get_cwd() + "/.claude"]


def test_isolated_user_home_creates_mountpoint_before_read_only_parent_bind(paths):
    cfg, _ = paths
    cfg.host_claude_dir.chmod(0o700)
    cfg = replace(cfg, username="alice", isolated_config_home=True)
    assert not (cfg.host_agents_dir / "demo/users/alice/.claude").exists()
    mounts = SandboxBuilder(cfg).workspace_mount_table()
    assert (cfg.host_agents_dir / "demo/users/alice/.claude").is_dir()
    assert mounts[-1].host == str(cfg.host_claude_dir)


@pytest.mark.parametrize("kind", ["mode", "relative", "missing", "file", "symlink", "ancestor_link", "owner"])
def test_unsafe_isolated_home_rejected(paths, tmp_path, monkeypatch, kind):
    cfg, _ = paths
    source = cfg.host_claude_dir
    source.chmod(0o700)
    if kind == "mode":
        source.chmod(0o755)
    elif kind == "relative":
        source = type(source)("relative")
    elif kind == "missing":
        source = tmp_path / "missing"
    elif kind == "file":
        source = tmp_path / "file"
        source.write_text("fixture")
    elif kind == "symlink":
        link = tmp_path / "linked-home"
        link.symlink_to(source, target_is_directory=True)
        source = link
    elif kind == "ancestor_link":
        link = tmp_path / "linked-parent"
        link.symlink_to(tmp_path, target_is_directory=True)
        source = link / source.name
    elif kind == "owner":
        uid = source.stat().st_uid
        monkeypatch.setattr(sandbox.os, "getuid", lambda: uid + 1)
    with pytest.raises(ValueError, match="Invalid isolated sandbox configuration home"):
        SandboxBuilder(replace(cfg, host_claude_dir=source, isolated_config_home=True)).workspace_mount_table()


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_isolated_home_flag_requires_exact_bool(paths, value):
    cfg, _ = paths
    with pytest.raises(ValueError, match="Invalid isolated sandbox configuration home"):
        SandboxBuilder(replace(cfg, isolated_config_home=value)).workspace_mount_table()


def test_isolated_home_rejects_symlinked_destination(paths, tmp_path):
    cfg, _ = paths
    cfg.host_claude_dir.chmod(0o700)
    target = tmp_path / "other-home"
    target.mkdir()
    (cfg.host_agents_dir / "demo/workspace/.claude").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="Invalid isolated sandbox configuration home"):
        SandboxBuilder(replace(cfg, isolated_config_home=True)).workspace_mount_table()
    assert not list(target.iterdir())
