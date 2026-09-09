"""phone-mcp ``make_call`` — the dialed number reaches the call API, never
the log.

The MCP code lives outside the proxy's import path and its entry module is
``make_call_server.py`` (not ``server.py``), so it is loaded by file
location here.
"""

import asyncio
import importlib.util
import logging

import httpx
import pytest

from tests._paths import CUSTOM_MCPS


@pytest.fixture
def server():
    path = CUSTOM_MCPS / "phone-mcp" / "make_call_server.py"
    spec = importlib.util.spec_from_file_location("phone_mcp_make_call_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_make_call_log_carries_no_number(server, monkeypatch, caplog):
    sent = {}

    def handler(request):
        sent["body"] = request.read()
        return httpx.Response(202, json={"call_id": "call-1"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        server.httpx, "AsyncClient",
        lambda **kw: real_client(transport=transport, **kw),
    )
    with caplog.at_level(logging.DEBUG, logger="make-call-mcp"):
        out = asyncio.run(server._handle_make_call({
            "phone_number": "+15550001111", "task_description": "say hi",
        }))
    assert "call-1" in out[0].text
    assert b"+15550001111" in sent["body"]   # the call API still gets it
    assert "Making call" in caplog.text
    assert "5550001111" not in caplog.text    # the log never does
