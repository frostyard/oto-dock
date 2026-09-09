"""The ordered per-chat off-loop writer (core/events/chat_writer.py).

Every job of one chat runs after the previous one, on the DB executor (never
the loop thread), never coalesced; chats are independent; a failing job
resolves its future with the exception and does not wedge the lane;
``drain`` waits for what was queued so far.
"""

import asyncio
import threading

import pytest

from core.events import chat_writer
from storage import database as task_store


@pytest.fixture(autouse=True)
def _clean_lanes():
    chat_writer.reset_for_tests()
    yield
    chat_writer.reset_for_tests()


@pytest.mark.asyncio
async def test_jobs_run_in_order_off_the_loop():
    loop_ident = threading.get_ident()
    seen: list[tuple[int, int]] = []

    def _job(i):
        def run():
            seen.append((i, threading.get_ident()))
            return i * 10
        return run

    futs = [chat_writer.submit("c1", _job(i), label=f"j{i}") for i in range(50)]
    assert chat_writer.pending("c1") >= 1
    results = await asyncio.gather(*futs)
    assert results == [i * 10 for i in range(50)]
    assert [i for i, _ in seen] == list(range(50))
    assert all(ident != loop_ident for _, ident in seen)
    assert await chat_writer.drain("c1", timeout=2)
    assert chat_writer.pending("c1") == 0


@pytest.mark.asyncio
async def test_chats_are_independent_but_each_ordered():
    seen: dict[str, list[int]] = {"a": [], "b": []}

    def _job(chat, i):
        def run():
            seen[chat].append(i)
        return run

    futs = []
    for i in range(20):
        futs.append(chat_writer.submit("a", _job("a", i)))
        futs.append(chat_writer.submit("b", _job("b", i)))
    await asyncio.gather(*futs)
    assert seen["a"] == list(range(20))
    assert seen["b"] == list(range(20))


@pytest.mark.asyncio
async def test_failed_job_does_not_wedge_the_lane():
    order: list[str] = []

    def ok(name):
        def run():
            order.append(name)
            return name
        return run

    def boom():
        order.append("boom")
        raise ValueError("nope")

    f1 = chat_writer.submit("c2", ok("first"))
    f2 = chat_writer.submit("c2", boom, label="boom")
    f3 = chat_writer.submit("c2", ok("third"))
    assert await f1 == "first"
    with pytest.raises(ValueError):
        await f2
    assert await f3 == "third"
    assert order == ["first", "boom", "third"]


@pytest.mark.asyncio
async def test_submit_during_a_running_job_joins_the_same_lane():
    gate = threading.Event()
    order: list[str] = []

    def slow():
        gate.wait(5)
        order.append("slow")

    def fast():
        order.append("fast")

    f1 = chat_writer.submit("c3", slow)
    await asyncio.sleep(0.05)  # the flusher is inside `slow`
    f2 = chat_writer.submit("c3", fast)
    assert chat_writer.pending("c3") == 2
    gate.set()
    await asyncio.gather(f1, f2)
    assert order == ["slow", "fast"]
    # The lane is dropped once idle and recreated on the next submit.
    assert chat_writer.pending("c3") == 0
    assert await chat_writer.submit("c3", lambda: "again") == "again"


@pytest.mark.asyncio
async def test_drain_waits_and_times_out():
    gate = threading.Event()
    chat_writer.submit("c4", lambda: gate.wait(5))
    assert not await chat_writer.drain("c4", timeout=0.1)
    gate.set()
    assert await chat_writer.drain("c4", timeout=2)
    assert await chat_writer.drain("never-used") is True


@pytest.mark.asyncio
async def test_drain_all_covers_every_lane():
    gates = [threading.Event() for _ in range(3)]
    for i, g in enumerate(gates):
        chat_writer.submit(f"d{i}", g.wait)
    assert not await chat_writer.drain_all(timeout=0.1)
    for g in gates:
        g.set()
    assert await chat_writer.drain_all(timeout=2)


def test_submit_needs_a_running_loop():
    with pytest.raises(RuntimeError):
        chat_writer.submit("c5", lambda: None)


@pytest.mark.asyncio
async def test_real_store_job_under_the_loop_guard(temp_db, loop_db_guard):
    temp_db.create_chat("cw1", "user-admin", "a1")

    def job():
        task_store.add_chat_message("cw1", "user", "hi")
        task_store.add_chat_message("cw1", "assistant", "hello")
        return task_store.get_last_chat_message_id("cw1")

    with loop_db_guard.active():
        last_id = await chat_writer.submit("cw1", job)
    rows = await asyncio.to_thread(temp_db.get_chat_messages, "cw1")
    assert [(r["role"], r["content"]) for r in rows] == [("user", "hi"), ("assistant", "hello")]
    assert rows[-1]["id"] == last_id
