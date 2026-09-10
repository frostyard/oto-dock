"""Process-local, explicitly scoped credential lease with independent revocation.

The encrypted account store remains authoritative. This guard pins one exact
credential generation; replacement requires shutdown and a fresh runtime. It
is not a distributed writer lease or a provider token refresh implementation.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import math
import time

from core.layers.copilot.credentials import (
    CopilotAccountScope, CopilotCredential, CredentialUnavailableError,
)


class CopilotLeaseGuard:
    def __init__(
        self, credential: CopilotCredential, scope: CopilotAccountScope, *,
        read_credential: Callable[[str, CopilotAccountScope], Awaitable[CopilotCredential]],
        on_invalid: Callable[[], None], check_interval: float = 2, read_timeout: float = 5,
    ):
        if not isinstance(credential, CopilotCredential) or not isinstance(scope, CopilotAccountScope):
            raise ValueError("An explicit Copilot credential and payer scope are required")
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in (check_interval, read_timeout)) or check_interval > 60 or read_timeout > 30:
            raise ValueError("Bounded Copilot lease observation limits are required")
        self._credential = credential
        self._scope = scope
        self._read = read_credential
        self._on_invalid = on_invalid  # synchronous, non-raising owner callback
        self._interval = check_interval
        self._timeout = read_timeout
        self._lock = asyncio.Lock()
        self._started = False
        self._starting = False
        self._invalid = False
        self._closed = False
        self._watch_task = None
        self._close_task = None
        self._expiry = None
        self._reads: set[asyncio.Task] = set()
        self._cancel_requested: set[asyncio.Task] = set()

    @classmethod
    async def acquire(cls, account_id, scope, *, on_invalid, check_interval=2, read_timeout=5):
        """Acquire from encrypted storage off-loop; never select another payer.

        Cancelling a to_thread future cannot stop an already-running database
        query. Such queries are read-only and their results cannot authorize a
        closed guard; PostgreSQL/pool timeouts own the underlying thread bound.
        """
        if (type(scope) is not CopilotAccountScope or type(read_timeout) not in (int, float)
                or not math.isfinite(read_timeout) or not 0 < read_timeout <= 30):
            raise CredentialUnavailableError("Invalid Copilot account acquisition")

        async def read(selected_id, selected_scope):
            from storage.copilot_account_store import read_credential
            return await asyncio.to_thread(read_credential, selected_id, selected_scope)

        failed = False
        try:
            async with asyncio.timeout(read_timeout):
                credential = await read(account_id, scope)
        except Exception:
            failed = True
        if failed:
            raise CredentialUnavailableError("Copilot account acquisition is unavailable")
        if type(credential) is not CopilotCredential or credential.account_id != account_id:
            raise CredentialUnavailableError("Copilot account acquisition is unavailable")
        guard = cls(credential, scope, read_credential=read, on_invalid=on_invalid,
                    check_interval=check_interval, read_timeout=read_timeout)
        try:
            await guard.start()
        except BaseException:
            await guard.close()
            raise
        return guard

    @property
    def credential(self) -> CopilotCredential:
        if not self.valid:
            raise CredentialUnavailableError("Copilot credential lease is unavailable")
        return self._credential

    @property
    def scope(self) -> CopilotAccountScope:
        return self._scope

    @property
    def valid(self) -> bool:
        return self._started and not self._invalid and not self._closed

    def _available(self):
        return (self._started or self._starting) and not self._invalid and not self._closed

    def _invalidate(self):
        if not self._invalid and not self._closed:
            self._invalid = True
            self._on_invalid()

    async def start(self):
        if self._started or self._starting or self._invalid or self._closed:
            raise CredentialUnavailableError("Copilot credential lease cannot be restarted")
        self._starting = True
        if self._credential.expires_at is not None:
            self._expiry = asyncio.get_running_loop().call_later(
                max(0, self._credential.expires_at - time.time()), self._invalidate,
            )
        try:
            await self.authorize()
            if not self._available():
                raise CredentialUnavailableError("Copilot credential lease is unavailable")
            self._started = True
        finally:
            self._starting = False
        self._watch_task = asyncio.create_task(self._watch(), name="copilot-account-observer")

    def _read_finished(self, task):
        self._reads.discard(task)
        self._cancel_requested.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve discarded results/errors without logging secrets.

    def _cancel_read(self, task):
        if not task.done() and task not in self._cancel_requested:
            self._cancel_requested.add(task)
            task.cancel()

    async def authorize(self):
        """Revalidate inside the supervisor's writer lock before each submission."""
        if not self._available():
            raise CredentialUnavailableError("Copilot credential lease is unavailable")
        async with self._lock:
            failed = False
            try:
                if not self._available():
                    raise CredentialUnavailableError("Copilot credential lease is unavailable")
                self._credential.ensure_usable(time.time())
                task = asyncio.create_task(self._read(self._credential.account_id, self.scope))
                self._reads.add(task)
                task.add_done_callback(self._read_finished)
                try:
                    done, _ = await asyncio.wait({task}, timeout=self._timeout)
                    if not done:
                        raise CredentialUnavailableError("Copilot credential observation timed out")
                    current = task.result()
                finally:
                    self._cancel_read(task)
                if not self._available() or type(current) is not CopilotCredential or current != self._credential:
                    raise CredentialUnavailableError("Copilot credential generation changed")
                current.ensure_usable(time.time())
            except asyncio.CancelledError:
                self._invalidate()
                raise
            except Exception:
                failed = True
            if failed:
                self._invalidate()
                # Neither a database error nor a raw credential-bearing SDK
                # exception becomes part of the caller-facing exception chain.
                raise CredentialUnavailableError("Copilot credential lease is unavailable")

    async def _watch(self):
        try:
            while self.valid:
                await asyncio.sleep(self._interval)
                await self.authorize()
        except (CredentialUnavailableError, asyncio.CancelledError):
            return

    async def _close(self):
        self._closed = True
        if self._expiry is not None:
            self._expiry.cancel()
        task = self._watch_task
        if task is not None:
            task.cancel()
        for read in tuple(self._reads):
            self._cancel_read(read)
        tasks = self._reads | ({task} if task is not None else set())
        pending = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._timeout)
        self._credential = None
        self._read = None
        self._on_invalid = None
        if pending:
            raise CredentialUnavailableError("Copilot credential lease cleanup is incomplete")

    async def close(self):
        if self._close_task is None:
            self._closed = True  # Close admission before the cleanup task is scheduled.
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None,
            )
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                if self._close_task.cancelled():
                    raise CredentialUnavailableError("Copilot credential lease cleanup failed") from None
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
