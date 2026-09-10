"""Durable profile provenance and exclusive writer ownership without SDK/DB."""

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.credentials import CopilotAccountScope, CredentialKind  # noqa: E402
from core.layers.copilot.session_records import (  # noqa: E402
    CopilotSessionProfile, CopilotSessionRecords, SessionRecordBusyError, SessionRecordError,
    SessionRecordExistsError, SessionRecordMismatchError, SessionRecordNotFoundError,
)
from core.layers.copilot.session_state import (  # noqa: E402
    PrivateCopilotSessionState, SessionStateAllocation, SessionStateError,
)


@pytest.fixture
def roots(tmp_path):
    records, states = tmp_path / "records", tmp_path / "states"
    records.mkdir(mode=0o700)
    states.mkdir(mode=0o700)
    return records, states


@pytest.fixture
def store(roots):
    return CopilotSessionRecords(roots[0], state_root=roots[1])


def profile(**changes):
    return CopilotSessionProfile(**{
        "account_id": "account-one", "principal_id": "github:user:123",
        "credential_kind": CredentialKind.USER_TOKEN, "scope": CopilotAccountScope.personal("owner"),
        "platform_session_id": "platform-session-one", "user_sub": "owner", "agent_id": "agent-one",
        "workspace": "/workspace/one", "model": "fixture-model", "enabled_tools": frozenset({"view", "bash"}),
        "config_digest": "a" * 64, **changes,
    })


def record_path(roots, selected=None):
    selected = profile() if selected is None else selected
    key = hashlib.sha256(selected.platform_session_id.encode()).hexdigest()
    return roots[0] / (key + ".json")


def ready(store, selected=None):
    selected = profile() if selected is None else selected
    record = store.create(selected, native_session_id="native-one")
    path = record.state.path
    (path / "history").write_text("private history marker")
    record.mark_ready()  # Fixture supplies the trusted clean-runtime-close proof.
    record.close()
    return path


def test_clean_resume_preserves_exact_state_and_marks_active_before_exposing_it(store, roots):
    selected = profile()
    path = ready(store, selected)
    record = store.open(selected)
    try:
        assert record.profile == selected and record.native_session_id == "native-one"
        assert record.state.path == path
        assert (path / "history").read_text() == "private history marker"
        assert json.loads(record_path(roots).read_text())["status"] == "active"
        assert stat.S_IMODE(record_path(roots).stat().st_mode) == 0o600
        assert stat.S_IMODE(next(roots[0].glob("*.lock")).stat().st_mode) == 0o600
        assert record.state.path.parent == roots[1]
        assert not record_path(roots).is_relative_to(record.state.path)
    finally:
        record.close()
    assert path.exists()
    with pytest.raises(SessionRecordMismatchError):
        store.open(selected)


def test_create_close_and_uncertain_startup_never_become_implicitly_resumable(store, roots):
    record = store.create(profile(), native_session_id="native-one")
    path = record.state.path
    record.close()
    record.close()
    assert json.loads(record_path(roots).read_text())["status"] == "active"
    assert path.exists()
    with pytest.raises(SessionRecordMismatchError):
        store.open(profile())
    with pytest.raises(SessionRecordExistsError):
        store.create(profile(), native_session_id="replacement")
    assert len(list(roots[1].iterdir())) == 1


def test_ready_commit_seals_state_and_still_holds_writer_lock_until_close(store):
    record = store.create(profile(), native_session_id="native-one")
    record.mark_ready()
    try:
        with pytest.raises(SessionRecordError):
            _ = record.state
        with pytest.raises(SessionRecordBusyError):
            store.open(profile())
    finally:
        record.close()
    with store.open(profile()) as reopened:
        assert reopened.native_session_id == "native-one"


@pytest.mark.parametrize("changes", [
    {"account_id": "different-account"}, {"principal_id": "github:user:456"},
    {"credential_kind": CredentialKind.INSTALLATION_TOKEN},
    {"scope": CopilotAccountScope.personal("different-owner"), "user_sub": "different-owner"}, {"scope": CopilotAccountScope.platform()},
    {"agent_id": "different-agent"}, {"workspace": "/workspace/different"},
    {"model": "different-model"}, {"enabled_tools": frozenset({"view"})}, {"config_digest": "b" * 64},
])
def test_every_identity_and_policy_binding_must_match_without_mutating_ready_record(store, roots, changes):
    path = ready(store)
    previous = record_path(roots).read_bytes()
    with pytest.raises(SessionRecordMismatchError):
        store.open(profile(**changes))
    assert record_path(roots).read_bytes() == previous
    assert (path / "history").read_text() == "private history marker"
    with store.open(profile()) as record:
        assert record.state.path == path


def test_same_workspace_different_platform_sessions_allocate_isolated_history(store):
    first = store.create(profile(), native_session_id="native-one")
    second = store.create(profile(platform_session_id="other-session"), native_session_id="native-two")
    try:
        assert first.state.path != second.state.path
        (first.state.path / "private").write_text("one account")
        assert not (second.state.path / "private").exists()
    finally:
        first.close()
        second.close()


def test_missing_resume_never_allocates_state_or_creates_a_record(store, roots):
    with pytest.raises(SessionRecordNotFoundError):
        store.open(profile())
    assert list(roots[1].iterdir()) == []
    assert list(roots[0].glob("*.json")) == []


def test_record_allows_no_token_revision_or_unreviewed_options(store, roots):
    ready(store)
    data = json.loads(record_path(roots).read_text())
    assert set(data) == {"version", "status", "profile", "native_session_id", "allocation"}
    assert "token" not in data["profile"] and "revision" not in data["profile"]
    assert data["profile"]["credential_kind"] == "user_token"
    with pytest.raises(TypeError):
        profile(token="must-never-persist")


@pytest.mark.parametrize("changes", [
    {"platform_session_id": ""}, {"user_sub": " driver"}, {"workspace": "relative"},
    {"workspace": "/workspace/../other"}, {"workspace": "/workspace\\other"},
    {"enabled_tools": frozenset()}, {"enabled_tools": {"bash"}}, {"enabled_tools": frozenset({"web_fetch"})},
    {"sdk_version": "future"}, {"runtime_version": "future"}, {"policy_profile": "unchecked"},
    {"config_digest": "A" * 64}, {"config_digest": "bad"}, {"credential_kind": "user_token"},
])
def test_profile_is_explicit_and_pinned(changes):
    with pytest.raises(SessionRecordError):
        profile(**changes)


def test_platform_payer_and_driver_remain_distinct_and_exact(store):
    selected = profile(scope=CopilotAccountScope.platform(), user_sub="driver-one")
    ready(store, selected)
    with pytest.raises(SessionRecordMismatchError):
        store.open(replace(selected, user_sub="driver-two"))
    with store.open(selected) as record:
        assert record.profile.scope.user_sub is None
        assert record.profile.user_sub == "driver-one"


def test_two_managers_in_same_process_obey_exclusive_flock(store, roots):
    record = store.create(profile(), native_session_id="native-one")
    other = CopilotSessionRecords(roots[0], state_root=roots[1])
    try:
        with pytest.raises(SessionRecordBusyError):
            other.open(profile())
        with pytest.raises(SessionRecordBusyError):
            other.create(profile(), native_session_id="native-two")
    finally:
        record.close()
    with pytest.raises(SessionRecordMismatchError):
        other.open(profile())


def test_cross_process_flock_and_crash_leave_active_record_unresumable(store, roots):
    record = store.create(profile(), native_session_id="native-one")
    lock_path = next(roots[0].glob("*.lock"))
    script = (
        "import fcntl,os,sys\n"
        "fd=os.open(sys.argv[1],os.O_RDWR)\n"
        "try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
        "except BlockingIOError: sys.exit(23)\n"
    )
    try:
        blocked = subprocess.run([sys.executable, "-I", "-c", script, str(lock_path)], timeout=2)
        assert blocked.returncode == 23
    finally:
        record.close()
    released = subprocess.run([sys.executable, "-I", "-c", script, str(lock_path)], timeout=2)
    assert released.returncode == 0
    with pytest.raises(SessionRecordMismatchError):
        store.open(profile())


@pytest.mark.parametrize("change", ["state", "root", "symlink", "permissions", "missing"])
def test_changed_allocation_is_never_adopted_repaired_or_recreated(store, roots, change):
    path = ready(store)
    original = path
    if change == "state":
        path.rename(path.with_name("retained-allocation"))
        path.mkdir(mode=0o700)
    elif change == "root":
        roots[1].rename(roots[1].with_name("retained-root"))
        roots[1].mkdir(mode=0o700)
        path.mkdir(mode=0o700)
    elif change == "symlink":
        path.rename(path.with_name("retained-allocation"))
        path.symlink_to(path.with_name("retained-allocation"), target_is_directory=True)
    elif change == "permissions":
        path.chmod(0o755)
    else:
        path.rename(path.with_name("retained-allocation"))
    before = set(roots[1].iterdir())
    with pytest.raises(SessionRecordError):
        store.open(profile())
    assert set(roots[1].iterdir()) == before
    if change not in {"symlink", "missing"}:
        assert original.is_dir()


@pytest.mark.parametrize("change", ["unknown", "version", "profile", "allocation", "duplicate", "oversize"])
def test_corrupt_record_is_rejected_without_overwrite_or_error_payload(store, roots, change):
    ready(store)
    path = record_path(roots)
    data = json.loads(path.read_text())
    if change == "unknown":
        data["token"] = "private-value"
    elif change == "version":
        data["version"] = True
    elif change == "profile":
        data["profile"]["private-value"] = "unexpected"
    elif change == "allocation":
        data["allocation"]["name"] = "../private-value"
    if change == "duplicate":
        path.write_text('{"version":1,"version":2,"private-value":true}')
    elif change == "oversize":
        path.write_text("private-value" * 10000)
    else:
        path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(SessionRecordError) as error:
        store.open(profile())
    assert "private-value" not in str(error.value) and error.value.__context__ is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["record", "lock"])
@pytest.mark.parametrize("change", ["symlink", "hardlink", "permissions"])
def test_host_record_files_must_be_private_regular_single_link(store, roots, target, change):
    ready(store)
    path = record_path(roots) if target == "record" else next(roots[0].glob("*.lock"))
    if change == "symlink":
        retained = path.with_name(path.name + ".retained")
        path.rename(retained)
        path.symlink_to(retained)
    elif change == "hardlink":
        os.link(path, path.with_name(path.name + ".linked"))
    else:
        path.chmod(0o644)
    with pytest.raises(SessionRecordError):
        store.open(profile())


def test_record_root_replacement_is_rejected_by_existing_manager(store, roots):
    ready(store)
    roots[0].rename(roots[0].with_name("retained-records"))
    roots[0].mkdir(mode=0o700)
    with pytest.raises(SessionRecordError):
        store.open(profile())
    assert list(roots[0].iterdir()) == []


def test_live_lock_replacement_prevents_ready_commit(store, roots):
    record = store.create(profile(), native_session_id="native-one")
    lock = next(roots[0].glob("*.lock"))
    lock.rename(lock.with_name("old-lock"))
    lock.touch(mode=0o600)
    try:
        with pytest.raises(SessionRecordError):
            record.mark_ready()
        with pytest.raises(SessionRecordError):
            _ = record.state
        assert json.loads(record_path(roots).read_text())["status"] == "active"
    finally:
        record.close()


def test_detach_and_exact_reopen_retain_history_and_reject_changed_inode(roots):
    original = PrivateCopilotSessionState.create(roots[1])
    path, allocation = original.path, original.allocation
    (path / "marker").write_text("retained")
    original.detach()
    original.detach()
    with pytest.raises(SessionStateError):
        _ = original.path
    reopened = PrivateCopilotSessionState.reopen(roots[1], allocation)
    assert (reopened.path / "marker").read_text() == "retained"
    reopened.detach()
    path.rename(path.with_name("retained-state"))
    path.mkdir(mode=0o700)
    with pytest.raises(SessionStateError):
        PrivateCopilotSessionState.reopen(roots[1], allocation)
    assert path.exists()


@pytest.mark.parametrize("changes", [{"name": "../other"}, {"name": "session-/other"},
    {"root_inode": True}, {"state_inode": 0}, {"state_device": -1}])
def test_state_allocation_identity_has_strict_fields(roots, changes):
    original = PrivateCopilotSessionState.create(roots[1])
    try:
        values = {**asdict(original.allocation), **changes}
        with pytest.raises(SessionStateError):
            SessionStateAllocation(**values)
    finally:
        original.discard()


@pytest.mark.parametrize("kind", ["relative", "nested", "same", "symlink", "public"])
def test_record_roots_are_existing_private_disjoint_directories(roots, kind):
    records, states = roots
    if kind == "relative":
        records = Path("relative")
    elif kind == "nested":
        records = states / "records"
        records.mkdir(mode=0o700)
    elif kind == "same":
        records = states
    elif kind == "symlink":
        alias = records.with_name("records-link")
        alias.symlink_to(records, target_is_directory=True)
        records = alias
    else:
        records.chmod(0o755)
    with pytest.raises(SessionRecordError):
        CopilotSessionRecords(records, state_root=states)


def test_root_paths_are_readonly_and_available_for_mount_overlap_checks(store, roots):
    assert store.root == roots[0] and store.state_root == roots[1]
    with pytest.raises(AttributeError):
        store.root = Path("/different")
    with pytest.raises(AttributeError):
        store.state_root = Path("/different")


@pytest.mark.parametrize("driver", ["", "other-owner", None, False])
def test_personal_payer_requires_matching_human_driver(driver):
    with pytest.raises(SessionRecordError):
        profile(user_sub=driver)


def test_explicit_service_driver_is_supported_only_under_platform_scope(store):
    selected = profile(scope=CopilotAccountScope.platform(), user_sub="")
    ready(store, selected)
    with store.open(selected) as record:
        assert record.profile.user_sub == ""
        assert record.profile.scope == CopilotAccountScope.platform()


def test_actual_process_crash_releases_lock_but_keeps_active_record_quarantined(store, roots):
    script = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from core.layers.copilot.session_records import CopilotSessionProfile, CopilotSessionRecords
from core.layers.copilot.credentials import CopilotAccountScope, CredentialKind
store = CopilotSessionRecords(Path(sys.argv[1]), state_root=Path(sys.argv[2]))
profile = CopilotSessionProfile(
    account_id="account-one", principal_id="github:user:123", credential_kind=CredentialKind.USER_TOKEN,
    scope=CopilotAccountScope.personal("owner"), platform_session_id="platform-session-one", user_sub="owner",
    agent_id="agent-one", workspace="/workspace/one", model="fixture-model", enabled_tools=frozenset({"view", "bash"}),
    config_digest="a" * 64,
)
record = store.create(profile, native_session_id="native-one")
(record.state.path / "history").write_text("preserved crash history")
os._exit(17)
'''
    result = subprocess.run([sys.executable, "-I", "-c", script, str(roots[0]), str(roots[1]),
                             str(Path(__file__).resolve().parents[3] / "proxy")], timeout=3)
    assert result.returncode == 17
    with pytest.raises(SessionRecordMismatchError):
        store.open(profile())
    assert json.loads(record_path(roots).read_text())["status"] == "active"
    assert (next(roots[1].iterdir()) / "history").read_text() == "preserved crash history"


def test_fresh_manager_reopens_durable_clean_record_without_original_state_object(store, roots):
    retained = ready(store)
    fresh = CopilotSessionRecords(roots[0], state_root=roots[1])
    with fresh.open(profile()) as record:
        assert record.state.path == retained
        assert (record.state.path / "history").read_text() == "private history marker"


def test_failed_ready_write_never_grants_resume_or_deletes_history(store, roots, monkeypatch):
    record = store.create(profile(), native_session_id="native-one")
    path = record.state.path
    (path / "history").write_text("retained on storage error")

    def fail_sync(_descriptor):
        raise OSError("private storage failure")

    with monkeypatch.context() as patch:
        patch.setattr("core.layers.copilot.session_records.os.fsync", fail_sync)
        with pytest.raises(SessionRecordError) as error:
            record.mark_ready()
        assert "private" not in str(error.value) and error.value.__context__ is None
    record.close()
    assert json.loads(record_path(roots).read_text())["status"] == "active"
    assert (path / "history").read_text() == "retained on storage error"
    assert not list(roots[0].glob("*.tmp-*"))
    with pytest.raises(SessionRecordMismatchError):
        store.open(profile())


def test_failed_active_commit_does_not_expose_handle_or_replace_history(store, roots, monkeypatch):
    path = ready(store)
    before = record_path(roots).read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("private storage failure")

    with monkeypatch.context() as patch:
        patch.setattr("core.layers.copilot.session_records.os.replace", fail_replace)
        with pytest.raises(SessionRecordError) as error:
            store.open(profile())
        assert "private" not in str(error.value) and error.value.__context__ is None
    assert record_path(roots).read_bytes() == before
    assert (path / "history").read_text() == "private history marker"
    assert len(list(roots[1].iterdir())) == 1
    assert not list(roots[0].glob("*.tmp-*"))
    with store.open(profile()) as record:
        assert record.state.path == path


def test_record_owner_detects_profile_rewrite_before_ready_commit(store, roots):
    record = store.create(profile(), native_session_id="native-one")
    document = json.loads(record_path(roots).read_text())
    document["profile"]["account_id"] = "unrelated-account"
    record_path(roots).write_text(json.dumps(document))
    try:
        with pytest.raises(SessionRecordError):
            record.mark_ready()
        assert json.loads(record_path(roots).read_text())["status"] == "active"
    finally:
        record.close()


def test_restrictive_umask_does_not_make_record_or_lock_unusable(store, roots):
    previous = os.umask(0o777)
    try:
        record = store.create(profile(), native_session_id="native-one")
        record.mark_ready()
        record.close()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(record_path(roots).stat().st_mode) == 0o600
    assert stat.S_IMODE(next(roots[0].glob("*.lock")).stat().st_mode) == 0o600
    with store.open(profile()):
        pass


def test_history_ready_is_exact_owner_read_only_and_requires_free_ready_record(store, roots):
    before = list(roots[0].iterdir())
    assert not store.is_ready(profile().platform_session_id, "owner")
    assert list(roots[0].iterdir()) == before
    path = ready(store)
    files = {file: (file.stat().st_ino, file.stat().st_mtime_ns) for root in roots for file in root.rglob("*")}
    assert store.is_ready(profile().platform_session_id, "owner")
    assert not store.is_ready(profile().platform_session_id, "another-owner")
    assert not store.is_ready("other-session", "owner")
    assert files == {file: (file.stat().st_ino, file.stat().st_mtime_ns) for root in roots for file in root.rglob("*")}
    record = store.open(profile())
    try:
        assert not store.is_ready(profile().platform_session_id, "owner")
    finally:
        record.close()
    assert path.exists() and not store.is_ready(profile().platform_session_id, "owner")


@pytest.mark.parametrize("attack", ["fifo", "symlink", "hardlink", "wrong-profile", "missing-allocation"])
def test_history_ready_rejects_untrusted_records_without_mutating(store, roots, attack):
    path = ready(store)
    file = record_path(roots)
    if attack == "fifo":
        file.unlink()
        os.mkfifo(file, mode=0o600)
    elif attack == "symlink":
        backup = file.with_suffix(".backup")
        file.rename(backup)
        file.symlink_to(backup)
    elif attack == "hardlink":
        os.link(file, file.with_suffix(".backup"))
    elif attack == "wrong-profile":
        document = json.loads(file.read_text())
        document["profile"]["platform_session_id"] = "another-session"
        file.write_text(json.dumps(document))
    else:
        path.rename(path.with_name("moved"))
    assert not store.is_ready(profile().platform_session_id, "owner")
