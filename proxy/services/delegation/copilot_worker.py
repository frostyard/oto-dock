"""Owned, local existing-engine workers for the Copilot delegation bridge.

No Copilot account or token enters this adapter. The existing task builder
selects the child's own user/platform subscription according to worker scope.
The owner must be retained when close reports incomplete cleanup.
"""
from __future__ import annotations

import asyncio
import math
import uuid

import config
from auth.providers import UserContext
from core.layers.copilot.host_tools import valid_delegate_args
from services.delegation.spawn_authz import authorize_spawn
from services.scheduler import scheduler
from storage import agent_store, database, remote_store

_AUTOMATIONS = frozenset({"delegation-mcp", "schedules-mcp", "triggers-mcp", "meetings-mcp"})
_CLEANUP_WAIT = 10.0
# Serializes Copilot admission through creation of the counted run row. Generic
# delegation callers retain their existing count-then-create semantics.
_ADMISSION_LOCK = asyncio.Lock()


class CopilotWorkerError(RuntimeError):
    """Sanitized refusal or unproven worker cleanup."""


def _observe(task):
    if not task.cancelled():
        task.exception()


class OwnedCopilotWorker:
    def __init__(self, user: UserContext, source_agent: str, target_agent: str,
                 name: str, prompt: str, parent_valid, authorize_parent, publish, tool_call_id: str,
                 *, timeout_seconds: float = 120, outcome_observer=None):
        if (type(user) is not UserContext or user.is_api_key or user.session_id
                or user.agent or user.is_external or not user.sub
                or user.sub == "api-key" or user.sub.startswith("session:")
                or not config.is_safe_agent_name(source_agent)
                or not config.is_safe_agent_name(target_agent)
                or not valid_delegate_args({"agent": target_agent, "name": name, "prompt": prompt}, (target_agent,))
                or type(tool_call_id) is not str or not tool_call_id or len(tool_call_id) > 256
                or tool_call_id != tool_call_id.strip() or not tool_call_id.isprintable()
                or not callable(parent_valid) or not callable(authorize_parent) or not callable(publish)
                or (outcome_observer is not None and not callable(outcome_observer))
                or type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120):
            raise CopilotWorkerError("Copilot worker configuration is unavailable")
        self._user_sub = user.sub
        self.source_agent, self.target_agent = source_agent, target_agent
        self.name, self._prompt = name, prompt
        self.tool_call_id = tool_call_id
        self._parent_valid, self._authorize_parent, self._publish = parent_valid, authorize_parent, publish
        self._timeout = timeout_seconds
        self._outcome_observer = outcome_observer
        self._outcome = None
        self.task_id = f"dyn-{uuid.uuid4().hex}"
        self.run_id = f"run-{uuid.uuid4().hex[:12]}"
        self.session_id = str(uuid.uuid4())
        self.chat_id = f"task-{self.run_id}"
        self._allocation = (self.task_id, self.run_id, self.session_id, self.chat_id)
        self._claimed = False
        self._execution_created = False
        self._driver = self._startup = self._runner = self._builder = None
        self._watcher = None
        self._producer = self._pump = self._layer = self._config = None
        self._native = None
        self._process = None
        self._native_readers = ()
        self._closing = None
        self._engine_started = False
        self._subscription_bound = False
        self._facts = None
        self._definition = None

    @property
    def closed(self) -> bool:
        return (self._closing is not None and self._closing.done()
                and not self._closing.cancelled() and self._closing.exception() is None)

    def allocation(self) -> dict:
        """Immutable allocation identity, copied for durable pre-dispatch binding."""
        return dict(zip(("task_id", "run_id", "session_id", "chat_id"), self._allocation, strict=True))

    def _check(self):
        try:
            valid = self._closing is None and self._parent_valid() is True
        except Exception:
            valid = False
        if not valid:
            raise CopilotWorkerError("Copilot worker authority is unavailable")

    def _read_authority(self):
        from core import execution_mode
        from core.config.task_config_builder import resolve_task_identity
        from services.mcp import mcp_registry

        row = database.get_user(self._user_sub)
        if not row:
            raise ValueError()
        roles = database.get_user_agent_roles(self._user_sub)
        user = UserContext(sub=row["sub"], email=row["email"], name=row["name"],
                           role=row["role"], agents=list(roles), agent_roles=roles,
                           agent=self.source_agent)
        if not user.is_admin and self.source_agent not in roles:
            raise ValueError()
        authz = authorize_spawn(user, target_agent=self.target_agent,
                                requested_scope="user", surface="task",
                                reserved_run_id=self.run_id if self._execution_created else None)
        agent = agent_store.get_agent(self.target_agent)
        engine = agent.get("execution_path") or "claude-code-cli"
        if engine not in {"claude-code-cli", "codex-cli"}:
            raise ValueError()
        if execution_mode.is_interactive(agent_default=agent.get("default_execution_mode") or ""):
            raise ValueError()
        identity = resolve_task_identity(self.target_agent, authz.scope, authz.created_by)
        target, _ = remote_store.resolve_execution_target(self.target_agent, identity.creds_user_sub, identity.role)
        if target != "local":
            raise ValueError()
        if _AUTOMATIONS.intersection(m.name for m in mcp_registry.get_agent_mcps(self.target_agent)):
            raise ValueError()
        model = config.get_cli_model(self.target_agent, layer=engine)
        if not isinstance(model, str) or not model:
            raise ValueError()
        return (authz, engine, model, identity)

    async def _authorize(self):
        self._check()
        await self._authorize_parent()
        self._check()
        try:
            facts = await asyncio.to_thread(self._read_authority)
        except Exception:
            raise CopilotWorkerError("Copilot worker target is unavailable") from None
        self._check()
        if self._facts is not None and facts != self._facts:
            raise CopilotWorkerError("Copilot worker configuration changed")
        self._facts = facts
        return facts

    async def run(self) -> dict:
        if self._driver is not None or self._closing is not None:
            raise CopilotWorkerError("Copilot worker has already been used")
        self._driver = asyncio.create_task(self._run())
        self._driver.add_done_callback(_observe)
        try:
            async with asyncio.timeout(self._timeout):
                await asyncio.shield(self._driver)
        except asyncio.CancelledError:
            await self.close()
            if asyncio.current_task().cancelling():
                raise
            raise CopilotWorkerError("Copilot worker authority is unavailable") from None
        except Exception:
            await self.close()
            raise CopilotWorkerError("Copilot worker did not complete") from None
        await self.close()
        return dict(self._outcome)

    async def _run(self):
        self._startup = asyncio.create_task(self._launch())
        self._startup.add_done_callback(_observe)
        await asyncio.shield(self._startup)
        self._watcher = asyncio.create_task(self._watch_authority())
        self._watcher.add_done_callback(_observe)
        if self._runner is not None:
            # A user may stop a queued scheduler run. That runner propagates
            # cancellation after stamping its terminal row; return the stored
            # cancelled result while this parent still has authority. Cancelling
            # this driver itself still propagates through the shield.
            await asyncio.shield(asyncio.gather(self._runner, return_exceptions=True))
        await self._authorize()
        row = await asyncio.to_thread(database.get_run, self.run_id)
        self._check()
        return self._result(row)

    def _result(self, row):
        if row is None:
            if self._execution_created:
                raise CopilotWorkerError("Copilot worker result is unavailable")
            return {"task_id": self.task_id, "run_id": self.run_id, "chat_id": self.chat_id,
                    "agent": self.target_agent, "status": "failed", "name": self.name,
                    "tool_id": self.tool_call_id, "output": "Worker was not started.",
                    "execution_created": False}
        if (row.get("id") != self.run_id or row.get("task_id") != self.task_id
                or row.get("agent") != self.target_agent or row.get("created_by") != self._user_sub
                or row.get("task_type") != "delegate"
                or row.get("session_id", self.session_id) not in (None, self.session_id)
                or row.get("chat_id") not in (None, "", self.chat_id)):
            raise CopilotWorkerError("Copilot worker result is unavailable")
        status = row.get("status")
        if status not in {"completed", "failed", "cancelled", "limit_exceeded"}:
            raise CopilotWorkerError("Copilot worker result is unavailable")
        output = row.get("output_text") if status == "completed" else "Worker did not complete successfully."
        if type(output) is not str:
            output = ""
        return {"task_id": self.task_id, "run_id": self.run_id, "chat_id": self.chat_id,
                "agent": self.target_agent, "status": status, "name": self.name, "tool_id": self.tool_call_id,
                "output": output.replace("\0", "").encode("utf-8")[-16384:].decode("utf-8", errors="ignore"),
                "execution_created": True}

    async def _launch(self):
        async with _ADMISSION_LOCK:
            authz, engine, model, _ = await self._authorize()
            self._definition = scheduler.TaskDefinition(
                id=self.task_id, name=self.name, agent=self.target_agent, prompt=self._prompt,
                scope=authz.scope, created_by=authz.created_by, task_type="delegate",
                timeout_seconds=max(1, math.ceil(self._timeout)), notification_mode="none",
                override_execution_path=engine, override_model=model,
            )
            await scheduler._execute_task(
                self._definition, trigger_type="manual", trigger_source=self.source_agent, owned_worker=self,
            )

    async def _watch_authority(self):
        try:
            while True:
                await asyncio.sleep(0.5)
                async with asyncio.timeout(5):
                    await self._authorize()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._begin_close()

    # Scheduler hooks, captured synchronously before each resource can escape.
    def capture_ids(self, run_id, session_id, chat_id):
        self._check()
        if (self._claimed or (self.task_id, run_id, session_id, chat_id) != self._allocation
                or (self.task_id, self.run_id, self.session_id, self.chat_id) != self._allocation):
            raise CopilotWorkerError("Copilot worker allocation is unavailable")
        from core.session.worker_ownership import claim_worker
        claim_worker(session_id, self)
        self._claimed = True

    def run_created(self):
        if not self._claimed or self._execution_created:
            raise CopilotWorkerError("Copilot worker allocation is unavailable")
        self._execution_created = True

    async def rows_created(self):
        self._check()
        await self._publish({"type": "delegate_spawn", "task_id": self.task_id,
                             "run_id": self.run_id, "chat_id": self.chat_id,
                             "agent": self.target_agent, "task_name": self.name, "surface": "task",
                             "name": self.name, "tool_id": self.tool_call_id})
        self._check()

    def capture_runner(self, task):
        self._runner = task
        task.add_done_callback(_observe)

    async def before_config(self):
        from core.session.session_manager import get_execution_layer
        _, engine, _, identity = await self._authorize()
        self.capture_layer(get_execution_layer(
            self.target_agent, execution_path=engine, execution_target="local",
            user_sub=identity.creds_user_sub, role=identity.role,
        ), self.session_id)

    def capture_layer(self, layer, session_id):
        if session_id != self.session_id or (self._layer is not None and self._layer is not layer):
            raise CopilotWorkerError("Copilot worker ownership changed")
        self._layer = layer

    async def build_config(self, factory):
        self._check()
        self._builder = asyncio.create_task(factory())
        self._builder.add_done_callback(_observe)
        self._config = await asyncio.shield(self._builder)
        return self._config

    async def before_start(self, configuration):
        _, engine, model, identity = await self._authorize()
        if (configuration is not self._config or configuration.execution_target != "local"
                or configuration.execution_path != engine or configuration.model != model
                or configuration.interactive or configuration.resume
                or configuration.subscription_user_sub != (identity.creds_user_sub or "")):
            raise CopilotWorkerError("Copilot worker configuration changed")
        self._check()

    async def start_engine(self, layer, session_id, configuration):
        from core.session.worker_ownership import session_capture
        from services.engines.subscription_pool import get_session_subscription
        marker = session_capture.set(self._capture_native)
        try:
            await layer.start_session(session_id, configuration)
            self._engine_started = True
            if self._native is None:
                raise CopilotWorkerError("Copilot worker ownership is unavailable")
        finally:
            if self._native is not None:
                client = getattr(self._native, "_client", None)
                self._process = getattr(self._native, "proc", None) if client is None else client.proc
                reader = getattr(client, "_reader_task", None)
                self._native_readers = (reader,) if isinstance(reader, asyncio.Task) else ()
            # Native startup precedes subscription binding in both existing
            # engines. An attempted/failed start has not necessarily taken the
            # seat acquired by the builder. Snapshot before runner cleanup can
            # release the binding and erase evidence of the transfer.
            self._subscription_bound = bool(configuration.subscription_id) and (
                get_session_subscription(session_id) == configuration.subscription_id
            )
            session_capture.reset(marker)

    def _capture_native(self, native):
        if (getattr(native, "session_id", None) != self.session_id
                or (self._native is not None and self._native is not native)):
            raise CopilotWorkerError("Copilot worker ownership changed")
        self._native = native
        self._check()

    def capture_producer(self, producer):
        self._producer = producer

    def capture_pump(self, pump):
        self._pump = pump

    def _begin_close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish_close())
            self._closing.add_done_callback(_observe)

    async def close(self):
        self._begin_close()
        cancelled = False
        deadline = asyncio.get_running_loop().time() + _CLEANUP_WAIT
        while not self._closing.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CopilotWorkerError("Copilot worker cleanup is incomplete")
            try:
                await asyncio.wait({self._closing}, timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
        if self._closing.cancelled() or self._closing.exception() is not None:
            raise CopilotWorkerError("Copilot worker cleanup is incomplete") from None
        if cancelled:
            raise asyncio.CancelledError()

    async def _finish_close(self):
        # Do not cancel startup's database threads or the config builder: their
        # late return must stay owned. Admission hooks refuse after close begins.
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        if self._driver is not None and not self._driver.done():
            self._driver.cancel()
        if self._driver is not None:
            await asyncio.gather(self._driver, return_exceptions=True)
        if self._startup is not None:
            await asyncio.gather(self._startup, return_exceptions=True)
        if self._pump is not None and not self._pump.is_done:
            self._pump.abort()
        if self._runner is not None and not self._runner.done():
            self._runner.cancel()
        if self._runner is not None:
            await asyncio.gather(self._runner, return_exceptions=True)
        if self._builder is not None:
            values = await asyncio.gather(self._builder, return_exceptions=True)
            if not isinstance(values[0], BaseException):
                self._config = values[0]
        layer_failed = False
        if self._layer is not None:
            try:
                await self._layer.close_session(self.session_id)
            except Exception:
                # Ancillary engine cleanup failure must not skip closing the
                # exact native owner that survived a failed pool startup.
                layer_failed = True
        if self._native is not None:
            # A failed start may already have removed the pool entry; retain
            # and close the exact constructor-owned object independently.
            await self._native.close()
            proc = self._process
            if proc is not None and proc.returncode is None:
                async with asyncio.timeout(5):
                    await proc.wait()
            if (proc is not None and proc.returncode is None) or (self._engine_started and proc is None):
                raise CopilotWorkerError("Copilot worker cleanup is incomplete")
            if self._native_readers:
                await asyncio.gather(*self._native_readers, return_exceptions=True)
        elif self._engine_started:
            raise CopilotWorkerError("Copilot worker cleanup is incomplete")
        if self._producer is not None:
            if not self._producer.done():
                self._producer.cancel()
            await asyncio.gather(self._producer, return_exceptions=True)
        if self._pump is not None and self._pump._task is not None:
            await asyncio.gather(self._pump._task, return_exceptions=True)
        if layer_failed:
            raise CopilotWorkerError("Copilot worker cleanup is incomplete")
        if not self._subscription_bound and self._config is not None and self._config.subscription_id:
            from storage import subscription_store
            await asyncio.to_thread(subscription_store.decrement_active_sessions, self._config.subscription_id)
        row = None
        if self._claimed:
            row = await asyncio.to_thread(database.get_run, self.run_id)
            # Validate binding before mutating any pending row. A missing row
            # is 'not started' only when creation was never observed.
            if row and row.get("status") in {"pending", "running"}:
                self._result({**row, "status": "failed"})
                await asyncio.to_thread(database.update_run, self.run_id, status="failed",
                                        error_message="Owned worker stopped before completion",
                                        completed_at=scheduler.now_iso())
                row = await asyncio.to_thread(database.get_run, self.run_id)
        result = self._result(row)
        if self._outcome_observer is not None:
            # This persistence authority is the immutable admission receipt,
            # independent of a disconnected/revoked parent or its SSE queue.
            # A failed write keeps the ownership claim, even after native
            # resources have joined; no fresh execution may replace it.
            await self._outcome_observer(dict(result))
        self._outcome = result
        if self._claimed:
            from core.session import session_state
            session_state._sessions.pop(self.session_id, None)
            session_state._save_sessions()
        if scheduler._active_task_ids.get(self.task_id) == self.run_id:
            scheduler._active_task_ids.pop(self.task_id, None)
        from core.session.worker_ownership import release_worker
        release_worker(self.session_id, self)
        self._config = None
        self._prompt = ""
