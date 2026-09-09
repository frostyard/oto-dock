"""Duplicate-reconnect race on the satellite connection registry.

When a satellite reconnects while the proxy still holds its old socket,
``register()`` replaces the dict entry and closes the old ws — and the OLD
handler's ``finally: deregister(...)`` then fires. Pre-fix it popped the
entry unconditionally, unregistering the LIVE connection and marking the
machine offline while the satellite kept its healthy new socket ("all
remote machines down" on the proxy, every satellite showing connected).

Status writes are OFF-loop since 2026-09-04 (the per-machine persister, see
``SatelliteConnectionManager._request_persist``): tests ``drain_persists()``
before asserting on the store mock, and the mock is patched at
``storage.remote_store`` because the persister resolves it at call time.
"""

import asyncio
from unittest.mock import patch

import pytest

from core.remote.satellite_connection import SatelliteConnectionManager


class _FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self, code=1000, reason=""):
        self.closed = True

    async def send_text(self, text):
        pass


def _register(mgr, machine_id, ws):
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        return asyncio.get_event_loop().run_until_complete(
            _register_async(mgr, machine_id, ws)
        )


async def _register_async(mgr, machine_id, ws):
    return await mgr.register(machine_id, ws, {})


@pytest.mark.asyncio
async def test_stale_deregister_does_not_evict_new_connection():
    mgr = SatelliteConnectionManager()
    with patch("storage.remote_store.update_machine_status") as status, \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        old_ws, new_ws = _FakeWS(), _FakeWS()
        old_conn = await mgr.register("m1", old_ws, {})
        new_conn = await mgr.register("m1", new_ws, {})  # duplicate
        assert old_ws.closed  # old socket closed by register
        assert await mgr.drain_persists()

        # The OLD handler's finally fires with ITS connection → no-op.
        status.reset_mock()
        await mgr.deregister("m1", expected=old_conn)
        assert await mgr.drain_persists()
        assert mgr.get_connection("m1") is new_conn
        # No "disconnected" status write from the stale path.
        assert not any(
            c.args[1] == "disconnected" for c in status.call_args_list
        )

        # The CURRENT handler's deregister still tears down for real.
        await mgr.deregister("m1", expected=new_conn)
        assert await mgr.drain_persists()
        assert mgr.get_connection("m1") is None
        assert status.call_args_list[-1].args[:2] == ("m1", "disconnected")


@pytest.mark.asyncio
async def test_duplicate_register_carries_inflight_sessions():
    mgr = SatelliteConnectionManager()
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        old_conn = await mgr.register("m1", _FakeWS(), {})
        q = asyncio.Queue()
        old_conn.session_queues["sid-1"] = q
        old_conn.session_execution_paths["sid-1"] = "claude-code-cli"

        new_conn = await mgr.register("m1", _FakeWS(), {})
        # Same queue OBJECT — producers keep their reference.
        assert new_conn.session_queues.get("sid-1") is q
        assert new_conn.session_execution_paths.get("sid-1") == "claude-code-cli"
        # Old writer task cancelled by register (its handler's deregister
        # is a no-op now).
        await asyncio.sleep(0)
        assert old_conn.writer_task.cancelled() or old_conn.writer_task.done()
        await mgr.deregister("m1", expected=new_conn)
        assert await mgr.drain_persists()


@pytest.mark.asyncio
async def test_unguarded_deregister_keeps_old_behavior():
    mgr = SatelliteConnectionManager()
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        await mgr.register("m1", _FakeWS(), {})
        await mgr.deregister("m1")  # no expected → unconditional
        assert mgr.get_connection("m1") is None
        assert await mgr.drain_persists()
