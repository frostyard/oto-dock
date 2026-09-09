"""Manifest ``always_load`` — the Direct-LLM deferred-tools opt-out: absent →
False, a bool is accepted, anything else fails the manifest load."""

from __future__ import annotations

import json

import pytest

import config as app_config
from services.mcp.mcp_manifest_parse import _parse_manifest


def _write(tmp_path, extra: dict):
    d = tmp_path / "x-mcp"
    d.mkdir()
    data = {
        "name": "x-mcp", "label": "X", "description": "X tools.", "version": "1.0.0",
        "category": "custom",
        "server": {"runtime": "python", "transport": "stdio", "command": "python", "args": ["s.py"]},
        "credentials": {"type": "none"},
        **extra,
    }
    p = d / "manifest.json"
    p.write_text(json.dumps(data))
    return p


def test_default_is_false(tmp_path):
    assert _parse_manifest(_write(tmp_path, {})).always_load is False


def test_true_is_accepted(tmp_path):
    assert _parse_manifest(_write(tmp_path, {"always_load": True})).always_load is True


@pytest.mark.parametrize("bad", ["yes", 1, None, {"x": 1}])
def test_non_bool_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="always_load"):
        _parse_manifest(_write(tmp_path, {"always_load": bad}))


def test_bundled_memory_mcp_is_always_loaded():
    m = _parse_manifest(app_config.MCPS_DIR / "custom" / "memory-mcp" / "manifest.json")
    assert m is not None and m.always_load is True
