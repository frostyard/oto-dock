"""Agent-read-deny of its OWN CLI config files.

The agent's per-session `.claude/*.json` + `.codex/{config.toml,auth.json}` carry
this session's secrets — the broker capability token, the swapped-in HTTP bearer
(local), the session JWT, the Codex model token. A prompt-injected agent
must not be able to Read / cat / grep its own config and paste the token in chat.

Gate is mirrored across all channels: `auth.path_policy._check_read_path` (native
Read + local bash cat/grep route here) and `services.path_policy_v2.
_protected_path_denial` (remote + MCP arg-paths). Matched ONLY at a session scope
root, so a repo that itself uses Claude Code / Codex stays readable.

NOT a malicious-MCP boundary — a native same-uid MCP can open() the file directly.
"""

from __future__ import annotations

from pathlib import Path

from services import path_roles
from services.path_policy_v2 import _protected_path_denial
from auth.path_policy import (
    SecurityContext,
    _AGENTS_DIR,
    _check_bash,
    _check_read_path,
)


# ---------------------------------------------------------------------------
# is_protected_agent_config_path — the core matcher
# ---------------------------------------------------------------------------

PROTECTED = [
    "/users/alice/.claude/personal-assistant-abc123.json",
    "/users/alice/.claude/mcp-config.json",
    "/users/alice/.claude/github-mcp-personal-assistant.json",  # instance config
    "/workspace/.claude/mcp-config.json",
    "/knowledge/.claude/ctx.json",
    "/users/alice/.codex/config.toml",
    "/users/alice/.codex/auth.json",
    "/workspace/.codex/config.toml",
    # host form (resolved local path)
    "/home/x/docker/oto-dock/agents/pa/users/alice/.claude/pa-abc.json",
    # satellite-absolute form
    "/home/frank/.oto-dock/agents/pa/users/alice/.codex/config.toml",
    # an external caller's tree (a phone caller who is not a platform user):
    # sandbox-virtual, host, and the ephemeral (withheld-number) host form
    "/caller/.codex/config.toml",
    "/caller/.codex/auth.json",
    "/caller/.claude/settings.json",
    "/caller/.claude/.credentials.json",
    "/home/x/agents/pa/externals/phone/302101234567/.codex/config.toml",
    "/home/x/agents/pa/externals/phone/302101234567/.claude/pa-abc.json",
    "/home/x/agents/pa/externals/phone/_ephemeral/11111111-2222/.codex/config.toml",
]

NOT_PROTECTED = [
    # repo nested under workspace/ — third-party config, secret-free → readable
    "/users/alice/workspace/myrepo/.claude/settings.json",
    "/workspace/myrepo/.codex/config.toml",
    "/home/x/agents/pa/users/alice/workspace/proj/.claude/foo.json",
    # a caller's own files, and a repo nested in a caller tree
    "/caller/workspace/notes.md",
    "/caller/workspace/proj/.claude/settings.json",
    "/home/x/agents/pa/externals/phone/302101234567/context/memory/topic.md",
    # not a config file
    "/users/alice/.codex/history.jsonl",
    "/users/alice/.claude/projects/transcript.jsonl",
    "/users/alice/.claude/projects/x.json",  # nested under .claude, not a config
    # not under a config dir at all
    "/users/alice/workspace/notes.json",
    "/users/alice/workspace/.env",
    # the dir itself (no filename)
    "/users/alice/.claude",
    "",
]


def test_protected_paths_matched():
    for p in PROTECTED:
        assert path_roles.is_protected_agent_config_path(p) is True, p


def test_non_protected_paths_not_matched():
    for p in NOT_PROTECTED:
        assert path_roles.is_protected_agent_config_path(p) is False, p


def test_handles_path_objects_and_none():
    assert path_roles.is_protected_agent_config_path(
        Path("/users/alice/.claude/x.json")
    ) is True
    assert path_roles.is_protected_agent_config_path(None) is False


# ---------------------------------------------------------------------------
# _check_read_path wiring (native Read + local bash) — universal (incl. admin)
# ---------------------------------------------------------------------------


def _admin_ctx() -> SecurityContext:
    return SecurityContext(
        role="admin", username="alice", agent="personal-assistant",
        is_admin_agent=True,
    )


def test_read_denies_own_claude_config_even_for_admin():
    p = (_AGENTS_DIR / "personal-assistant" / "users" / "alice"
         / ".claude" / "personal-assistant-abc.json").resolve()
    decision = _check_read_path(p, _admin_ctx())
    assert decision.allowed is False
    assert "agent CLI config" in decision.reason


def test_read_denies_own_codex_config_even_for_admin():
    p = (_AGENTS_DIR / "personal-assistant" / "users" / "alice"
         / ".codex" / "config.toml").resolve()
    decision = _check_read_path(p, _admin_ctx())
    assert decision.allowed is False
    assert "agent CLI config" in decision.reason


def test_read_allows_repo_nested_claude_config():
    """OSS regression — a repo that itself uses Claude Code stays readable."""
    p = (_AGENTS_DIR / "personal-assistant" / "users" / "alice"
         / "workspace" / "myrepo" / ".claude" / "settings.json").resolve()
    decision = _check_read_path(p, _admin_ctx())
    assert decision.allowed is True


def test_read_allows_normal_workspace_file():
    p = (_AGENTS_DIR / "personal-assistant" / "workspace" / "notes.md").resolve()
    decision = _check_read_path(p, _admin_ctx())
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# _check_bash wiring (cat/grep route through _check_read_path)
# ---------------------------------------------------------------------------


def test_bash_cat_of_own_config_denied():
    decision = _check_bash(
        "cat /users/alice/.claude/personal-assistant-abc.json", _admin_ctx(),
    )
    assert decision.allowed is False
    assert "agent CLI config" in decision.reason


def test_bash_grep_of_own_codex_config_denied():
    decision = _check_bash(
        "grep Bearer /users/alice/.codex/config.toml", _admin_ctx(),
    )
    assert decision.allowed is False


def test_bash_cat_repo_config_allowed():
    decision = _check_bash(
        "cat /users/alice/workspace/myrepo/.claude/settings.json", _admin_ctx(),
    )
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# _protected_path_denial wiring (remote + MCP arg-paths)
# ---------------------------------------------------------------------------


def test_protected_denial_blocks_own_config():
    assert _protected_path_denial(
        "/users/alice/.claude/mcp-config.json", writing=False
    ) != ""
    assert _protected_path_denial(
        "/users/alice/.codex/config.toml", writing=False
    ) != ""


def test_protected_denial_allows_repo_config():
    assert _protected_path_denial(
        "/users/alice/workspace/myrepo/.codex/config.toml", writing=False
    ) == ""
    assert _protected_path_denial(
        "/workspace/proj/.claude/settings.json", writing=False
    ) == ""


# ---------------------------------------------------------------------------
# command_references_protected_agent_config — raw-text backstop
# ---------------------------------------------------------------------------


def test_command_backstop_matches_scope_root_config():
    for cmd in [
        "cat /users/alice/.claude/personal-assistant-abc.json",
        "grep Bearer /workspace/.codex/config.toml",
        "cat ~/.oto-dock/agents/pa/users/alice/.codex/auth.json",
    ]:
        assert path_roles.command_references_protected_agent_config(cmd) is True, cmd


def test_command_backstop_skips_repo_and_unrelated():
    for cmd in [
        "cat /users/alice/workspace/repo/.claude/settings.json",  # repo nested
        "cat /workspace/proj/.codex/config.toml",                 # repo nested
        "ls /workspace",
        "cat /users/alice/workspace/notes.md",
        "",
    ]:
        assert path_roles.command_references_protected_agent_config(cmd) is False, cmd
