"""Private scratch allocation, path substitution and independent process races."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import hashlib
import multiprocessing
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot import sandbox_home as module  # noqa: E402
from core.layers.copilot.sandbox_home import CopilotSandboxHomes, SandboxHomeError  # noqa: E402


def name(session_id):
    return "session-" + hashlib.sha256(session_id.encode()).hexdigest()


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    return path


def test_stable_distinct_empty_homes_preserve_existing_contents(root):
    with CopilotSandboxHomes(root) as homes:
        first = homes.get("session-a")
        assert first == root / name("session-a") and list(first.iterdir()) == []
        assert stat.S_IMODE(first.stat().st_mode) == 0o700
        (first / "fixture-state").write_text("preserve me")
        nested = first / "nested"
        nested.mkdir()
        assert homes.get("session-a") == first
        second = homes.get("session-b")
        assert second != first and list(second.iterdir()) == []
        assert homes.root == root
        with pytest.raises(AttributeError):
            homes.root = second
    with CopilotSandboxHomes(root) as reopened:
        assert reopened.get("session-a") == first
        assert (first / "fixture-state").read_text() == "preserve me"
    assert nested.is_dir()  # close never deletes scratch state.


@pytest.mark.parametrize("session_id", ["../escape", "/absolute/path", "a/b/../../c", r"..\escape"])
def test_pathlike_ids_are_hashed_without_interpreting_components(root, session_id):
    with CopilotSandboxHomes(root) as homes:
        path = homes.get(session_id)
        assert path.parent == root and path.name == name(session_id)
        assert len(list(root.iterdir())) == 1


@pytest.mark.parametrize("session_id", [None, 1, b"id", "", " ", " id", "id ", "a\x00b", "a\nb", "x" * 257])
def test_invalid_id_cannot_allocate(root, session_id):
    with CopilotSandboxHomes(root) as homes:
        with pytest.raises(SandboxHomeError) as caught:
            homes.get(session_id)
        assert caught.value.__context__ is None and list(root.iterdir()) == []


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777, 0o600, 0o1700])
def test_existing_root_must_be_exactly_private_and_is_never_repaired(root, mode):
    root.chmod(mode)
    with pytest.raises(SandboxHomeError):
        CopilotSandboxHomes(root)
    assert stat.S_IMODE(root.stat().st_mode) == mode


def test_root_rejects_nonpath_relative_traversal_missing_and_symlink_components(root):
    linked = root.parent / "linked"
    linked.symlink_to(root, target_is_directory=True)
    for candidate in (str(root), Path("relative"), root / ".." / root.name,
                      root / "missing", linked, linked / "child"):
        with pytest.raises(SandboxHomeError):
            CopilotSandboxHomes(candidate)


@pytest.mark.parametrize("kind", ["symlink", "file", "hardlink", "public_directory"])
def test_preexisting_unsafe_session_entry_is_rejected_without_changing_it(root, kind):
    target = root.parent / "outside"
    target.mkdir(mode=0o700)
    secret = target / "secret"
    secret.write_text("unrelated engine credential fixture")
    child = root / name("session-a")
    if kind == "symlink":
        child.symlink_to(target, target_is_directory=True)
    elif kind == "file":
        child.write_text("original")
    elif kind == "hardlink":
        os.link(secret, child)
    else:
        child.mkdir(mode=0o755)
    before = child.lstat()
    with CopilotSandboxHomes(root) as homes, pytest.raises(SandboxHomeError):
        homes.get("session-a")
    after = child.lstat()
    assert (before.st_ino, before.st_mode, before.st_nlink) == (after.st_ino, after.st_mode, after.st_nlink)
    assert secret.read_text() == "unrelated engine credential fixture"


def test_root_replacement_rejects_even_equal_mode_new_directory(root):
    with CopilotSandboxHomes(root) as homes:
        displaced = root.with_name("displaced")
        root.rename(displaced)
        root.mkdir(mode=0o700)
        with pytest.raises(SandboxHomeError):
            homes.get("session-a")
        with pytest.raises(SandboxHomeError):
            _ = homes.root
        assert list(root.iterdir()) == [] and list(displaced.iterdir()) == []


def test_known_child_replacement_and_mode_mutation_are_not_adopted(root):
    with CopilotSandboxHomes(root) as homes:
        path = homes.get("session-a")
        moved = root / "moved"
        path.rename(moved)
        path.mkdir(mode=0o700)
        with pytest.raises(SandboxHomeError):
            homes.get("session-a")
        second = homes.get("session-b")
        second.chmod(0o755)
        with pytest.raises(SandboxHomeError):
            homes.get("session-b")
        assert stat.S_IMODE(second.stat().st_mode) == 0o755


def test_wrong_owner_is_rejected_even_with_correct_permissions(root, monkeypatch):
    original = module.os.fstat

    def wrong_owner(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1,
                               st_dev=info.st_dev, st_ino=info.st_ino)

    monkeypatch.setattr(module.os, "fstat", wrong_owner)
    with pytest.raises(SandboxHomeError):
        CopilotSandboxHomes(root)


def test_close_is_idempotent_and_releases_root_descriptor(root):
    homes = CopilotSandboxHomes(root)
    fd = homes._root_fd
    path = homes.get("session-a")
    homes.close()
    homes.close()
    with pytest.raises(OSError):
        os.fstat(fd)
    with pytest.raises(SandboxHomeError):
        homes.get("session-b")
    assert path.is_dir()


def _process_get(root_text):
    with CopilotSandboxHomes(Path(root_text)) as homes:
        child = homes.get("same-session")
        return str(child), child.stat().st_ino


def test_independent_process_creators_converge_on_one_directory(root):
    # fork inherits no allocator instance or root descriptor: each worker pins
    # and validates its own root, as independent execution-layer workers do.
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("fork")) as pool:
        results = list(pool.map(_process_get, [str(root)] * 16))
    assert len(set(results)) == 1
    assert len(list(root.iterdir())) == 1
    assert stat.S_IMODE(Path(results[0][0]).stat().st_mode) == 0o700


def test_shared_instance_thread_creators_converge_without_descriptor_races(root):
    with CopilotSandboxHomes(root) as homes, ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(homes.get, ["same-session"] * 16))
        assert len(set(paths)) == 1
        assert len(list(root.iterdir())) == 1


def test_child_substitution_between_open_and_validation_is_rejected(root, monkeypatch):
    child = root / name("session-a")
    child.mkdir(mode=0o700)
    original_stat = module.os.stat
    replaced = False

    def swap_at_stat(path, *args, **kwargs):
        nonlocal replaced
        if path == child.name and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            child.rename(root / "displaced-child")
            child.mkdir(mode=0o700)
        return original_stat(path, *args, **kwargs)

    with CopilotSandboxHomes(root) as homes:
        monkeypatch.setattr(module.os, "stat", swap_at_stat)
        with pytest.raises(SandboxHomeError):
            homes.get("session-a")
        assert replaced and (root / "displaced-child").is_dir()


def test_root_mode_change_is_rejected_without_allocating_or_repairing(root):
    with CopilotSandboxHomes(root) as homes:
        root.chmod(0o750)
        with pytest.raises(SandboxHomeError):
            homes.get("session-a")
        assert list(root.iterdir()) == []
        assert stat.S_IMODE(root.stat().st_mode) == 0o750


def test_existing_child_owner_mismatch_is_rejected(root, monkeypatch):
    child = root / name("session-a")
    child.mkdir(mode=0o700)
    child_inode = child.stat().st_ino
    original = module.os.fstat

    def wrong_child_owner(fd):
        info = original(fd)
        if info.st_ino == child_inode:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1,
                                   st_dev=info.st_dev, st_ino=info.st_ino)
        return info

    with CopilotSandboxHomes(root) as homes:
        monkeypatch.setattr(module.os, "fstat", wrong_child_owner)
        with pytest.raises(SandboxHomeError):
            homes.get("session-a")


def test_restrictive_umask_never_silently_returns_or_chmods_incorrect_mode(root):
    with CopilotSandboxHomes(root) as homes:
        previous = os.umask(0o700)
        try:
            with pytest.raises(SandboxHomeError):
                homes.get("session-a")
        finally:
            os.umask(previous)
        child = root / name("session-a")
        try:
            assert stat.S_IMODE(child.stat().st_mode) == 0
            # The host operator owns repair/removal of rejected state; get never
            # upgrades an existing directory, even after the umask is corrected.
            with pytest.raises(SandboxHomeError):
                homes.get("session-a")
        finally:
            # Test-only repair permits pytest to remove this exact fixture.
            child.chmod(0o700)
