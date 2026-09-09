"""``services.infra.file_bookkeeping`` — the tree-level tombstone / publish
helpers confine the subtree they walk to the agent dir before touching it,
so a path that resolves outside the agent (a planted symlink, a stray
absolute path) records and pushes nothing.
"""

import asyncio
import os

import pytest

from services.infra import file_bookkeeping as fb


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agents" / "pa"
    (d / "workspace" / "docs").mkdir(parents=True)
    (d / "workspace" / "docs" / "a.md").write_text("a")
    (d / "workspace" / "docs" / "b.md").write_text("b")
    return d


@pytest.fixture
def outside(tmp_path, agent_dir):
    """A dir outside the agent tree, also reachable through a symlink
    planted inside the workspace."""
    out = tmp_path / "outside"
    out.mkdir()
    (out / "x.md").write_text("x")
    os.symlink(out, agent_dir / "workspace" / "link")
    return out


def _capture(monkeypatch, name):
    seen = []

    async def _rec(*a, **k):
        seen.append(a)
    monkeypatch.setattr(fb, name, _rec)
    return seen


def test_tombstone_subtree_walks_only_inside_the_agent(agent_dir, outside, monkeypatch):
    seen = _capture(monkeypatch, "tombstone_path")
    asyncio.run(fb.tombstone_subtree("pa", agent_dir, agent_dir / "workspace" / "docs"))
    assert sorted(s[1] for s in seen) == ["workspace/docs/a.md", "workspace/docs/b.md"]

    seen.clear()
    asyncio.run(fb.tombstone_subtree("pa", agent_dir, agent_dir / "workspace" / "link"))
    asyncio.run(fb.tombstone_subtree("pa", agent_dir, outside))
    assert seen == []


def test_push_tree_write_records_only_inside_the_agent(agent_dir, outside, monkeypatch):
    seen = _capture(monkeypatch, "record_platform_write")
    from services.remote import workspace_fanout
    monkeypatch.setattr(workspace_fanout, "has_fanout_candidates", lambda *a, **k: False)
    asyncio.run(fb.push_tree_write(
        "pa", agent_dir / "workspace" / "docs", agent_dir, writer="alice"))
    assert sorted(s[1] for s in seen) == ["workspace/docs/a.md", "workspace/docs/b.md"]
    assert {s[2] for s in seen} == {"alice"}

    seen.clear()
    asyncio.run(fb.push_tree_write("pa", agent_dir / "workspace" / "link", agent_dir))
    asyncio.run(fb.push_tree_write("pa", outside, agent_dir))
    assert seen == []
