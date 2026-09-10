"""Per-call reported metrics are strict, nullable and independent of turn queues."""

from copy import deepcopy
from pathlib import Path
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.usage import (  # noqa: E402
    CopilotUsageObserver, normalize_usage_event, validate_usage_frame,
)


def event(**data):
    return {"id": str(uuid.uuid4()), "type": "assistant.usage", "data": {"model": "reported-model", **data}}


def test_reported_metrics_preserve_unknown_and_do_not_convert_multiplier_or_sum_reasoning():
    raw = event(inputTokens=0, outputTokens=20, reasoningTokens=10, cost=1.5,
                copilotUsage={"totalNanoAiu": 2.25}, apiCallId="private-call")
    frame = normalize_usage_event(raw)
    assert frame == {"type": "usage", "event_id": raw["id"], "reported_model": "reported-model",
                     "input_tokens": 0, "output_tokens": 20, "reasoning_tokens": 10,
                     "cache_read_tokens": None, "cache_write_tokens": None, "reported_nano_aiu": 2.25}
    validate_usage_frame(frame)


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0.5, -1, 2 ** 53, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens"])
def test_token_coercion_is_rejected(field, value):
    with pytest.raises(ValueError, match="Invalid Copilot usage report"):
        normalize_usage_event(event(**{field: value}))


@pytest.mark.parametrize("value", [True, "1", -1, 2 ** 53, 10 ** 500, float("nan"), float("inf")])
def test_nano_aiu_invalid_without_overflow(value):
    with pytest.raises(ValueError):
        normalize_usage_event(event(copilotUsage={"totalNanoAiu": value}))


@pytest.mark.parametrize("model", [None, True, "", " spaced", "newline\n", "x" * 257])
def test_model_is_reported_valid_text_only(model):
    with pytest.raises(ValueError):
        normalize_usage_event(event(model=model))


def test_nonusage_and_children_never_contribute_even_with_invalid_counters():
    for kind in ("session.usage_info", "session.shutdown", "session.compaction_complete", "assistant.message"):
        assert normalize_usage_event({"type": kind, "data": {"inputTokens": 100}}) is None
    raw = event(inputTokens="invalid")
    assert normalize_usage_event({**raw, "agentId": "child"}) is None
    raw["data"]["parentToolCallId"] = "parent"
    assert normalize_usage_event(raw) is None


@pytest.mark.parametrize("identity", [None, 1, "event-1", "{6ffb85bf-23df-45aa-8f35-839c7c9761fe}",
                                      "6FFB85BF-23DF-45AA-8F35-839C7C9761FE"])
def test_uuid_must_be_canonical(identity):
    raw = event()
    raw["id"] = identity
    with pytest.raises(ValueError):
        normalize_usage_event(raw)


def test_frame_exact_keys_and_nullable_fields():
    frame = normalize_usage_event(event())
    for key in frame:
        incomplete = dict(frame)
        incomplete.pop(key)
        with pytest.raises(ValueError):
            validate_usage_frame(incomplete)
    with pytest.raises(ValueError):
        validate_usage_frame({**frame, "cost": 1})
    with pytest.raises(ValueError):
        normalize_usage_event(event(copilotUsage="private"))


def test_dedup_uses_snapshot_not_callback_mutation_or_ignored_vendor_fields():
    received = []

    def callback(frame):
        received.append(deepcopy(frame))
        frame["input_tokens"] = 999

    observer = CopilotUsageObserver(callback)
    raw = event(inputTokens=1)
    observer.observe(raw)
    raw["data"]["duration"] = 10
    observer.observe(raw)
    assert len(received) == 1 and received[0]["input_tokens"] == 1
    raw["data"]["inputTokens"] = 2
    with pytest.raises(ValueError, match="identity changed"):
        observer.observe(raw)
    assert len(received) == 1


def test_bounded_dedup_retains_first_uuid_and_never_retries_uncertain_callback():
    received = []
    observer = CopilotUsageObserver(received.append)
    first = event()
    observer.observe(first)
    for _ in range(999):
        observer.observe(event())
    observer.observe(first)
    with pytest.raises(ValueError, match="limit"):
        observer.observe(event())
    assert len(received) == 1000

    calls = []

    def fail(frame):
        calls.append(frame)
        raise RuntimeError("fixture")

    observer = CopilotUsageObserver(fail)
    with pytest.raises(RuntimeError):
        observer.observe(first)
    observer.observe(first)
    assert len(calls) == 1


def test_callback_must_be_synchronous_without_returned_awaitable():
    async def callback(frame):
        pass

    with pytest.raises(ValueError):
        CopilotUsageObserver(callback)
    with pytest.raises(ValueError):
        CopilotUsageObserver(lambda frame: callback(frame)).observe(event())  # noqa: PLW0108
    with pytest.raises(ValueError):
        CopilotUsageObserver(lambda frame: True).observe(event())
