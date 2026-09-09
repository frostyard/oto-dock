"""Tunnel stream lifecycle: idle sweep, absolute age cap, per-machine cap,
satellite abort frame, and the invariant that reaping CLOSES the upstream
(cancels the dispatch task) instead of merely forgetting the entry.

Before 2026-09-04 the sweeper cut every long-lived MCP streamable-HTTP GET
at the 15-min clamp ("swept leaked stream" every ~15 min on any machine with
tunneled HTTP MCPs) and left the httpx response open until upstream spoke.
"""

import asyncio
import time

import pytest

from core.remote import satellite_http_tunnel as tun
from core.remote.satellite_http_tunnel import (
    SatelliteHttpTunnelDispatcher,
    _HttpStream,
)


class FakeConnection:
    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg: dict) -> None:
        self.sent.append(msg)


class FakeManager:
    def __init__(self, conn):
        self.conn = conn

    def get_connection(self, machine_id):
        return self.conn


def _stream(machine_id="m1", stream_id="s1", *, created_ago=0.0, active_ago=None,
            timeout_s=30):
    now = time.monotonic()
    st = _HttpStream(stream_id=stream_id, machine_id=machine_id, timeout_s=timeout_s)
    st.created_at = now - created_ago
    st.last_activity = now - (active_ago if active_ago is not None else created_ago)
    return st


async def _park(stream: _HttpStream, closed: list) -> None:
    """Stand-in dispatch task: 'holds an upstream response' until cancelled."""
    stream.upstream_open = True
    try:
        await asyncio.Event().wait()
    finally:
        stream.upstream_open = False
        closed.append(stream.stream_id)


@pytest.mark.asyncio
async def test_sweep_spares_active_stream_reaps_idle_and_closes_it():
    disp = SatelliteHttpTunnelDispatcher()
    closed: list[str] = []
    # Old but still delivering: activity 5 s ago.
    live = _stream(stream_id="live", created_ago=3600, active_ago=5)
    # Old and silent: idle past timeout + grace.
    idle = _stream(stream_id="idle", created_ago=3600, active_ago=3600)
    for st in (live, idle):
        disp._streams[(st.machine_id, st.stream_id)] = st
        st.task = asyncio.create_task(_park(st, closed))
    await asyncio.sleep(0)

    reaped = disp._sweep_once()
    await asyncio.sleep(0)

    assert reaped == [("m1", "idle")]
    assert ("m1", "live") in disp._streams
    assert ("m1", "idle") not in disp._streams
    assert idle.cancel_event.is_set()
    assert closed == ["idle"]          # the task was cancelled → upstream closed
    assert idle.upstream_open is False
    assert live.upstream_open is True
    live.task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sweep_absolute_age_cap_reaps_even_active_streams():
    disp = SatelliteHttpTunnelDispatcher()
    ancient = _stream(stream_id="old", created_ago=tun._STREAM_MAX_AGE_S + 10, active_ago=1)
    disp._streams[("m1", "old")] = ancient
    assert disp._sweep_once() == [("m1", "old")]


@pytest.mark.asyncio
async def test_silent_open_stream_still_expires_at_the_clamp():
    """No abort frame from an old satellite: a silent-but-open stream keeps
    today's behaviour (expires after timeout + grace)."""
    disp = SatelliteHttpTunnelDispatcher()
    st = _stream(stream_id="silent", created_ago=tun._MAX_STREAM_TIMEOUT_S + tun._STREAM_GRACE_S + 5,
                 active_ago=tun._MAX_STREAM_TIMEOUT_S + tun._STREAM_GRACE_S + 5,
                 timeout_s=tun._MAX_STREAM_TIMEOUT_S)
    st.upstream_open = True
    disp._streams[("m1", "silent")] = st
    assert disp._sweep_once() == [("m1", "silent")]


@pytest.mark.asyncio
async def test_abort_frame_closes_stream():
    disp = SatelliteHttpTunnelDispatcher()
    closed: list[str] = []
    st = _stream(stream_id="s9")
    disp._streams[("m1", "s9")] = st
    st.task = asyncio.create_task(_park(st, closed))
    await asyncio.sleep(0)

    assert disp.abort_stream("m1", "s9") is True
    await asyncio.sleep(0)
    assert closed == ["s9"]
    assert ("m1", "s9") not in disp._streams
    assert disp.abort_stream("m1", "s9") is False  # idempotent / unknown


@pytest.mark.asyncio
async def test_cancel_machine_streams_closes_upstreams():
    disp = SatelliteHttpTunnelDispatcher()
    closed: list[str] = []
    for sid in ("a", "b"):
        st = _stream(stream_id=sid)
        disp._streams[("m1", sid)] = st
        st.task = asyncio.create_task(_park(st, closed))
    other = _stream(machine_id="m2", stream_id="c")
    disp._streams[("m2", "c")] = other
    other.task = asyncio.create_task(_park(other, closed))
    await asyncio.sleep(0)

    await disp.cancel_machine_streams(FakeManager(FakeConnection()), "m1")
    await asyncio.sleep(0)
    assert sorted(closed) == ["a", "b"]
    assert ("m2", "c") in disp._streams
    other.task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_per_machine_open_stream_cap_returns_503(monkeypatch):
    disp = SatelliteHttpTunnelDispatcher()
    conn = FakeConnection()
    mgr = FakeManager(conn)
    monkeypatch.setattr(tun, "_MAX_OPEN_STREAMS_PER_MACHINE", 2)
    for sid in ("a", "b"):
        disp._streams[("m1", sid)] = _stream(stream_id=sid)

    await disp.handle_request_frame(mgr, "m1", {
        "stream_id": "c", "method": "GET", "path": "/v1/hooks/permission",
        "headers": {}, "body_b64": "", "body_eof": True, "timeout_s": 30,
    })
    assert ("m1", "c") not in disp._streams
    assert conn.sent[-1]["type"] == "http_response"
    assert conn.sent[-1]["status"] == 503
    assert conn.sent[-1]["error"] == "too-many-streams"


def test_request_chunk_refreshes_activity():
    disp = SatelliteHttpTunnelDispatcher()
    st = _stream(stream_id="s1", created_ago=100, active_ago=100)
    disp._streams[("m1", "s1")] = st
    before = st.last_activity
    disp.handle_request_chunk("m1", {"stream_id": "s1", "body_b64": "", "body_eof": False})
    assert st.last_activity > before
