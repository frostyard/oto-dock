"""Own SDK-hosted tool callbacks independently of the runtime's task registry.

Session abort/idle does not cancel these Python callbacks. A supervisor must
explicitly cancel and join them before using their cancellation as settlement
proof. This module neither launches a runtime nor registers an execution layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import math
from typing import TypeVar

T = TypeVar("T")


class DuplicateCallbackError(ValueError):
    """A tool call ID has already been consumed by this session registry."""


class CallbackExecutionError(RuntimeError):
    """Sanitized callback failure; never includes the original exception message."""

    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__(f"Tool callback failed ({error_type})")


class CallbackRegistry:
    """Single-event-loop callback ownership and conservative cancellation proof.

    ``on_change`` must be synchronous and non-raising, normally the coordinator's
    ``invalidate_observation``. It runs before each ownership transition. IDs
    cannot be reused for this registry's lifetime, even after completion/error.
    Create a fresh registry for a different session; do not recycle IDs to replay
    uncertain side effects.
    """

    def __init__(self, on_change: Callable[[], None]):
        self._on_change = on_change
        self._tasks: dict[str, asyncio.Task] = {}
        self._seen: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._cancelled: set[str] = set()

    @property
    def pending_ids(self) -> frozenset[str]:
        # Conservative until the done callback invalidates observers and drops
        # ownership; checking task.done() here could expose the new state early.
        return frozenset(self._tasks)

    async def run(self, tool_call_id: str, factory: Callable[[], Awaitable[T]]) -> T:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("A nonempty tool call ID is required")
        if tool_call_id in self._seen:
            raise DuplicateCallbackError("Tool call ID already consumed")
        self._on_change()
        self._seen.add(tool_call_id)
        task = asyncio.create_task(self._invoke(factory), name="copilot-owned-callback")
        self._tasks[tool_call_id] = task
        task.add_done_callback(lambda completed: self._settled(tool_call_id, completed))
        # SDK cancellation may stop waiting, but cannot erase our ownership or
        # silently stop a callback halfway through an externally visible action.
        return await asyncio.shield(task)

    @staticmethod
    async def _invoke(factory: Callable[[], Awaitable[T]]) -> T:
        try:
            return await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_type = type(exc).__name__
        # Raise outside the except suite: neither message nor exception context
        # retains the underlying potentially secret-bearing error.
        raise CallbackExecutionError(error_type)

    def _settled(self, tool_call_id: str, task: asyncio.Task) -> None:
        self._on_change()
        if task.cancelled():
            self._cancelled.add(tool_call_id)
        else:
            # Retrieve errors even if the SDK stopped awaiting this callback.
            # The registry retains ID sets, not results, tracebacks, or tasks.
            task.exception()
        self._tasks.pop(tool_call_id, None)
        self._cancel_requested.discard(tool_call_id)

    async def cancel_all(self, timeout: float) -> frozenset[str]:
        """Request cancellation once and wait at most ``timeout`` seconds.

        Returns cumulative IDs confirmed cancelled and joined in this registry;
        intersect them with currently open tool IDs before supplying settlement
        proof. A late cancellation after an earlier timeout remains available on
        the next call. A callback that suppresses cancellation and returns a
        value is completed, but is never reported as cancelled.

        Resistant callbacks remain owned/pending. Repeated cancellation calls do
        not inject another CancelledError into their cleanup. Escalation beyond
        cooperative cancellation belongs to the supervisor's process boundary.
        """
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Cancellation timeout must be finite and nonnegative")
        tasks = tuple(self._tasks.items())
        for tool_call_id, task in tasks:
            if not task.done() and tool_call_id not in self._cancel_requested:
                self._on_change()
                self._cancel_requested.add(tool_call_id)
                task.cancel()
        if tasks:
            # wait_for(gather(...)) can exceed its timeout while a callback
            # suppresses cancellation; asyncio.wait keeps this deadline bounded.
            await asyncio.wait([task for _, task in tasks], timeout=timeout)
        return frozenset(self._cancelled)
