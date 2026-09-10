"""Cookie-authenticated local Copilot preview, separate from generic chat routing."""

import asyncio
from contextlib import suppress
import json
from typing import Annotated, Literal
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from auth.providers import UserContext, get_current_user, require_auth
from services.engines.copilot_chat import CopilotChatError


class _PrivateRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                pass
            raise HTTPException(422, "Invalid Copilot chat request")
        return handler


router = APIRouter(prefix="/v1/copilot/chat", route_class=_PrivateRoute)
_HISTORY_READ_SECONDS = 15
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
Answer = Annotated[str, Field(strict=True, max_length=4096)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CreateRequest(_Body):
    agent: Identifier
    account_id: Identifier
    model: Identifier
    permission_mode: Literal["default", "acceptEdits", "plan", "dontAsk"] = "default"


class TurnRequest(_Body):
    text: Annotated[str, Field(strict=True, min_length=1, max_length=65536)]


class ResumeRequest(_Body):
    revision: Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]


class PermissionRequest(_Body):
    request_id: Identifier
    approved: StrictBool


class QuestionAnswer(_Body):
    answers: list[Answer] = Field(max_length=20)


class QuestionRequest(_Body):
    request_id: Identifier
    answers: dict[Identifier, QuestionAnswer] = Field(max_length=16)


def _human(request, user):
    user = require_auth(user)
    if (type(user) is not UserContext or not request.cookies.get("session")
            or "authorization" in request.headers or user.is_api_key is not False
            or user.session_id or user.agent or user.external_claim
            or user.external_channel or user.external_id
            or not isinstance(user.sub, str) or not user.sub or len(user.sub) > 256
            or user.sub.strip() != user.sub or not user.sub.isprintable()
            or user.sub == "api-key" or user.sub.startswith("session:")):
        raise HTTPException(403, "Human cookie authentication is required")
    return user


def _mutation(request, *, body=True):
    # Trust the ASGI URL (including the server's configured proxy-header
    # policy), never browser-supplied Forwarded/X-Forwarded-Host alternatives.
    origin = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin") != origin:
        raise HTTPException(403, "Same-origin request required")
    if body and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON request required")


def _service(request):
    service = getattr(request.app.state, "copilot_chat", None)
    if service is None:
        raise HTTPException(503, "Copilot chat preview is unavailable")
    return service


_ERRORS = {
    400: "Invalid Copilot chat request", 403: "Copilot chat access is unavailable",
    404: "Copilot chat session not found", 409: "Copilot chat is busy or closed",
    413: "Copilot chat input is too large", 429: "Copilot chat capacity is unavailable",
    422: "Invalid Copilot chat request",
    503: "Copilot chat preview is unavailable",
}


async def _call(operation):
    status = 503
    try:
        return await operation
    except CopilotChatError as error:
        status = error.status_code if error.status_code in _ERRORS else 503
    except Exception:
        pass
    raise HTTPException(status, _ERRORS[status])


async def _join(operation):
    """Shield cleanup from both ASGI cancel scopes and repeated task cancellation."""
    task = asyncio.create_task(operation)
    cancelled = False
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _disconnected(request, stopped):
    while not stopped.is_set():
        # Starlette probes receive inside its own cancel scope. An explicit
        # stop flag also terminates if that probe swallowed task cancellation.
        if await request.is_disconnected() or stopped.is_set():
            return
        await asyncio.sleep(0.1)


class _CreatedResponse(JSONResponse):
    def __init__(self, service, user, session_id, conversation_id):
        super().__init__({"session_id": session_id,
                          "conversation_id": conversation_id}, status_code=201,
                         headers={"Cache-Control": "no-store"})
        self.service, self.user, self.session_id = service, user, session_id

    async def __call__(self, scope, receive, send):
        delivered = False
        try:
            await super().__call__(scope, receive, send)
            delivered = True
        finally:
            if not delivered:
                await _join(self.service.close(self.user, self.session_id))


class _TurnResponse(StreamingResponse):
    def __init__(self, turn, close_session):
        self.turn = turn
        self.close_session = close_session
        super().__init__(self._events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })

    async def _events(self):
        try:
            async for event in self.turn:
                yield "data: " + json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n\n"
        except Exception:
            yield 'data: {"type":"error","message":"Copilot chat stream ended unexpectedly"}\n\n'

    async def __call__(self, scope, receive, send):
        delivered = False

        async def tracked_send(message):
            nonlocal delivered
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                delivered = True

        try:
            await super().__call__(scope, receive, tracked_send)
        finally:
            # A prepared turn owns work even if ASGI failed before consuming its
            # first event. This must not depend on an async generator's finally.
            try:
                if not delivered:
                    self.turn.delivery_failed()
                    with suppress(CopilotChatError):
                        await _join(self.close_session())
            finally:
                with suppress(CopilotChatError):
                    await _join(self.turn.aclose())


@router.get("/status")
async def status(request: Request, user: UserContext | None = Depends(get_current_user)):
    _human(request, user)
    return {"available": getattr(request.app.state, "copilot_chat", None) is not None}


@router.post("/sessions", status_code=201)
async def create(req: CreateRequest, request: Request, user: UserContext | None = Depends(get_current_user)):
    user = _human(request, user)
    _mutation(request)
    service = _service(request)
    return await _open_session(request, user, service, service.create(
        user, req.agent, req.account_id, req.model, permission_mode=req.permission_mode,
    ))


async def _open_session(request, user, service, operation):
    # Creation and cold resume share exact-owner disposal on a lost response.
    task = asyncio.create_task(operation)
    stopped = asyncio.Event()
    disconnected = asyncio.create_task(_disconnected(request, stopped))
    handed_off = False
    try:
        await asyncio.wait((task, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if disconnected.done() or await request.is_disconnected():
            raise HTTPException(499, "Copilot chat connection closed")
        session_id = await _call(task)
        async def response_metadata():
            return service.conversation_id(user, session_id)
        conversation_id = await _call(response_metadata())
        response = _CreatedResponse(service, user, session_id, conversation_id)
        handed_off = True
        return response
    finally:
        stopped.set()
        disconnected.cancel()
        if not handed_off:
            async def abandon():
                task.cancel()
                result = (await asyncio.gather(task, return_exceptions=True))[0]
                if isinstance(result, str):
                    await service.close(user, result)
            await _join(abandon())
        await asyncio.gather(disconnected, return_exceptions=True)


@router.get("/conversations")
async def list_conversations(request: Request,
                             limit: Annotated[int, Query(ge=1, le=100)] = 20,
                             offset: Annotated[int, Query(ge=0, le=10000)] = 0,
                             agent: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
                             user: UserContext | None = Depends(get_current_user)):
    user = _human(request, user)
    filters = {"agent": agent} if agent is not None else {}
    result = await _call(_history_read(_service(request).list_conversations(user, limit=limit, offset=offset, **filters)))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, request: Request,
                           agent: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
                           user: UserContext | None = Depends(get_current_user)):
    user = _human(request, user)
    filters = {"agent": agent} if agent is not None else {}
    result = await _call(_history_read(_service(request).get_conversation(user, str(conversation_id), **filters)))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


async def _history_read(operation):
    # One page must not accumulate a separate authorization deadline per row.
    async with asyncio.timeout(_HISTORY_READ_SECONDS):
        return await operation


@router.post("/conversations/{conversation_id}/resume", status_code=201)
async def resume(conversation_id: UUID, req: ResumeRequest, request: Request,
                 agent: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
                 user: UserContext | None = Depends(get_current_user)):
    user = _human(request, user)
    _mutation(request)
    service = _service(request)
    filters = {"agent": agent} if agent is not None else {}
    return await _open_session(request, user, service, service.resume(user, str(conversation_id), req.revision, **filters))


@router.post("/sessions/{session_id}/turn")
async def turn(session_id: UUID, req: TurnRequest, request: Request,
               user: UserContext | None = Depends(get_current_user)):
    user = _human(request, user)
    _mutation(request)
    service = _service(request)
    prepared = await _call(service.prepare_turn(user, str(session_id), req.text))
    return _TurnResponse(prepared, lambda: service.close(user, str(session_id)))


@router.post("/sessions/{session_id}/permission", status_code=204)
async def permission(session_id: UUID, req: PermissionRequest, request: Request,
                     user: UserContext | None = Depends(get_current_user)):
    user = _human(request, user)
    _mutation(request)
    await _call(_service(request).permission(user, str(session_id), req.request_id, req.approved))
    return Response(status_code=204)


@router.post("/sessions/{session_id}/question", status_code=204)
async def question(session_id: UUID, req: QuestionRequest, request: Request,
                   user: UserContext | None = Depends(get_current_user)):
    user = _human(request, user)
    _mutation(request)
    await _call(_service(request).question(user, str(session_id), req.request_id,
                                         {key: value.model_dump() for key, value in req.answers.items()}))
    return Response(status_code=204)


@router.delete("/sessions/{session_id}", status_code=204)
async def close(session_id: UUID, request: Request, user: UserContext | None = Depends(get_current_user)):
    user = _human(request, user)
    _mutation(request, body=False)
    await _call(_service(request).close(user, str(session_id)))
    return Response(status_code=204)
