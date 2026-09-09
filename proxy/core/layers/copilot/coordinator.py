"""In-process Copilot event and settlement coordination, independent of the SDK.

The owner routes the serialized runtime stream through ``receive_event`` in
order, then captures ``begin_reconciliation`` before awaiting task, processing,
and pending-request RPCs. ``finish_reconciliation`` rejects stale/incomplete
observations and delegates DONE to the translator's existing completion gates.
Never call translator.settle_idle separately when using this coordinator.

This primitive does not fetch snapshots, resolve permissions, cancel tools,
supervise processes or persist ownership. It runs on one asyncio event loop;
its writer lock is neither a cross-process lease nor crash recovery. A stream
gap/transport loss requires rebuilding and reconciling the session explicitly.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import AsyncIterator

from core.events.common_events import CommonEvent, DONE
from core.layers.copilot.translator import CopilotEventTranslator


class TaskState(Enum):
    RUNNING = "running"
    IDLE = "idle"  # A waiting background agent can resume; this is not complete.
    ORPHANED = "orphaned"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskObservation:
    task_id: str
    state: TaskState


@dataclass(frozen=True)
class SettlementObservation:
    """Authoritative snapshots fetched after capturing the checkpoint.

    None means unknown/unavailable, never empty. Supply all native agent, shell
    and client tasks. Pending permissions must include unresolved host callback
    futures as well as native prompts. Pending tools must include host callbacks
    even when absent from native tasks.list. ``processing`` includes continuations.
    Pending messages includes host queued/dispatching submissions. Cancelled tool
    IDs are affirmative proof that the host cancelled and joined those callbacks
    and the runtime no longer owns pending execution; never infer them from ACK.
    History preservation is separate evidence needed to classify graceful abort;
    an empty task list or an accepted abort RPC does not establish it.
    """

    processing: bool | None
    tasks: tuple[TaskObservation, ...] | None
    pending_permissions: frozenset[str] | None
    pending_tools: frozenset[str] | None
    history_preserved: bool | None = None
    pending_messages: frozenset[str] | None = None
    cancelled_tool_ids: frozenset[str] = frozenset()

    def is_settled(self) -> bool:
        if (self.processing is not False or not isinstance(self.tasks, tuple)
                or self.pending_permissions != frozenset()
                or self.pending_tools != frozenset()
                or self.pending_messages != frozenset()):
            return False
        terminal = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
        seen = set()
        for task in self.tasks:
            if (not isinstance(task, TaskObservation)
                    or not isinstance(task.task_id, str) or not task.task_id
                    or task.task_id in seen or task.state not in terminal):
                return False
            seen.add(task.task_id)
        return True


@dataclass(frozen=True)
class IdleCheckpoint:
    revision: int
    event_id: str


class AbortState(Enum):
    NONE = "none"
    REQUESTED = "requested"
    ACKNOWLEDGED = "acknowledged"
    GRACEFUL = "graceful"
    SETTLED_UNVERIFIED = "settled_unverified"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AbortTicket:
    turn_id: str
    request_number: int


class EventSequenceError(RuntimeError):
    """The event stream cannot establish safe contiguous session state."""


def _event_digest(event: dict) -> bytes:
    """Hash canonical strict JSON without retaining raw replay payloads."""
    def validate(value) -> None:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError
            for item in value.values():
                validate(item)
        elif isinstance(value, list):
            for item in value:
                validate(item)
        elif type(value) not in (str, int, float, bool, type(None)):
            raise ValueError

    try:
        validate(event)
        serialized = json.dumps(event, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        # Serializer errors can include input values; expose only the category.
        raise ValueError("Copilot event must contain valid serialized JSON") from None
    return hashlib.sha256(serialized.encode("utf-8")).digest()


class CopilotTurnCoordinator:
    """One stream consumer plus a lock for outbound send/control ownership."""

    def __init__(self) -> None:
        self._translator = CopilotEventTranslator()
        self._sequence = 0
        self._sequence_ids: dict[int, str] = {}
        self._event_digests: dict[str, bytes] = {}
        self._revision = 0
        self._stream_intact = True
        self._turn_id: str | None = None
        self._turn_open = False
        self._idle_aborted = False
        self._idle_abort_ticket: AbortTicket | None = None
        self._abort_ticket: AbortTicket | None = None
        self._abort_count = 0
        self._abort_state = AbortState.NONE
        self._writer_lock = asyncio.Lock()
        self._writer_owner: asyncio.Task | None = None

    @property
    def abort_state(self) -> AbortState:
        return self._abort_state

    @property
    def last_sequence(self) -> int:
        return self._sequence

    @asynccontextmanager
    async def writer(self) -> AsyncIterator[None]:
        """Serialize mutation dispatch/ACK; never hold this for a whole turn.

        Release before consuming a response stream or calling send_and_wait;
        otherwise abort/steer would wait behind the turn they must interrupt.
        Inbound event handling and callback resolution deliberately do not take
        this lock, so approvals cannot deadlock behind a blocked send.
        Reentrant use is an error rather than a silent wait on oneself.
        """
        owner = asyncio.current_task()
        if self._writer_owner is owner:
            raise RuntimeError("Copilot writer lock is not reentrant")
        async with self._writer_lock:
            self._writer_owner = owner
            try:
                yield
            finally:
                self._writer_owner = None

    def transport_lost(self) -> None:
        """Invalidate every pending snapshot; never infer process death."""
        self._stream_intact = False
        self._revision += 1

    def invalidate_observation(self) -> None:
        """Call before host callback, approval, or queued-send state changes.

        Host mutations need this even without a corresponding runtime event.
        Later snapshots must include the new host state in pending_tools,
        pending_permissions and pending_messages. This only invalidates prior
        snapshots; it does not claim the new operation completed.
        """
        self._revision += 1

    def receive_event(self, sequence: int, event: dict) -> list[CommonEvent]:
        """Consume contiguous router sequence numbers, starting at one.

        Re-delivery of the same ID and payload at its old sequence is harmless.
        An identical duplicate at the next sequence advances the cursor without
        replaying output. Changed-payload duplicates poison the stream, including
        at old sequences; canonical digests ignore dictionary key order only.
        Gaps or conflicting old sequences poison settlement until recovery;
        they must not be concealed by assigning fresh sequence numbers remotely.
        """
        if not self._stream_intact:
            raise EventSequenceError("Copilot stream requires explicit recovery")
        event_id = event.get("id") if isinstance(event, dict) else None
        if (not isinstance(event_id, str) or not event_id
                or not isinstance(event.get("type"), str) or not event["type"]
                or not isinstance(event.get("data"), dict)):
            self.transport_lost()
            raise ValueError("Copilot event requires nonempty ID/type and object data")
        try:
            digest = _event_digest(event)
        except ValueError:
            self.transport_lost()
            raise
        prior_digest = self._event_digests.get(event_id)
        if prior_digest is not None and prior_digest != digest:
            self.transport_lost()
            raise EventSequenceError("Copilot event ID was reused with a changed payload")
        if (type(sequence) is not int or sequence < 1
                or (sequence <= self._sequence and self._sequence_ids.get(sequence) != event_id)
                or sequence > self._sequence + 1):
            self.transport_lost()
            raise EventSequenceError("Copilot stream has a gap or conflicting sequence")
        if sequence <= self._sequence:
            return []
        duplicate = prior_digest is not None
        try:
            events = self._translator.translate(event)
        except (ValueError, TypeError):
            self.transport_lost()
            raise
        self._sequence = sequence
        self._sequence_ids[sequence] = event_id
        self._event_digests[event_id] = digest
        if duplicate:
            return events
        self._revision += 1
        data = event["data"]
        main = not (event.get("agentId") or data.get("parentToolCallId"))
        if main and event["type"] == "assistant.turn_start" and events:
            turn_id = data["turnId"]
            # Native turn IDs identify model iterations, not Oto submissions.
            # A tool continuation must not reset a pending abort ticket.
            if not self._turn_open:
                self._turn_id = turn_id
                self._turn_open = True
                self._abort_ticket = None
                self._abort_state = AbortState.NONE
        if main and event["type"] == "session.idle":
            self._idle_aborted = data.get("aborted") is True
            self._idle_abort_ticket = self._abort_ticket
        return events

    def begin_reconciliation(self) -> IdleCheckpoint | None:
        """Capture before fetching snapshots; do not reuse earlier RPC results."""
        idle = self._translator.pending_idle_id
        if not self._stream_intact or idle is None:
            return None
        return IdleCheckpoint(self._revision, idle)

    def finish_reconciliation(
        self, checkpoint: IdleCheckpoint, observation: SettlementObservation,
    ) -> list[CommonEvent]:
        """Atomically validate observations and emit at most one DONE.

        Fresh background/permission/child activity invalidates the checkpoint.
        The translator currently requires another genuine idle after such
        activity; this method never manufactures or rearms a runtime idle.
        """
        if (not isinstance(checkpoint, IdleCheckpoint)
                or not isinstance(observation, SettlementObservation)
                or not self._stream_intact or checkpoint != self.begin_reconciliation()
                or not observation.is_settled()):
            return []
        if observation.cancelled_tool_ids:
            if (self._abort_ticket is None or self._abort_state == AbortState.REJECTED
                    or not self._idle_aborted or self._idle_abort_ticket != self._abort_ticket):
                return []
        events = self._translator.reconcile_cancelled_tools(observation.cancelled_tool_ids)
        events.extend(self._translator.settle_idle(checkpoint.event_id, background_settled=True))
        completed = any(event.type == DONE for event in events)
        if completed:
            self._turn_open = False
        if completed and self._abort_ticket is not None:
            if self._abort_state != AbortState.REJECTED:
                self._abort_state = (
                    AbortState.GRACEFUL if self._idle_aborted
                    and self._idle_abort_ticket == self._abort_ticket
                    and observation.history_preserved is True
                    else AbortState.SETTLED_UNVERIFIED
                )
        return events

    def request_abort(self) -> AbortTicket:
        """Record intent before invoking the runtime; this does not abort it."""
        if not self._stream_intact or not self._turn_open or self._turn_id is None:
            raise RuntimeError("No known live Copilot turn to interrupt")
        self._abort_count += 1
        self._abort_ticket = AbortTicket(self._turn_id, self._abort_count)
        self._abort_state = AbortState.REQUESTED
        self._revision += 1
        return self._abort_ticket

    def acknowledge_abort(self, ticket: AbortTicket, *, accepted: bool) -> bool:
        """Ignore stale acknowledgements; accepted never means graceful."""
        if (not self._stream_intact or ticket != self._abort_ticket
                or self._abort_state != AbortState.REQUESTED):
            return False
        self._abort_state = AbortState.ACKNOWLEDGED if accepted is True else AbortState.REJECTED
        self._revision += 1
        return True
