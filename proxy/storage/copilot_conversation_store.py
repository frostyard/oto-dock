"""Isolated personal Copilot history; database state is not native resume proof.

Trusted synchronous service API. Callers authenticate current agent access and
account eligibility. Every transaction also scopes rows to the original human.
No generic chat rows, credentials, engine routing, or inference are created.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
import math
import uuid

import config
from storage.pg import get_conn


MAX_EVENTS = 1000
MAX_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 256 * 1024
_DB_TIMEOUT_MS = 5000
_UNSET = object()
_EVENT_TYPES = frozenset({"text", "tool_use", "tool_input", "tool_result",
                          "permission_prompt", "question_prompt", "error"})


class CopilotConversationError(RuntimeError):
    pass


class CopilotConversationNotFound(CopilotConversationError):
    pass


class CopilotConversationConflict(CopilotConversationError):
    pass


class CopilotConversationLimit(CopilotConversationError):
    pass


_ERRORS = {
    CopilotConversationError: "Copilot conversation request is unavailable",
    CopilotConversationNotFound: "Copilot conversation was not found",
    CopilotConversationConflict: "Copilot conversation state changed",
    CopilotConversationLimit: "Copilot conversation history limit reached",
}


def _safe(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        error_type = CopilotConversationError
        try:
            return operation(*args, **kwargs)
        except CopilotConversationError as error:
            error_type = type(error) if type(error) in _ERRORS else CopilotConversationError
        except Exception:
            pass
        raise error_type(_ERRORS[error_type])
    return call


def _text(value):
    return (type(value) is str and 0 < len(value) <= 256
            and value == value.strip() and value.isprintable())


def _uuid(value):
    return type(value) is str and str(uuid.UUID(value)) == value


def _identity(cid, owner, generation=_UNSET):
    if not _uuid(cid) or not _text(owner) or (generation is not _UNSET and not _uuid(generation)):
        raise CopilotConversationError()


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connection():
    # Transaction-local settings reset on commit or rollback before the
    # connection returns to the shared pool. Pool acquisition keeps its own
    # existing bound; these limits cover server work and row-lock contention.
    with get_conn() as conn:
        timeout = f"{_DB_TIMEOUT_MS}ms"
        conn.execute("SELECT set_config('statement_timeout', %s, true), "
                     "set_config('lock_timeout', %s, true)", (timeout, timeout))
        yield conn


def _json(value, depth=0):
    if depth > 32:
        raise CopilotConversationError()
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        if "\x00" in value:
            raise CopilotConversationError()
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise CopilotConversationError()
        return
    if type(value) is list:
        for item in value:
            _json(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise CopilotConversationError()
            _json(key, depth + 1)
            _json(item, depth + 1)
        return
    raise CopilotConversationError()


def _encoded(event):
    _json(event)
    data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    size = len(data.encode("utf-8"))
    if size > MAX_FRAME_BYTES:
        raise CopilotConversationLimit()
    return data, size


def _row(conn, cid, owner, generation=_UNSET):
    row = conn.execute(
        "SELECT * FROM copilot_conversations WHERE id = %s AND user_sub = %s FOR UPDATE",
        (cid, owner),
    ).fetchone()
    if row is None:
        raise CopilotConversationNotFound()
    if generation is not _UNSET and row["generation"] != generation:
        raise CopilotConversationConflict()
    return row


def _open(row, *, active):
    if row["state"] != "open" or row["turn_active"] is not active:
        raise CopilotConversationConflict()


def _append(conn, row, event):
    data, size = _encoded(event)
    count, total = row["event_count"] + 1, row["event_bytes"] + size
    if count > MAX_EVENTS or total > MAX_BYTES:
        raise CopilotConversationLimit()
    conn.execute(
        "INSERT INTO copilot_conversation_events (conversation_id, seq, payload, created_at) VALUES (%s,%s,%s,%s)",
        (row["id"], count, data, _now()),
    )
    return {"event_count": count, "event_bytes": total}


def _update(conn, row, **fields):
    # Fields are exclusively private, literal keyword arguments from this file.
    fields.update(revision=row["revision"] + 1, updated_at=_now())
    assignments = ", ".join(f"{key} = %s" for key in fields)
    result = conn.execute(
        f"UPDATE copilot_conversations SET {assignments} WHERE id = %s AND user_sub = %s "
        "AND generation = %s RETURNING *",
        (*fields.values(), row["id"], row["user_sub"], row["generation"]),
    ).fetchone()
    if result is None:
        raise CopilotConversationConflict()
    conn.commit()
    return dict(result)


@_safe
def create(conversation_id, owner_sub, *, agent, account_id, model, permission_mode,
           platform_session_id, generation):
    _identity(conversation_id, owner_sub, generation)
    if (not _text(agent) or not config.is_safe_agent_name(agent) or not _uuid(account_id)
            or not _text(model) or permission_mode not in {"default", "acceptEdits", "plan", "dontAsk"}
            or not _uuid(platform_session_id)):
        raise CopilotConversationError()
    with _connection() as conn:
        now = _now()
        row = conn.execute(
            """INSERT INTO copilot_conversations
               (id,user_sub,agent,account_id,model,permission_mode,platform_session_id,generation,created_at,updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *""",
            (conversation_id, owner_sub, agent, account_id, model, permission_mode,
             platform_session_id, generation, now, now),
        ).fetchone()
        if row is None:
            raise CopilotConversationConflict()
        conn.commit()
        return dict(row)


@_safe
def get(cid, owner):
    _identity(cid, owner)
    with _connection() as conn:
        row = conn.execute("SELECT * FROM copilot_conversations WHERE id = %s AND user_sub = %s",
                           (cid, owner)).fetchone()
        return dict(row) if row is not None else None


@_safe
def list_conversations(owner, limit=20, offset=0):
    if (not _text(owner) or type(limit) is not int or not 1 <= limit <= 100
            or type(offset) is not int or not 0 <= offset <= 10000):
        raise CopilotConversationError()
    with _connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM copilot_conversations WHERE user_sub = %s "
            "ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s", (owner, limit, offset),
        ).fetchall()]


@_safe
def events(cid, owner):
    _identity(cid, owner)
    with _connection() as conn:
        _row(conn, cid, owner)
        rows = conn.execute(
            """SELECT e.seq, e.payload FROM copilot_conversation_events e
               JOIN copilot_conversations c ON c.id = e.conversation_id
               WHERE c.id = %s AND c.user_sub = %s ORDER BY e.seq""", (cid, owner),
        ).fetchall()
        return [{**json.loads(row["payload"]), "seq": row["seq"]} for row in rows]


@_safe
def begin_turn(cid, owner, generation, text):
    _identity(cid, owner, generation)
    if type(text) is not str or not text.strip():
        raise CopilotConversationError()
    if len(text.encode("utf-8")) > 65536:
        raise CopilotConversationLimit()
    with _connection() as conn:
        row = _row(conn, cid, owner, generation)
        _open(row, active=False)
        counts = _append(conn, row, {"type": "user", "content": text})
        return _update(conn, row, **counts, turn_active=True, last_turn_complete=False,
                       title=row["title"] or " ".join(text.split())[:80])


@_safe
def append_event(cid, owner, generation, event):
    _identity(cid, owner, generation)
    if type(event) is not dict or event.get("type") not in _EVENT_TYPES or "seq" in event:
        raise CopilotConversationError()
    with _connection() as conn:
        row = _row(conn, cid, owner, generation)
        _open(row, active=True)
        return _update(conn, row, **_append(conn, row, event))


@_safe
def finish_turn(cid, owner, generation):
    _identity(cid, owner, generation)
    with _connection() as conn:
        row = _row(conn, cid, owner, generation)
        _open(row, active=True)
        return _update(conn, row, **_append(conn, row, {"type": "turn_complete"}),
                       turn_active=False, last_turn_complete=True)


@_safe
def claim_resume(cid, owner, expected_revision, generation):
    _identity(cid, owner, generation)
    if type(expected_revision) is not int or expected_revision < 1:
        raise CopilotConversationError()
    with _connection() as conn:
        row = _row(conn, cid, owner)
        if (row["state"] != "closed" or row["turn_active"] or not row["last_turn_complete"]
                or row["revision"] != expected_revision or row["generation"] == generation):
            raise CopilotConversationConflict()
        return _update(conn, row, state="open", generation=generation)


@_safe
def finish_close(cid, owner, generation, resumable):
    _identity(cid, owner, generation)
    if type(resumable) is not bool:
        raise CopilotConversationError()
    with _connection() as conn:
        row = _row(conn, cid, owner, generation)
        if row["state"] != "open":
            raise CopilotConversationConflict()
        closed = resumable and row["last_turn_complete"] and not row["turn_active"]
        return _update(conn, row, state="closed" if closed else "incomplete", turn_active=False)
