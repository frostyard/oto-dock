"""Cookie/CSRF/stream ownership HTTP boundaries with an inert chat service."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from auth.providers import UserContext, get_current_user


BASE = "/v1/copilot/chat"
SID = str(uuid.uuid4())
CID = str(uuid.uuid4())
SECRET = "private-provider-input-must-not-leak"


class Turn:
    def __init__(self):
        self.frames = [{"type": "text", "text": "hello"}, {"type": "turn_complete"}]
        self.index = 0
        self.started = False
        self.closed = asyncio.Event()
        self.error = None
        self.hold = None
        self.failed_delivery = False

    def __aiter__(self):
        return self

    def delivery_failed(self):
        self.failed_delivery = True

    async def __anext__(self):
        self.started = True
        if self.error:
            raise self.error
        if self.hold and self.index:
            await self.hold.wait()
        if self.index >= len(self.frames):
            raise StopAsyncIteration
        frame = self.frames[self.index]
        self.index += 1
        return frame

    async def aclose(self):
        self.closed.set()


class Service:
    def __init__(self):
        self.calls = []
        self.turn = Turn()
        self.error = None
        self.create_hook = None
        self.closed = asyncio.Event()

    async def create(self, user, agent, account_id, model, permission_mode="default"):
        options = {"agent": agent, "account_id": account_id, "model": model, "permission_mode": permission_mode}
        self.calls.append(("create", user, options))
        if self.error:
            raise self.error
        if self.create_hook:
            await self.create_hook()
        return SID

    def conversation_id(self, user, sid):
        return CID

    async def list_conversations(self, user, *, limit, offset):
        self.calls.append(("list", user, limit, offset))
        if self.error:
            raise self.error
        return {"conversations": [{"id": CID}], "has_more": False}

    async def get_conversation(self, user, cid):
        self.calls.append(("get", user, cid))
        if self.error:
            raise self.error
        return {"conversation": {"id": CID}, "events": [{"type": "user", "content": "hello", "seq": 1}]}

    async def resume(self, user, cid, revision):
        self.calls.append(("resume", user, cid, revision))
        if self.error:
            raise self.error
        if self.create_hook:
            await self.create_hook()
        return SID

    async def prepare_turn(self, user, sid, text):
        self.calls.append(("turn", user, sid, text))
        if self.error:
            raise self.error
        return self.turn

    async def permission(self, user, sid, request_id, approved):
        self.calls.append(("permission", user, sid, request_id, approved))
        if self.error:
            raise self.error

    async def question(self, user, sid, request_id, answers):
        self.calls.append(("question", user, sid, request_id, answers))
        if self.error:
            raise self.error

    async def close(self, user, sid):
        self.calls.append(("close", user, sid))
        self.closed.set()
        if self.error:
            raise self.error


@pytest_asyncio.fixture
async def api():
    from api.agents import copilot_chat

    user = [UserContext("alice", "alice@example.com", "Alice", "member")]
    app = FastAPI()
    app.include_router(copilot_chat.router)
    app.dependency_overrides[get_current_user] = lambda: user[0]
    service = Service()
    app.state.copilot_chat = service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                 cookies={"session": "authenticated-fixture"}) as client:
        yield SimpleNamespace(app=app, client=client, user=user, service=service)


def create_body(**changes):
    return {"agent": "demo", "account_id": "account-one", "model": "fixture-model", **changes}


async def post(api, path="/sessions", body=None, **options):
    return await api.client.post(BASE + path, json=create_body() if body is None else body,
                                 headers={"Origin": "http://testserver"}, **options)


def frames(response):
    return [json.loads(line.removeprefix("data:").strip())
            for line in response.text.splitlines() if line.startswith("data:")]


@pytest.mark.asyncio
async def test_status_and_missing_service_do_not_provision_or_dispatch(api):
    assert (await api.client.get(BASE + "/status")).json() == {"available": True}
    api.app.state.copilot_chat = None
    assert (await api.client.get(BASE + "/status")).json() == {"available": False}
    assert (await post(api)).status_code == 503
    assert api.service.calls == []


@pytest.mark.asyncio
async def test_create_and_controls_forward_authenticated_identity_with_strict_data(api):
    response = await post(api)
    assert response.status_code == 201 and response.json() == {"session_id": SID, "conversation_id": CID}
    assert api.service.calls == [("create", api.user[0], create_body(permission_mode="default"))]
    response = await post(api, f"/sessions/{SID}/permission", {"request_id": "request-one", "approved": False})
    assert response.status_code == 204
    answers = {"question-one": {"answers": ["selected", "custom answer"]}}
    response = await post(api, f"/sessions/{SID}/question", {"request_id": "request-two", "answers": answers})
    assert response.status_code == 204
    response = await api.client.delete(BASE + f"/sessions/{SID}", headers={"Origin": "http://testserver"})
    assert response.status_code == 204
    assert api.service.calls[1:] == [
        ("permission", api.user[0], SID, "request-one", False),
        ("question", api.user[0], SID, "request-two", answers),
        ("close", api.user[0], SID),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [None, "api", "session", "external", "agent", "synthetic"])
async def test_cookie_human_required_before_any_service_dispatch(api, principal):
    api.user[0] = {
        None: None,
        "api": replace(api.user[0], is_api_key=True),
        "session": replace(api.user[0], session_id=SID),
        "external": replace(api.user[0], external_claim="phone:caller"),
        "agent": replace(api.user[0], agent="demo"),
        "synthetic": replace(api.user[0], sub="session:" + SID),
    }[principal]
    response = await post(api)
    assert response.status_code in {401, 403}
    assert api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["Bearer fixture", "Basic fixture", ""])
async def test_authorization_header_rejected_even_with_cookie(api, authorization):
    response = await api.client.post(BASE + "/sessions", json=create_body(),
                                     headers={"Origin": "http://testserver", "Authorization": authorization})
    assert response.status_code in {401, 403}
    assert api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "null", "http://evil.test", "https://testserver",
                                     "http://testserver.evil.test", "http://testserver/path"])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_mutations_require_exact_trusted_origin(api, origin, method):
    headers = {} if origin is None else {"Origin": origin}
    path = "/sessions" if method == "POST" else f"/sessions/{SID}"
    response = await api.client.request(method, BASE + path, headers=headers,
                                       **({"json": create_body()} if method == "POST" else {}))
    assert response.status_code == 403
    assert api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.test", "X-Forwarded-Host": "evil.test"},
    {"Origin": "https://testserver", "X-Forwarded-Proto": "https"},
    {"Origin": "https://evil.test", "Forwarded": "host=evil.test;proto=https"},
])
async def test_forwarded_headers_cannot_redefine_trusted_origin(api, headers):
    response = await api.client.post(BASE + "/sessions", json=create_body(), headers=headers)
    assert response.status_code == 403 and api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
async def test_json_content_type_required_for_body_routes(api, content_type):
    headers = {"Origin": "http://testserver"}
    if content_type:
        headers["Content-Type"] = content_type
    response = await api.client.post(BASE + "/sessions", content=json.dumps(create_body()), headers=headers)
    assert response.status_code in {403, 415, 422}
    assert api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"account_scope": "platform"}, {"resume": True}, {"enabled_tools": ["bash"]},
    {"agent": ""}, {"agent": "x" * 257}, {"account_id": 123}, {"model": None},
    {"model": SECRET * 30}, {"permission_mode": "auto"}, {"permission_mode": "bypassPermissions"},
])
async def test_create_body_strict_and_validation_error_contains_no_input(api, changes):
    response = await post(api, body=create_body(**changes))
    assert response.status_code == 422
    assert SECRET not in response.text and "input" not in response.json()
    assert api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("turn", {"text": ""}), ("turn", {"text": "x" * 65537}), ("turn", {"text": 123}),
    ("turn", {"text": "valid", "images": [SECRET]}),
    ("permission", {"request_id": "request", "approved": "false"}),
    ("permission", {"request_id": "request", "approved": 1}),
    ("permission", {"request_id": "request", "approved": None}),
    ("permission", {"request_id": "", "approved": True}),
    ("question", {"request_id": "request", "answers": []}),
    ("question", {"request_id": "request", "answers": {"q": {"answers": [1]}}}),
    ("question", {"request_id": "request", "answers": {"q": {"answers": ["a"], "extra": SECRET}}}),
    ("question", {"request_id": "request", "answers": {"q": {"answers": ["a"] * 21}}}),
])
async def test_control_and_turn_bodies_are_strict(api, path, body):
    response = await post(api, f"/sessions/{SID}/{path}", body)
    assert response.status_code == 422 and SECRET not in response.text
    assert api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["not-a-session", "x" * 257, "123"])
async def test_invalid_session_ids_rejected_before_service(api, sid):
    response = await post(api, f"/sessions/{sid}/turn", {"text": "hello"})
    assert response.status_code == 422 and api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 409, 413, 422, 429, 503])
async def test_service_errors_map_before_sse_headers_and_never_echo_detail(api, status):
    from services.engines.copilot_chat import CopilotChatError

    api.service.error = CopilotChatError(status, SECRET)
    response = await post(api, f"/sessions/{SID}/turn", {"text": "hello"})
    assert response.status_code == status
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert SECRET not in response.text


@pytest.mark.asyncio
async def test_raw_service_error_is_sanitized(api):
    api.service.error = RuntimeError(SECRET)
    response = await post(api)
    assert response.status_code == 503 and SECRET not in response.text


@pytest.mark.asyncio
async def test_sse_event_order_permissions_terminal_and_final_cleanup(api):
    api.service.turn.frames = [
        {"type": "text", "text": "first"},
        {"type": "permission_prompt", "request_id": "p", "tool_name": "Bash", "tool_input": {"command": "pwd"}},
        {"type": "question_prompt", "request_id": "q", "questions": []},
        {"type": "text", "text": "last"},
        {"type": "turn_complete"},
    ]
    response = await post(api, f"/sessions/{SID}/turn", {"text": "hello"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert frames(response) == api.service.turn.frames
    assert api.service.turn.closed.is_set()
    assert api.service.calls == [("turn", api.user[0], SID, "hello")]


@pytest.mark.asyncio
async def test_stream_iteration_error_is_sanitized_and_closes_turn(api):
    api.service.turn.error = RuntimeError(SECRET)
    response = await post(api, f"/sessions/{SID}/turn", {"text": "hello"})
    events = frames(response)
    assert any(event["type"] == "error" for event in events)
    assert not any(event["type"] == "turn_complete" for event in events)
    assert SECRET not in response.text and api.service.turn.closed.is_set()


async def asgi_request(api, path, *, body, send_hook=None, disconnected=None, spec="2.3"):
    body_sent = False
    disconnected = disconnected or asyncio.Event()
    messages = []

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if send_hook:
            await send_hook(message)
        messages.append(dict(message))

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": BASE + path,
        "raw_path": (BASE + path).encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), (b"origin", b"http://testserver"),
                    (b"content-type", b"application/json"), (b"cookie", b"session=authenticated-fixture")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    await api.app(scope, receive, send)
    return messages


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["http.response.start", "http.response.body"])
@pytest.mark.parametrize("resuming", [False, True])
async def test_undelivered_created_session_is_closed(api, fail_at, resuming):
    async def failure(message):
        if message["type"] == fail_at:
            raise OSError("client disconnected")

    with pytest.raises(OSError):
        await asgi_request(api, f"/conversations/{CID}/resume" if resuming else "/sessions",
                           body={"revision": 1} if resuming else create_body(), send_hook=failure, spec="2.4")
    assert api.service.closed.is_set()
    assert api.service.calls[-1] == ("close", api.user[0], SID)


@pytest.mark.asyncio
async def test_prepared_turn_closes_when_transport_fails_before_iterator_starts(api):
    async def failure(message):
        if message["type"] == "http.response.start":
            raise OSError("client disconnected before headers")

    with pytest.raises(Exception):
        await asgi_request(api, f"/sessions/{SID}/turn", body={"text": "hello"}, send_hook=failure, spec="2.4")
    assert api.service.turn.closed.is_set() and not api.service.turn.started
    assert api.service.closed.is_set()


@pytest.mark.asyncio
async def test_midstream_disconnect_closes_prepared_turn(api):
    disconnected, first = asyncio.Event(), asyncio.Event()
    api.service.turn.hold = asyncio.Event()

    async def observe(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first.set()

    pending = asyncio.create_task(asgi_request(api, f"/sessions/{SID}/turn", body={"text": "hello"},
                                               send_hook=observe, disconnected=disconnected))
    try:
        await asyncio.wait_for(first.wait(), 1)
        disconnected.set()
        await asyncio.wait_for(pending, 1)
        assert api.service.turn.closed.is_set()
    finally:
        disconnected.set()
        api.service.turn.hold.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
@pytest.mark.parametrize("resuming", [False, True])
async def test_cancelled_create_joins_and_closes_late_created_session(api, disconnect, resuming):
    entered, cancelled, release, disconnected = (asyncio.Event() for _ in range(4))

    async def late():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    api.service.create_hook = late
    pending = asyncio.create_task(asgi_request(api, f"/conversations/{CID}/resume" if resuming else "/sessions",
                                               body={"revision": 1} if resuming else create_body(), disconnected=disconnected))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if disconnect:
            disconnected.set()
        else:
            pending.cancel()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not pending.done() and not api.service.closed.is_set()
        release.set()
        if disconnect:
            messages = await asyncio.wait_for(pending, 1)
            assert next(message for message in messages if message["type"] == "http.response.start")["status"] == 499
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 1)
        assert api.service.closed.is_set()
        assert api.service.calls[-1] == ("close", api.user[0], SID)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_cookie_is_rejected_even_if_dependency_returns_human(api):
    api.client.cookies.clear()
    assert (await api.client.get(BASE + "/status")).status_code == 403
    assert (await post(api)).status_code == 403
    assert api.service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("turn", {"text": "hello"}),
    ("permission", {"request_id": "p", "approved": True}),
    ("question", {"request_id": "q", "answers": {}}),
])
async def test_control_routes_require_origin_and_cannot_bypass_missing_service(api, path, body):
    endpoint = BASE + f"/sessions/{SID}/{path}"
    response = await api.client.post(endpoint, json=body)
    assert response.status_code == 403 and api.service.calls == []
    api.app.state.copilot_chat = None
    response = await api.client.post(endpoint, json=body, headers={"Origin": "http://testserver"})
    assert response.status_code == 503 and api.service.calls == []


@pytest.mark.asyncio
async def test_malformed_json_validation_never_echoes_raw_request(api):
    response = await api.client.post(BASE + "/sessions", content='{"agent":"' + SECRET,
                                     headers={"Origin": "http://testserver", "Content-Type": "application/json"})
    assert response.status_code == 422 and SECRET not in response.text
    assert api.service.calls == []


@pytest.mark.asyncio
async def test_repeated_transport_cancellation_cannot_cancel_owned_turn_cleanup(api):
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = False
    calls = 0

    async def close():
        nonlocal cancelled, calls
        calls += 1
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        api.service.turn.closed.set()

    async def failure(message):
        if message["type"] == "http.response.start":
            raise asyncio.CancelledError

    api.service.turn.aclose = close
    pending = asyncio.create_task(asgi_request(api, f"/sessions/{SID}/turn", body={"text": "hello"},
                                               send_hook=failure, spec="2.4"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
        assert api.service.turn.closed.is_set() and not cancelled and calls == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["terminal_frame", "final_body"])
async def test_terminal_transport_failure_closes_session_even_after_turn_complete(api, failure_at):
    async def failure(message):
        if message["type"] != "http.response.body":
            return
        terminal = b"turn_complete" in message.get("body", b"")
        final = message.get("more_body") is False
        if (failure_at == "terminal_frame" and terminal) or (failure_at == "final_body" and final):
            raise OSError("connection lost after engine completed")

    with pytest.raises(Exception):
        await asgi_request(api, f"/sessions/{SID}/turn", body={"text": "hello"}, send_hook=failure, spec="2.4")
    assert api.service.turn.index == len(api.service.turn.frames)
    assert api.service.turn.failed_delivery
    assert api.service.closed.is_set() and api.service.turn.closed.is_set()
    assert api.service.calls[-1] == ("close", api.user[0], SID)


@pytest.mark.asyncio
async def test_starlette_handled_disconnect_during_terminal_send_closes_session(api):
    disconnected, reached = asyncio.Event(), asyncio.Event()

    async def interrupt_terminal(message):
        if message["type"] == "http.response.body" and b"turn_complete" in message.get("body", b""):
            reached.set()
            disconnected.set()
            await asyncio.Event().wait()

    await asyncio.wait_for(asgi_request(api, f"/sessions/{SID}/turn", body={"text": "hello"},
                                       send_hook=interrupt_terminal, disconnected=disconnected), 1)
    assert reached.is_set() and api.service.turn.index == len(api.service.turn.frames)
    assert api.service.turn.failed_delivery
    assert api.service.closed.is_set() and api.service.turn.closed.is_set()


@pytest.mark.asyncio
async def test_history_read_and_explicit_resume_forward_identity_and_disable_caching(api):
    listing = await api.client.get(BASE + '/conversations?limit=3&offset=2')
    detail = await api.client.get(BASE + f'/conversations/{CID}')
    assert listing.json() == {'conversations': [{'id': CID}], 'has_more': False}
    assert detail.json()['events'] == [{'type': 'user', 'content': 'hello', 'seq': 1}]
    assert listing.headers['cache-control'] == detail.headers['cache-control'] == 'no-store'
    assert api.service.calls == [('list', api.user[0], 3, 2), ('get', api.user[0], CID)]
    response = await post(api, f'/conversations/{CID}/resume', {'revision': 7})
    assert response.status_code == 201
    assert response.json() == {'session_id': SID, 'conversation_id': CID}
    assert response.headers['cache-control'] == 'no-store'
    assert api.service.calls[-1] == ('resume', api.user[0], CID, 7)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/conversations', f'/conversations/{CID}'])
@pytest.mark.parametrize('principal', ['missing-cookie', 'api', 'bearer', 'session'])
async def test_history_reads_require_human_cookie_before_service(api, path, principal):
    headers = {}
    if principal == 'missing-cookie':
        api.client.cookies.clear()
    elif principal == 'api':
        api.user[0] = replace(api.user[0], is_api_key=True)
    elif principal == 'session':
        api.user[0] = replace(api.user[0], session_id=SID)
    else:
        headers['Authorization'] = 'Bearer fixture'
    response = await api.client.get(BASE + path, headers=headers)
    assert response.status_code == 403 and not api.service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'offset=-1', 'offset=10001', 'limit=1.5', 'offset=bad'])
async def test_history_query_bounds_reject_before_storage(api, query):
    assert (await api.client.get(BASE + '/conversations?' + query)).status_code == 422
    assert not api.service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {}, {'revision': True}, {'revision': '1'}, {'revision': 0}, {'revision': -1},
    {'revision': 2**63}, {'revision': 1, 'model': SECRET}, {'revision': 1, 'account_id': SECRET},
    {'revision': 1, 'permission_mode': 'acceptEdits'},
])
async def test_resume_requires_exact_revision_and_refuses_config_overrides(api, body):
    response = await post(api, f'/conversations/{CID}/resume', body)
    assert response.status_code == 422 and SECRET not in response.text
    assert not api.service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', [None, 'null', 'https://foreign.example'])
async def test_resume_requires_same_origin(api, origin):
    response = await api.client.post(BASE + f'/conversations/{CID}/resume', json={'revision': 1},
                                     headers={} if origin is None else {'Origin': origin})
    assert response.status_code == 403 and not api.service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/conversations', f'/conversations/{CID}', f'/conversations/{CID}/resume'])
@pytest.mark.parametrize('failure', ['disabled', 'private-error', 'missing'])
async def test_history_service_failures_are_sanitized(api, path, failure):
    from services.engines.copilot_chat import CopilotChatError
    expected = 503
    if failure == 'disabled':
        api.app.state.copilot_chat = None
    elif failure == 'private-error':
        api.service.error = ValueError(SECRET)
    else:
        api.service.error = CopilotChatError(404, SECRET)
        expected = 404
    response = await post(api, path, {'revision': 1}) if path.endswith('/resume') else await api.client.get(BASE + path)
    assert response.status_code == expected and SECRET not in response.text


@pytest.mark.asyncio
async def test_created_metadata_failure_is_sanitized_and_closes_owned_runtime(api):
    def fail(*args):
        raise ValueError(SECRET)
    api.service.conversation_id = fail
    response = await post(api)
    assert response.status_code == 503 and SECRET not in response.text
    assert api.service.closed.is_set()


@pytest.mark.asyncio
async def test_whole_history_page_deadline_is_sanitized_without_starting_runtime(api, monkeypatch):
    from api.agents import copilot_chat
    cancelled = asyncio.Event()
    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(copilot_chat, '_HISTORY_READ_SECONDS', 0.01)
    api.service.list_conversations = blocked
    response = await api.client.get(BASE + '/conversations')
    assert response.status_code == 503 and cancelled.is_set()
    assert api.service.calls == []
