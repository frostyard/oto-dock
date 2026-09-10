"""Durable worker quarantine; terminal run rows are not cleanup evidence.

Startup restores unresolved receipts even when the Copilot preview is disabled.
Only the exact finalizer that committed joined cleanup can release a receipt.
These records never create, resume, retry, or adopt an execution session.
"""

from copy import deepcopy

_by_session = {}
_by_chat = {}
_MAX_UNSETTLED = 10000


def _checked(receipt):
    if type(receipt) is not dict or any(
        type(receipt.get(key)) is not str or not receipt[key]
        or receipt[key] != receipt[key].strip() or not receipt[key].isprintable()
        or len(receipt[key]) > 256
        for key in ("receipt_id", "conversation_id", "tool_id", "session_id", "chat_id")
    ):
        raise ValueError("Worker recovery identity is unavailable")
    return deepcopy(receipt)


def quarantine(receipt):
    receipt = _checked(receipt)
    sid, cid = receipt["session_id"], receipt["chat_id"]
    if any(prior is not None and prior != receipt
           for prior in (_by_session.get(sid), _by_chat.get(cid))):
        raise ValueError("Worker recovery identity changed")
    _by_session[sid] = _by_chat[cid] = receipt


def release(receipt):
    receipt = _checked(receipt)
    sid, cid = receipt["session_id"], receipt["chat_id"]
    if _by_session.get(sid) == receipt and _by_chat.get(cid) == receipt:
        del _by_session[sid]
        del _by_chat[cid]


def session_quarantined(session_id):
    return type(session_id) is str and session_id in _by_session


def chat_quarantined(chat_id):
    return type(chat_id) is str and chat_id in _by_chat


async def restore(*, store=None):
    from storage import copilot_delegation_store
    from storage.pg import run_db

    rows = await run_db((copilot_delegation_store if store is None else store).list_unsettled)
    if type(rows) is not list or len(rows) > _MAX_UNSETTLED:
        raise ValueError("Worker recovery is unavailable")
    # Validate the whole result before publishing any restored map. Merge with
    # retained in-process claims; another lifespan cannot erase an old owner.
    sessions, chats = dict(_by_session), dict(_by_chat)
    for row in rows:
        receipt = _checked(row)
        sid, cid = receipt["session_id"], receipt["chat_id"]
        if any(prior is not None and prior != receipt
               for prior in (sessions.get(sid), chats.get(cid))):
            raise ValueError("Worker recovery identity changed")
        sessions[sid] = chats[cid] = receipt
    _by_session.update(sessions)
    _by_chat.update(chats)
