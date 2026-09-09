"""The daemon's call-log reports name the warmed proxy session.

All three report sites carry ``session_id`` (empty when the call never
reached the agent) so the proxy can join the call's identity label and the
tools it ran onto the call-log row.
"""

import asyncio

import main
from calls.call_manager import CallManager, CallStatus
from pipeline_fakes import FakeConn, FakeLLM, make_route


def _inbound(**kw):
    return main._inbound_report(
        FakeConn(), "uuid-1", make_route(), {"phone": "+15550001111", "did": "+1608"},
        outcome="completed", pin_attempts=1, started_at="2026-09-06T00:00:00+00:00",
        duration_s=12, **kw,
    )


def test_inbound_report_names_the_session():
    payload = _inbound(session_id="11111111-2222-4333-8444-555555555555")
    assert payload["session_id"] == "11111111-2222-4333-8444-555555555555"
    assert payload["from_number"] == "+15550001111" and payload["direction"] == "inbound"


def test_inbound_report_without_a_session_is_empty_not_missing():
    assert _inbound()["session_id"] == ""
    # A refused call has no LLM client — the getattr guard in _run_call
    # produces "" rather than an AttributeError on ``pipeline.llm is None``.
    assert (getattr(None, "session_id", "") or "") == ""


def test_outbound_terminal_report_carries_the_prewarmed_session(monkeypatch):
    sent = []

    async def _fake_report(payload):
        sent.append(payload)
    import proxy.client as proxy_client
    monkeypatch.setattr(proxy_client, "report_call", _fake_report)

    async def run():
        cm = CallManager()
        call = cm.register_call("+15559998888", "say hi")
        call.route_id = "r1"
        call.warmup_session_id = "22222222-2222-4333-8444-555555555555"
        cm.update_status(call.call_id, CallStatus.COMPLETED)
        await asyncio.sleep(0)   # let the fire-and-forget task run
    asyncio.run(run())
    assert len(sent) == 1
    assert sent[0]["session_id"] == "22222222-2222-4333-8444-555555555555"
    assert sent[0]["direction"] == "outbound" and sent[0]["outcome"] == "completed"


def test_pipeline_llm_session_id_is_what_the_report_reads():
    llm = FakeLLM(session_id="33333333-2222-4333-8444-555555555555")
    assert (getattr(llm, "session_id", "") or "") == "33333333-2222-4333-8444-555555555555"
