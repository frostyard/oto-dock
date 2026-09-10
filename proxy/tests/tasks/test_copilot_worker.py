"""Owned worker integration through the real scheduler, with no inference."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from auth.providers import UserContext
from core.execution_layer import AgentConfig
from core.session import session_state
from services.delegation import copilot_worker as workers
from services.scheduler import scheduler
from storage import agent_store, database, mcp_store, remote_store

_REAL_ADMITTED_SLOT = scheduler._admitted_slot


@pytest.fixture
def harness(monkeypatch):
    from core import execution_mode
    from core.config import task_config_builder
    from core.events import stream_pump, task_producer
    from core.session import session_manager
    from core.session import worker_ownership
    from services.billing import usage_service
    from services.knowledge import library_projector
    from services.mcp import mcp_registry

    h = SimpleNamespace(runs={}, chats={}, frames=[], order=[], configs=[], alive=False,
                        engine="codex-cli", scope="user", target="local", mcps=[], cap=4,
                        parent=True, finish=asyncio.Event(), start=asyncio.Event(), close=asyncio.Event(),
                        start_failure=False, close_failure=False, roles={"cto": "editor", "repo": "editor"})
    h.finish.set()
    h.close.set()
    h.user = UserContext(sub="alice", email="alice@example.test", name="Alice", role="member", agents=["cto", "repo"])
    monkeypatch.setattr(database, "get_user", lambda sub: {"sub": "alice", "email": "alice@example.test", "name": "Alice", "role": "member"})
    monkeypatch.setattr(database, "get_user_agent_roles", lambda sub: dict(h.roles))
    monkeypatch.setattr(database, "get_username_by_sub", lambda sub: "alice")
    monkeypatch.setattr(agent_store, "get_agent", lambda name: {"execution_path": h.engine, "collaborative": h.scope == "user", "default_scope": h.scope, "display_name": name})
    monkeypatch.setattr(agent_store, "is_admin_only", lambda name: False)
    monkeypatch.setattr(agent_store, "get_delegation_targets", lambda name: ["repo"])
    monkeypatch.setattr(mcp_store, "get_mcp_state", lambda name: {"enabled": True})
    monkeypatch.setattr(mcp_store, "get_mcp_config_values", lambda name: {"MAX_PARALLEL_SPAWNS": str(h.cap)})
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda name: [SimpleNamespace(name=n) for n in h.mcps])
    monkeypatch.setattr(execution_mode, "is_interactive", lambda **kw: False)
    monkeypatch.setattr(remote_store, "resolve_execution_target", lambda *a, **kw: (h.target, None))
    monkeypatch.setattr(workers.config, "get_cli_model", lambda *a, **kw: "child-model")
    monkeypatch.setattr(usage_service, "check_user_limit", lambda *a: {"allowed": True})
    monkeypatch.setattr(usage_service, "check_agent_limit", lambda *a: {"allowed": True})
    monkeypatch.setattr(session_state, "_sessions", {})
    monkeypatch.setattr(worker_ownership, "_owners", {})
    monkeypatch.setattr(workers, "_ADMISSION_LOCK", asyncio.Lock())
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)
    monkeypatch.setattr(scheduler, "_active_task_ids", {})
    monkeypatch.setattr(scheduler, "_running_tasks", {})
    monkeypatch.setattr(database, "count_active_delegate_runs", lambda sub: sum(r["status"] in {"pending", "running"} for r in h.runs.values()))
    def create_run(rid, tid, agent, trigger_type, trigger_source, prompt, task_type, scope, created_by):
        h.runs[rid] = dict(id=rid, task_id=tid, agent=agent, task_type=task_type, scope=scope,
                           created_by=created_by, status="pending")
    monkeypatch.setattr(database, "create_run", create_run)
    monkeypatch.setattr(database, "update_run", lambda rid, **kw: h.runs[rid].update(kw))
    monkeypatch.setattr(database, "get_run", h.runs.get)
    def create_chat(cid, owner, agent, *args, **kwargs):
        h.chats[cid] = dict(id=cid, user_sub=owner, agent=agent, **kwargs)
    monkeypatch.setattr(database, "create_chat", create_chat)
    monkeypatch.setattr(database, "update_chat", lambda cid, **kw: h.chats[cid].update(kw))
    monkeypatch.setattr(database, "get_chat", h.chats.get)
    monkeypatch.setattr(database, "get_last_chat_message_id", lambda cid: 0)
    monkeypatch.setattr(database, "add_chat_message", lambda *a, **kw: 1)
    monkeypatch.setattr(database, "get_chat_messages", lambda cid: [{"id": 2, "role": "assistant", "content": "worker report"}])
    monkeypatch.setattr(database, "get_active_meeting_for_chat", lambda cid: None)
    monkeypatch.setattr(database, "get_dynamic_task", lambda tid: None)
    monkeypatch.setattr(database, "delete_dynamic_task", lambda tid: None)
    monkeypatch.setattr(library_projector, "schedule_reconcile_for_agent", lambda agent: None)

    @asynccontextmanager
    async def admitted(*args):
        yield
    monkeypatch.setattr(scheduler, "_admitted_slot", admitted)
    async def build(agent, task, sid, **kw):
        h.order.append("config")
        cfg = AgentConfig(agent_name=agent, execution_path=h.engine, model="child-model",
                          subscription_user_sub="alice" if h.scope == "user" else "",
                          client_type="task")
        h.configs.append(cfg)
        return cfg
    monkeypatch.setattr(task_config_builder, "build_task_agent_config", build)
    class Layer:
        async def start_session(self, sid, cfg):
            h.order.append("start")
            class Native:
                session_id = sid
                proc = SimpleNamespace(returncode=None)
                async def close(self):
                    await h.close.wait()
                    if h.close_failure:
                        raise RuntimeError("PRIVATE native close failure")
                    self.proc.returncode = 0
                    h.alive = False
            h.native = Native()
            worker_ownership.capture_session(h.native)
            h.alive = True
            h.start.set()
            if h.start_failure:
                raise ValueError("PRIVATE PROVIDER STARTUP ERROR")
        async def close_session(self, sid):
            await h.close.wait()
            if h.close_failure:
                raise ValueError("PRIVATE CLEANUP ERROR")
            h.alive = False
        async def is_session_process_dead(self, sid):
            return not h.alive
    h.layer = Layer()
    monkeypatch.setattr(session_manager, "get_execution_layer", lambda *a, **kw: h.layer)
    async def produce(*a, **kw):
        await h.finish.wait()
    monkeypatch.setattr(task_producer, "task_produce", produce)
    class Pump:
        def __init__(self, **kw):
            self.producer = kw["producer"]
            self._task = None
            self.last_error = None
            self.is_done = False
        def start(self):
            self._task = asyncio.create_task(self.wait())
        async def wait(self):
            try:
                await asyncio.shield(self.producer)
            finally:
                self.is_done = True
        def abort(self):
            self.producer.cancel()
    monkeypatch.setattr(stream_pump, "ChatStreamPump", Pump)
    monkeypatch.setattr(stream_pump, "_active_pumps", {})
    async def watch(layer, pump, *a):
        await asyncio.shield(pump._task)
    monkeypatch.setattr(scheduler, "_watch_task_pump", watch)
    async def no_op(*a, **kw):
        pass
    monkeypatch.setattr(scheduler, "_close_interactive_task_session", no_op)
    async def publish(frame):
        assert frame["run_id"] in h.runs and frame["chat_id"] in h.chats
        h.order.append("publish")
        h.frames.append(frame)
    h.publish = publish
    h.authorize = no_op
    def owner(**kw):
        return workers.OwnedCopilotWorker(h.user, "cto", "repo", "QA", "Check the work",
                                          lambda: h.parent, h.authorize, h.publish, "tool-call", **kw)
    h.owner = owner
    return h


@pytest.mark.asyncio
async def test_real_scheduler_worker_publishes_before_config_and_joins(harness):
    h = harness
    h.cap = 1
    worker = h.owner()
    result = await worker.run()
    assert result["status"] == "completed" and result["output"] == "worker report"
    assert result["tool_id"] == "tool-call" and result["name"] == "QA"
    assert h.order == ["publish", "config", "start"]
    assert worker.closed and not h.alive and not scheduler._running_tasks
    assert worker._definition.on_complete_agent is None
    assert worker._definition.retry.max_attempts == 1 and worker._definition.continue_session is None
    assert h.configs[0].subscription_user_sub == "alice"
    assert not session_state._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("target", "remote-host"), ("engine", "copilot-cli"),
                                         ("mcps", ["delegation-mcp"]), ("mcps", ["meetings-mcp"])])
async def test_unsupported_targets_refuse_before_rows_or_runtime(harness, field, value):
    h = harness
    setattr(h, field, value)
    worker = h.owner()
    with pytest.raises(workers.CopilotWorkerError):
        await worker.run()
    assert not h.runs and not h.order and worker.closed


@pytest.mark.asyncio
async def test_rechecks_current_access_even_if_supplied_user_is_stale(harness):
    h = harness
    h.roles.pop("repo")
    with pytest.raises(workers.CopilotWorkerError):
        await h.owner().run()
    assert not h.runs


@pytest.mark.asyncio
async def test_shared_target_uses_platform_child_identity(harness):
    h = harness
    h.scope = "agent"
    worker = h.owner()
    await worker.run()
    assert h.configs[0].subscription_user_sub == ""
    assert h.runs[worker.run_id]["scope"] == "agent"


@pytest.mark.asyncio
async def test_parent_cancellation_joins_running_worker(harness):
    h = harness
    h.finish.clear()
    worker = h.owner()
    task = asyncio.create_task(worker.run())
    await h.start.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.closed and not h.alive and worker._runner.done()


@pytest.mark.asyncio
async def test_start_failure_never_returns_provider_error(harness):
    h = harness
    h.start_failure = True
    worker = h.owner()
    result = await worker.run()
    assert result["status"] == "failed" and "PRIVATE" not in str(result)
    assert worker.closed and not h.alive


@pytest.mark.asyncio
async def test_cleanup_failure_retains_owner(harness):
    h = harness
    h.close_failure = True
    worker = h.owner()
    with pytest.raises(workers.CopilotWorkerError, match="cleanup is incomplete"):
        await worker.run()
    assert not worker.closed and worker._layer is h.layer and h.alive
    with pytest.raises(workers.CopilotWorkerError):
        await worker.close()


@pytest.mark.asyncio
async def test_duplicate_run_cannot_repeat_side_effect(harness):
    worker = harness.owner()
    await worker.run()
    with pytest.raises(workers.CopilotWorkerError):
        await worker.run()
    assert len(harness.runs) == 1


@pytest.mark.asyncio
async def test_role_demotion_during_turn_closes_native_owner(harness):
    h = harness
    h.finish.clear()
    worker = h.owner()
    task = asyncio.create_task(worker.run())
    await h.start.wait()
    h.roles["repo"] = "viewer"  # Still a member, but a different security context.
    with pytest.raises(workers.CopilotWorkerError):
        await asyncio.wait_for(task, 3)
    assert worker.closed and h.native.proc.returncode == 0


@pytest.mark.asyncio
async def test_parent_predicate_revocation_closes_paused_child(harness):
    h = harness
    h.finish.clear()
    worker = h.owner()
    task = asyncio.create_task(worker.run())
    await h.start.wait()
    h.parent = False
    with pytest.raises(workers.CopilotWorkerError):
        await asyncio.wait_for(task, 3)
    assert worker.closed and not h.alive


@pytest.mark.asyncio
async def test_session_jwt_cannot_bypass_nested_delegation_restriction(harness):
    from fastapi import HTTPException
    from services.delegation.spawn_authz import authorize_spawn
    h = harness
    h.finish.clear()
    worker = h.owner()
    task = asyncio.create_task(worker.run())
    await h.start.wait()
    token_user = UserContext(sub="alice", email="a@b", name="A", role="admin",
                             is_api_key=True, session_id=worker.session_id, agent="repo")
    with pytest.raises(HTTPException, match="Nested delegation"):
        authorize_spawn(token_user, target_agent="repo", requested_scope="user")
    h.finish.set()
    await task


@pytest.mark.asyncio
async def test_concurrent_parents_share_cap_one_admission(harness):
    h = harness
    h.cap = 1
    h.finish.clear()
    first, second = h.owner(), h.owner()
    task = asyncio.create_task(first.run())
    await h.start.wait()
    with pytest.raises(workers.CopilotWorkerError):
        await second.run()
    assert len(h.runs) == 1
    h.finish.set()
    await task


@pytest.mark.asyncio
async def test_publish_failure_leaves_no_pending_run_or_runtime(harness):
    h = harness
    async def fail(frame):
        raise ValueError("PRIVATE DATABASE ERROR")
    h.publish = fail
    worker = h.owner()
    with pytest.raises(workers.CopilotWorkerError):
        await worker.run()
    assert h.runs[worker.run_id]["status"] == "failed"
    assert not h.configs and not h.alive and worker.closed


@pytest.mark.asyncio
async def test_late_builder_retains_and_releases_its_subscription(harness, monkeypatch):
    from core.config import task_config_builder
    from storage import subscription_store
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()
    seats = []
    monkeypatch.setattr(subscription_store, "decrement_active_sessions", seats.append)
    async def build(*a, **kw):
        entered.set()
        await release.wait()
        return AgentConfig(agent_name="repo", subscription_id="child-seat")
    monkeypatch.setattr(task_config_builder, "build_task_agent_config", build)
    worker = h.owner()
    task = asyncio.create_task(worker.run())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not worker.closed and not worker._builder.cancelled()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.closed and seats == ["child-seat"] and not h.alive


@pytest.mark.asyncio
async def test_repeated_close_waiter_cancellation_does_not_cancel_cleanup(harness):
    h = harness
    h.finish.clear()
    worker = h.owner()
    running = asyncio.create_task(worker.run())
    await h.start.wait()
    h.close.clear()
    close = asyncio.create_task(worker.close())
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.sleep(0)
    assert not worker.closed and not worker._closing.cancelled()
    h.close.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    with pytest.raises(workers.CopilotWorkerError):
        await running
    assert worker.closed


@pytest.mark.asyncio
async def test_cleanup_timeout_keeps_task_and_native_owner_until_join(harness, monkeypatch):
    h = harness
    h.finish.clear()
    worker = h.owner()
    running = asyncio.create_task(worker.run())
    await h.start.wait()
    h.close.clear()
    monkeypatch.setattr(workers, "_CLEANUP_WAIT", 0.05)
    with pytest.raises(workers.CopilotWorkerError, match="cleanup is incomplete"):
        await worker.close()
    assert not worker.closed and not worker._closing.done() and worker._native is h.native
    h.close.set()
    await worker.close()
    with pytest.raises(workers.CopilotWorkerError):
        await running
    assert worker.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli"])
async def test_actual_engine_factory_captures_before_failed_start(engine, monkeypatch):
    from core.session.worker_ownership import session_capture
    from services.engines import subscription_pool
    from core import concurrency
    captured = []
    class Native:
        def __init__(self, **kw):
            self.session_id = kw["session_id"]
            self.proc = None
        async def start(self):
            assert captured == [self]
            self.proc = SimpleNamespace(returncode=None)
            raise RuntimeError("controlled startup failure")
        async def close(self):
            self.proc.returncode = 0
    monkeypatch.setattr(subscription_pool, "release_subscription", lambda sid: None)
    monkeypatch.setattr(concurrency, "release_chat_slot", lambda sid: None)
    marker = session_capture.set(captured.append)
    try:
        if engine == "claude-code-cli":
            from core.layers.cli import session
            monkeypatch.setattr(session, "_persistent_sessions", {})
            monkeypatch.setattr(session, "PersistentSession", Native)
            with pytest.raises(RuntimeError, match="controlled startup failure"):
                await session.get_or_create_persistent_session("owned-fixture", agent_prompt="fixture")
            assert "owned-fixture" not in session._persistent_sessions
        else:
            from core.layers.codex import session
            monkeypatch.setattr(session, "_codex_sessions", {})
            monkeypatch.setattr(session, "CodexAppServerSession", Native)
            with pytest.raises(RuntimeError, match="controlled startup failure"):
                await session.create_codex_session("owned-fixture", "repo", "fixture")
            assert "owned-fixture" not in session._codex_sessions
        assert len(captured) == 1
        await captured[0].close()
        assert captured[0].proc.returncode == 0
    finally:
        session_capture.reset(marker)


@pytest.mark.asyncio
async def test_failed_start_before_subscription_bind_releases_acquired_seat(harness, monkeypatch):
    from core.config import task_config_builder
    from services.engines import subscription_pool
    from storage import subscription_store
    h = harness
    original = task_config_builder.build_task_agent_config
    async def build(*a, **kw):
        cfg = await original(*a, **kw)
        cfg.subscription_id = "acquired-seat"
        return cfg
    seats = []
    monkeypatch.setattr(task_config_builder, "build_task_agent_config", build)
    monkeypatch.setattr(subscription_pool, "get_session_subscription", lambda sid: None)
    monkeypatch.setattr(subscription_store, "decrement_active_sessions", seats.append)
    h.start_failure = True
    worker = h.owner()
    result = await worker.run()
    assert result["status"] == "failed" and seats == ["acquired-seat"]
    await worker.close()
    assert seats == ["acquired-seat"] and worker.closed


@pytest.mark.parametrize("changes", [{"created_by": "bob"}, {"agent": "foreign"},
                                     {"status": "unknown"}, {"task_type": "scheduled"}])
def test_cap_subtraction_never_accepts_unrelated_reservation(harness, changes):
    from fastapi import HTTPException
    from services.delegation.spawn_authz import authorize_spawn
    h = harness
    h.runs["reservation"] = {"created_by": "alice", "agent": "repo", "status": "running",
                             "task_type": "delegate", **changes}
    with pytest.raises(HTTPException, match="reservation is unavailable"):
        authorize_spawn(h.user, target_agent="repo", requested_scope="user",
                        source_agent="cto", reserved_run_id="reservation")


@pytest.mark.asyncio
async def test_no_native_constructor_capture_cannot_claim_clean_start(harness, monkeypatch):
    h = harness
    async def bad_start(*a):
        h.alive = True
    monkeypatch.setattr(h.layer, "start_session", bad_start)
    worker = h.owner()
    with pytest.raises(workers.CopilotWorkerError, match="cleanup is incomplete"):
        await worker.run()
    assert not worker.closed and worker._layer is h.layer


@pytest.mark.asyncio
async def test_result_identity_mismatch_is_rejected(harness, monkeypatch):
    h = harness
    original = database.get_run
    def get(rid):
        row = original(rid)
        if row and row.get("status") == "completed":
            return {**row, "chat_id": "foreign"}
        return row
    monkeypatch.setattr(database, "get_run", get)
    worker = h.owner()
    with pytest.raises(workers.CopilotWorkerError):
        await worker.run()
    assert worker.closed and not h.alive


def test_completed_worker_does_not_reserve_another_slot_to_return_result(harness):
    from services.delegation.spawn_authz import authorize_spawn
    h = harness
    h.cap = 1
    h.runs["done"] = {"created_by": "alice", "agent": "repo", "status": "completed", "task_type": "delegate"}
    h.runs["next"] = {"created_by": "alice", "agent": "repo", "status": "running", "task_type": "delegate"}
    authz = authorize_spawn(h.user, target_agent="repo", requested_scope="user",
                            source_agent="cto", reserved_run_id="done")
    assert authz.created_by == "alice"


@pytest.mark.asyncio
async def test_layer_cleanup_error_still_joins_exact_native_process(harness, monkeypatch):
    h = harness
    async def fail(sid):
        raise RuntimeError("PRIVATE ancillary cleanup failure")
    monkeypatch.setattr(h.layer, "close_session", fail)
    worker = h.owner()
    with pytest.raises(workers.CopilotWorkerError, match="cleanup is incomplete"):
        await worker.run()
    assert not worker.closed and h.native.proc.returncode == 0 and not h.alive


@pytest.mark.asyncio
async def test_actual_scheduler_user_cancel_returns_terminal_result(harness):
    h = harness
    h.finish.clear()
    worker = h.owner()
    task = asyncio.create_task(worker.run())
    await h.start.wait()
    assert await scheduler.cancel_run(worker.run_id)
    result = await task
    assert result["status"] == "cancelled" and result["chat_id"] == worker.chat_id
    assert worker.closed and not h.alive


@pytest.mark.asyncio
async def test_actual_scheduler_queued_cancel_returns_terminal_result(harness, monkeypatch):
    from core import concurrency
    h = harness
    queued = asyncio.Event()
    @asynccontextmanager
    async def wait_slot(*a, **kw):
        queued.set()
        await asyncio.Event().wait()
        yield
    monkeypatch.setattr(concurrency, "task_slot", wait_slot)
    monkeypatch.setattr(scheduler, "_admitted_slot", _REAL_ADMITTED_SLOT)
    worker = h.owner()
    task = asyncio.create_task(worker.run())
    await queued.wait()
    assert await scheduler.cancel_run(worker.run_id)
    result = await task
    assert result["status"] == "cancelled" and result["chat_id"] == worker.chat_id
    assert worker.closed and not h.alive and not h.configs
