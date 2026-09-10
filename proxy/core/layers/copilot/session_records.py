"""Durable host-only profile records for one guarded native Copilot session.

Both configured roots must be existing private host directories, outside every
agent workspace and sandbox mount. Records are not an authorization database:
the trusted factory must revalidate account access and live policy separately.
An ACTIVE record is never automatically resumed, even after its flock is free;
only verified runtime shutdown after a clean completed turn permits mark_ready().
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .credentials import AccountScopeKind, CopilotAccountScope, CredentialKind
from .runtime import RUNTIME_VERSION, SDK_VERSION
from .session_state import (
    PrivateCopilotSessionState, SessionStateAllocation, _identity, _open_directory, _private_owner,
)

POLICY_PROFILE = "native-shell-v1"
_TOOLS = frozenset({"bash", "create", "edit", "view", "glob", "grep"})
_MAX_RECORD = 65536


class SessionRecordError(RuntimeError):
    """A durable Copilot session record is unavailable; never fall back to create."""


class SessionRecordBusyError(SessionRecordError):
    """Another host owner holds this platform session's exclusive writer lock."""


class SessionRecordNotFoundError(SessionRecordError):
    """The explicitly requested resume record does not exist."""


class SessionRecordMismatchError(SessionRecordError):
    """Stored provenance differs, or its prior runtime was not cleanly retired."""


class SessionRecordExistsError(SessionRecordError):
    """Creation cannot replace an existing platform session record."""


def _text(value, limit=4096):
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and value.isprintable())


@dataclass(frozen=True)
class CopilotSessionProfile:
    account_id: str
    principal_id: str
    credential_kind: CredentialKind
    scope: CopilotAccountScope
    platform_session_id: str
    user_sub: str
    agent_id: str
    workspace: str
    model: str
    enabled_tools: frozenset[str]
    config_digest: str
    sdk_version: str = SDK_VERSION
    runtime_version: str = RUNTIME_VERSION
    policy_profile: str = POLICY_PROFILE

    def __post_init__(self):
        if (not all(_text(value) for value in (
                self.account_id, self.principal_id, self.platform_session_id,
                self.agent_id, self.workspace, self.model))
                or not isinstance(self.credential_kind, CredentialKind)
                or not isinstance(self.scope, CopilotAccountScope)
                or (self.scope.kind is AccountScopeKind.PERSONAL and self.user_sub != self.scope.user_sub)
                or (self.scope.kind is AccountScopeKind.PLATFORM and self.user_sub != "" and not _text(self.user_sub))
                or not Path(self.workspace).is_absolute() or ".." in Path(self.workspace).parts
                or "\\" in self.workspace
                or not isinstance(self.enabled_tools, frozenset) or not self.enabled_tools
                or not self.enabled_tools <= _TOOLS
                or not isinstance(self.config_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.config_digest) is None
                or self.sdk_version != SDK_VERSION or self.runtime_version != RUNTIME_VERSION
                or self.policy_profile != POLICY_PROFILE):
            raise SessionRecordError("Invalid Copilot session profile")

    def _document(self):
        return {
            **{name: getattr(self, name) for name in (
                "account_id", "principal_id", "platform_session_id", "user_sub", "agent_id", "workspace",
                "model", "config_digest", "sdk_version", "runtime_version", "policy_profile",
            )},
            "credential_kind": self.credential_kind.value,
            "scope": {"kind": self.scope.kind.value, "user_sub": self.scope.user_sub},
            "enabled_tools": sorted(self.enabled_tools),
        }


def _private_file(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SessionRecordError("Invalid Copilot session record")
        result[key] = value
    return result


class CopilotSessionRecords:
    """Trusted roots are siblings; only individual state allocations are mounted."""

    def __init__(self, root: Path, *, state_root: Path):
        try:
            self._root, self._state_root = Path(root), Path(state_root)
            if any(not path.is_absolute() or ".." in path.parts for path in (self._root, self._state_root)):
                raise ValueError()
            if self._root.is_relative_to(self._state_root) or self._state_root.is_relative_to(self._root):
                raise ValueError()
            self._root_identity = self._directory_identity(self._root)
            self._state_root_identity = self._directory_identity(self._state_root)
            return
        except Exception:
            pass
        raise SessionRecordError("Invalid Copilot session record roots")

    @property
    def root(self) -> Path:
        return self._root

    @property
    def state_root(self) -> Path:
        return self._state_root

    @staticmethod
    def _directory_identity(path):
        descriptor = _open_directory(path)
        try:
            info = os.fstat(descriptor)
            if not _private_owner(info) or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError()
            return _identity(info)
        finally:
            os.close(descriptor)

    def _lock(self, profile):
        descriptor = lock = None
        error = SessionRecordError
        try:
            if not isinstance(profile, CopilotSessionProfile):
                raise ValueError()
            if self._directory_identity(self._state_root) != self._state_root_identity:
                raise ValueError()
            descriptor = _open_directory(self._root)
            info = os.fstat(descriptor)
            if (_identity(info) != self._root_identity or not _private_owner(info)
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError()
            key = hashlib.sha256(profile.platform_session_id.encode()).hexdigest()
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                lock = os.open(key + ".lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=descriptor)
                os.fchmod(lock, 0o600)
                os.fsync(descriptor)
            except FileExistsError:
                lock = os.open(key + ".lock", flags, dir_fd=descriptor)
            if not _private_file(os.fstat(lock)):
                raise ValueError()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                error = SessionRecordBusyError
                raise
            return LockedCopilotSessionRecord(self, profile, descriptor, lock, key)
        except Exception:
            if lock is not None:
                os.close(lock)
            if descriptor is not None:
                os.close(descriptor)
        raise error("Copilot session record could not be exclusively opened")

    def create(self, profile: CopilotSessionProfile, *, native_session_id: str):
        if not _text(native_session_id, 256):
            raise SessionRecordError("Invalid native Copilot session identity")
        handle = self._lock(profile)
        error = SessionRecordError
        try:
            try:
                handle._read()
            except FileNotFoundError:
                pass
            else:
                error = SessionRecordExistsError
                raise error()
            handle._state = PrivateCopilotSessionState.create(self._state_root)
            handle._document = {
                "version": 1, "status": "active", "profile": profile._document(),
                "native_session_id": native_session_id, "allocation": asdict(handle._state.allocation),
            }
            handle._write(handle._document)
            return handle
        except Exception:
            handle.close()
        raise error("Copilot session record could not be created")

    def open(self, profile: CopilotSessionProfile):
        handle = self._lock(profile)
        error = SessionRecordError
        try:
            try:
                document = handle._read()
            except FileNotFoundError:
                error = SessionRecordNotFoundError
                raise
            if document["profile"] != profile._document() or document["status"] != "ready":
                error = SessionRecordMismatchError
                raise error()
            handle._state = PrivateCopilotSessionState.reopen(
                self._state_root, SessionStateAllocation(**document["allocation"]),
            )
            handle._document = {**document, "status": "active"}
            # A crash at any later point leaves ACTIVE, even if no SDK RPC ran.
            handle._write(handle._document)
            return handle
        except Exception:
            handle.close()
        raise error("Copilot session resume provenance could not be established")


class LockedCopilotSessionRecord:
    """One exclusive runtime owner; close retains history and current status."""

    def __init__(self, records, profile, descriptor, lock, key):
        self._records, self._profile = records, profile
        self._root_fd, self._lock_fd, self._key = descriptor, lock, key
        self._state = None
        self._document = None
        self._closed = False

    def _validate(self):
        if self._closed or self._records._directory_identity(self._records._root) != self._records._root_identity:
            raise SessionRecordError("Copilot session record ownership changed")
        info = os.stat(self._key + ".lock", dir_fd=self._root_fd, follow_symlinks=False)
        if not _private_file(info) or _identity(info) != _identity(os.fstat(self._lock_fd)):
            raise SessionRecordError("Copilot session record ownership changed")

    def _read(self):
        self._validate()
        descriptor = os.open(self._key + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=self._root_fd)
        try:
            info = os.fstat(descriptor)
            if not _private_file(info) or not 0 < info.st_size <= _MAX_RECORD:
                raise ValueError()
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                document = json.loads(stream.read(_MAX_RECORD + 1), object_pairs_hook=_unique_object)
        finally:
            os.close(descriptor)
        if (not isinstance(document, dict)
                or document.keys() != {"version", "status", "profile", "native_session_id", "allocation"}
                or type(document["version"]) is not int or document["version"] != 1
                or document["status"] not in ("active", "ready")
                or not isinstance(document["profile"], dict)
                or not isinstance(document["allocation"], dict)
                or not _text(document["native_session_id"], 256)):
            raise ValueError()
        SessionStateAllocation(**document["allocation"])
        return document

    def _write(self, document):
        self._validate()
        data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        if len(data) > _MAX_RECORD:
            raise ValueError()
        temporary = self._key + ".tmp-" + uuid.uuid4().hex
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=self._root_fd)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(os.dup(descriptor), "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._validate()
            os.replace(temporary, self._key + ".json", src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._root_fd)

    @property
    def profile(self):
        return self._profile

    @property
    def native_session_id(self):
        try:
            self._validate()
            return self._document["native_session_id"]
        except Exception:
            pass
        raise SessionRecordError("Copilot native session identity is unavailable")

    @property
    def state(self):
        try:
            self._validate()
            if self._document["status"] != "active":
                raise ValueError()
            _ = self._state.path
            return self._state
        except Exception:
            pass
        raise SessionRecordError("Copilot session state is unavailable")

    def mark_ready(self):
        """Trusted factory only: runtime fully closed and clean DONE verified.

        This is a provenance commit, not a shutdown request. No further runtime
        work may use this handle; state access is sealed until verified reopen.
        """
        try:
            self._validate()
            if self._read() != self._document:
                raise ValueError()
            _ = self._state.path
            document = {**self._document, "status": "ready"}
            self._write(document)
            self._document = document
            return
        except Exception:
            pass
        raise SessionRecordError("Copilot session could not be marked resumable")

    def close(self):
        if not self._closed:
            self._closed = True
            if self._state is not None:
                self._state.detach()
            os.close(self._lock_fd)
            os.close(self._root_fd)

    def __enter__(self):
        self._validate()
        return self

    def __exit__(self, *_args):
        self.close()
