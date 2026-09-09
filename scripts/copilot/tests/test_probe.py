"""Probe isolation must not accidentally select ambient credentials."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "copilot_probe", Path(__file__).resolve().parents[1] / "probe.py",
)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_child_environment_excludes_ambient_secrets(monkeypatch, tmp_path):
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "COPILOT_CLI_PATH", "BASH_ENV"):
        monkeypatch.setenv(key, "must-not-reach-runtime")
    env = probe.child_environment(tmp_path)
    assert "must-not-reach-runtime" not in env.values()
    assert env["COPILOT_DISABLE_KEYTAR"] == "1"
    assert env["COPILOT_SKIP_CLI_DOWNLOAD"] == "1"
    assert env["COPILOT_HOME"] == str(tmp_path / "state")


@pytest.mark.parametrize("token", ["gho_test", "ghu_test", "github_pat_test"])
def test_selected_token_stays_in_memory(monkeypatch, capsys, token):
    def run(command, **kwargs):
        assert command == ["gh", "auth", "token"]
        assert kwargs["capture_output"] and kwargs["timeout"] == 10
        return SimpleNamespace(stdout=token + "\n")
    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe.selected_token() == token
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("token", ["", "ghp_not_supported", "unknown_secret"])
def test_unsupported_auth_fails_without_disclosing_token(monkeypatch, token):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=token))
    with pytest.raises(ValueError, match="unsupported") as exc:
        probe.selected_token()
    assert not token or token not in str(exc.value)
