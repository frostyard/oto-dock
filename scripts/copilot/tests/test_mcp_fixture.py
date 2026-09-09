"""Offline fixture and permission-boundary tests; no inference credentials."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("mcp_fixture", SCRIPTS / "mcp_fixture.py")
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
_probe_spec = importlib.util.spec_from_file_location("mcp_probe", SCRIPTS / "mcp_probe.py")
probe = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(probe)


@pytest.mark.parametrize("wrapped", [False, True])
def test_stdio_fixture_round_trip_and_secret_redaction(tmp_path, wrapped):
    audit = tmp_path / "audit.jsonl"
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "marker", "arguments": {}}},
    ]
    secret = "test-only-value-must-never-be-serialized"
    server = probe.fixture_server(audit, wrapped)
    result = subprocess.run(
        [server["command"], *server["args"]],
        input="\n".join(json.dumps(request) for request in requests) + "\n",
        text=True, capture_output=True, timeout=10, check=True,
        env={"GH_TOKEN": secret, "GITHUB_TOKEN": secret,
             **{name: secret for name in probe.INFERENCE_TOKEN_NAMES},
             **server.get("env", {})},
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert [tool["name"] for tool in responses[1]["result"]["tools"]] == ["marker"]
    assert responses[2]["result"]["content"][0]["text"] == fixture.MARKER
    record = json.loads(audit.read_text())
    assert record["token_presence"] == {
        "COPILOT_SDK_AUTH_TOKEN": not wrapped, "COPILOT_CONNECTION_TOKEN": not wrapped,
        "COPILOT_GITHUB_TOKEN": not wrapped, "GH_TOKEN": True, "GITHUB_TOKEN": True,
    }
    assert secret not in result.stdout + result.stderr + audit.read_text()


@pytest.mark.parametrize("params", [
    {"name": "shell", "arguments": {}},
    {"name": "marker", "arguments": {"command": "touch /tmp/never"}},
])
def test_unsupported_invocations_do_not_run(params, tmp_path):
    audit = tmp_path / "audit"
    response = fixture.handle({"id": 1, "method": "tools/call", "params": params}, audit)
    assert "error" in response
    assert not audit.exists()


@pytest.mark.parametrize("changes,allowed", [
    ({}, True), ({"kind": "shell"}, False), ({"server_name": "another"}, False),
    ({"tool_name": "write"}, False), ({"args": {"anything": "value"}}, False),
    ({"tool_name": "oto-fixture-marker", "args": "{}"}, True),
    ({"args": '{"command": "not allowed"}'}, False),
])
def test_permission_allowlist_is_exact(changes, allowed):
    request = {"kind": "mcp", "server_name": "oto-fixture", "tool_name": "marker", "args": {}}
    request.update(changes)
    assert probe.fixture_request(SimpleNamespace(**request)) is allowed
