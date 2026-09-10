"""Application-owned personal Copilot preview; no chat rows or engine routing."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import uuid

import config
from core.layers.copilot.reasoning import valid_reasoning_effort
from core.layers.copilot.usage import validate_usage_frame
from auth.providers import UserContext
from core.concurrency import acquire_chat_slot, release_chat_slot
from core.config.copilot_config_builder import authorize_copilot_history, build_copilot_agent_config
from core.layers.copilot.credentials import CopilotAccountScope
from core.layers.copilot.native_tool_policy import SUPPORTED_NATIVE_TOOLS
from core.session import session_state as state


class CopilotChatError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


async def _join(task):
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise CopilotChatError(503, "Copilot cleanup is incomplete") from None
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _human(user):
    if (type(user) is not UserContext or user.is_api_key is not False
            or user.session_id or user.agent or user.external_claim or user.external_channel or user.external_id
            or not isinstance(user.sub, str) or not user.sub or len(user.sub) > 256
            or user.sub != user.sub.strip() or not user.sub.isprintable()
            or user.sub == "api-key" or user.sub.startswith("session:")):
        raise CopilotChatError(403, "Human authentication is required")


def _message(text):
    try:
        return isinstance(text, str) and bool(text.strip()) and len(text.encode("utf-8")) <= 65536
    except UnicodeError:
        return False


def _request_id(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        raise CopilotChatError(422, "A request identifier is required")


def _agent_filter(agent):
    if agent is not None and (not isinstance(agent, str) or not 0 < len(agent) <= 256
                              or not config.is_safe_agent_name(agent)):
        raise CopilotChatError(422, "A valid agent is required")


@dataclass(eq=False)
class _Entry:
    sid: str
    user: UserContext
    agent: str
    account_id: str
    model: str
    mode: str
    reasoning_effort: str | None = None
    delegation_enabled: bool = False
    workers: dict = field(default_factory=dict)
    delegation_pending: set = field(default_factory=set)
    handle: str = ""
    cid: str = ""
    resume: bool = False
    expected_revision: int | None = None
    store_attempted: bool = False
    uncertain_delivery: bool = False
    layer_start_attempted: bool = False
    mutations: set = field(default_factory=set)
    config: object = None
    admitted: bool = False
    startup: asyncio.Task | None = None
    closing: asyncio.Task | None = None
    turn: object = None
    last_activity: float = 0
    pending: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)
    usage_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=128))
    usage_task: asyncio.Task | None = None
    usage_failed: bool = False
    usage_sealed: bool = False


_END = object()
_PERSISTED = frozenset({"text", "tool_use", "tool_input", "tool_result",
                        "permission_prompt", "question_prompt", "error"})


class CopilotChatTurn:
    """Prepared stream ownership, including abandoned and unstarted consumers."""

    def __init__(self, service, entry, text):
        self._service, self._entry, self._text = service, entry, text
        self._queue = asyncio.Queue(maxsize=128)
        self._bytes = 0
        self._emit_lock = asyncio.Lock()
        self._durable_started = self._durable_finished = False
        self._complete = self._failed = self._exhausted = self._reading = False
        self._closing = None
        self._producer = self._prompts = None
        self._deadline = asyncio.get_running_loop().call_later(service.turn_timeout, self._expire)

    def _start(self):
        self._producer = asyncio.create_task(self._produce())
        self._producer.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    def _expire(self):
        self._failed = True
        self._service._begin_close(self._entry)

    async def _emit(self, frame):
        async with self._emit_lock:
            self._bytes += len(json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if self._bytes > 1024 * 1024:
                raise ValueError()
            await self._service._mutation(self._entry, "append_event", frame)
            await self._queue.put(frame)

    async def _forward_prompts(self):
        queue = state.get_permission_queue(self._entry.sid)
        while True:
            frame = await queue.get()
            if not isinstance(frame, dict):
                raise ValueError()
            kind, request_id = frame.get("event_type"), frame.get("request_id")
            if (kind not in {"permission_prompt", "question_prompt"}
                    or not isinstance(request_id, str) or not request_id):
                raise ValueError()
            if state.get_permission_request_session(request_id) != self._entry.sid:
                continue  # A waiter already retired before its queued frame.
            question = request_id in state._question_events
            if question != (kind == "question_prompt"):
                raise ValueError()
            if question:
                questions = frame.get("tool_input", {}).get("questions")
                if (not isinstance(questions, list) or len(questions) != 1
                        or not isinstance(questions[0], dict) or not isinstance(questions[0].get("id"), str)
                        or not questions[0]["id"] or questions[0].get("multiSelect") is not False
                        or type(questions[0].get("isOther")) is not bool
                        or not isinstance(questions[0].get("options"), list)
                        or any(not isinstance(option, dict) or not isinstance(option.get("label"), str)
                               for option in questions[0]["options"])):
                    raise ValueError()
                self._entry.questions[request_id] = deepcopy(questions[0])
            self._entry.pending[request_id] = kind
            await self._emit({"type": kind, "request_id": request_id,
                              "tool_name": frame.get("tool_name", ""), "tool_input": frame.get("tool_input", {})})

    def _prompt_finished(self, task):
        if not task.cancelled() and task.exception() is not None:
            self._failed = True
            self._service._begin_close(self._entry)

    async def _produce(self):
        done = 0
        try:
            self._prompts = asyncio.create_task(self._forward_prompts())
            self._prompts.add_done_callback(self._prompt_finished)
            async with self._service.layer.session_lock(self._entry.sid):
                async for event in self._service.layer.send_message(self._entry.sid, self._text):
                    if event.type == "done":
                        done += 1
                    elif event.type == "error":
                        raise ValueError()
                    elif event.type in _PERSISTED:
                        await self._emit({**event.data, "type": event.type})
            if done != 1 or self._failed or self._entry.closing is not None:
                raise ValueError()
            self._prompts.cancel()
            await asyncio.gather(self._prompts, return_exceptions=True)
            await self._service._flush_usage(self._entry)
            await self._service._mutation(self._entry, "finish_turn",
                                          success=lambda: setattr(self, "_durable_finished", True))
            await self._queue.put({"type": "turn_complete"})
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failed = True
            self._service._begin_close(self._entry)
        finally:
            self._text = ""
            if self._prompts is not None:
                self._prompts.cancel()
                await asyncio.gather(self._prompts, return_exceptions=True)

    def __aiter__(self):
        return self

    def delivery_failed(self):
        """Transport did not deliver its final body; do not advertise resume."""
        self._failed = True
        # The iterator may already have detached this turn before ASGI sends
        # its final empty body. Retain the fence on the captured owner too.
        self._entry.uncertain_delivery = True

    async def __anext__(self):
        if self._exhausted:
            raise StopAsyncIteration
        if self._complete:
            await self.aclose()
            raise StopAsyncIteration
        if self._reading:
            raise CopilotChatError(409, "Copilot turn already has a consumer")
        self._reading = True
        try:
            frame = await self._queue.get()
            if frame is _END:
                self._exhausted = True
                raise StopAsyncIteration
            if frame["type"] == "turn_complete":
                if self._failed or self._entry.closing is not None:
                    raise CopilotChatError(503, "Copilot turn did not complete")
                self._complete = True
            return frame
        except asyncio.CancelledError:
            await self.aclose()
            raise
        finally:
            self._reading = False

    async def _stop(self):
        self._deadline.cancel()
        if self._producer is not None:
            self._producer.cancel()
            await asyncio.gather(self._producer, return_exceptions=True)
        if self._prompts is not None:
            self._prompts.cancel()
            await asyncio.gather(self._prompts, return_exceptions=True)
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._failed:
            frame = {"type": "error", "message": "Copilot turn did not complete"} if self._durable_started and not self._durable_finished else None
            if self._durable_started and not self._durable_finished:
                try:
                    await self._service._mutation(self._entry, "append_event", frame)
                except CopilotChatError:
                    # No undurable frame is delivered. Cleanup still quarantines
                    # the unfinished generation, or retains its failed claim.
                    frame = None
            if frame is not None:
                self._queue.put_nowait(frame)
        self._queue.put_nowait(_END)
        self._entry.pending.clear()
        self._entry.questions.clear()

    async def _close(self):
        if self._complete and not self._failed and self._entry.closing is None:
            self._deadline.cancel()
            if self._producer is not None:
                await self._producer
            if self._entry.turn is self:
                self._entry.turn = None
                self._entry.last_activity = asyncio.get_running_loop().time()
                self._entry.pending.clear()
                self._entry.questions.clear()
        else:
            await self._service._close_entry(self._entry)
        self._exhausted = True

    async def aclose(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
            self._closing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await _join(self._closing)


class CopilotChatService:
    def __init__(self, layer, *, max_sessions=4, max_per_user=2, idle_timeout=300,
                 turn_timeout=300, watch_interval=5, authorization_timeout=5, store=None, database_timeout=40,
                 model_timeout=45):
        if (type(max_sessions) is not int or not 1 <= max_sessions <= 8
                or type(max_per_user) is not int or not 1 <= max_per_user <= max_sessions
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (idle_timeout, turn_timeout, watch_interval, authorization_timeout, database_timeout, model_timeout))
                or turn_timeout > 300 or watch_interval > 10 or authorization_timeout > 10 or database_timeout > 60
                or model_timeout > 60):
            raise ValueError("Invalid Copilot preview limits")
        from storage import copilot_conversation_store

        self.store = copilot_conversation_store if store is None else store
        self.database_timeout = database_timeout
        self.model_timeout = model_timeout
        self.layer = layer
        self.max_sessions, self.max_per_user = max_sessions, max_per_user
        self.idle_timeout, self.turn_timeout = idle_timeout, turn_timeout
        self.watch_interval, self.authorization_timeout = watch_interval, authorization_timeout
        self._entries = {}
        self._closing = None
        self._watcher = asyncio.create_task(self._watch())

    def _entry(self, user, sid):
        _human(user)
        entry = next((item for item in self._entries.values() if item.handle == sid), None) if isinstance(sid, str) else None
        if entry is None or entry.user.sub != user.sub:
            raise CopilotChatError(404, "Copilot session was not found")
        return entry

    def _check(self, entry):
        if (self._closing is not None or self._entries.get(entry.sid) is not entry
                or entry.closing is not None or asyncio.current_task().cancelling()):
            raise CopilotChatError(503, "Copilot session is unavailable")

    async def _config(self, entry, user):
        options = {"reasoning_effort": entry.reasoning_effort} if entry.reasoning_effort is not None else {}
        if entry.delegation_enabled:
            options["delegation_enabled"] = True
        async with asyncio.timeout(self.authorization_timeout):
            return await build_copilot_agent_config(
                user=user, agent_name=entry.agent, account_id=entry.account_id,
                account_scope=CopilotAccountScope.personal(entry.user.sub), model=entry.model,
                permission_mode=entry.mode, client_type="dashboard", enabled_tools=SUPPORTED_NATIVE_TOOLS, **options,
            )

    async def _authorize(self, entry, user):
        self._check(entry)
        failed = False
        try:
            config = await self._config(entry, user)
            self._check(entry)
            if config != entry.config:
                raise ValueError()
        except asyncio.CancelledError:
            await self._close_entry(entry)
            raise
        except Exception:
            failed = True
        if failed:
            await self._close_entry(entry)
            raise CopilotChatError(403, "Copilot session access is unavailable")

    async def _db(self, operation, *args, entry=None, success=None, **kwargs):
        """Bound waiting without cancelling a mutation that may still commit.

        Timed-out writes remain attached to the exact generation. Cleanup must
        drain them before its CAS; failed drainage retains the capacity claim.
        """
        from storage import copilot_conversation_store as contract
        from storage.pg import run_db

        mutation = operation not in {"get", "list_conversations", "events"}
        if mutation and entry is None:
            raise ValueError("Copilot mutation ownership is required")
        task = asyncio.create_task(run_db(getattr(self.store, operation), *args, **kwargs))
        if mutation:
            entry.mutations.add(task)

        def finished(done):
            if not done.cancelled() and done.exception() is None and success is not None:
                success()
            if mutation:
                entry.mutations.discard(done)

        task.add_done_callback(finished)
        deadline = asyncio.get_running_loop().time() + (self.database_timeout if mutation else self.authorization_timeout)
        cancelled, failure = False, None
        while True:
            try:
                remaining = max(0, deadline - asyncio.get_running_loop().time())
                complete, _ = await asyncio.wait({task}, timeout=remaining)
                if not complete:
                    failure = CopilotChatError(503, "Copilot history is unavailable")
                    break
                result = task.result()
                break
            except asyncio.CancelledError:
                if not mutation:
                    raise
                if task.cancelled():
                    failure = CopilotChatError(503, "Copilot history is unavailable")
                    break
                cancelled = True
            except Exception as error:
                status = (404 if isinstance(error, contract.CopilotConversationNotFound) else
                          409 if isinstance(error, contract.CopilotConversationConflict) else
                          429 if isinstance(error, contract.CopilotConversationLimit) else 503)
                failure = CopilotChatError(status, "Copilot history is unavailable")
                break
        if cancelled:
            raise asyncio.CancelledError
        if failure is not None:
            raise failure
        return result

    async def _mutation(self, entry, operation, *args, success=None):
        return await self._db(operation, entry.cid, entry.user.sub, entry.handle, *args,
                              entry=entry, success=success)

    def _receive_usage(self, entry, frame):
        """Synchronous native observer; retain the captured owner through close."""
        try:
            if (self._entries.get(entry.sid) is not entry or entry.usage_sealed
                    or entry.usage_failed or entry.usage_task is None or entry.usage_task.done()):
                raise ValueError()
            validate_usage_frame(frame)
            entry.usage_queue.put_nowait(deepcopy(frame))
        except Exception:
            entry.usage_failed = True
            self._begin_close(entry)
            raise ValueError("Copilot usage observation is unavailable") from None

    async def _record_usage(self, entry):
        try:
            while True:
                frame = await entry.usage_queue.get()
                try:
                    if frame is _END:
                        return
                    recorded = await self._mutation(entry, "append_usage", frame)
                    turn = entry.turn
                    # Delivery is optional while idle/closing. The durable
                    # report is conversation-level and remains readable later.
                    if (recorded and turn is not None and not turn._durable_finished
                            and not turn._failed and entry.closing is None):
                        turn._bytes += len(json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                        if turn._bytes > 1024 * 1024:
                            raise ValueError()
                        turn._queue.put_nowait(deepcopy(frame))
                finally:
                    entry.usage_queue.task_done()
        except (Exception, asyncio.CancelledError):
            entry.usage_failed = True
            self._begin_close(entry)
            # Wake a turn waiting for drainage; it observes usage_failed.
            while not entry.usage_queue.empty():
                entry.usage_queue.get_nowait()
                entry.usage_queue.task_done()

    async def _flush_usage(self, entry):
        if entry.usage_task is not None:
            async with asyncio.timeout(self.database_timeout):
                await entry.usage_queue.join()
        if entry.usage_failed:
            raise CopilotChatError(503, "Copilot usage could not be recorded")

    async def _finish_usage(self, entry):
        # Called only after native shutdown has joined every callback source.
        entry.usage_sealed = True
        if entry.usage_task is not None and not entry.usage_task.done():
            async with asyncio.timeout(self.database_timeout):
                await entry.usage_queue.put(_END)
            _, pending = await asyncio.wait({entry.usage_task}, timeout=self.database_timeout)
            if pending:
                raise CopilotChatError(503, "Copilot usage cleanup is incomplete")

    def _capacity(self, user):
        if self._closing is not None:
            raise CopilotChatError(503, "Copilot preview is shutting down")
        if (len(self._entries) >= self.max_sessions
                or sum(entry.user.sub == user.sub for entry in self._entries.values()) >= self.max_per_user):
            raise CopilotChatError(429, "Copilot preview session limit reached")

    async def _start(self, entry):
        entry.config = await self._config(entry, entry.user)
        self._check(entry)
        entry.store_attempted = True
        if entry.resume:
            await self._db("claim_resume", entry.cid, entry.user.sub, entry.expected_revision,
                           entry.handle, entry=entry)
        else:
            await self._db("create", entry.cid, entry.user.sub, agent=entry.agent,
                           account_id=entry.account_id, model=entry.model, permission_mode=entry.mode,
                           platform_session_id=entry.sid, generation=entry.handle,
                           reasoning_effort=entry.reasoning_effort, delegation_enabled=entry.delegation_enabled, entry=entry)
        self._check(entry)
        async with asyncio.timeout(5):
            admission = await acquire_chat_slot(entry.sid, target="local", execution_path="copilot-cli", user_sub=entry.user.sub)
            entry.admitted = bool(admission)
        self._check(entry)
        if not admission:
            raise CopilotChatError(429, "Local session capacity is unavailable")
        config = deepcopy(entry.config)
        config.resume = entry.resume
        entry.usage_task = asyncio.create_task(self._record_usage(entry))
        entry.layer_start_attempted = True
        options = {}
        if entry.delegation_enabled:
            async def delegate(tool_call_id, args):
                return await self._delegate(entry, tool_call_id, args)
            options["delegate_handler"] = delegate
        await self.layer.start_session(entry.sid, config, usage_observer=lambda frame: self._receive_usage(entry, frame), **options)
        self._check(entry)
        entry.last_activity = asyncio.get_running_loop().time()

    async def _launch(self, entry):
        self._entries[entry.sid] = entry
        entry.startup = asyncio.create_task(self._start(entry))
        status = 503
        try:
            await entry.startup
            return entry.handle
        except asyncio.CancelledError:
            await self._close_entry(entry)
            raise
        except CopilotChatError as error:
            status = error.status_code
        except Exception:
            pass
        await self._close_entry(entry)
        raise CopilotChatError(status, "Copilot session could not be started")

    async def create(self, user, agent, account_id, model, permission_mode="default", reasoning_effort=None, delegation_enabled=False):
        _human(user)
        if not valid_reasoning_effort(reasoning_effort) or type(delegation_enabled) is not bool:
            raise CopilotChatError(422, "Invalid Copilot conversation options")
        self._capacity(user)
        sid = str(uuid.uuid4())
        entry = _Entry(sid, deepcopy(user), agent, account_id, model, permission_mode,
                       reasoning_effort=reasoning_effort, delegation_enabled=delegation_enabled, handle=sid, cid=str(uuid.uuid4()))
        return await self._launch(entry)

    async def _delegate(self, entry, tool_call_id, args):
        from core.layers.copilot.host_tools import valid_delegate_args
        from services.delegation.copilot_worker import OwnedCopilotWorker

        self._check(entry)
        turn = entry.turn
        if (not entry.delegation_enabled or turn is None or not turn._durable_started
                or turn._failed or turn._complete
                or not valid_delegate_args(args, entry.config.delegation_targets)
                or tool_call_id in entry.delegation_pending
                or len(entry.delegation_pending) >= 4):
            raise CopilotChatError(403, "Copilot delegation is unavailable")
        # Reserve before any await so simultaneous host callbacks share one cap.
        entry.delegation_pending.add(tool_call_id)
        worker = None

        def parent_valid():
            return (self._entries.get(entry.sid) is entry and self._closing is None
                    and entry.closing is None and entry.turn is turn
                    and not turn._failed and not turn._complete)

        async def authorize_parent():
            # Do not await parent shutdown from an owned child callback: parent
            # cleanup must join this callback, so that would form a wait cycle.
            try:
                self._check(entry)
                current = await self._config(entry, entry.user)
                self._check(entry)
                if not parent_valid() or current != entry.config:
                    raise ValueError()
            except (Exception, asyncio.CancelledError):
                self._begin_close(entry)
                raise CopilotChatError(403, "Copilot delegation is unavailable") from None

        async def publish(frame):
            await authorize_parent()
            emission = asyncio.create_task(turn._emit(frame))
            try:
                while not emission.done():
                    await asyncio.wait({emission}, timeout=0.1)
                    if not parent_valid():
                        raise CopilotChatError(503, "Copilot delegation is unavailable")
                await emission
            finally:
                # Startup owns this publisher. A disconnected full SSE queue
                # must not hold startup while parent shutdown joins its child.
                # _db still joins any in-flight commit before emission ends.
                async def settle():
                    if not emission.done():
                        emission.cancel()
                    await asyncio.gather(emission, return_exceptions=True)
                await _join(asyncio.create_task(settle()))

        try:
            await authorize_parent()
            reserved = await self._db("reserve_delegation", entry.cid, entry.user.sub,
                                      entry.handle, tool_call_id, args, entry=entry)
            if not reserved:
                raise CopilotChatError(409, "Copilot delegation was already requested")
            await authorize_parent()
            worker = OwnedCopilotWorker(
                user=deepcopy(entry.user), source_agent=entry.agent, target_agent=args["agent"],
                name=args["name"], prompt=args["prompt"], tool_call_id=tool_call_id,
                parent_valid=parent_valid, authorize_parent=authorize_parent, publish=publish,
            )
            entry.workers[tool_call_id] = worker
            result = await worker.run()
            await worker.close()
            await authorize_parent()
            frame = {**result, "type": "delegate_result", "tool_id": tool_call_id, "name": args["name"]}
            await publish(frame)
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        finally:
            if worker is not None:
                cleanup = asyncio.create_task(worker.close())
                try:
                    await _join(cleanup)
                except BaseException:
                    self._begin_close(entry)
                    raise
                else:
                    entry.workers.pop(tool_call_id, None)
            entry.delegation_pending.discard(tool_call_id)

    async def _model_credential(self, entry):
        from storage.copilot_account_store import read_credential
        from storage.pg import run_db

        async with asyncio.timeout(self.authorization_timeout):
            return await run_db(read_credential, entry.account_id, CopilotAccountScope.personal(entry.user.sub))

    async def _discover_models(self, entry):
        entry.config = await self._config(entry, entry.user)
        self._check(entry)
        credential = await self._model_credential(entry)
        self._check(entry)
        async with asyncio.timeout(5):
            entry.admitted = bool(await acquire_chat_slot(
                entry.sid, target="local", execution_path="copilot-cli", user_sub=entry.user.sub,
            ))
        self._check(entry)
        if not entry.admitted:
            raise CopilotChatError(429, "Local session capacity is unavailable")
        entry.layer_start_attempted = True
        models = await self.layer.list_models(entry.sid, deepcopy(entry.config))
        self._check(entry)
        # Discovery may have taken time. Never return an inventory after its
        # agent, human or selected payer authorization changed in that interval.
        current = await self._config(entry, entry.user)
        self._check(entry)
        if current != entry.config:
            raise CopilotChatError(403, "Copilot model access is unavailable")
        if await self._model_credential(entry) != credential:
            raise CopilotChatError(403, "Copilot model account changed")
        self._check(entry)
        return {"models": models}

    async def list_models(self, user, agent, account_id):
        _human(user)
        _agent_filter(agent)
        if agent is None or not isinstance(account_id, str) or not 0 < len(account_id) <= 256:
            raise CopilotChatError(422, "An agent and personal account are required")
        self._capacity(user)
        sid = str(uuid.uuid4())
        # This is a capacity/cleanup owner only. No conversation row, handle or
        # native session is created; the placeholder model is never dispatched.
        entry = _Entry(sid, deepcopy(user), agent, account_id, "catalog-discovery", "default")
        self._entries[sid] = entry
        entry.startup = asyncio.create_task(self._discover_models(entry))
        status = 503
        result = None
        try:
            async with asyncio.timeout(self.model_timeout):
                result = await entry.startup
        except asyncio.CancelledError:
            # A revoked catalog or shutdown can cancel its child operation
            # without the HTTP request being cancelled. Report unavailability
            # instead of leaking an internal cancellation through ASGI.
            if asyncio.current_task().cancelling():
                raise
        except CopilotChatError as error:
            status = error.status_code
        except Exception:
            pass
        finally:
            # Also joins cancellation during startup/RPC, and retains capacity
            # if runtime shutdown cannot be confirmed. A result is never exposed
            # while the temporary discovery owner remains alive.
            await self._close_entry(entry)
        if result is None:
            raise CopilotChatError(status, "Copilot models are unavailable")
        return result

    def conversation_id(self, user, handle):
        return self._entry(user, handle).cid

    async def _history_authorize(self, user, agent):
        failed = False
        try:
            async with asyncio.timeout(self.authorization_timeout):
                await authorize_copilot_history(user, agent)
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
        if failed:
            raise CopilotChatError(403, "Copilot conversation access is unavailable")

    async def _metadata(self, user, row):
        await self._history_authorize(user, row["agent"])
        resumable = (row["state"] == "closed" and row["last_turn_complete"] is True
                     and row["turn_active"] is False)
        if resumable:
            resumable = await self.layer.history_ready(row["platform_session_id"], user.sub)
        fields = ("id", "agent", "account_id", "model", "permission_mode", "title",
                  "created_at", "updated_at", "state", "revision")
        metadata = {key: row[key].isoformat() if hasattr(row[key], "isoformat") else row[key] for key in fields}
        metadata["reasoning_effort"] = row.get("reasoning_effort")
        metadata["delegation_enabled"] = row.get("delegation_enabled", False)
        metadata.update(can_resume=bool(resumable), reason="Ready to resume" if resumable else "Conversation is not ready to resume")
        return metadata

    async def list_conversations(self, user, limit=20, offset=0, agent=None):
        _human(user)
        _agent_filter(agent)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
            raise CopilotChatError(422, "Invalid conversation pagination")
        if agent is not None:
            await self._history_authorize(user, agent)
        filters = {"agent": agent} if agent is not None else {}
        rows = await self._db("list_conversations", user.sub, limit=limit, offset=offset, **filters)
        metadata = []
        for row in rows:
            try:
                metadata.append(await self._metadata(user, row))
            except CopilotChatError as error:
                if error.status_code != 403:
                    raise
        more = False
        if len(rows) == limit and offset + limit <= 10000:
            more = bool(await self._db("list_conversations", user.sub, limit=1, offset=offset + limit, **filters))
        return {"conversations": metadata, "has_more": more}

    async def get_conversation(self, user, cid, agent=None):
        _human(user)
        _agent_filter(agent)
        row = await self._db("get", cid, user.sub)
        if row is None or (agent is not None and row["agent"] != agent):
            raise CopilotChatError(404, "Copilot conversation was not found")
        metadata = await self._metadata(user, row)
        events = await self._db("events", cid, user.sub)
        await self._history_authorize(user, row["agent"])
        return {"conversation": metadata, "events": events}

    async def resume(self, user, cid, expected_revision, agent=None):
        _human(user)
        _agent_filter(agent)
        if type(expected_revision) is not int or expected_revision < 1:
            raise CopilotChatError(422, "A conversation revision is required")
        row = await self._db("get", cid, user.sub)
        if row is None or (agent is not None and row["agent"] != agent):
            raise CopilotChatError(404, "Copilot conversation was not found")
        await self._history_authorize(user, row["agent"])
        self._capacity(user)
        sid = row["platform_session_id"]
        if sid in self._entries:
            raise CopilotChatError(409, "Copilot conversation already has an owner")
        entry = _Entry(sid, deepcopy(user), row["agent"], row["account_id"], row["model"], row["permission_mode"],
                       reasoning_effort=row.get("reasoning_effort"), delegation_enabled=row.get("delegation_enabled", False),
                       handle=str(uuid.uuid4()), cid=cid,
                       resume=True, expected_revision=expected_revision)
        return await self._launch(entry)

    async def prepare_turn(self, user, sid, text):
        entry = self._entry(user, sid)
        self._check(entry)
        if not _message(text):
            raise CopilotChatError(422, "A bounded text message is required")
        if entry.turn is not None or entry.startup is None or not entry.startup.done():
            raise CopilotChatError(409, "Copilot session already has an active turn")
        # Reserve synchronously before reauthorization yields to another caller.
        turn = CopilotChatTurn(self, entry, text)
        entry.turn = turn
        try:
            await self._authorize(entry, user)
            self._check(entry)
            await self._mutation(entry, "begin_turn", text,
                                 success=lambda: setattr(turn, "_durable_started", True))
            self._check(entry)
            turn._start()
            return turn
        except BaseException:
            await turn.aclose()
            raise

    async def stream(self, user, sid, text):
        turn = await self.prepare_turn(user, sid, text)
        try:
            async for frame in turn:
                yield frame
        finally:
            await turn.aclose()

    async def permission(self, user, sid, request_id, approved):
        entry = self._entry(user, sid)
        _request_id(request_id)
        if type(approved) is not bool:
            raise CopilotChatError(422, "A permission decision is required")
        await self._authorize(entry, user)
        if (entry.pending.get(request_id) != "permission_prompt"
                or state.get_permission_request_session(request_id) != entry.sid
                or request_id not in state._permission_events or request_id in state._question_events):
            raise CopilotChatError(409, "Copilot permission request is no longer pending")
        failed = False
        try:
            await self.layer.respond_permission(entry.sid, request_id, approved)
        except asyncio.CancelledError:
            await self._close_entry(entry)
            raise
        except Exception:
            failed = True
        if failed:
            await self._close_entry(entry)
            raise CopilotChatError(503, "Copilot permission response could not be delivered")
        entry.pending.pop(request_id, None)

    async def question(self, user, sid, request_id, answers):
        entry = self._entry(user, sid)
        _request_id(request_id)
        if (type(answers) is not dict or not 0 < len(answers) <= 32
                or any(not isinstance(key, str) or not 0 < len(key) <= 256
                       or type(value) is not dict or set(value) != {"answers"}
                       or type(value["answers"]) is not list or not 1 <= len(value["answers"]) <= 2
                       or any(not isinstance(answer, str) or not 0 < len(answer) <= 8192 for answer in value["answers"])
                       for key, value in answers.items())):
            raise CopilotChatError(422, "A bounded question answer is required")
        await self._authorize(entry, user)
        question = entry.questions.get(request_id)
        if question is not None:
            if set(answers) != {question["id"]}:
                raise CopilotChatError(422, "Answer the current Copilot question")
            values = answers[question["id"]]["answers"]
            labels = {option["label"] for option in question["options"]}
            if ((not question["isOther"] and (len(values) != 1 or values[0] not in labels))
                    or (len(values) == 2 and values[0] not in labels)):
                raise CopilotChatError(422, "Select an offered answer or permitted free text")
        if (question is None or entry.pending.get(request_id) != "question_prompt"
                or state.get_permission_request_session(request_id) != entry.sid
                or request_id not in state._question_events or request_id in state._permission_events
                or not state.resolve_question(request_id, deepcopy(answers))):
            raise CopilotChatError(409, "Copilot question is no longer pending")
        entry.pending.pop(request_id, None)
        entry.questions.pop(request_id, None)

    async def close(self, user, sid):
        entry = self._entry(user, sid)
        # An original owner may always stop their own resource after access
        # revocation. No fresh database read can delay or deny that cleanup.
        await self._close_entry(entry)

    def _begin_close(self, entry):
        if entry.closing is None:
            entry.closing = asyncio.create_task(self._finish_close(entry))
            entry.closing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return entry.closing

    async def _close_entry(self, entry):
        await _join(self._begin_close(entry))

    async def _finish_close(self, entry):
        failed = False
        try:
            if entry.startup is not None and not entry.startup.done():
                entry.startup.cancel()
                await asyncio.gather(entry.startup, return_exceptions=True)
            cleanup = [self.layer.close_session(entry.sid)] if entry.layer_start_attempted else []
            if entry.turn is not None:
                if not entry.turn._complete:
                    entry.turn._failed = True
                cleanup.append(entry.turn._stop())
            cleanup.extend(worker.close() for worker in tuple(entry.workers.values()))
            results = await asyncio.gather(*cleanup, return_exceptions=True)
            native_dead = (not entry.layer_start_attempted
                           or await self.layer.is_session_process_dead(entry.sid))
            # Failed native cleanup may still have joined the callback source.
            # Drain its writer in that case, but retain the failed owner below.
            if native_dead or await self.layer.is_usage_source_closed(entry.sid):
                await self._finish_usage(entry)
            if not native_dead or any(isinstance(result, BaseException) for result in results):
                raise ValueError()
            if entry.mutations:
                _, pending = await asyncio.wait(tuple(entry.mutations), timeout=min(5, self.database_timeout))
                if pending:
                    raise ValueError()
            if entry.store_attempted:
                row = await self._db("get", entry.cid, entry.user.sub)
                # A losing CAS must never quarantine the winning generation.
                if row is not None and row["generation"] == entry.handle:
                    delivered = (not entry.uncertain_delivery and not entry.usage_failed
                                 and (entry.turn is None or (entry.turn._complete and not entry.turn._failed)))
                    ready = (entry.layer_start_attempted and delivered
                             and await self.layer.history_ready(entry.sid, entry.user.sub))
                    await self._db("finish_close", entry.cid, entry.user.sub, entry.handle,
                                   resumable=ready, entry=entry)
            from core.session.owned_sessions import get_owned_session
            from core.session.session_manager import has_legacy_session

            # The layer releases reservations whose registration it still
            # owns. This fallback covers failure before layer registration,
            # never a visible replacement's resources. No await may separate
            # these ownership reads from the synchronous release.
            if (entry.admitted and state.get_session_security(entry.sid) is None
                    and get_owned_session(entry.sid) is None and not has_legacy_session(entry.sid)):
                release_chat_slot(entry.sid)
        except (Exception, asyncio.CancelledError):
            failed = True
        if failed:
            raise CopilotChatError(503, "Copilot cleanup is incomplete")
        if self._entries.get(entry.sid) is entry:
            del self._entries[entry.sid]

    async def _inspect(self, entry):
        if entry.closing is not None or entry.startup is None or not entry.startup.done():
            return
        try:
            if not await self.layer.is_session_alive(entry.sid):
                await self._close_entry(entry)
                return
            await self._authorize(entry, entry.user)
            if (entry.turn is None and not entry.pending
                    and asyncio.get_running_loop().time() - entry.last_activity >= self.idle_timeout):
                await self._close_entry(entry)
        except (CopilotChatError, asyncio.CancelledError):
            return
        except Exception:
            self._begin_close(entry)

    async def _watch(self):
        try:
            while self._closing is None:
                await asyncio.sleep(self.watch_interval)
                await asyncio.gather(*(self._inspect(entry) for entry in tuple(self._entries.values())))
        except asyncio.CancelledError:
            return

    async def _finish_all(self, entries):
        self._watcher.cancel()
        await asyncio.gather(self._watcher, return_exceptions=True)
        results = await asyncio.gather(*(self._close_entry(entry) for entry in entries), return_exceptions=True)
        failed = any(isinstance(result, BaseException) for result in results)
        try:
            await self.layer.aclose()
        except (Exception, asyncio.CancelledError):
            failed = True
        if failed:
            raise CopilotChatError(503, "Copilot preview cleanup is incomplete")

    async def aclose(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish_all(tuple(self._entries.values())))
            self._closing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await _join(self._closing)
