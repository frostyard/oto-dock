"""Offline archive/layout proofs use tiny fixture assets in place of vendor pins."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib import metadata
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot import provisioning as module  # noqa: E402
from core.layers.copilot.provisioning import CopilotProvisioningError  # noqa: E402


@pytest.fixture
def provision(tmp_path, monkeypatch):
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module.platform, "libc_ver", lambda: ("glibc", "2.39"))
    assets = {
        "copilot-runtime": b"fixture wrapper",
        "runtime.node": b"fixture native runtime",
        "ripgrep/bin/linux-x64/rg": b"fixture search helper",
        "schemas/api.schema.json": b"{}",
    }
    pins = {name: ("package/" + name, len(data), hashlib.sha256(data).hexdigest(),
                   0o500 if name != "schemas/api.schema.json" else 0o400) for name, data in assets.items()}
    monkeypatch.setattr(module, "_ASSETS", pins)
    archive = tmp_path / "release.tgz"

    def write_archive(*, extra=(), omitted=(), changed=None):
        with tarfile.open(archive, "w:gz") as package:
            for name, original in assets.items():
                if name in omitted:
                    continue
                data = (changed or {}).get(name, original)
                member = tarfile.TarInfo("package/" + name)
                member.size = len(data)
                package.addfile(member, io.BytesIO(data))
            for member, data in extra:
                package.addfile(member, io.BytesIO(data) if data is not None else None)
        monkeypatch.setattr(module, "ARCHIVE_SIZE", archive.stat().st_size)
        monkeypatch.setattr(module, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())

    write_archive()
    return SimpleNamespace(root=tmp_path / "install", archive=archive, assets=assets, write_archive=write_archive)


def test_atomic_install_exact_assets_and_readonly_repeatable_verification(provision):
    result = module.initialize(provision.root, provision.archive)
    assert result.runtime_path == provision.root / "runtime/copilot-runtime"
    assert {result.records_root, result.state_root, result.homes_root} == {
        provision.root / "records", provision.root / "state", provision.root / "homes",
    }
    assert all(not list(path.iterdir()) for path in (result.records_root, result.state_root, result.homes_root))
    for name, data in provision.assets.items():
        path = provision.root / "runtime" / name
        assert path.read_bytes() == data
        assert stat.S_IMODE(path.stat().st_mode) == module._ASSETS[name][3]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700
               for path in provision.root.rglob("*") if path.is_dir())
    session_file = result.state_root / "existing-user-state"
    session_file.write_text("preserve exactly")
    before = {path: path.stat() for path in (provision.root, result.state_root, session_file, result.runtime_path)}
    assert module.load(provision.root) == result
    # Already initialized calls verify, never repair or need the input again.
    assert module.initialize(provision.root, provision.archive.with_name("absent.tgz")) == result
    for path, info in before.items():
        after = path.stat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (info.st_ino, info.st_mode, info.st_mtime_ns)
    assert session_file.read_text() == "preserve exactly"


@pytest.mark.parametrize("existing", ["empty", "partial", "file", "symlink"])
def test_existing_unqualified_destination_is_never_filled_or_replaced(provision, existing):
    if existing == "file":
        provision.root.write_text("user data")
    elif existing == "symlink":
        provision.root.symlink_to(provision.root.parent)
    else:
        provision.root.mkdir(mode=0o700)
        if existing == "partial":
            (provision.root / "records").mkdir(mode=0o700)
    before = provision.root.lstat()
    with pytest.raises(CopilotProvisioningError):
        module.initialize(provision.root, provision.archive)
    after = provision.root.lstat()
    # Validation reads directory entries, which may advance atime on the host
    # filesystem. Identity, permissions and actual change timestamps must stay.
    preserved = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
                 "st_size", "st_mtime_ns", "st_ctime_ns")
    assert tuple(getattr(after, key) for key in preserved) == tuple(getattr(before, key) for key in preserved)
    assert not list(provision.root.parent.glob(".copilot-install-*"))


@pytest.mark.parametrize("mutation", ["root-mode", "state-mode", "runtime-mode", "file-mode", "hash",
                                      "missing", "unexpected-file", "unexpected-directory", "symlink", "hardlink", "fifo"])
def test_load_rejects_tampered_or_partial_installation_without_repair(provision, mutation):
    paths = module.initialize(provision.root, provision.archive)
    file = paths.runtime_path
    if mutation == "root-mode":
        provision.root.chmod(0o755)
    elif mutation == "state-mode":
        paths.state_root.chmod(0o755)
    elif mutation == "runtime-mode":
        file.parent.chmod(0o755)
    elif mutation == "file-mode":
        file.chmod(0o700)
    elif mutation == "hash":
        file.chmod(0o700)
        file.write_bytes(b"X" * len(provision.assets["copilot-runtime"]))
        file.chmod(0o500)
    elif mutation == "missing":
        paths.state_root.rmdir()
    elif mutation == "unexpected-file":
        (file.parent / "manifest.json").write_text("untrusted")
    elif mutation == "unexpected-directory":
        (file.parent / "extra").mkdir(mode=0o700)
    elif mutation == "symlink":
        file.unlink()
        file.symlink_to(provision.archive)
    elif mutation == "hardlink":
        os.link(file, provision.root.parent / "linked-runtime")
    elif mutation == "fifo":
        file.unlink()
        os.mkfifo(file, mode=0o500)
    before = provision.root.lstat()
    with pytest.raises(CopilotProvisioningError) as caught:
        module.load(provision.root)
    assert caught.value.__context__ is None
    assert provision.root.lstat() == before


def test_directory_owner_mismatch_rejected(provision, monkeypatch):
    module.initialize(provision.root, provision.archive)
    monkeypatch.setattr(module.os, "getuid", lambda: -1)
    with pytest.raises(CopilotProvisioningError):
        module.load(provision.root)


@pytest.mark.parametrize("mutation", ["hash", "size", "symlink", "hardlink", "fifo"])
def test_unverified_archive_rejected_before_staging(provision, mutation):
    archive = provision.archive
    if mutation == "hash":
        archive.write_bytes(b"X" * archive.stat().st_size)
    elif mutation == "size":
        archive.write_bytes(b"truncated")
    elif mutation == "symlink":
        alias = archive.with_name("alias.tgz")
        alias.symlink_to(archive)
        archive = alias
    elif mutation == "hardlink":
        os.link(archive, archive.with_name("linked.tgz"))
    elif mutation == "fifo":
        archive.unlink()
        os.mkfifo(archive)
    with pytest.raises(CopilotProvisioningError):
        module.initialize(provision.root, archive)
    assert not provision.root.exists()
    assert not list(provision.root.parent.glob(".copilot-install-*"))


@pytest.mark.parametrize("bad_member", ["duplicate", "symlink", "hardlink", "traversal", "absolute", "directory"])
def test_archive_structure_rejected_even_with_matching_fixture_digest(provision, bad_member):
    names = {"duplicate": "package/copilot-runtime", "traversal": "package/../escape", "absolute": "/escape"}
    member = tarfile.TarInfo(names.get(bad_member, "package/extra"))
    member.size = 0
    if bad_member == "symlink":
        member.type, member.linkname = tarfile.SYMTYPE, "../../escape"
    elif bad_member == "hardlink":
        member.type, member.linkname = tarfile.LNKTYPE, "package/copilot-runtime"
    elif bad_member == "directory":
        member.type = tarfile.DIRTYPE
    provision.write_archive(extra=[(member, None)])
    with pytest.raises(CopilotProvisioningError):
        module.initialize(provision.root, provision.archive)
    assert not provision.root.exists()
    assert not (provision.root.parent / "escape").exists()
    assert not list(provision.root.parent.glob(".copilot-install-*"))


@pytest.mark.parametrize("failure", ["missing-member", "wrong-member-size", "wrong-member-hash", "member-count", "expanded", "archive-size"])
def test_bounded_missing_or_modified_assets_fail_without_partial_publication(provision, monkeypatch, failure):
    if failure == "missing-member":
        provision.write_archive(omitted=("runtime.node",))
    elif failure == "wrong-member-size":
        provision.write_archive(changed={"runtime.node": b"short"})
    elif failure == "wrong-member-hash":
        provision.write_archive(changed={"runtime.node": b"X" * len(provision.assets["runtime.node"])})
    elif failure == "member-count":
        monkeypatch.setattr(module, "_MAX_MEMBERS", 1)
    elif failure == "expanded":
        monkeypatch.setattr(module, "_MAX_EXPANDED", 1)
    elif failure == "archive-size":
        monkeypatch.setattr(module, "_MAX_ARCHIVE", 1)
    with pytest.raises(CopilotProvisioningError):
        module.initialize(provision.root, provision.archive)
    assert not provision.root.exists()
    assert not list(provision.root.parent.glob(".copilot-install-*"))


@pytest.mark.parametrize("kind", ["ancestor", "descendant", "equal", "double-slash"])
def test_forbidden_workspace_overlap_rejected(provision, kind):
    root = provision.root
    forbidden = root.parent if kind == "ancestor" else root / "workspace" if kind == "descendant" else root
    if kind == "double-slash":
        root = Path("/" + str(root))
        forbidden = provision.root.parent
    with pytest.raises(CopilotProvisioningError):
        module.initialize(root, provision.archive, forbidden_roots=(forbidden,))
    assert not provision.root.exists()


def test_symlink_ancestor_rejected(provision):
    alias = provision.root.parent / "alias"
    alias.symlink_to(provision.root.parent)
    with pytest.raises(CopilotProvisioningError):
        module.initialize(alias / "install", provision.archive)
    assert not provision.root.exists()


def test_nonprivate_parent_is_not_chmodded(provision):
    provision.root.parent.chmod(0o755)
    try:
        with pytest.raises(CopilotProvisioningError):
            module.initialize(provision.root, provision.archive)
        assert stat.S_IMODE(provision.root.parent.stat().st_mode) == 0o755
        assert not provision.root.exists()
    finally:
        provision.root.parent.chmod(0o700)


def test_destination_created_during_install_is_not_overwritten(provision, monkeypatch):
    publish = module._publish

    def race(parent, staging, destination):
        provision.root.mkdir(mode=0o700)
        (provision.root / "user-state").write_text("preserve")
        publish(parent, staging, destination)

    monkeypatch.setattr(module, "_publish", race)
    with pytest.raises(CopilotProvisioningError):
        module.initialize(provision.root, provision.archive)
    assert (provision.root / "user-state").read_text() == "preserve"
    assert set(path.name for path in provision.root.iterdir()) == {"user-state"}
    assert not list(provision.root.parent.glob(".copilot-install-*"))


def test_concurrent_initializers_never_overwrite_winner(provision, monkeypatch):
    publish, barrier = module._publish, threading.Barrier(2)

    def race(*args):
        barrier.wait(timeout=5)
        publish(*args)

    monkeypatch.setattr(module, "_publish", race)

    def initialize():
        try:
            return module.initialize(provision.root, provision.archive)
        except CopilotProvisioningError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: initialize(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert module.load(provision.root) in results
    assert not list(provision.root.parent.glob(".copilot-install-*"))


def test_extraction_uses_verified_immutable_snapshot(provision, monkeypatch):
    archive = module._archive

    def snapshot_then_replace(path):
        source = archive(path)
        path.write_bytes(b"unverified replacement")
        return source

    monkeypatch.setattr(module, "_archive", snapshot_then_replace)
    paths = module.initialize(provision.root, provision.archive)
    assert paths.runtime_path.read_bytes() == provision.assets["copilot-runtime"]


@pytest.mark.parametrize("system,machine,libc", [("Darwin", "x86_64", "glibc"), ("Linux", "aarch64", "glibc"),
                                               ("Linux", "x86_64", "musl")])
def test_unqualified_hosts_cannot_initialize(provision, monkeypatch, system, machine, libc):
    monkeypatch.setattr(module.platform, "system", lambda: system)
    monkeypatch.setattr(module.platform, "machine", lambda: machine)
    monkeypatch.setattr(module.platform, "libc_ver", lambda: (libc, "1"))
    with pytest.raises(CopilotProvisioningError):
        module.initialize(provision.root, provision.archive)
    assert not provision.root.exists()


@pytest.mark.parametrize("installed", ["1.0.13", "1.0.12", None])
def test_sdk_check_is_explicit_and_metadata_only(monkeypatch, installed):
    def version(name):
        assert name == "github-copilot-sdk"
        if installed is None:
            raise metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(module.metadata, "version", version)
    if installed == module.SDK_VERSION:
        assert module.check_sdk() is None
    else:
        with pytest.raises(CopilotProvisioningError) as caught:
            module.check_sdk()
        assert caught.value.__context__ is None


def test_module_import_needs_no_site_packages():
    proxy = str(Path(__file__).resolve().parents[3] / "proxy")
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import core.layers.copilot.provisioning"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": proxy},
        capture_output=True, timeout=5,
    )
    assert result.returncode == 0
