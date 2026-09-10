"""Own permission/question waits separately from tool execution callbacks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import math
from typing import TypeVar
import uuid

from core.layers.copilot.callbacks import CallbackRegistry

T = TypeVar("T")


class RequestUnavailableError(RuntimeError):
    """A host request cannot produce a usable answer; contains no payload."""


class CopilotRequestRegistry:
    """Track host waits even when the SDK abandons its callback waiter.

    The pinned SDK exposes no request ID to its permission/question handlers.
    These IDs are local ownership IDs, not native replay or resolution proof.
    A control invalidates all outstanding answers before awaiting its RPC.
    Even a policy callback that suppresses cancellation cannot approve work
    after that invalidation, including after a subsequent resume.
    """

    def __init__(self, on_change: Callable[[], None]):
        self._registry = CallbackRegistry(on_change=on_change)
        self._on_change = on_change
        self._deliveries: set[str] = set()
        self._generation = 0
        self._paused = False
        self._closed = False

    @property
    def pending_ids(self) -> frozenset[str]:
        return self._registry.pending_ids | frozenset(self._deliveries)

    def pause_admissions(self) -> None:
        if not self._paused:
            self._registry.pause_admissions()
            self._generation += 1
            self._paused = True

    def resume_admissions(self) -> None:
        if self._closed:
            raise RequestUnavailableError("Copilot requests are unavailable")
        self._registry.resume_admissions()
        self._paused = False

    def close_admissions(self) -> None:
        if not self._closed:
            self._registry.close_admissions()
            self._generation += 1
            self._closed = True

    async def run(self, factory: Callable[[], Awaitable[T]]) -> T:
        if self._paused or self._closed:
            raise RequestUnavailableError("Copilot requests are unavailable")
        generation = self._generation

        def check() -> None:
            if self._paused or self._closed or generation != self._generation:
                raise RequestUnavailableError("Copilot request was invalidated")

        async def invoke() -> T:
            check()
            result = await factory()
            check()
            return result

        request_id = f"request-{uuid.uuid4().hex}"
        self._on_change()
        self._deliveries.add(request_id)
        try:
            result = await self._registry.run(request_id, invoke)
            check()
            return result
        except Exception:
            pass
        finally:
            # The policy task's done callback can run before its shielded
            # waiter resumes. Keep ownership through validation/delivery too,
            # so settlement cannot overtake an answer about to be returned.
            self._on_change()
            self._deliveries.discard(request_id)
        # No raw policy error, request, answer or exception context escapes.
        # CancelledError deliberately propagates to the SDK bridge, which must
        # answer with the native reject/decline shape rather than logging it.
        raise RequestUnavailableError("Copilot request could not be answered")

    async def cancel_all(self, timeout: float) -> frozenset[str]:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Cancellation timeout must be finite and nonnegative")
        self.pause_admissions()
        return await self._registry.cancel_all(timeout)
