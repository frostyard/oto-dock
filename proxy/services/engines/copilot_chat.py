"""Application-owned personal Copilot preview; no chat rows or engine routing."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import uuid

from auth.providers import UserContext
from core.concurrency import acquire_chat_slot, release_chat_slot
from core.config.copilot_config_builder import build_copilot_agent_config
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


@dataclass(eq=False)
class _Entry:
    sid: str
    user: UserContext
    agent: str
    account_id: str
    model: str
    mode: str
    config: object = None
    admitted: bool = False
    startup: asyncio.Task | None = None
    closing: asyncio.Task | None = None
    turn: object = None
    last_activity: float = 0
    pending: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)


_END = object()


class CopilotChatTurn:
    """Prepared stream ownership, including abandoned and unstarted consumers."""

    def __init__(self, service, entry, text):
        self._service, self._entry, self._text = service, entry, text
        self._queue = asyncio.Queue(maxsize=128)
        self._bytes = 0
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
        self._bytes += len(json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if self._bytes > 1024 * 1024:
            raise ValueError()
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
                    else:
                        await self._emit({**event.data, "type": event.type})
            if done != 1 or self._failed or self._entry.closing is not None:
                raise ValueError()
            self._prompts.cancel()
            await asyncio.gather(self._prompts, return_exceptions=True)
            await self._emit({"type": "turn_complete"})
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
            self._queue.put_nowait({"type": "error", "message": "Copilot turn did not complete"})
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
                 turn_timeout=300, watch_interval=5, authorization_timeout=5):
        if (type(max_sessions) is not int or not 1 <= max_sessions <= 8
                or type(max_per_user) is not int or not 1 <= max_per_user <= max_sessions
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (idle_timeout, turn_timeout, watch_interval, authorization_timeout))
                or turn_timeout > 300 or watch_interval > 10 or authorization_timeout > 10):
            raise ValueError("Invalid Copilot preview limits")
        self.layer = layer
        self.max_sessions, self.max_per_user = max_sessions, max_per_user
        self.idle_timeout, self.turn_timeout = idle_timeout, turn_timeout
        self.watch_interval, self.authorization_timeout = watch_interval, authorization_timeout
        self._entries = {}
        self._closing = None
        self._watcher = asyncio.create_task(self._watch())

    def _entry(self, user, sid):
        _human(user)
        entry = self._entries.get(sid) if isinstance(sid, str) else None
        if entry is None or entry.user.sub != user.sub:
            raise CopilotChatError(404, "Copilot session was not found")
        return entry

    def _check(self, entry):
        if (self._closing is not None or self._entries.get(entry.sid) is not entry
                or entry.closing is not None or asyncio.current_task().cancelling()):
            raise CopilotChatError(503, "Copilot session is unavailable")

    async def _config(self, entry, user):
        async with asyncio.timeout(self.authorization_timeout):
            return await build_copilot_agent_config(
                user=user, agent_name=entry.agent, account_id=entry.account_id,
                account_scope=CopilotAccountScope.personal(entry.user.sub), model=entry.model,
                permission_mode=entry.mode, client_type="dashboard", enabled_tools=SUPPORTED_NATIVE_TOOLS,
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

    async def _start(self, entry):
        entry.config = await self._config(entry, entry.user)
        self._check(entry)
        async with asyncio.timeout(5):
            admission = await acquire_chat_slot(entry.sid, target="local", execution_path="copilot-cli", user_sub=entry.user.sub)
            # Record the successful reservation before the cancellation/current
            # ownership check: a late admission still needs safe cleanup.
            entry.admitted = bool(admission)
        self._check(entry)
        if not admission:
            raise CopilotChatError(429, "Local session capacity is unavailable")
        await self.layer.start_session(entry.sid, entry.config)
        self._check(entry)
        entry.last_activity = asyncio.get_running_loop().time()

    async def create(self, user, agent, account_id, model, permission_mode="default"):
        _human(user)
        if self._closing is not None:
            raise CopilotChatError(503, "Copilot preview is shutting down")
        if (len(self._entries) >= self.max_sessions
                or sum(entry.user.sub == user.sub for entry in self._entries.values()) >= self.max_per_user):
            raise CopilotChatError(429, "Copilot preview session limit reached")
        entry = _Entry(str(uuid.uuid4()), deepcopy(user), agent, account_id, model, permission_mode)
        self._entries[entry.sid] = entry
        entry.startup = asyncio.create_task(self._start(entry))
        status = 503
        try:
            await entry.startup
            return entry.sid
        except asyncio.CancelledError:
            await self._close_entry(entry)
            raise
        except CopilotChatError as error:
            status = error.status_code
        except Exception:
            pass
        await self._close_entry(entry)
        raise CopilotChatError(status, "Copilot session could not be started")

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
                or state.get_permission_request_session(request_id) != sid
                or request_id not in state._permission_events or request_id in state._question_events):
            raise CopilotChatError(409, "Copilot permission request is no longer pending")
        failed = False
        try:
            await self.layer.respond_permission(sid, request_id, approved)
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
                or state.get_permission_request_session(request_id) != sid
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
            cleanup = [self.layer.close_session(entry.sid)]
            if entry.turn is not None:
                if not entry.turn._complete:
                    entry.turn._failed = True
                cleanup.append(entry.turn._stop())
            results = await asyncio.gather(*cleanup, return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                raise ValueError()
            if not await self.layer.is_session_process_dead(entry.sid):
                raise ValueError()
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
