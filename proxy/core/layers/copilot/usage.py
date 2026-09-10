"""Reported per-request Copilot metrics; never prices, invoices or turn totals."""

import hashlib
import inspect
import json
import uuid

_MAX_NUMBER = 2 ** 53 - 1
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
_FIELDS = frozenset({"type", "event_id", "reported_model", "reported_nano_aiu", *_TOKEN_FIELDS})
_NATIVE_TOKENS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens")


def _identifier(value):
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def validate_usage_frame(frame) -> None:
    """Validate the exact public/storage shape without coercion or mutation."""
    if (type(frame) is not dict or set(frame) != _FIELDS or frame["type"] != "usage"
            or not _identifier(frame["event_id"]) or type(frame["reported_model"]) is not str
            or not 0 < len(frame["reported_model"]) <= 256
            or frame["reported_model"] != frame["reported_model"].strip()
            or not frame["reported_model"].isprintable()):
        raise ValueError("Invalid Copilot usage report")
    for field in _TOKEN_FIELDS:
        value = frame[field]
        if value is not None and (type(value) is not int or not 0 <= value <= _MAX_NUMBER):
            raise ValueError("Invalid Copilot usage report")
    value = frame["reported_nano_aiu"]
    if value is not None and (type(value) not in (int, float) or not 0 <= value <= _MAX_NUMBER):
        raise ValueError("Invalid Copilot usage report")


def normalize_usage_event(raw) -> dict | None:
    """Ignore context/checkpoint/child counters; missing metrics stay unknown."""
    if type(raw) is not dict:
        raise ValueError("Invalid Copilot usage event")
    if raw.get("type") != "assistant.usage":
        return None
    data = raw.get("data")
    if type(data) is not dict:
        raise ValueError("Invalid Copilot usage event")
    if raw.get("agentId") not in (None, "") or data.get("parentToolCallId") not in (None, ""):
        return None
    units = data.get("copilotUsage")
    if units is not None and type(units) is not dict:
        raise ValueError("Invalid Copilot usage event")
    frame = {"type": "usage", "event_id": raw.get("id"), "reported_model": data.get("model"),
             "reported_nano_aiu": units.get("totalNanoAiu") if units is not None else None,
             **{target: data.get(source) for target, source in zip(_TOKEN_FIELDS, _NATIVE_TOKENS, strict=True)}}
    validate_usage_frame(frame)
    return frame


class CopilotUsageObserver:
    """Bounded runtime UUID tombstones; persistence owns cross-runtime dedup."""

    def __init__(self, callback):
        if not callable(callback) or inspect.iscoroutinefunction(callback):
            raise ValueError("Copilot usage observer must be synchronous")
        self._callback = callback
        self._seen: dict[str, str] = {}

    def observe(self, raw) -> None:
        frame = normalize_usage_event(raw)
        if frame is None:
            return
        key = frame["event_id"]
        digest = hashlib.sha256(json.dumps(frame, sort_keys=True, separators=(",", ":"),
                                          allow_nan=False).encode()).hexdigest()
        previous = self._seen.get(key)
        if previous is not None:
            if previous != digest:
                raise ValueError("Copilot usage report identity changed")
            return
        if len(self._seen) >= 1000:
            raise ValueError("Copilot usage observation limit reached")
        # A callback may enqueue before raising. Never retry that uncertain
        # delivery; the owner fails closed and durable storage deduplicates IDs.
        self._seen[key] = digest
        result = self._callback(frame.copy())
        if result is not None:
            if inspect.iscoroutine(result):
                result.close()
            raise ValueError("Copilot usage observer must return synchronously")
