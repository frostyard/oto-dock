"""Configuration and real subprocess credential-boundary regressions."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest

PROXY = Path(__file__).resolve().parents[3] / "proxy"
sys.path.insert(0, str(PROXY))
from core.layers.copilot.mcp_config import INFERENCE_TOKEN_NAMES, wrap_stdio_servers  # noqa: E402


def wrap(servers):
    return wrap_stdio_servers(servers, interpreter="python3", interceptor_path="/state/interceptor.py")


def test_all_servers_wrapped_without_changing_source_or_tool_filters():
    source = {
        "plain": {"command": "marker", "tools": ["read"], "env": {"GH_TOKEN": "repo-only"}},
        "broker": {"type": "stdio", "command": "node", "args": ["mcp.js"],
                   "env": {"OTO_MCP_FETCH_TOKEN": "capability", "OTO_TOOL_ARG_PATHS": "[]"}},
    }
    original = deepcopy(source)
    result = wrap(source)
    assert source == original
    assert result["plain"]["args"] == ["/state/interceptor.py", "--", "marker"]
    assert result["plain"]["tools"] == ["read"]
    assert result["plain"]["env"]["GH_TOKEN"] == "repo-only"
    assert result["broker"]["env"]["OTO_MCP_FETCH_TOKEN"] == "capability"
    assert result["broker"]["env"]["OTO_TOOL_ARG_PATHS"] == "[]"
    assert wrap(result) == result
    result["plain"]["tools"].append("write")
    assert source == original


def test_strip_lists_merge_and_explicit_inference_secrets_are_removed():
    env = {key: "secret" for key in INFERENCE_TOKEN_NAMES}
    env.update({"copilot_sdk_auth_token": "other-secret", "oto_strip_keys": " custom_secret , ",
                "OTO_STRIP_KEYS": "another_secret,COPILOT_GITHUB_TOKEN"})
    result = wrap({"fixture": {"command": "marker", "env": env}})["fixture"]["env"]
    assert set(result) == {"OTO_STRIP_KEYS"}
    assert set(result["OTO_STRIP_KEYS"].split(",")) == {
        *INFERENCE_TOKEN_NAMES, "CUSTOM_SECRET", "ANOTHER_SECRET",
    }


@pytest.mark.parametrize("server", [
    {}, {"url": "https://example.invalid", "type": "http"}, {"command": "marker", "type": "sse"},
    {"command": "marker", "args": "not-an-array"}, {"command": "marker", "args": [None]},
    {"command": "marker", "env": {"SECRET": 1}}, {"command": "marker", "env": {"A=B": "x"}},
    {"command": "marker", "env": {"OTO_STRIP_KEYS": "not a name"}},
    {"command": "python3", "args": ["/state/interceptor.py", "--"]},
    {"command": "secret\0value"}, {"command": "marker", "env": {"SECRET": "secret\0value"}},
])
def test_unsupported_or_malformed_config_fails_without_serializing_values(server):
    with pytest.raises(ValueError) as error:
        wrap({"private-server-name": server})
    assert "private-server-name" not in str(error.value)
    assert "secret\0value" not in str(error.value)


def test_interceptor_removes_all_case_variants_after_broker_injection(monkeypatch):
    spec = importlib.util.spec_from_file_location("copilot_test_interceptor", PROXY / "core/stdio_path_interceptor.py")
    interceptor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(interceptor)
    config = wrap({"fixture": {"command": "marker", "env": {"OTO_MCP_FETCH_TOKEN": "capability"}}})
    child_env = dict(config["fixture"]["env"])
    for key in INFERENCE_TOKEN_NAMES:
        child_env[key] = "ambient"
        child_env[key.lower()] = "ambient-case-variant"
    child_env["oto_mcp_fetch_token"] = "case-variant-capability"
    child_env["OTO_BEARER_OTHER"] = "other-bearer"
    monkeypatch.setattr(interceptor, "_fetch_mcp_credentials", lambda token: {
        "env": {"GITHUB_TOKEN": "repository-credential", "COPILOT_SDK_AUTH_TOKEN": "broker-injected",
                "OTO_STRIP_KEYS": "", "oto_strip_keys": "KEEP",
                "OTO_MCP_FETCH_TOKEN": "reinjected", "oto_mcp_fetch_token": "reinjected-variant"},
    })
    interceptor._apply_broker_credentials(child_env)
    assert child_env == {"GITHUB_TOKEN": "repository-credential"}
