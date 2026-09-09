"""HTTP-over-WS tunnel — abort frame (satellite 0.5.115).

When the local client (hook script / MCP client) gives up on a tunneled
request, the satellite tells the platform with ``http_abort`` so the proxy
closes its upstream at once instead of holding the stream until its idle
sweep. Covers the two paths: client gone while waiting for the first frame
(polled liveness), and a mid-stream timeout.
"""

import asyncio

import aiohttp
import pytest
import pytest_asyncio

from satellite.transport import http_tunnel
from satellite.transport.http_tunnel import LocalTunnelServer


class FakeWSClient:
    def __init__(self):
        self._authenticated = True
        self.sent: list[dict] = []
        self.tunnel: LocalTunnelServer | None = None

    async def enqueue_send(self, msg: dict) -> None:
        self.sent.append(msg)


@pytest_asyncio.fixture
async def tunnel_pair(monkeypatch):
    monkeypatch.setattr(http_tunnel, "_CLIENT_POLL_S", 0.1)
    ws = FakeWSClient()
    tunnel = LocalTunnelServer(ws)
    ws.tunnel = tunnel
    port = await tunnel.start()
    yield ws, tunnel, port
    await tunnel.stop()


async def _wait_abort(ws: FakeWSClient, timeout: float = 5.0) -> dict | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for m in ws.sent:
            if m.get("type") == "http_abort":
                return m
        await asyncio.sleep(0.05)
    return None


@pytest.mark.asyncio
async def test_client_disconnect_while_waiting_sends_abort(tunnel_pair):
    ws, tunnel, port = tunnel_pair
    timeout = aiohttp.ClientTimeout(total=0.5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with pytest.raises(asyncio.TimeoutError):
            async with session.post(f"http://127.0.0.1:{port}/v1/hooks/permission",
                                    data=b"{}") as resp:
                await resp.read()
    # The request frame went out, then the client vanished → abort frame.
    req = next(m for m in ws.sent if m.get("type") == "http_request")
    abort = await _wait_abort(ws)
    assert abort is not None
    assert abort["stream_id"] == req["stream_id"]
    assert req["stream_id"] not in tunnel._streams


@pytest.mark.asyncio
async def test_first_frame_timeout_sends_abort(tunnel_pair):
    ws, tunnel, port = tunnel_pair
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/hooks/permission",
            data=b"{}", headers={"X-Tunnel-Timeout-S": "1"},
        ) as resp:
            assert resp.status == 504
    abort = await _wait_abort(ws, timeout=2.0)
    assert abort is not None
