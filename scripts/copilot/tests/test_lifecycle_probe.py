"""Offline lifecycle probe controls and report redaction, without SDK or inference."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location(
    "lifecycle_probe", Path(__file__).resolve().parents[1] / "lifecycle_probe.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def event(kind, **data):
    return SimpleNamespace(raw_type=kind, data=SimpleNamespace(**data))


def test_observations_only_record_known_content_and_never_raw_payload():
    observations = probe.Observations()
    secret = "sensitive-arbitrary-content"
    observations.on_event(event("user.message", content=secret, message_id="memory-only-id"))
    observations.on_event(event("assistant.message", content=secret))
    observations.on_event(event("assistant.message", content=probe.MARKER))
    observations.on_event(event("session.idle"))
    assert observations.marker.is_set() and observations.idle.is_set()
    public = json.dumps({"timeline": observations.timeline, "counts": dict(observations.counts)})
    assert secret not in public and "memory-only-id" not in public


def test_task_snapshot_omits_task_identity_and_untrusted_descriptions():
    task = SimpleNamespace(type="client", status=SimpleNamespace(value="running"),
                           sequence=3, id="private-id", description="private-description")
    assert probe.task_snapshot(SimpleNamespace(tasks=[task])) == [
        {"type": "client", "status": "running", "sequence": 3},
    ]


@pytest.mark.asyncio
async def test_observation_timeout_does_not_set_event():
    observed = asyncio.Event()
    assert not await probe.observed_within(observed, 0.001)
    assert not observed.is_set()
    observed.set()
    assert await probe.observed_within(observed, 0.001)


@pytest.mark.asyncio
async def test_controlled_callback_waits_and_records_external_cancellation():
    observations = probe.Observations()
    held = probe.HeldTool(observations)
    running = asyncio.create_task(held.handle(SimpleNamespace(arguments={})))
    await asyncio.wait_for(held.started.wait(), timeout=1)
    assert not held.finished.is_set()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert held.finished.is_set() and held.cancelled and held.calls == 1
    assert observations.timeline == ["callback.started", "callback.cancelled"]


@pytest.mark.asyncio
async def test_controlled_callback_rejects_arguments_before_start():
    held = probe.HeldTool(probe.Observations())
    with pytest.raises(AssertionError):
        await held.handle(SimpleNamespace(arguments={"command": "not-authorized"}))
    assert not held.started.is_set() and held.calls == 0
