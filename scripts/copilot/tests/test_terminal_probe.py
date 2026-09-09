"""Offline safeguards for native PTY history compatibility probes."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

_spec = importlib.util.spec_from_file_location(
    'terminal_probe', Path(__file__).resolve().parents[1] / 'terminal_probe.py',
)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_terminal_environment_does_not_inherit_tokens_or_configuration(monkeypatch, tmp_path):
    for name in ('GH_TOKEN', 'COPILOT_GITHUB_TOKEN', 'GITHUB_TOKEN', 'BASH_ENV',
                 'COPILOT_ALLOW_ALL', 'COPILOT_PROVIDER_BASE_URL', 'COPILOT_HOME'):
        monkeypatch.setenv(name, 'ambient-must-not-reach-terminal')
    env = probe.child_environment(tmp_path)
    assert 'ambient-must-not-reach-terminal' not in env.values()
    assert env['COPILOT_HOME'] == str(tmp_path / 'state')
    assert env['COPILOT_DISABLE_KEYTAR'] == '1'


def test_history_reader_tolerates_partial_live_append(tmp_path):
    history = tmp_path / 'events.jsonl'
    complete = {'type': 'assistant.message', 'data': {'content': 'first'}}
    history.write_text(json.dumps(complete) + '\n{"type":')
    assert probe.read_events(history) == [complete]


def test_history_reader_rejects_corrupt_completed_record(tmp_path):
    history = tmp_path / 'events.jsonl'
    history.write_text('{bad}\n{"type":"session.idle"}\n')
    with pytest.raises(ValueError, match='Malformed completed history'):
        probe.read_events(history)


def test_child_messages_cannot_fake_main_history_recall():
    frames = [
        {'type': 'assistant.message', 'data': {'content': 'main'}},
        {'type': 'assistant.message', 'agentId': 'child', 'data': {'content': 'child'}},
        {'type': 'assistant.message', 'data': {'content': 'nested', 'parentToolCallId': 'tool'}},
    ]
    assert probe.assistant_messages(frames) == ['main']


def test_writer_handoff_rejects_a_live_previous_runtime(monkeypatch):
    child = SimpleNamespace(status=lambda: probe.psutil.STATUS_SLEEPING)
    monkeypatch.setattr(probe.psutil, 'Process', lambda: SimpleNamespace(children=lambda **_: [child]))
    with pytest.raises(RuntimeError, match='Previous writer still running'):
        probe.require_no_writer()


def test_writer_handoff_ignores_exited_zombies(monkeypatch):
    child = SimpleNamespace(status=lambda: probe.psutil.STATUS_ZOMBIE)
    monkeypatch.setattr(probe.psutil, 'Process', lambda: SimpleNamespace(children=lambda **_: [child]))
    probe.require_no_writer()


def test_matching_archive_cannot_authorize_a_different_executable(monkeypatch, tmp_path):
    archive, executable = tmp_path / 'cli.tar.gz', tmp_path / 'copilot'
    contents = b'verified-cli-bytes'
    with tarfile.open(archive, 'w:gz') as tar:
        member = tarfile.TarInfo('copilot')
        member.size = len(contents)
        tar.addfile(member, io.BytesIO(contents))
    monkeypatch.setattr(probe, 'CLI_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
    executable.write_bytes(contents)
    probe.verify_cli_archive(archive, executable)
    executable.write_bytes(b'unrelated-executable')
    with pytest.raises(ValueError, match='differs from verified archive'):
        probe.verify_cli_archive(archive, executable)
    archive.write_bytes(b'corrupted-download')
    with pytest.raises(ValueError, match='archive checksum mismatch'):
        probe.verify_cli_archive(archive, executable)
