"""Trusted context-local capture before an existing engine starts a worker.

This is not a request/config option. The bounded worker adapter installs the
callback only around its own layer.start_session call; concurrent sessions do
not share a capture callback.
"""
from contextvars import ContextVar

session_capture = ContextVar("owned_worker_session_capture", default=None)
_owners = {}


def claim_worker(session_id, owner):
    if session_id in _owners:
        raise RuntimeError("Worker session is already owned")
    _owners[session_id] = owner


def release_worker(session_id, owner):
    if _owners.get(session_id) is owner:
        del _owners[session_id]


def is_owned_worker(session_id):
    """Retained cleanup claims also deny further delegation."""
    from services.delegation.recovery import session_quarantined
    return session_id in _owners or session_quarantined(session_id)


def is_owned_worker_chat(chat_id):
    from services.delegation.recovery import chat_quarantined
    return chat_quarantined(chat_id) or (bool(chat_id) and any(
        getattr(owner, "chat_id", None) == chat_id for owner in tuple(_owners.values())))


def capture_session(session):
    callback = session_capture.get()
    if callback is not None:
        callback(session)
