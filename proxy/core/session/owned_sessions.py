"""Loop-owned runtime claims for engines independent of legacy session pools.

Membership protects resources until the actual owner confirms cleanup. It is
not authentication, engine selection, a distributed lock, or proof of liveness.
Only trusted lifecycle code may publish/release claims; callbacks capture the
exact owner generation, never a lookup by mutable agent config or session ID.
"""

import asyncio
from dataclasses import dataclass, field
import inspect
from typing import Awaitable, Callable


class SessionOwnershipError(RuntimeError):
    """A host runtime claim or its cleanup is unavailable."""


_sessions: dict[str, "OwnedSession"] = {}
_shutting_down = False


def _text(value, *, empty=False):
    return (isinstance(value, str) and len(value) <= 256
            and ((empty and value == "") or (bool(value) and value.strip() == value and value.isprintable())))


@dataclass(frozen=True, eq=False)
class OwnedSession:
    session_id: str
    engine: str
    agent: str
    user_sub: str
    username: str  # Mount username, not the human's attribution/display name.
    local: bool
    _active: Callable[[], bool] = field(repr=False)
    _close: Callable[[], Awaitable[None]] = field(repr=False)
    _closing: asyncio.Task | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        """Usable for new requests; failure never grants admission."""
        if _sessions.get(self.session_id) is not self or self._closing is not None:
            return False
        try:
            active = self._active() is True
            return active and _sessions.get(self.session_id) is self and self._closing is None
        except (Exception, asyncio.CancelledError):
            return False

    async def _finish_close(self):
        failed = False
        try:
            await self._close()
            if _sessions.get(self.session_id) is self:
                failed = True
        except (Exception, asyncio.CancelledError):
            failed = True
        if failed:
            raise SessionOwnershipError("Owned session cleanup did not complete")

    async def close(self) -> bool:
        """Join one exact-generation close; cancelling a waiter cannot kill it.

        Stale snapshots return False and never address a replacement. The owner
        releases its claim only after confirmed cleanup; errors retain ownership.
        """
        if _sessions.get(self.session_id) is not self:
            return False
        if self._closing is None:
            task = asyncio.create_task(self._finish_close())
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            object.__setattr__(self, "_closing", task)
        await asyncio.shield(self._closing)
        return True


def register_owned_session(*, session_id: str, engine: str, agent: str,
                           user_sub: str, username: str, local: bool = True,
                           active: Callable[[], bool],
                           close: Callable[[], Awaitable[None]]) -> OwnedSession:
    """Claim synchronously before startup's first await. No callback runs here."""
    if (not all(_text(value) for value in (session_id, engine, agent))
            or not all(_text(value, empty=True) for value in (user_sub, username))
            or type(local) is not bool or not callable(active) or inspect.iscoroutinefunction(active)
            or not callable(close)):
        raise SessionOwnershipError("Invalid owned session registration")
    if _shutting_down or session_id in _sessions:
        raise SessionOwnershipError("Owned session admission is unavailable")
    handle = OwnedSession(session_id, engine, agent, user_sub, username, local, active, close)
    _sessions[session_id] = handle
    return handle


def release_owned_session(handle: OwnedSession) -> bool:
    """The actual owner calls this after cleanup, with its original handle."""
    if type(handle) is not OwnedSession or _sessions.get(handle.session_id) is not handle:
        return False
    del _sessions[handle.session_id]
    return True


def get_owned_session(session_id: str) -> OwnedSession | None:
    return _sessions.get(session_id)


def owned_sessions(*, local_only: bool = False) -> tuple[OwnedSession, ...]:
    if type(local_only) is not bool:
        raise ValueError("Invalid owned session snapshot scope")
    return tuple(handle for handle in _sessions.values() if not local_only or handle.local)


def owned_session_ids(*, local_only: bool = False) -> frozenset[str]:
    return frozenset(handle.session_id for handle in owned_sessions(local_only=local_only))


def begin_owned_session_shutdown() -> tuple[OwnedSession, ...]:
    """Stop new claims before taking the shutdown snapshot; no reopen in-process."""
    global _shutting_down
    _shutting_down = True
    return owned_sessions()
