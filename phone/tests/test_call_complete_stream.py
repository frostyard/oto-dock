"""[CALL_COMPLETE] streamed mid-reply: the caller hears the text BEFORE the
marker and nothing after it (live-hit 2026-09-07: the agent said goodbye,
emitted the marker, then appended a call report — the whole report was read
out before the hang-up). The tokens after the marker still reach the
transcript, and the post-stream detection still ends the call.
"""

import asyncio

from pipeline_fakes import FakeLLM, make_route


def _spoken(p) -> str:
    return "".join(text for text, _ in p.tts.text_chunks)


def test_text_after_the_marker_never_reaches_tts(make_pipeline):
    llm = FakeLLM(llm_mode="proxy", responses=[[
        "Perfect, the test is done. ", "Have a nice day! ",
        "[CALL_", "COMPLETE]", " Call report: ", "the caller confirmed ",
        "clear audio on both sides.",
    ]])
    p = make_pipeline(route=make_route(llm_mode="proxy"), llm=llm)
    p.state._warmup_done.set()

    asyncio.run(p._process_utterance("thanks, that was it"))

    spoken = _spoken(p)
    assert "Have a nice day!" in spoken
    assert "report" not in spoken.lower() and "confirmed" not in spoken
    assert "[CALL_COMPLETE]" not in spoken
    assert p.tts.text_chunks[-1][1] is True            # the context was closed
    assert p.state._call_complete is True
    assert "Call report" in p.state._full_response      # the transcript keeps it


def test_marker_at_the_very_end_still_speaks_the_farewell(make_pipeline):
    llm = FakeLLM(llm_mode="proxy", responses=[["Goodbye! ", "[CALL_COMPLETE]"]])
    p = make_pipeline(route=make_route(llm_mode="proxy"), llm=llm)
    p.state._warmup_done.set()

    asyncio.run(p._process_utterance("bye"))

    assert "Goodbye!" in _spoken(p)
    assert "[CALL_COMPLETE]" not in _spoken(p)
    assert p.state._call_complete is True
