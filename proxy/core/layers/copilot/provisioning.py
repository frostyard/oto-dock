"""Explicit offline installation of the qualified Linux Copilot runtime.

Supply the official release archive locally. No download, SDK import, credential
lookup, native execution, existing-data repair, or engine registration occurs.
The private parent must remain under host control and outside sandbox mounts.
Callers must pass their agent/workspace roots as forbidden_roots.
"""

from contextlib import suppress
import ctypes
from dataclasses import dataclass
import hashlib
from importlib import metadata
import io
import os
from pathlib import Path, PurePosixPath
import platform
import secrets
import shutil
import stat
import tarfile

from .session_state import _identity, _open_directory

SDK_VERSION = "1.0.13"
RUNTIME_VERSION = "1.0.83"
ARCHIVE_NAME = "github-copilot-1.0.83-linux-x64.tgz"
ARCHIVE_URL = "https://github.com/github/copilot-cli/releases/download/v1.0.83/" + ARCHIVE_NAME
CHECKSUM_URL = "https://github.com/github/copilot-cli/releases/download/v1.0.83/SHA256SUMS.txt"
# Verified against the official release API digest and SHA256SUMS.txt, 2026-09-10.
ARCHIVE_SHA256 = "888f8fbb4575c335afba4a8863c647ef04f81e5124c7c794bdcaee90c5fa4503"
ARCHIVE_SIZE = 51632715
_MAX_ARCHIVE = 64 * 1024 * 1024
_MAX_EXPANDED = 256 * 1024 * 1024
_MAX_MEMBERS = 512
_DIRECTORIES = frozenset({"records", "state", "homes", "runtime"})
# Installed path -> (official archive member, exact size, SHA256, private mode).
# This hostless asset set matches the already qualified staged SDK runtime.
# Pins are source code, never values trusted from a generated local manifest.
_ASSETS = {
    'animations/app-install-nudge.json.gz': ('package/animations/app-install-nudge.json.gz', 9900,
        '8a4165e6e95f127fe3123e647580c31def2d52b7824f2c4e48b505339132543e', 0o400),
    'animations/banner.json.gz': ('package/animations/banner.json.gz', 4887,
        'cc6dcef840f5f63c9683e84bd55478e6fd2a22130bf1be6f53dece870e996446', 0o400),
    'builtin/customize-cloud-agent/SKILL.md': ('package/builtin/customize-cloud-agent/SKILL.md', 18679,
        '6acee791cd48a31eb881431c0f1394d3c79d33d62f0d58b2d9b699e4a275ea93', 0o400),
    'builtin/discover-resources/SKILL.md': ('package/builtin/discover-resources/SKILL.md', 2700,
        'f20ff1ccc42073a2927112891c04788bc99ff797c7773c5f0397c2e265e3b323', 0o400),
    'builtin/github-pr-media/SKILL.md': ('package/builtin/github-pr-media/SKILL.md', 4938,
        'eba36e66d053b3db19e8f51cfb7d28186ef2baf90159f51bf2333747b150070b', 0o400),
    'builtin-skills/customize-cloud-agent/SKILL.md': ('package/builtin-skills/customize-cloud-agent/SKILL.md', 18679,
        '6acee791cd48a31eb881431c0f1394d3c79d33d62f0d58b2d9b699e4a275ea93', 0o400),
    'builtin-skills/discover-resources/SKILL.md': ('package/builtin-skills/discover-resources/SKILL.md', 2700,
        'f20ff1ccc42073a2927112891c04788bc99ff797c7773c5f0397c2e265e3b323', 0o400),
    'builtin-skills/github-pr-media/SKILL.md': ('package/builtin-skills/github-pr-media/SKILL.md', 4938,
        'eba36e66d053b3db19e8f51cfb7d28186ef2baf90159f51bf2333747b150070b', 0o400),
    'copilot-runtime': ('package/prebuilds/linux-x64/copilot-runtime', 416968,
        '580f45a5dca10be9122180bce579055be25123efd348f8ed40ffd3e00aaf2044', 0o500),
    'definitions/code-review.agent.yaml': ('package/definitions/code-review.agent.yaml', 4604,
        '1290258ee70b44faca1f0c74be26254f13e8401e825713ff3f5523b848b361f9', 0o400),
    'definitions/explore.agent.yaml': ('package/definitions/explore.agent.yaml', 2687,
        '35e599117cd266cad7fb716741bf8b0c8dcc667b7b225caef305b841198757b3', 0o400),
    'definitions/rem-agent.agent.yaml': ('package/definitions/rem-agent.agent.yaml', 968,
        '895ac0f7d0ce5cd006501eed7f1abfd38405ba93c30d16be9c7205bed6f335bf', 0o400),
    'definitions/research.agent.yaml': ('package/definitions/research.agent.yaml', 5596,
        '82726101e23ed841a200f301546a6ae9f19739ca106686f2ca2c73a1afcffecc', 0o400),
    'definitions/rubber-duck.agent.yaml': ('package/definitions/rubber-duck.agent.yaml', 4820,
        'eb624f30ef69f8cfd18e7f3f14bcca63e13d1a0d3e05af006372b8d537bbd15d', 0o400),
    'definitions/security-review.agent.yaml': ('package/definitions/security-review.agent.yaml', 14222,
        '7b6769eb585913a0ad59dadb3bf2e42cd9cfadb1540b341f23a28e5728be2d53', 0o400),
    'definitions/sidekick/cloud-session-search.yaml': ('package/definitions/sidekick/cloud-session-search.yaml', 1809,
        '0eaa7fe8f8cecb923c87c9598e22c4e338bfdc069cc9c78325eb6b589fa94bcd', 0o400),
    'definitions/sidekick/github-context-memory.yaml': ('package/definitions/sidekick/github-context-memory.yaml', 4114,
        '22b699a6dd5b2d7964f8b2a913618ae478cf6910df2fc3ba336cf38b1be3b2ef', 0o400),
    'definitions/sidekick/github-context.yaml': ('package/definitions/sidekick/github-context.yaml', 2200,
        '9b7692e969e9ab36d7053fb2ef99ba9dd2ae78f6df75e645f54eb3cdb3fc6252', 0o400),
    'definitions/sidekick/session-search.yaml': ('package/definitions/sidekick/session-search.yaml', 1889,
        '8b6f48742fe847002e5ba6e00b05abc5339a5891387cd306790eb3b8c7dd8c11', 0o400),
    'definitions/sidekick/subconscious-agent.yaml': ('package/definitions/sidekick/subconscious-agent.yaml', 2616,
        '9fb4cb881677adc08c69818ecdde127276b1afe006817802c381a3ff5fb5887f', 0o400),
    'definitions/sidekick/test-sidekick-context-changed.yaml': ('package/definitions/sidekick/test-sidekick-context-changed.yaml', 1029,
        '117edd3750a403c6cabf7964f217ae8129b16f9d0a6d2503e09ebe79d64f9d50', 0o400),
    'definitions/sidekick/test-sidekick-persistent.yaml': ('package/definitions/sidekick/test-sidekick-persistent.yaml', 861,
        'a5ab1c16dd5f9a030010a375f7f26bca56aa196b212ea52ac2c5736cd80f3989', 0o400),
    'definitions/sidekick/test-sidekick-restart.yaml': ('package/definitions/sidekick/test-sidekick-restart.yaml', 831,
        '21e44442b8380a758793d637a60fe94c226a6547d23c5bea3a14546f7f3e60ec', 0o400),
    'definitions/sidekick/test-sidekick-trigger-once.yaml': ('package/definitions/sidekick/test-sidekick-trigger-once.yaml', 828,
        '14a7cedf6ead133828280744b293e90402a2d47568c98a67273c1d7c3acdd53e', 0o400),
    'definitions/task.agent.yaml': ('package/definitions/task.agent.yaml', 2074,
        '6625b8a1be22ce2fd7566fce684dd07aca4aed8ceb4fa76b350ff62dcb9093e1', 0o400),
    'ripgrep/bin/linux-x64/rg': ('package/ripgrep/bin/linux-x64/rg', 5728000,
        'f2ee496a39139b8d9ad9408081217a55f1382cef4950100565d147d247364856', 0o500),
    'runtime.node': ('package/prebuilds/linux-x64/runtime.node', 91132040,
        '79f649256bdb76f448c6804cc7165eea3ba90b7773ace6758aeb77518125fbd8', 0o500),
    'schemas/api.schema.json': ('package/schemas/api.schema.json', 1639823,
        '0d4416b3e042bf0064d5349dc2a30e40f2755f0ece0d9ef046fbbcfebaf4de42', 0o400),
    'schemas/session-events.schema.json': ('package/schemas/session-events.schema.json', 783312,
        'bea5ebe5060d68b70ce366f46b866d8dd19c252ef1df6445905cd2377954458e', 0o400),
    'tgrep/bin/linux-x64/tgrep': ('package/tgrep/bin/linux-x64/tgrep', 7588624,
        'd788c5323f1cd6e610e3f8629fc7797ec0ddaaf602027114c7ad508e12bd5d48', 0o500),
}


class CopilotProvisioningError(RuntimeError):
    """The selected installation or host does not meet the pinned contract."""


@dataclass(frozen=True)
class ProvisionedCopilotPaths:
    root: Path
    runtime_path: Path
    records_root: Path
    state_root: Path
    homes_root: Path


def check_sdk() -> None:
    """Check SDK metadata without importing it; dependency health needs pip check."""
    try:
        if metadata.version("github-copilot-sdk") == SDK_VERSION:
            return
    except metadata.PackageNotFoundError:
        pass
    raise CopilotProvisioningError("Install the pinned optional Copilot SDK")


def _check_host():
    if (platform.system() != "Linux" or platform.machine() != "x86_64"
            or platform.libc_ver()[0] != "glibc"):
        raise CopilotProvisioningError("Copilot provisioning requires Linux x86_64 glibc")


def _path(value):
    if (not isinstance(value, Path) or value.anchor != "/" or ".." in value.parts
            or value == Path("/")):
        raise ValueError()
    return value


def _scope(root, forbidden_roots):
    _path(root)
    if not isinstance(forbidden_roots, tuple):
        raise ValueError()
    for forbidden in forbidden_roots:
        _path(forbidden)
        canonical = forbidden.resolve()
        if root.is_relative_to(canonical) or canonical.is_relative_to(root):
            raise ValueError()


def _private(info, mode=0o700, *, directory=True):
    return (info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == mode
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1))


def _directory(name, parent):
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    if not _private(os.fstat(descriptor)):
        os.close(descriptor)
        raise ValueError()
    return descriptor


def _asset_directories():
    return {str(parent) for name in _ASSETS for parent in PurePosixPath(name).parents if str(parent) != "."}


def _parent_directory(root, relative):
    descriptor = os.dup(root)
    try:
        for component in PurePosixPath(relative).parts[:-1]:
            child = _directory(component, descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_runtime(descriptor, prefix=""):
    directories = _asset_directories()
    expected = {name.split("/")[0] for name in _ASSETS}
    if prefix:
        expected = {name[len(prefix):].split("/")[0] for name in _ASSETS if name.startswith(prefix)}
    if set(os.listdir(descriptor)) != expected:
        raise ValueError()
    for name in expected:
        relative = prefix + name
        if relative in directories:
            child = _directory(name, descriptor)
            try:
                _verify_runtime(child, relative + "/")
            finally:
                os.close(child)
        else:
            _, size, digest, mode = _ASSETS[relative]
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(child, "rb") as source:
                before = os.fstat(source.fileno())
                if not _private(before, mode, directory=False) or before.st_size != size:
                    raise ValueError()
                if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                    raise ValueError()
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (not _private(after, mode, directory=False) or _identity(after) != _identity(before)
                        or after.st_size != size or after.st_mtime_ns != before.st_mtime_ns):
                    raise ValueError()


def _load(root, forbidden_roots):
    _scope(root, forbidden_roots)
    descriptor = _open_directory(root)
    try:
        before = os.fstat(descriptor)
        if not _private(before) or set(os.listdir(descriptor)) != _DIRECTORIES:
            raise ValueError()
        for name in _DIRECTORIES:
            child = _directory(name, descriptor)
            try:
                if name == "runtime":
                    _verify_runtime(child)
            finally:
                os.close(child)
        current = _open_directory(root)
        try:
            if _identity(os.fstat(current)) != _identity(before):
                raise ValueError()
        finally:
            os.close(current)
    finally:
        os.close(descriptor)
    return ProvisionedCopilotPaths(root, root / "runtime/copilot-runtime", root / "records", root / "state", root / "homes")


def load(root: Path, *, forbidden_roots: tuple[Path, ...] = ()) -> ProvisionedCopilotPaths:
    """Read-only verification; session contents are neither read nor modified."""
    _check_host()
    try:
        return _load(root, forbidden_roots)
    except Exception:
        pass
    raise CopilotProvisioningError("Copilot installation is invalid or incomplete")


def _archive(archive):
    _path(archive)
    parent = _open_directory(archive.parent)
    try:
        descriptor = os.open(archive.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size != ARCHIVE_SIZE or info.st_size > _MAX_ARCHIVE):
            raise ValueError()
        # A bounded immutable snapshot closes the hash/extraction mutation race.
        data = source.read(_MAX_ARCHIVE + 1)
        if len(data) != ARCHIVE_SIZE or hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
            raise ValueError()
        return io.BytesIO(data)


def _extract(source, stage):
    runtime = _directory("runtime", stage)
    try:
        for name in sorted(_asset_directories(), key=lambda value: (value.count("/"), value)):
            parent = _parent_directory(runtime, name)
            try:
                os.mkdir(PurePosixPath(name).name, mode=0o700, dir_fd=parent)
            finally:
                os.close(parent)
        selected = {spec[0]: (name, spec) for name, spec in _ASSETS.items()}
        seen, extracted = set(), set()
        expanded = 0
        with tarfile.open(fileobj=source, mode="r|gz") as package:
            for member in package:
                name = PurePosixPath(member.name)
                expanded += member.size
                if (member.name in seen or len(seen) >= _MAX_MEMBERS or expanded > _MAX_EXPANDED
                        or not member.isfile() or member.size < 0 or member.issparse()
                        or name.is_absolute() or ".." in name.parts or "\\" in member.name
                        or str(name) != member.name or not name.parts or name.parts[0] != "package"):
                    raise ValueError()
                seen.add(member.name)
                if member.name not in selected:
                    continue
                destination, (_, size, digest, mode) = selected[member.name]
                if member.size != size:
                    raise ValueError()
                parent = _parent_directory(runtime, destination)
                try:
                    descriptor = os.open(PurePosixPath(destination).name,
                                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         mode, dir_fd=parent)
                finally:
                    os.close(parent)
                with os.fdopen(descriptor, "wb") as target, package.extractfile(member) as content:
                    if not _private(os.fstat(target.fileno()), mode, directory=False):
                        raise ValueError()
                    checksum = hashlib.sha256()
                    remaining = size
                    while remaining:
                        data = content.read(min(remaining, 1024 * 1024))
                        if not data:
                            raise ValueError()
                        remaining -= len(data)
                        checksum.update(data)
                        target.write(data)
                    if checksum.hexdigest() != digest:
                        raise ValueError()
                    target.flush()
                    os.fsync(target.fileno())
                extracted.add(destination)
        if extracted != set(_ASSETS):
            raise ValueError()
        _verify_runtime(runtime)
        os.fsync(runtime)
    finally:
        os.close(runtime)


def _publish(parent, staging, destination):
    # os.rename may replace an existing empty directory. Linux NOREPLACE is
    # mandatory so another initializer or existing user data can never lose.
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(staging), parent, os.fsencode(destination), 1):
        raise OSError(ctypes.get_errno(), "Copilot installation publication failed")


def initialize(root: Path, archive: Path, *, forbidden_roots: tuple[Path, ...] = ()) -> ProvisionedCopilotPaths:
    """Atomically create an absent installation, or verify an existing one.

    Parent must already be UID-owned 0700. Existing partial/empty installations
    are rejected, never filled or overwritten. A racing initializer may be
    retried after the winning installation has published its complete tree.
    """
    _check_host()
    parent = stage = None
    staging = None
    try:
        _scope(root, forbidden_roots)
        parent = _open_directory(root.parent)
        parent_info = os.fstat(parent)
        if not _private(parent_info) or not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError()
        try:
            os.stat(root.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return _load(root, forbidden_roots)
        # Hash the pinned input before creating any installation directory.
        with _archive(archive) as source:
            staging = ".copilot-install-" + secrets.token_hex(16)
            os.mkdir(staging, mode=0o700, dir_fd=parent)
            stage = _directory(staging, parent)
            for name in _DIRECTORIES:
                os.mkdir(name, mode=0o700, dir_fd=stage)
            _extract(source, stage)
            os.fsync(stage)
            current = _open_directory(root.parent)
            try:
                if _identity(os.fstat(current)) != _identity(parent_info):
                    raise ValueError()
            finally:
                os.close(current)
            _publish(parent, staging, root.name)
            staging = None
            os.fsync(parent)
        return _load(root, forbidden_roots)
    except Exception:
        pass
    finally:
        if stage is not None:
            os.close(stage)
        if staging is not None and parent is not None:
            # Only our newly allocated temporary sibling; never the destination.
            with suppress(OSError):
                shutil.rmtree(staging, dir_fd=parent)
        if parent is not None:
            os.close(parent)
    raise CopilotProvisioningError("Copilot installation could not be initialized")
