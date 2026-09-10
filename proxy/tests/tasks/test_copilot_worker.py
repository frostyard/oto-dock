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
    assert not worker.closed and not h.alive


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


def test_allocation_is_complete_stable_and_constructor_has_no_side_effects(harness):
    from uuid import UUID
    from core.session.worker_ownership import is_owned_worker
    h = harness
    worker = h.owner()
    allocation = worker.allocation()
    assert set(allocation) == {"task_id", "run_id", "session_id", "chat_id"}
    assert allocation["task_id"] == worker.task_id
    assert allocation["chat_id"] == f"task-{allocation['run_id']}"
    assert str(UUID(allocation["session_id"])) == allocation["session_id"]
    allocation["run_id"] = "changed"
    assert worker.allocation()["run_id"] == worker.run_id != "changed"
    assert not is_owned_worker(worker.session_id)
    assert not h.runs and not h.chats and not session_state._sessions


@pytest.mark.asyncio
async def test_unstarted_close_does_not_read_database_or_mutate_session_index(harness, monkeypatch):
    from core.session.worker_ownership import is_owned_worker
    h = harness
    outcomes = []
    async def observe(result):
        outcomes.append(result)
    worker = h.owner(outcome_observer=observe)
    def forbidden(*a, **kw):
        pytest.fail("Unstarted worker must not inspect or mutate database/session state")
    monkeypatch.setattr(database, "get_run", forbidden)
    monkeypatch.setattr(session_state, "_save_sessions", forbidden)
    h.parent = False
    await worker.close()
    await worker.close()
    assert worker.closed and len(outcomes) == 1
    assert outcomes[0]["execution_created"] is False
    assert outcomes[0]["status"] == "failed" and outcomes[0]["output"] == "Worker was not started."
    assert not is_owned_worker(worker.session_id)
    assert not h.runs and not h.chats and not h.order


@pytest.mark.asyncio
async def test_outcome_persisted_after_join_and_before_claim_release(harness):
    from core.session.worker_ownership import is_owned_worker
    h = harness
    entered, persist = asyncio.Event(), asyncio.Event()
    outcomes = []
    async def observe(result):
        assert not h.alive and h.native.proc.returncode == 0
        assert worker._runner.done() and worker._builder.done() and worker._producer.done()
        assert worker._pump._task.done()
        assert is_owned_worker(worker.session_id)
        outcomes.append(dict(result))
        entered.set()
        await persist.wait()
        result["output"] = "observer mutation must not change public result"
    worker = h.owner(outcome_observer=observe)
    allocation = worker.allocation()
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(entered.wait(), 2)
    assert not worker.closed and not task.done()
    assert h.frames[0]["run_id"] == allocation["run_id"]
    assert h.frames[0]["chat_id"] == allocation["chat_id"]
    assert h.runs[allocation["run_id"]]["task_id"] == allocation["task_id"]
    h.parent = False  # Receipt persistence must not depend on live parent authority.
    persist.set()
    result = await task
    assert result == outcomes[0] and result["execution_created"] is True
    assert result["output"] == "worker report"
    assert worker.closed and not is_owned_worker(worker.session_id)
    await worker.close()
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_outcome_failure_keeps_claim_after_actual_process_join(harness):
    from core.session.worker_ownership import is_owned_worker
    h = harness
    calls = []
    async def fail(result):
        calls.append(result)
        raise RuntimeError("PRIVATE ledger error")
    worker = h.owner(outcome_observer=fail)
    with pytest.raises(workers.CopilotWorkerError, match="^Copilot worker cleanup is incomplete$"):
        await worker.run()
    assert not worker.closed and not h.alive and h.native.proc.returncode == 0
    assert is_owned_worker(worker.session_id)
    with pytest.raises(workers.CopilotWorkerError, match="cleanup is incomplete"):
        await worker.close()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_parent_cancel_still_persists_cleaned_child_outcome(harness):
    from core.session.worker_ownership import is_owned_worker
    h = harness
    h.finish.clear()
    outcomes = []
    async def observe(result):
        assert not h.alive and worker._runner.done()
        outcomes.append(result)
    worker = h.owner(outcome_observer=observe)
    task = asyncio.create_task(worker.run())
    await h.start.wait()
    h.parent = False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.closed and not is_owned_worker(worker.session_id)
    assert len(outcomes) == 1 and outcomes[0]["execution_created"] is True
    assert outcomes[0]["status"] == h.runs[worker.run_id]["status"]
    assert outcomes[0]["status"] in {"failed", "cancelled"}


@pytest.mark.asyncio
async def test_close_waiter_cancel_during_outcome_write_does_not_cancel_write(harness):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def observe(result):
        calls.append(result)
        entered.set()
        await release.wait()
    worker = h.owner(outcome_observer=observe)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not worker.closed and not worker._closing.cancelled()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.closed and len(calls) == 1


@pytest.mark.asyncio
async def test_native_cleanup_failure_does_not_publish_outcome(harness):
    h = harness
    h.close_failure = True
    outcomes = []
    async def observe(result):
        outcomes.append(result)
    worker = h.owner(outcome_observer=observe)
    with pytest.raises(workers.CopilotWorkerError, match="cleanup is incomplete"):
        await worker.run()
    assert not worker.closed and not outcomes


@pytest.mark.asyncio
async def test_creation_failure_without_row_records_not_started(harness, monkeypatch):
    h = harness
    outcomes = []
    async def observe(result):
        outcomes.append(result)
    def fail(*a):
        raise RuntimeError("PRIVATE database error")
    monkeypatch.setattr(database, "create_run", fail)
    worker = h.owner(outcome_observer=observe)
    with pytest.raises(workers.CopilotWorkerError):
        await worker.run()
    assert worker.closed and not h.runs and not h.order
    assert len(outcomes) == 1 and outcomes[0]["execution_created"] is False


@pytest.mark.asyncio
async def test_commit_then_error_is_created_and_terminalized_without_redispatch(harness, monkeypatch):
    from services.billing import usage_service
    h = harness
    outcomes, creations = [], []
    original = database.create_run
    async def observe(result):
        outcomes.append(result)
    def committed_then_failed(*a):
        creations.append(a[0])
        original(*a)
        raise RuntimeError("PRIVATE ambiguous commit")
    # This catches the generic scheduler's usage-check exception fallback:
    # owned work must never try a second create after an uncertain first write.
    monkeypatch.setattr(usage_service, "check_user_limit", lambda *a: {"allowed": False})
    monkeypatch.setattr(database, "create_run", committed_then_failed)
    worker = h.owner(outcome_observer=observe)
    with pytest.raises(workers.CopilotWorkerError):
        await worker.run()
    assert worker.closed and creations == [worker.run_id] and not h.order
    assert h.runs[worker.run_id]["status"] == "failed"
    assert len(outcomes) == 1 and outcomes[0]["execution_created"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, {"id": "foreign"}, {"task_id": "foreign"},
                                  {"agent": "foreign"}, {"created_by": "foreign"},
                                  {"task_type": "scheduled"}, {"session_id": "foreign"},
                                  {"chat_id": "foreign"}, {"status": "unknown"}])
async def test_unverifiable_created_execution_cannot_publish_cleanup_outcome(harness, monkeypatch, change):
    from core.session.worker_ownership import is_owned_worker
    h = harness
    outcomes = []
    original = database.get_run
    async def observe(result):
        outcomes.append(result)
    def get(rid):
        row = original(rid)
        if row and row.get("status") == "completed":
            return None if change is None else {**row, **change}
        return row
    monkeypatch.setattr(database, "get_run", get)
    worker = h.owner(outcome_observer=observe)
    with pytest.raises(workers.CopilotWorkerError, match="cleanup is incomplete"):
        await worker.run()
    assert not worker.closed and not h.alive and not outcomes
    assert is_owned_worker(worker.session_id)


@pytest.mark.asyncio
async def test_scheduler_rejects_changed_preallocated_binding_before_allocation(harness, monkeypatch):
    h = harness
    worker = h.owner()
    original = worker.allocation()
    monkeypatch.setattr(worker, "allocation", lambda: {**original, "task_id": "foreign"})
    with pytest.raises(workers.CopilotWorkerError):
        await worker.run()
    assert worker.closed and not h.runs and not h.chats and not session_state._sessions


@pytest.mark.asyncio
async def test_cancel_during_run_creation_waits_for_commit_before_outcome(harness, monkeypatch):
    import threading
    h = harness
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = database.create_run
    outcomes = []
    def held_create(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        original(*args)
    async def observe(result):
        assert worker._startup.done()
        outcomes.append(result)
    monkeypatch.setattr(database, "create_run", held_create)
    worker = h.owner(outcome_observer=observe)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not worker.closed and not outcomes and not h.runs
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.closed and not h.order
    assert len(outcomes) == 1 and outcomes[0]["execution_created"] is True
    assert outcomes[0]["status"] == h.runs[worker.run_id]["status"] == "failed"


@pytest.mark.asyncio
async def test_outcome_is_not_published_until_unbound_subscription_is_released(harness, monkeypatch):
    from core.config import task_config_builder
    from services.engines import subscription_pool
    from storage import subscription_store
    h = harness
    h.start_failure = True
    original = task_config_builder.build_task_agent_config
    seats, outcomes = [], []
    async def build(*a, **kw):
        cfg = await original(*a, **kw)
        cfg.subscription_id = "acquired-seat"
        return cfg
    async def observe(result):
        assert seats == ["acquired-seat"]
        assert h.native.proc.returncode == 0
        outcomes.append(result)
    monkeypatch.setattr(task_config_builder, "build_task_agent_config", build)
    monkeypatch.setattr(subscription_pool, "get_session_subscription", lambda sid: None)
    monkeypatch.setattr(subscription_store, "decrement_active_sessions", seats.append)
    worker = h.owner(outcome_observer=observe)
    result = await worker.run()
    assert result == outcomes[0] and result["status"] == "failed"
    assert worker.closed and seats == ["acquired-seat"]


@pytest.mark.asyncio
async def test_foreign_pending_row_is_not_terminalized_or_reported(harness):
    h = harness
    outcomes = []
    async def observe(result):
        outcomes.append(result)
    async def publish(frame):
        h.runs[frame["run_id"]]["created_by"] = "foreign"
        raise RuntimeError("Publication stopped")
    h.publish = publish
    worker = h.owner(outcome_observer=observe)
    with pytest.raises(workers.CopilotWorkerError, match="cleanup is incomplete"):
        await worker.run()
    assert not worker.closed and not outcomes and not h.order
    assert h.runs[worker.run_id]["status"] == "pending"
