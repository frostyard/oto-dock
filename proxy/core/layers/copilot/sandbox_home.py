"""Host-owned scratch homes isolated from every other engine's credentials.

The caller supplies an existing private root outside all sandbox mounts. Only
one session's hashed child is mounted as its writable scratch home. Reopening
preserves its contents; this helper never copies, chmods, or deletes state.
"""

from __future__ import annotations

from contextlib import suppress
import hashlib
import os
from pathlib import Path
import stat
import threading

from core.layers.copilot.session_state import _identity, _open_directory


class SandboxHomeError(RuntimeError):
    """The selected private scratch home could not be safely established."""


def _private_directory(info):
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700)


class CopilotSandboxHomes:
    """Pin a trusted root; converge concurrent creators on stable session paths.

    The root's pathname must stay under exclusive host control. Model processes
    may write inside their mounted child, but must never receive the parent root.
    Directory substitution after a successful get is rejected by this instance.
    close() releases only descriptors; it does not remove session contents.
    """

    def __init__(self, root: Path):
        descriptor = None
        try:
            if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
                raise ValueError()
            descriptor = _open_directory(root)
            info = os.fstat(descriptor)
            if not _private_directory(info):
                raise ValueError()
            self._root = root
            self._root_fd = descriptor
            self._root_identity = _identity(info)
            self._children = {}
            self._lock = threading.Lock()
            self._closed = False
            return
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
        raise SandboxHomeError("Invalid Copilot scratch home root")

    def _validate_root(self):
        if self._closed:
            raise ValueError()
        descriptor = _open_directory(self._root)
        try:
            info = os.fstat(descriptor)
            pinned = os.fstat(self._root_fd)
            if (not _private_directory(info) or not _private_directory(pinned)
                    or _identity(info) != self._root_identity or _identity(pinned) != self._root_identity):
                raise ValueError()
        finally:
            os.close(descriptor)

    @property
    def root(self) -> Path:
        try:
            with self._lock:
                self._validate_root()
                return self._root
        except Exception:
            pass
        raise SandboxHomeError("Copilot scratch home root is unavailable")

    def get(self, session_id: str) -> Path:
        """Create/revalidate an exact 0700 directory, never interpret an ID as a path.

        The process umask must permit owner rwx when creating new directories.
        An unexpected mode fails closed rather than repairing existing state.
        """
        try:
            if (not isinstance(session_id, str) or not 0 < len(session_id) <= 256
                    or session_id != session_id.strip() or not session_id.isprintable()):
                raise ValueError()
            name = "session-" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            with self._lock:
                self._validate_root()
                with suppress(FileExistsError):
                    os.mkdir(name, mode=0o700, dir_fd=self._root_fd)
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=self._root_fd,
                )
                try:
                    info = os.fstat(descriptor)
                    current = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                    identity = _identity(info)
                    if (not _private_directory(info) or not _private_directory(current)
                            or _identity(current) != identity
                            or self._children.get(name, identity) != identity):
                        raise ValueError()
                    self._validate_root()
                    self._children[name] = identity
                    return self._root / name
                finally:
                    os.close(descriptor)
        except Exception:
            pass
        raise SandboxHomeError("Copilot scratch home is unavailable")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                os.close(self._root_fd)
                self._children.clear()

    def __enter__(self):
        _ = self.root
        return self

    def __exit__(self, *_):
        self.close()
