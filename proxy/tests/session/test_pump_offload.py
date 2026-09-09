"""The stream pump's persistence runs off the event loop, in turn order.

Regression fence for the 2026-09-04 hardening slice: every store call the
pump makes rides the per-chat writer lane (``core/events/chat_writer.py``),
the cutoff/live-state seam moves only when a save lands, a superseding
pump's cutoff observes its predecessor's rows, and the pump leaves
``_active_pumps`` only after its rows are committed.
"""

import asyncio
import threading

import pytest

from core.events import chat_writer, stream_pump
from core.events.common_events import (
    CommonEvent, DONE, PRODUCER_DONE, QUEUE_TURN, SYSTEM, TEXT,
)
from core.events.stream_pump import ChatStreamPump
from storage import database as task_store


def _scripted_pump(chat_id: str, events: list[CommonEvent], **kw) -> ChatStreamPump:
    """A pump whose producer replays ``events`` then PRODUCER_DONE, yielding
    between events like a real layer does."""
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        for ev in events:
            await event_queue.put(ev)
            await asyncio.sleep(0)
        await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))
        await asyncio.sleep(3600)

    producer = asyncio.get_event_loop().create_task(_produce())
    pump = ChatStreamPump(
        chat_id=chat_id, session_id=f"sess-{chat_id}", producer=producer,
        event_queue=event_queue, perm_queue=None, **kw,
    )
    stream_pump._active_pumps[chat_id] = pump
    return pump


def _idle_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    return ChatStreamPump(
        chat_id=chat_id, session_id=f"sess-{chat_id}", producer=producer,
        event_queue=asyncio.Queue(), perm_queue=None,
    )


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


@pytest.fixture
def store_calls(monkeypatch):
    """Record (store function, thread ident, key arg) for the pump's writes."""
    calls: list[tuple[str, int, object]] = []
    real_add = task_store.add_chat_message
    real_upd = task_store.update_chat

    def rec_add(chat_id, role, *a, **k):
        calls.append(("add_chat_message", threading.get_ident(), role))
        return real_add(chat_id, role, *a, **k)

    def rec_upd(chat_id, **k):
        calls.append(("update_chat", threading.get_ident(), tuple(sorted(k))))
        return real_upd(chat_id, **k)

    monkeypatch.setattr(stream_pump.task_store, "add_chat_message", rec_add)
    monkeypatch.setattr(stream_pump.task_store, "update_chat", rec_upd)
    return calls


@pytest.mark.asyncio
async def test_full_turn_persists_off_loop_in_order(temp_db, loop_db_guard, store_calls):
    temp_db.create_chat("po1", "user-admin", "a1")
    task_store.add_chat_message("po1", "user", "hi")
    store_calls.clear()  # the seed row above is the test's own loop-side write
    loop_ident = threading.get_ident()
    pump = _scripted_pump(
        "po1", [CommonEvent(type=TEXT, data={"content": "Hello!"})],
        chat_owner="user-admin", chat_agent="a1",
    )
    q = pump.attach()
    try:
        with loop_db_guard.active():
            await pump.start()
        # The pump left the registry only after its rows landed: no drain
        # needed here, the DB already has them.
        assert "po1" not in stream_pump._active_pumps
        rows = [(m["role"], m["content"]) for m in task_store.get_chat_messages("po1")]
        assert rows == [("user", "hi"), ("assistant", "Hello!")]
        assert task_store.get_chat("po1")["last_response_at"]
        # Writer order: the assistant row before the turn-end stamp; every
        # write on a DB-executor thread, none on the loop.
        kinds = [(c[0], c[2]) for c in store_calls]
        assert ("add_chat_message", "assistant") in kinds
        assert kinds.index(("add_chat_message", "assistant")) < kinds.index(
            ("update_chat", ("last_response_at",)))
        assert all(c[1] != loop_ident for c in store_calls)
        # all_done reached the viewer AFTER the rows (it follows the drain),
        # then pump_ended.
        types = [f.get("pump_type") for f in _drain(q)]
        assert types.index("all_done") < types.index("pump_ended")
        assert pump._db_msg_cutoff_id == task_store.get_last_chat_message_id("po1")
    finally:
        stream_pump._chat_streaming_state.pop("po1", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_cutoff_is_none_until_the_start_job_lands(temp_db):
    temp_db.create_chat("po2", "user-admin", "a1")
    task_store.add_chat_message("po2", "user", "earlier")
    before = task_store.get_last_chat_message_id("po2")
    pump = _idle_pump("po2")
    try:
        assert pump._db_msg_cutoff_id is None  # withhold nothing meanwhile
        pump._submit_turn_start()
        assert await chat_writer.drain("po2", timeout=5)
        assert pump._db_msg_cutoff_id == before
    finally:
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_superseding_pump_cutoff_observes_predecessor_rows(temp_db):
    temp_db.create_chat("po3", "user-admin", "a1")
    first = _idle_pump("po3")
    second = _idle_pump("po3")
    try:
        first._turn_blocks.append({"type": "text", "content": "turn one"})
        first._save_turn_blocks()          # queued on the lane...
        second._submit_turn_start()        # ...and the next pump's start job behind it
        assert await chat_writer.drain("po3", timeout=5)
        last = task_store.get_last_chat_message_id("po3")
        assert first._db_msg_cutoff_id == last
        assert second._db_msg_cutoff_id == last  # sees turn one's row
    finally:
        first.producer.cancel()
        second.producer.cancel()


@pytest.mark.asyncio
async def test_live_blocks_trim_only_when_the_save_lands(temp_db, monkeypatch):
    temp_db.create_chat("po4", "user-admin", "a1")
    gate = threading.Event()
    real_add = task_store.add_chat_message

    def slow_add(*a, **k):
        gate.wait(5)
        return real_add(*a, **k)
    monkeypatch.setattr(stream_pump.task_store, "add_chat_message", slow_add)

    pump = _idle_pump("po4")
    live = {"live_blocks": [], "session_id": pump.session_id}
    stream_pump._chat_streaming_state["po4"] = live
    try:
        blk_x = {"type": "text", "content": "X"}
        pump._turn_blocks.append(blk_x)
        live["live_blocks"].append(blk_x)
        fut = pump._save_turn_blocks()
        # Job in flight: a viewer reconnecting now still sees X from live_state.
        blk_y = {"type": "text", "content": "Y"}
        live["live_blocks"].append(blk_y)
        await asyncio.sleep(0.05)
        assert live["live_blocks"] == [blk_x, blk_y]
        gate.set()
        await fut
        await asyncio.sleep(0)  # the done-callback runs on the loop
        # Exactly the persisted block was trimmed; the later one survives.
        assert live["live_blocks"] == [blk_y]
        assert [m["content"] for m in task_store.get_chat_messages("po4")] == ["X"]
    finally:
        stream_pump._chat_streaming_state.pop("po4", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_meeting_rows_keep_chronological_order(temp_db, loop_db_guard):
    temp_db.create_chat("meeting-po5", "user-admin", "a1")
    pump = _scripted_pump(
        "meeting-po5",
        [
            CommonEvent(type=SYSTEM, data={"subtype": "meeting_started", "participants": []}),
            CommonEvent(type=QUEUE_TURN, data={"text": "first prompt"}),
            CommonEvent(type=TEXT, data={"content": "answer"}),
            CommonEvent(type=DONE, data={}),
        ],
        chat_owner="user-admin", chat_agent="a1",
    )
    pump.attach()
    try:
        with loop_db_guard.active():
            await pump.start()
        rows = [(m["role"], m.get("event_type") or "", m["content"])
                for m in task_store.get_chat_messages("meeting-po5")]
        assert rows == [
            ("event", "system", ""),
            ("user", "", "first prompt"),
            ("assistant", "", "answer"),
        ]
    finally:
        stream_pump._chat_streaming_state.pop("meeting-po5", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_recovery_suppress_skips_the_save(temp_db):
    temp_db.create_chat("po6", "user-admin", "a1")
    pump = _idle_pump("po6")
    try:
        stream_pump.suppress_recovery_flush("po6")
        pump._turn_blocks.append({"type": "text", "content": "lost turn"})
        assert pump._save_turn_blocks() is None
        assert pump._turn_blocks == []
        assert await chat_writer.drain("po6", timeout=5)
        assert task_store.get_chat_messages("po6") == []
    finally:
        stream_pump._recovery_suppress_flush.discard("po6")
        pump.producer.cancel()
