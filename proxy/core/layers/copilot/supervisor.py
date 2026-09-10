"""Bounded single-consumer Copilot turns over an explicitly owned backend.

This is an unregistered session component, not an ExecutionLayer. The backend
owns RPCs; the runtime owner owns processes. Callers install receive_event before
creating/resuming a session and explicitly inventory pending host requests.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import math
from typing import AsyncIterator, Awaitable, Callable, Protocol
import uuid

from core.events.common_events import CommonEvent, DONE, TOOL_RESULT
from core.layers.copilot.callbacks import CallbackRegistry
from core.layers.copilot.requests import CopilotRequestRegistry
from core.layers.copilot.permission_events import CopilotPermissionEvents
from core.layers.copilot.coordinator import (
    CopilotTurnCoordinator, SettlementObservation, TaskObservation,
)


@dataclass(frozen=True)
class RuntimeSnapshot:
    processing: bool | None
    tasks: tuple[TaskObservation, ...] | None
    pending_permissions: frozenset[str] | None
    pending_messages: frozenset[str] | None
    # Only the guarded shell adapter supplies this after an accepted control,
    # terminal native inventory AND independent owned-process settlement.
    native_shells_stopped: bool = False


class SessionBackend(Protocol):
    async def send(self, prompt: str, *, immediate: bool = False) -> str: ...
    async def abort(self) -> None: ...
    async def interrupt(self) -> bool: ...
    async def snapshot(self) -> RuntimeSnapshot: ...
    async def is_processing(self) -> bool: ...
    async def disconnect(self) -> None: ...


class SessionSupervisorError(RuntimeError):
    """Sanitized failure; no raw SDK exception or credential context escapes."""


@dataclass(frozen=True)
class ControlAcknowledgement:
    accepted: bool
    callbacks_stopped: bool
    # Neither field means graceful history preservation or completion.


class CopilotSessionSupervisor:
    def __init__(
        self, *, pending_requests: Callable[[], frozenset[str] | None],
        close_runtime: Callable[[], Awaitable[None]], queue_capacity: int = 512,
        rpc_timeout: float = 10, turn_timeout: float = 90,
        authorize_submission: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if (type(queue_capacity) is not int or queue_capacity < 1
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (rpc_timeout, turn_timeout))):
            raise ValueError("Positive Copilot session limits are required")
        self.coordinator = CopilotTurnCoordinator()
        self._changed = asyncio.Event()
        self.callbacks = CallbackRegistry(on_change=self.invalidate_observation)
        self.requests = CopilotRequestRegistry(on_change=self.invalidate_observation)
        self._pending_requests = pending_requests
        self._close_runtime = close_runtime
        self._authorize_submission = authorize_submission
        self._backend: SessionBackend | None = None
        self._events: deque[CommonEvent] = deque()
        self._capacity = queue_capacity
        self._rpc_timeout = rpc_timeout
        self._turn_timeout = turn_timeout
        self._sequence = 0
        self._delivered: set[str] = set()
        self._pending_inputs: set[str] = set()
        self._pending_user_inputs: set[str] = set()
        self._completed_user_inputs: set[str] = set()
        self._permission_events = CopilotPermissionEvents()
        self._open_tools: set[str] = set()
        self._native_shell_tools: set[str] = set()
        self._cancelled_tools: frozenset[str] = frozenset()
        self._interrupt_ticket = None
        self._control_pending = False
        self._control_awaiting_settlement = False
        self._active = False
        self._stream_generation = 0
        self._finishing = False
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._failure: SessionSupervisorError | None = None
        self._deadline: asyncio.TimerHandle | None = None

    def bind(self, backend: SessionBackend) -> None:
        if self._backend is not None or self._closed or self._close_task is not None:
            raise SessionSupervisorError("Copilot session is already bound or closed")
        self._backend = backend

    def invalidate_observation(self) -> None:
        """Also call this before changing externally owned permission/question state."""
        self.coordinator.invalidate_observation()
        self._changed.set()

    def _fail(self, message: str) -> None:
        self._failure = self._failure or SessionSupervisorError(message)
        self.coordinator.transport_lost()
        self._changed.set()
        # Stop owned work even if the consumer is paused at a yielded event.
        self._begin_close()

    def _check(self) -> SessionBackend:
        if self._failure:
            raise self._failure
        if self._closed or self._close_task is not None or self._backend is None:
            raise SessionSupervisorError("Copilot session is not available")
        return self._backend

    def _emit(self, events: list[CommonEvent]) -> None:
        if not events:
            return
        if len(self._events) + len(events) > self._capacity:
            self._fail("Copilot event consumer exceeded its bounded buffer")
            return
        for event in events:
            if event.type == DONE:
                # Completion is decided before the consumer drains the queue.
                # A pause on a preceding tool result must not admit new work.
                self._finishing = True
                self._cancel_deadline()
                self.requests.pause_admissions()
            if event.type == TOOL_RESULT:
                self._open_tools.discard(event.data.get("tool_id"))
                self._native_shell_tools.discard(event.data.get("tool_id"))
            self._events.append(event)
        self._changed.set()

    def receive_event(self, event) -> None:
        """SDK callback: validate before updating delivery/tool ownership state."""
        if self._closed or self._failure:
            return
        try:
            raw = event if isinstance(event, dict) else event.to_dict()
            self._sequence += 1
            translated = self.coordinator.receive_event(self._sequence, raw)
            kind, data = raw["type"], raw["data"]
            self._permission_events.observe(kind, data)
            if kind == "user.message" and isinstance(data.get("messageId"), str):
                message_id = data["messageId"]
                self._delivered.add(message_id)
                self._pending_inputs.discard(message_id)
            if kind in ("user_input.requested", "user_input.completed"):
                request_id = data.get("requestId")
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError("Invalid Copilot user input identity")
                if kind == "user_input.completed":
                    self._completed_user_inputs.add(request_id)
                    self._pending_user_inputs.discard(request_id)
                elif request_id not in self._completed_user_inputs:
                    self._pending_user_inputs.add(request_id)
            if kind == "tool.execution_start":
                # Translated starts exclude replays and child-owned tools.
                for output in translated:
                    if output.type == "tool_use":
                        self._open_tools.add(output.data["tool_id"])
                        if output.data.get("name") == "bash":
                            self._native_shell_tools.add(output.data["tool_id"])
            self._emit(translated)
            self._changed.set()
        except Exception:
            self._fail("Copilot event stream is invalid")

    def invalidate_credentials(self) -> None:
        """Called by the account observer even while the consumer is paused."""
        self._fail("Copilot account authorization changed; reconnect the session")

    async def _submit(self, prompt: str, *, immediate: bool = False,
                      stream_generation: int | None = None) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("A nonempty Copilot prompt is required")
        async with self.coordinator.writer():
            if stream_generation is not None and (
                not self._active or self._finishing or self._stream_generation != stream_generation
            ):
                raise SessionSupervisorError("No active Copilot stream to submit to")
            backend = self._check()
            if self._control_awaiting_settlement:
                raise SessionSupervisorError("Copilot control awaiting settlement; new input was not submitted")
            if self._authorize_submission is not None:
                authorization_failed = False
                try:
                    async with asyncio.timeout(self._rpc_timeout):
                        await self._authorize_submission()
                except asyncio.CancelledError:
                    self.invalidate_credentials()
                    raise
                except Exception:
                    self.invalidate_credentials()
                    authorization_failed = True
                if authorization_failed:
                    # Provider/store callbacks can carry tokens in exceptions;
                    # leave that exception suite before raising the public error.
                    raise self._failure
                # Settlement can progress while account validation awaits I/O.
                backend = self._check()
                if stream_generation is not None and (
                    not self._active or self._finishing or self._stream_generation != stream_generation
                ):
                    raise SessionSupervisorError("No active Copilot stream to submit to")
            self.coordinator.invalidate_observation(new_submission=True)
            self._interrupt_ticket = None
            dispatch_id = str(uuid.uuid4())
            self._pending_inputs.add(dispatch_id)
            try:
                async with asyncio.timeout(self._rpc_timeout):
                    message_id = await backend.send(prompt, immediate=immediate)
                if not isinstance(message_id, str) or not message_id:
                    raise ValueError("Missing accepted message ID")
                self.invalidate_observation()
                self._pending_inputs.remove(dispatch_id)
                if message_id not in self._delivered:
                    self._pending_inputs.add(message_id)
                return message_id
            except asyncio.CancelledError:
                self._fail("Copilot message acceptance is uncertain")
                raise
            except Exception:
                # An interrupted ACK can hide accepted input. Never retry it
                # automatically or interpret a missing ACK as no side effect.
                self._fail("Copilot message acceptance is uncertain")
                raise self._failure from None

    async def steer(self, prompt: str) -> str:
        """Return the accepted input ID, not a claim of immediate delivery."""
        if not self._active or self._finishing:
            raise SessionSupervisorError("No active Copilot stream to steer")
        return await self._submit(prompt, immediate=True, stream_generation=self._stream_generation)

    async def _observe(self) -> SettlementObservation:
        backend = self._check()
        async with asyncio.timeout(self._rpc_timeout):
            native = await backend.snapshot()
        requests = self._pending_requests()
        if not isinstance(native, RuntimeSnapshot) or (
            native.processing is not None and type(native.processing) is not bool
        ) or type(native.native_shells_stopped) is not bool:
            raise ValueError("Invalid Copilot runtime snapshot")
        for identities in (native.pending_permissions, native.pending_messages, requests):
            if identities is not None and (
                not isinstance(identities, frozenset)
                or any(not isinstance(identity, str) or not identity for identity in identities)
            ):
                raise ValueError("Invalid Copilot pending request inventory")
        permissions = (native.pending_permissions | requests | self.requests.pending_ids
                       if native.pending_permissions is not None and requests is not None else None)
        # A question's host handler can return before its native reply is
        # acknowledged (or fail without replying). Keep the event request ID
        # pending until native completion; host callback join is not that proof.
        messages = (native.pending_messages | frozenset(self._pending_inputs) | frozenset(self._pending_user_inputs)
                    if native.pending_messages is not None else None)
        return SettlementObservation(
            processing=native.processing, tasks=native.tasks,
            pending_permissions=permissions, pending_messages=messages,
            pending_tools=self.callbacks.pending_ids,
            cancelled_tool_ids=self._cancelled_tools & self._open_tools,
            cancelled_permission_tool_ids=(self._permission_events.cancelled_tool_ids
                                          & self._open_tools - self._cancelled_tools),
            cancelled_native_shell_tool_ids=frozenset(
                self._native_shell_tools & self._open_tools
                - self._cancelled_tools - self._permission_events.cancelled_tool_ids
                if native.native_shells_stopped else frozenset()
            ),
        )

    async def _reconcile(self) -> None:
        if self._control_pending:
            return
        try:
            if self._interrupt_ticket is not None:
                checkpoint = self.coordinator.begin_interrupt_reconciliation(self._interrupt_ticket)
                if checkpoint is None:
                    return
                first = await self._observe()
                if not first.is_settled():
                    return
                async with asyncio.timeout(self._rpc_timeout):
                    barrier = await self._check().is_processing()
                second = await self._observe()
                events = self.coordinator.finish_interrupt_reconciliation(
                    checkpoint, first, second, processing_barrier=barrier,
                )
            else:
                checkpoint = self.coordinator.begin_reconciliation()
                if checkpoint is None:
                    return
                events = self.coordinator.finish_reconciliation(checkpoint, await self._observe())
            self._emit(events)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("Copilot settlement could not be established")

    async def stream(self, prompt: str) -> AsyncIterator[CommonEvent]:
        """One producer; consumer cancellation closes the owned runtime."""
        if self._active:
            raise SessionSupervisorError("A Copilot stream already owns this session")
        self._check()
        self.callbacks.resume_admissions()
        self.requests.resume_admissions()
        self._active = True
        self._stream_generation += 1
        self._finishing = False
        self._control_awaiting_settlement = False
        self._deadline = asyncio.get_running_loop().call_later(
            self._turn_timeout, self._expire_turn, self._stream_generation,
        )
        completed = False
        try:
            await self._submit(prompt, stream_generation=self._stream_generation)
            while True:
                self._check()
                while self._events:
                    self._check()
                    event = self._events.popleft()
                    if event.type == DONE:
                        completed = True
                        self._finishing = True
                    yield event
                    if completed:
                        return
                self._changed.clear()
                await self._reconcile()
                if self._events or self._failure:
                    continue
                # Runtime task retirement need not emit session.idle. The
                # bounded poll only initiates authoritative reconciliation.
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=0.1)
        finally:
            self._cancel_deadline()
            self._active = False
            if completed:
                self._control_awaiting_settlement = False
            if not completed:
                try:
                    await self.close()
                except SessionSupervisorError:
                    # Preserve the original stream failure; explicit close()
                    # still exposes any independently failed cleanup result.
                    if self._failure is None:
                        raise

    def _cancel_deadline(self) -> None:
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None

    def _expire_turn(self, stream_generation: int) -> None:
        if self._active and not self._finishing and stream_generation == self._stream_generation:
            self._fail("Copilot turn exceeded its deadline")

    async def _control(self, *, interrupt: bool) -> ControlAcknowledgement:
        if not self._active or self._finishing:
            raise SessionSupervisorError("No active Copilot stream to control")
        stream_generation = self._stream_generation
        async with self.coordinator.writer():
            if (not self._active or self._finishing
                    or self._stream_generation != stream_generation):
                raise SessionSupervisorError("No active Copilot stream to control")
            backend = self._check()
            if self._control_awaiting_settlement:
                raise SessionSupervisorError("Copilot control awaiting settlement; new control was not submitted")
            self._control_pending = True
            self.invalidate_observation()
            # Invalidate answers before the control RPC: a dashboard response
            # arriving while abort/interrupt is in flight must not approve work.
            self.requests.pause_admissions()
            try:
                if interrupt:
                    ticket = self.coordinator.request_interrupt()
                    async with asyncio.timeout(self._rpc_timeout):
                        accepted = await backend.interrupt()
                    if type(accepted) is not bool:
                        raise ValueError("Invalid Copilot interrupt acknowledgement")
                    self.coordinator.acknowledge_interrupt(ticket, accepted=accepted)
                    self._interrupt_ticket = ticket if accepted else None
                else:
                    ticket = self.coordinator.request_abort()
                    async with asyncio.timeout(self._rpc_timeout):
                        await backend.abort()
                    accepted = True
                    self.coordinator.acknowledge_abort(ticket, accepted=True)
                if accepted:
                    # A new submission would discard the coordinator's control
                    # ticket before it can settle cancelled, still-open tools.
                    self._control_awaiting_settlement = True
                    self.callbacks.pause_admissions()
                    self._cancelled_tools, _ = await asyncio.gather(
                        self.callbacks.cancel_all(timeout=self._rpc_timeout),
                        self.requests.cancel_all(timeout=self._rpc_timeout),
                    )
                else:
                    self.requests.resume_admissions()
                return ControlAcknowledgement(
                    accepted, not self.callbacks.pending_ids and not self.requests.pending_ids,
                )
            except asyncio.CancelledError:
                self._fail("Copilot control outcome is uncertain")
                raise
            except Exception:
                self._fail("Copilot control outcome is uncertain")
                raise self._failure from None
            finally:
                self._control_pending = False
                self.invalidate_observation()

    async def abort(self) -> ControlAcknowledgement:
        return await self._control(interrupt=False)

    async def interrupt(self) -> ControlAcknowledgement:
        return await self._control(interrupt=True)

    async def _close(self) -> None:
        """The injected runtime owner must itself bound process shutdown."""
        self._closed = True
        self.coordinator.transport_lost()
        self._changed.set()
        failed = False
        try:
            await asyncio.gather(
                self.callbacks.cancel_all(timeout=self._rpc_timeout),
                self.requests.cancel_all(timeout=self._rpc_timeout),
            )
            if self._backend is not None:
                async with asyncio.timeout(self._rpc_timeout):
                    await self._backend.disconnect()
        except Exception:
            failed = True
        finally:
            try:
                await self._close_runtime()
            except Exception:
                failed = True
        if failed or self.callbacks.pending_ids or self.requests.pending_ids:
            raise SessionSupervisorError("Copilot session cleanup is incomplete")

    def _begin_close(self) -> asyncio.Task:
        self._cancel_deadline()
        self.callbacks.close_admissions()
        self.requests.close_admissions()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(self._observe_close_result)
        return self._close_task

    @staticmethod
    def _observe_close_result(task: asyncio.Task) -> None:
        # A paused/abandoned consumer may never await background failure cleanup.
        # Retrieve its sanitized exception without logging or retaining context.
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        """Repeated caller cancellation cannot abandon the shared cleanup task."""
        task = self._begin_close()
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise SessionSupervisorError("Copilot session cleanup was cancelled") from None
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
