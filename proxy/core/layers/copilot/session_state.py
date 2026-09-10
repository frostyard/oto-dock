"""Private runtime history whose lifetime is independent of runtime processes.

The caller supplies a trusted host root outside every agent-writable workspace
and sandbox mount. Each create() allocates a distinct directory; there is no
account lookup, history copying, token persistence, or automatic disposal on
runtime restart. Retain the same object to resume, then explicitly discard it.
"""

from contextlib import suppress
import os
from pathlib import Path
import shutil
import stat
import tempfile

SANDBOX_STATE_DIRECTORY = "/var/lib/otodock/copilot"


class SessionStateError(RuntimeError):
    """Private Copilot state cannot be created, accessed, or safely discarded."""


def _open_directory(path: Path) -> int:
    """Walk from / with no symlink following, including ancestor components."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _private_owner(info: os.stat_result) -> bool:
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


class PrivateCopilotSessionState:
    sandbox_destination = SANDBOX_STATE_DIRECTORY

    def __init__(self) -> None:
        raise TypeError("Use PrivateCopilotSessionState.create")

    @classmethod
    def create(cls, root: Path) -> "PrivateCopilotSessionState":
        """Allocate 0700 state under an existing, owned, trusted Linux root."""
        descriptor = None
        name = None
        try:
            root = Path(root)
            if not root.is_absolute() or ".." in root.parts:
                raise SessionStateError("Invalid Copilot state root")
            if not shutil.rmtree.avoids_symlink_attacks:
                raise SessionStateError("Safe Copilot state cleanup is unavailable")
            descriptor = _open_directory(root)
            info = os.fstat(descriptor)
            if not _private_owner(info):
                raise SessionStateError("Invalid Copilot state root")
            # Allocate through the pinned root FD, not a pathname that could
            # have been replaced between validation and mkdtemp().
            allocated = tempfile.mkdtemp(prefix="session-", dir=f"/proc/self/fd/{descriptor}")
            name = Path(allocated).name
            state_fd = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                               dir_fd=descriptor)
            try:
                # O_PATH permits repairing a restrictive umask without needing
                # read access first; /proc resolves the exact pinned directory.
                os.chmod(f"/proc/self/fd/{state_fd}", 0o700)
                state_info = os.fstat(state_fd)
            finally:
                os.close(state_fd)
            instance = object.__new__(cls)
            instance._root = root
            instance._root_fd = descriptor
            instance._root_identity = _identity(info)
            instance._name = name
            instance._state_identity = _identity(state_info)
            instance._discarded = False
            instance._validate()
            return instance
        except Exception:
            if descriptor is not None:
                if name is not None:
                    # The new directory has not been exposed or populated yet.
                    with suppress(OSError):
                        os.rmdir(name, dir_fd=descriptor)
                os.close(descriptor)
            raise SessionStateError("Copilot private state creation failed") from None

    def _validate(self) -> None:
        if self._discarded:
            raise SessionStateError("Copilot private state was discarded")
        current_fd = _open_directory(self._root)
        try:
            root_info = os.fstat(current_fd)
            if _identity(root_info) != self._root_identity or not _private_owner(root_info):
                raise SessionStateError("Copilot state ownership changed")
        finally:
            os.close(current_fd)
        info = os.stat(self._name, dir_fd=self._root_fd, follow_symlinks=False)
        if (_identity(info) != self._state_identity or not _private_owner(info)
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise SessionStateError("Copilot state ownership changed")

    @property
    def path(self) -> Path:
        """Stable host mount source; fails closed after replacement or discard."""
        try:
            self._validate()
            return self._root / self._name
        except Exception:
            raise SessionStateError("Copilot private state is unavailable") from None

    def discard(self) -> None:
        """Remove only this allocation; a changed root/entry is never adopted.

        Stop every runtime using this state before disposal. The trusted root
        must remain under exclusive host control throughout this operation.
        """
        if self._discarded:
            return
        try:
            self._validate()
            shutil.rmtree(self._name, dir_fd=self._root_fd)
        except Exception:
            raise SessionStateError("Copilot private state could not be safely discarded") from None
        self._discarded = True
        os.close(self._root_fd)

    def close(self) -> None:
        """Explicit final discard, not a runtime-rotation hook."""
        self.discard()
