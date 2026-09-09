"""The per-session Codex config files that must never sync between hosts are
listed on BOTH sides (proxy and satellite file_sync) and the two sets must stay
identical — nothing else enforces the "MUST stay identical" comment."""

import ast
from pathlib import Path

from core.remote import file_sync as proxy_file_sync

_SATELLITE_FILE_SYNC = Path(__file__).resolve().parents[3] / "satellite" / "transport" / "file_sync.py"


def _satellite_host_local_files() -> frozenset:
    tree = ast.parse(_SATELLITE_FILE_SYNC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_CODEX_HOST_LOCAL_FILES" for t in node.targets
        ):
            return frozenset(ast.literal_eval(node.value.args[0]))
    raise AssertionError("satellite file_sync has no _CODEX_HOST_LOCAL_FILES")


def test_codex_host_local_files_match_between_proxy_and_satellite():
    assert proxy_file_sync._CODEX_HOST_LOCAL_FILES == _satellite_host_local_files()
    # The per-session model catalog joined the set with satellite 0.5.117.
    assert "models.json" in proxy_file_sync._CODEX_HOST_LOCAL_FILES
    assert proxy_file_sync._is_codex_runtime_state("workspace/.codex/models.json")
    assert not proxy_file_sync._is_codex_runtime_state("workspace/models.json")
