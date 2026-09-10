"""Owned attached-shell settlement for a guarded native-only SDK session.

Use only with the pinned sandbox, the native tool policy and verified same-profile
history. This is not a process inventory: detached shells, arbitrary daemonized
command descendants, subagents and shell reuse are not qualified by this adapter.
The runtime owner remains responsible for process-tree shutdown on any failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import math

from core.layers.copilot.coordinator import TaskObservation, TaskState
from core.layers.copilot.permissions import _text
from core.layers.copilot.sdk_session import CopilotSdkSession, task_observations

_TERMINAL = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.RETIRED})


class NativeShellError(RuntimeError):
    """Sanitized shell ownership failure; close the owned runtime, never retry work."""


@dataclass(frozen=True)
class _Shell:
    fingerprint: bytes
    state: TaskState


class CopilotNativeShellSession(CopilotSdkSession):
    """Fresh adapter per runtime/session; never reconstruct ownership from text.

    Native task IDs are scoped by the bound SDK session. Cancellation is invoked
    only after an accepted supervisor control, not on behalf of model tool input.
    The supervisor must pause policy admissions before calling abort/interrupt.
    Model-facing read/list/stop tools remain disabled in the native policy.
    """

    def __init__(self, session, *, processes_settled, rpc_timeout: float = 5, cancel_timeout: float = 5,
                 maximum_shells: int = 256):
        if (not _text(getattr(session, "session_id", None), 256)
                or not callable(processes_settled)
                or type(maximum_shells) is not int or maximum_shells < 1
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (rpc_timeout, cancel_timeout))):
            raise ValueError("Explicit bounded native shell session ownership is required")
        super().__init__(session, rpc_timeout=rpc_timeout)
        self._session_id = session.session_id
        self._cancel_timeout = cancel_timeout
        self._maximum_shells = maximum_shells
        self._processes_settled = processes_settled
        self._shells: dict[str, _Shell] = {}
        self._missing_shells: set[str] = set()
        self._snapshot_lock = asyncio.Lock()
        self._failed = False
        self._controlled = False

    def _check_identity(self):
        if self._failed or self.session.session_id != self._session_id:
            self._failed = True
            raise NativeShellError("Copilot native shell ownership is unavailable")

    def _observe_tasks(self, tasks):
        observations = task_observations(tasks)
        updated = dict(self._shells)
        present = set()
        for task, observation in zip(tasks, observations, strict=True):
            # Session tasks contain no creation cwd or tool-call provenance.
            # Only the same guarded native-only profile supplies that boundary.
            if (getattr(task, "type", None) != "shell"
                    or getattr(getattr(task, "attachment_mode", None), "value",
                               getattr(task, "attachment_mode", None)) != "attached"
                    or not _text(getattr(task, "command", None), 32768)
                    or not isinstance(getattr(task, "started_at", None), datetime)):
                raise NativeShellError("Copilot native shell task profile is unsupported")
            mode = getattr(task, "execution_mode", None)
            if getattr(mode, "value", mode) not in {"sync", "background"}:
                raise NativeShellError("Copilot native shell execution mode is unknown")
            fingerprint = hashlib.sha256(
                (task.started_at.isoformat() + "\x00" + task.command).encode("utf-8")
            ).digest()
            previous = updated.get(observation.task_id)
            if previous is not None and (
                previous.fingerprint != fingerprint
                or (previous.state in _TERMINAL and previous.state != observation.state)
            ):
                raise NativeShellError("Copilot native shell task identity changed")
            updated[observation.task_id] = _Shell(fingerprint, observation.state)
            present.add(observation.task_id)
        if len(updated) > self._maximum_shells:
            raise NativeShellError("Copilot native shell ownership exceeded its bound")
        # A missing running task is uncertainty, not evidence of completion.
        # Keep terminal tombstones to reject ID reuse across subsequent turns.
        for identity, shell in tuple(updated.items()):
            if identity not in present and shell.state not in _TERMINAL:
                updated[identity] = _Shell(shell.fingerprint, TaskState.UNKNOWN)
        self._shells = updated
        self._missing_shells = set(updated) - present
        return tuple(TaskObservation(identity, shell.state) for identity, shell in updated.items())

    async def snapshot(self):
        self._check_identity()
        try:
            async with self._snapshot_lock:
                self._check_identity()
                async with asyncio.timeout(self.rpc_timeout):
                    await self.session.rpc.tasks.refresh(timeout=self.rpc_timeout)
                    snapshot = await super().snapshot()
                self._check_identity()
                processes = self._processes_settled()
                if processes is not None and type(processes) is not bool:
                    raise NativeShellError("Invalid Copilot owned process inventory")
                if processes is not True:
                    snapshot = replace(snapshot, tasks=(*snapshot.tasks, TaskObservation(
                        "otodock:runtime-processes", TaskState.UNKNOWN,
                    )))
                else:
                    # Sync native tasks disappear on natural exit. Only a
                    # separate owned-process fence can retire a missing task;
                    # retirement is neither successful execution nor an ACK.
                    for identity in self._missing_shells:
                        shell = self._shells[identity]
                        if shell.state not in _TERMINAL:
                            self._shells[identity] = _Shell(shell.fingerprint, TaskState.RETIRED)
                    snapshot = replace(snapshot, tasks=tuple(
                        TaskObservation(identity, shell.state) for identity, shell in self._shells.items()
                    ))
                    if self._controlled and all(task.state in _TERMINAL for task in snapshot.tasks):
                        snapshot = replace(snapshot, native_shells_stopped=True)
                return snapshot
        except asyncio.CancelledError:
            self._failed = True
            raise
        except Exception:
            self._failed = True
        raise NativeShellError("Copilot native shell inventory could not be established")

    async def _cancel_shells(self):
        """Cancel exact observed IDs, then poll terminal inventory; ACK is not join."""
        self._check_identity()
        try:
            from copilot.rpc import TasksCancelRequest

            async with asyncio.timeout(self._cancel_timeout):
                attempted = set()
                while True:
                    snapshot = await self.snapshot()
                    active = [task.task_id for task in snapshot.tasks if task.state not in _TERMINAL]
                    if not active:
                        return
                    for identity in active:
                        if identity in attempted or identity not in self._shells:
                            continue
                        self._check_identity()
                        result = await self.session.rpc.tasks.cancel(
                            TasksCancelRequest(id=identity), timeout=self.rpc_timeout,
                        )
                        if type(result.cancelled) is not bool:
                            raise NativeShellError("Invalid Copilot shell cancellation acknowledgement")
                        # False can mean natural completion won the race. Neither
                        # result permits forgetting the task before a fresh read.
                        attempted.add(identity)
                    await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self._failed = True
            raise
        except Exception:
            self._failed = True
        raise NativeShellError("Copilot native shell cancellation could not be established")

    async def _control(self, *, interrupt):
        self._check_identity()
        try:
            await self.snapshot()
            async with asyncio.timeout(self.rpc_timeout):
                if interrupt:
                    accepted = await super().interrupt()
                else:
                    await super().abort()
                    accepted = True
            if accepted:
                await self._cancel_shells()
                self._controlled = True
            return accepted
        except asyncio.CancelledError:
            self._failed = True
            raise
        except Exception:
            self._failed = True
        raise NativeShellError("Copilot native shell control outcome is uncertain")

    async def abort(self):
        await self._control(interrupt=False)

    async def interrupt(self):
        return await self._control(interrupt=True)

    async def send(self, prompt: str, *, immediate: bool = False):
        self._check_identity()
        self._controlled = False
        return await super().send(prompt, immediate=immediate)
