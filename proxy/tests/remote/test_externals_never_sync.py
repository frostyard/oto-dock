"""Callers' trees (agents/<slug>/externals/) never leave the proxy host:
not in the satellite manifest, not pushed, not accepted back, not fanned out.
"""

from __future__ import annotations

from core.remote import file_sync
from services.remote import workspace_fanout


def test_manifest_skips_externals(tmp_path):
    agent_dir = tmp_path / "support"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "workspace" / "shared.txt").write_text("x")
    caller = agent_dir / "externals" / "phone" / "1" / "workspace"
    caller.mkdir(parents=True)
    (caller / "private.txt").write_text("y")
    manifest = file_sync.compute_manifest(agent_dir)
    paths = {e.path for e in manifest}
    assert "workspace/shared.txt" in paths
    assert not any(p.startswith("externals/") for p in paths)


def test_push_and_write_back_predicates():
    rel = "externals/phone/1/workspace/private.txt"
    assert file_sync._is_other_user_or_sensitive(rel, "alice")
    assert not file_sync.should_sync_to_target(rel, "alice", "manager")
    assert not file_sync.should_sync_to_target(rel, None, "admin")   # admin-shared target too
    assert not file_sync.can_write_back(rel, "manager", "alice")
    assert file_sync.should_sync_to_target("workspace/shared.txt", None, "admin")


def test_fanout_has_no_targets(monkeypatch):
    assert workspace_fanout.fanout_targets("support", "externals/phone/1/context/memory/t.md") == []
