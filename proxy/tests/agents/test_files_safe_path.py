"""``safe_agent_path`` — the file API's single path gate.

Canonicalize, confine to the agent root, then role-check the RESOLVED
agent-relative path. Locks the traversal (400), symlink-escape (403) and
in-tree-symlink (allowed, authorized at the target) verdicts.
"""

import os

import pytest
from fastapi import HTTPException

from api.agents.files import safe_agent_path
from auth.providers import UserContext


def _manager() -> UserContext:
    return UserContext(
        sub="user-mgr", email="m@t.com", name="M", role="creator",
        agents=["test-agent"], agent_roles={"test-agent": "manager"},
    )


def _editor() -> UserContext:
    return UserContext(
        sub="user-ed", email="e@t.com", name="E", role="creator",
        agents=["test-agent"], agent_roles={"test-agent": "editor"},
    )


@pytest.fixture
def agent_dir(tmp_path, monkeypatch):
    from storage import database as task_store
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "mgr")
    d = tmp_path / "agents" / "test-agent"
    (d / "workspace").mkdir(parents=True)
    (d / "config").mkdir()
    return d


def test_plain_workspace_path_resolves(agent_dir):
    resolved, uname = safe_agent_path(
        agent_dir, "test-agent", "workspace/notes.md", _manager())
    assert resolved == agent_dir.resolve() / "workspace" / "notes.md"
    assert uname == "mgr"


def test_dotdot_segment_is_400(agent_dir):
    with pytest.raises(HTTPException) as ei:
        safe_agent_path(
            agent_dir, "test-agent", "workspace/../../etc/passwd", _manager())
    assert ei.value.status_code == 400


def test_symlink_escaping_the_agent_is_403(agent_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, agent_dir / "workspace" / "link")
    with pytest.raises(HTTPException) as ei:
        safe_agent_path(agent_dir, "test-agent", "workspace/link/secret", _manager())
    assert ei.value.status_code == 403


def test_symlink_inside_the_agent_is_authorized_at_its_target(agent_dir):
    """``workspace/link → config``: the role check sees config/, not the
    workspace scope the caller named — a manager passes and gets the real
    location, an editor is refused."""
    os.symlink(agent_dir / "config", agent_dir / "workspace" / "link")
    resolved, _ = safe_agent_path(
        agent_dir, "test-agent", "workspace/link/agent.md", _manager())
    assert resolved == agent_dir.resolve() / "config" / "agent.md"
    with pytest.raises(HTTPException) as ei:
        safe_agent_path(agent_dir, "test-agent", "workspace/link/agent.md", _editor())
    assert ei.value.status_code == 403
