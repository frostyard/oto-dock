"""Offline setup CLI: explicit inputs, sanitized failures, no ambient downloads."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIRECTORY.parents[1] / "proxy"))
spec = importlib.util.spec_from_file_location("provision_local_cli", DIRECTORY / "provision_local.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.mark.parametrize("action", ["initialize", "check"])
def test_explicit_actions_and_excluded_roots(monkeypatch, capsys, action):
    calls = []
    monkeypatch.setattr(cli, "check_sdk", lambda: calls.append("sdk"))
    monkeypatch.setattr(cli, "initialize", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: calls.append((args, kwargs)))
    args = [action, "--root", "/private/install", "--forbid-root", "/agents", "--forbid-root", "/config"]
    if action == "initialize":
        args += ["--archive", "/download/pinned.tgz"]
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "passed" and report["action"] == action
    expected = (Path("/private/install"),)
    if action == "initialize":
        expected += (Path("/download/pinned.tgz"),)
    assert calls == ["sdk", (expected, {"forbidden_roots": (Path("/agents"), Path("/config"))})]


@pytest.mark.parametrize("action", ["initialize", "check"])
def test_missing_sdk_prevents_filesystem_operation_and_sanitizes_errors(monkeypatch, capsys, action):
    def fail():
        raise cli.CopilotProvisioningError("private fixture details")

    def unexpected(*args, **kwargs):
        pytest.fail("Preflight failure must precede provisioning")

    monkeypatch.setattr(cli, "check_sdk", fail)
    monkeypatch.setattr(cli, "initialize", unexpected)
    monkeypatch.setattr(cli, "load", unexpected)
    args = [action, "--root", "/private/install"]
    if action == "initialize":
        args += ["--archive", "/download/pinned.tgz"]
    assert cli.main(args) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["result"] == "failed"
    assert "private fixture details" not in output


@pytest.mark.parametrize("args", [
    ["initialize", "--root", "/private/install"],
    ["check", "--root", "/private/install", "--archive", "/download/pinned.tgz"],
    ["check"],
])
def test_invalid_action_arguments_do_not_start_preflight(args, monkeypatch):
    def unexpected():
        pytest.fail("Invalid CLI input must not run preflight")

    monkeypatch.setattr(cli, "check_sdk", unexpected)
    with pytest.raises(SystemExit) as caught:
        cli.main(args)
    assert caught.value.code == 2
