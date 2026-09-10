"""Atomic PostgreSQL recovery receipts plus explicit in-memory seam tests."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import uuid

import pytest

from storage import agent_store, copilot_conversation_store as history
from storage import copilot_delegation_store as store, schema
from storage.pg import get_conn

OWNER, OTHER = "user-admin", "user-viewer"


def identifier():
    return str(uuid.uuid4())


def allocation():
    run_id = "run-" + uuid.uuid4().hex[:12]
    return {"task_id": "dyn-" + uuid.uuid4().hex, "run_id": run_id,
            "session_id": identifier(), "chat_id": "task-" + run_id}


def arguments(**changes):
    return {"agent": "repo", "name": "Review", "prompt": "Review the fixture", **changes}


def result(receipt, **changes):
    return {**{key: receipt[key] for key in ("tool_id", "task_id", "run_id", "chat_id", "agent", "name")},
            "status": "completed", "output": "Reviewed fixture", "execution_created": True, **changes}


def reserve(row, tool_id="call-one", args=None, alloc=None):
    return store.reserve(row["id"], OWNER, row["generation"], tool_id, args or arguments(), alloc or allocation())


@pytest.fixture
def conversation():
    agent_store.create_agent("copilot-ledger", "Copilot ledger fixture")
    row = history.create(identifier(), OWNER, agent="copilot-ledger", account_id=identifier(),
                         model="fixture", permission_mode="default", platform_session_id=identifier(),
                         generation=identifier(), delegation_enabled=True)
    history.begin_turn(row["id"], OWNER, row["generation"], "Start")
    return row


def test_reservation_atomic_receipt_and_public_unverified_projection(conversation):
    receipt = reserve(conversation)
    store.validate_receipt(receipt)
    events = history.events(conversation["id"], OWNER)
    assert [event["type"] for event in events] == ["user", "delegation_request"]
    assert events[-1]["prompt_digest"] == receipt["prompt_digest"]
    assert "prompt" not in receipt and "account_id" not in receipt
    public = store.list_outcomes(conversation["id"], OWNER)
    assert public == [{**{key: receipt[key] for key in ("tool_id", "task_id", "run_id", "chat_id", "agent", "name")},
                       "recovery_state": "unverified", "status": None, "output": None, "execution_created": None}]
    assert store.list_unsettled() == [receipt]
    assert not set(public[0]) & {"receipt_id", "session_id", "generation", "user_sub", "prompt_digest"}


def test_reservation_race_only_one_audit_and_allocation(conversation):
    barrier = threading.Barrier(2)

    def compete():
        barrier.wait()
        return reserve(conversation)

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda _: compete(), range(2)))
    assert sum(value is not None for value in outcomes) == 1
    assert len(store.list_outcomes(conversation["id"], OWNER)) == 1
    assert len(history.events(conversation["id"], OWNER)) == 2


def test_legacy_reservation_never_redispatches_or_creates_ledger(conversation):
    assert history.reserve_delegation(conversation["id"], OWNER, conversation["generation"], "call-one", arguments())
    before = history.get(conversation["id"], OWNER)
    assert reserve(conversation) is None
    assert store.list_outcomes(conversation["id"], OWNER) == []
    assert history.get(conversation["id"], OWNER) == before


def test_unique_allocation_failure_rolls_back_audit_and_counts(conversation):
    alloc = allocation()
    reserve(conversation, alloc=alloc)
    before = history.get(conversation["id"], OWNER)
    with pytest.raises(store.CopilotDelegationError):
        reserve(conversation, "another", alloc=alloc)
    assert history.get(conversation["id"], OWNER) == before
    assert len(history.events(conversation["id"], OWNER)) == 2
    assert len(store.list_outcomes(conversation["id"], OWNER)) == 1


def test_update_failure_rolls_back_ledger_insert_and_audit(conversation, monkeypatch):
    before = history.get(conversation["id"], OWNER)

    def fail(*_, **__):
        raise RuntimeError("private DB failure")

    monkeypatch.setattr(history, "_update", fail)
    with pytest.raises(store.CopilotDelegationError) as caught:
        reserve(conversation)
    assert caught.value.__context__ is None and "private" not in str(caught.value)
    assert history.get(conversation["id"], OWNER) == before
    assert store.list_outcomes(conversation["id"], OWNER) == []
    assert len(history.events(conversation["id"], OWNER)) == 1


@pytest.mark.parametrize("new_generation", [False, True])
def test_finalize_after_parent_close_or_generation_change_and_read_cold(conversation, new_generation):
    receipt = reserve(conversation)
    history.finish_turn(conversation["id"], OWNER, conversation["generation"])
    closed = history.finish_close(conversation["id"], OWNER, conversation["generation"], True)
    if new_generation:
        history.claim_resume(conversation["id"], OWNER, closed["revision"], identifier())
    before = history.get(conversation["id"], OWNER)
    answer = result(receipt)
    assert store.finish(receipt, answer) is True
    assert store.finish(deepcopy(receipt), deepcopy(answer)) is False
    assert history.get(conversation["id"], OWNER) == before
    assert store.list_unsettled() == []
    public = store.list_outcomes(conversation["id"], OWNER)[0]
    assert public["recovery_state"] == "settled" and public["output"] == answer["output"]
    assert public["execution_created"] is True and public["status"] == "completed"


def test_owner_generation_active_and_opt_in_fences(conversation):
    with pytest.raises(store.CopilotDelegationNotFound):
        store.reserve(conversation["id"], OTHER, conversation["generation"], "call", arguments(), allocation())
    with pytest.raises(store.CopilotDelegationConflict):
        store.reserve(conversation["id"], OWNER, identifier(), "call", arguments(), allocation())
    with pytest.raises(store.CopilotDelegationNotFound):
        store.list_outcomes(conversation["id"], OTHER)
    history.finish_turn(conversation["id"], OWNER, conversation["generation"])
    with pytest.raises(store.CopilotDelegationConflict):
        reserve(conversation)
    history.begin_turn(conversation["id"], OWNER, conversation["generation"], "Next")
    with get_conn() as conn:
        conn.execute("UPDATE copilot_conversations SET delegation_enabled=FALSE WHERE id=%s", (conversation["id"],))
        conn.commit()
    with pytest.raises(store.CopilotDelegationConflict):
        reserve(conversation)


def test_receipt_mismatch_and_conflicting_terminal_never_overwrite(conversation):
    receipt = reserve(conversation)
    changed = {**receipt, "generation": identifier()}
    with pytest.raises(store.CopilotDelegationConflict):
        store.finish(changed, result(changed))
    assert store.finish(receipt, result(receipt))
    with pytest.raises(store.CopilotDelegationConflict):
        store.finish(receipt, result(receipt, status="failed", output="different"))
    assert store.list_outcomes(conversation["id"], OWNER)[0]["output"] == "Reviewed fixture"


def test_competing_terminal_updates_have_one_winner(conversation):
    receipt = reserve(conversation)
    barrier = threading.Barrier(2)

    def finish(output):
        barrier.wait()
        try:
            return store.finish(receipt, result(receipt, output=output))
        except store.CopilotDelegationConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        values = list(pool.map(finish, ("one", "two")))
    assert values.count(True) == 1 and values.count("conflict") == 1


def test_independent_ledger_and_public_budgets(conversation, monkeypatch):
    for index in range(store.MAX_DELEGATIONS):
        receipt = reserve(conversation, f"call-{index}")
        assert store.finish(receipt, result(receipt, output="\x01" * 16384))
    before = history.get(conversation["id"], OWNER)
    with pytest.raises(store.CopilotDelegationLimit):
        reserve(conversation, "overflow")
    assert history.get(conversation["id"], OWNER) == before
    public = store.list_outcomes(conversation["id"], OWNER)
    assert len(public) == 32 and len(json.dumps(public).encode()) < store.MAX_PUBLIC_BYTES
    assert before["event_bytes"] < history.MAX_BYTES  # Output uses its own explicit budget.
    monkeypatch.setattr(store, "MAX_PUBLIC_BYTES", 1)
    with pytest.raises(store.CopilotDelegationLimit):
        store.list_outcomes(conversation["id"], OWNER)


def test_unsettled_overflow_fails_without_partial_inventory(conversation, monkeypatch):
    reserve(conversation)
    reserve(conversation, "another")
    monkeypatch.setattr(store, "MAX_UNSETTLED", 1)
    with pytest.raises(store.CopilotDelegationLimit):
        store.list_unsettled()


def test_schema_initialization_preserves_existing_receipt(conversation):
    receipt = reserve(conversation)
    with get_conn() as conn:
        schema.init_copilot_delegations(conn)
        schema.init_copilot_delegations(conn)
        conn.commit()
    assert store.list_unsettled() == [receipt]


@pytest.fixture
def memory():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts/copilot"))
    from conversation_fixture import MemoryConversations
    from delegation_fixture import MemoryDelegations
    h = MemoryConversations()
    row = h.create(identifier(), OWNER, agent="source", account_id=identifier(), model="fixture",
                   permission_mode="default", platform_session_id=identifier(), generation=identifier(), delegation_enabled=True)
    h.begin_turn(row["id"], OWNER, row["generation"], "Start")
    return h, MemoryDelegations(h), row


def test_memory_receipt_atomic_copy_isolation_and_post_close_finish(memory):
    h, ledger, row = memory
    receipt = ledger.reserve(row["id"], OWNER, row["generation"], "call", arguments(), allocation())
    assert ledger.reserve(row["id"], OWNER, row["generation"], "call", arguments(), allocation()) is None
    copy = deepcopy(receipt)
    receipt["name"] = "Tampered"
    assert ledger.list_unsettled() == [copy]
    h.finish_close(row["id"], OWNER, row["generation"], False)
    assert ledger.finish(copy, result(copy)) is True
    assert ledger.finish(copy, result(copy)) is False
    assert ledger.list_outcomes(row["id"], OWNER)[0]["recovery_state"] == "settled"
    with pytest.raises(store.CopilotDelegationConflict):
        ledger.finish(copy, result(copy, output="different"))


def test_memory_rollback_and_legacy_duplicate(memory, monkeypatch):
    h, ledger, row = memory
    assert h.reserve_delegation(row["id"], OWNER, row["generation"], "legacy", arguments())
    assert ledger.reserve(row["id"], OWNER, row["generation"], "legacy", arguments(), allocation()) is None
    before = h.get(row["id"], OWNER), h.events(row["id"], OWNER)

    def fail(*_, **__):
        raise ValueError("secret")

    monkeypatch.setattr(h, "_update", fail)
    with pytest.raises(store.CopilotDelegationError):
        ledger.reserve(row["id"], OWNER, row["generation"], "new", arguments(), allocation())
    assert (h.get(row["id"], OWNER), h.events(row["id"], OWNER)) == before
    assert ledger.list_outcomes(row["id"], OWNER) == []


def test_memory_limits_and_owner_fences(memory, monkeypatch):
    _, ledger, row = memory
    monkeypatch.setattr(store, "MAX_DELEGATIONS", 1)
    ledger.reserve(row["id"], OWNER, row["generation"], "call", arguments(), allocation())
    with pytest.raises(store.CopilotDelegationLimit):
        ledger.reserve(row["id"], OWNER, row["generation"], "other", arguments(), allocation())
    with pytest.raises(store.CopilotDelegationNotFound):
        ledger.list_outcomes(row["id"], OTHER)
    monkeypatch.setattr(store, "MAX_UNSETTLED", 0)
    with pytest.raises(store.CopilotDelegationLimit):
        ledger.list_unsettled()


@pytest.mark.parametrize("field,value", [("task_id", "dyn-wrong"), ("run_id", "run-wrong"),
                                          ("session_id", "bad"), ("chat_id", "other"), ("extra", "x")])
def test_validation_bad_allocation_never_opens_database(monkeypatch, field, value):
    accesses = []
    monkeypatch.setattr(history, "_connection", lambda: accesses.append(True))
    bad = {**allocation(), field: value}
    with pytest.raises(store.CopilotDelegationError) as caught:
        store.reserve(identifier(), OWNER, identifier(), "call", arguments(), bad)
    assert accesses == [] and caught.value.__context__ is None


@pytest.mark.parametrize("field,value", [("execution_created", 1), ("status", "running"), ("output", "x" * 16385),
                                          ("output", "\0private"), ("output", "\ud800"), ("session_id", "private")])
def test_validation_bad_result_never_opens_database(monkeypatch, field, value):
    receipt, _ = store._new_receipt({"id": identifier(), "user_sub": OWNER, "agent": "source"},
                                    identifier(), "call", arguments(), allocation())
    accesses = []
    monkeypatch.setattr(history, "_connection", lambda: accesses.append(True))
    with pytest.raises(store.CopilotDelegationError) as caught:
        store.finish(receipt, result(receipt, **{field: value}))
    assert accesses == [] and caught.value.__context__ is None and "private" not in str(caught.value)


def test_locked_parent_reservation_times_out_without_partial_audit(conversation, monkeypatch):
    monkeypatch.setattr(history, "_DB_TIMEOUT_MS", 100)
    with get_conn() as conn:
        conn.execute("SELECT id FROM copilot_conversations WHERE id=%s FOR UPDATE", (conversation["id"],))
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(reserve, conversation)
            with pytest.raises(store.CopilotDelegationError):
                pending.result(timeout=3)
        conn.rollback()
    assert store.list_outcomes(conversation["id"], OWNER) == []
    assert len(history.events(conversation["id"], OWNER)) == 1


def test_history_limit_rolls_back_before_any_allocation(conversation, monkeypatch):
    monkeypatch.setattr(history, "MAX_EVENTS", 1)
    with pytest.raises(store.CopilotDelegationLimit):
        reserve(conversation)
    assert store.list_outcomes(conversation["id"], OWNER) == []
    assert len(history.events(conversation["id"], OWNER)) == 1


@pytest.mark.parametrize("field,value", [("receipt_id", "not-uuid"), ("user_sub", " other "),
                                          ("source_agent", "../source"), ("agent", None),
                                          ("prompt_digest", "not-digest"), ("extra", "private")])
def test_validation_malformed_receipt_is_sanitized_before_database(monkeypatch, field, value):
    receipt, _ = store._new_receipt({"id": identifier(), "user_sub": OWNER, "agent": "source"},
                                    identifier(), "call", arguments(), allocation())
    answer = result(receipt)
    receipt[field] = value
    accesses = []
    monkeypatch.setattr(history, "_connection", lambda: accesses.append(True))
    with pytest.raises(store.CopilotDelegationError) as caught:
        store.finish(receipt, answer)
    assert accesses == [] and caught.value.__context__ is None and "private" not in str(caught.value)


@pytest.mark.parametrize("parent", ["source_agent", "user"])
def test_parent_deletion_keeps_private_quarantine_until_exact_finalization(conversation, parent):
    receipt = reserve(conversation)
    with get_conn() as conn:
        if parent == "source_agent":
            conn.execute("DELETE FROM agents WHERE slug=%s", (conversation["agent"],))
        else:
            conn.execute("DELETE FROM users WHERE sub=%s", (OWNER,))
        conn.commit()
    assert history.get(conversation["id"], OWNER) is None
    assert store.list_unsettled() == [receipt]
    with pytest.raises(store.CopilotDelegationNotFound):
        store.list_outcomes(conversation["id"], OWNER)
    forged = {**receipt, "generation": identifier()}
    with pytest.raises(store.CopilotDelegationConflict):
        store.finish(forged, result(forged))
    assert store.list_unsettled() == [receipt]
    assert store.finish(receipt, result(receipt)) is True
    assert store.finish(receipt, result(receipt)) is False
    assert store.list_unsettled() == []
    with pytest.raises(store.CopilotDelegationNotFound):
        store.list_outcomes(conversation["id"], OWNER)
    # Finalization retains an immutable recovery record; it never recreates a
    # deleted user, source agent, or parent conversation to make history visible.
    with get_conn() as conn:
        row = conn.execute("SELECT cleanup_joined,result_payload FROM copilot_delegations WHERE receipt_id=%s",
                           (receipt["receipt_id"],)).fetchone()
    assert row["cleanup_joined"] is True and json.loads(row["result_payload"]) == result(receipt)
    assert history.get(conversation["id"], OWNER) is None


def test_schema_upgrade_drops_old_cascades_without_erasing_receipt(conversation):
    receipt = reserve(conversation)
    with get_conn() as conn:
        # Simulate the initial ledger schema, then run the idempotent upgrade.
        conn.execute("""ALTER TABLE copilot_delegations
                        ADD CONSTRAINT copilot_delegations_conversation_id_fkey
                        FOREIGN KEY (conversation_id) REFERENCES copilot_conversations(id) ON DELETE CASCADE,
                        ADD CONSTRAINT copilot_delegations_user_sub_fkey
                        FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE""")
        schema.init_copilot_delegations(conn)
        schema.init_copilot_delegations(conn)
        conn.execute("DELETE FROM copilot_conversations WHERE id=%s", (conversation["id"],))
        conn.commit()
    assert store.list_unsettled() == [receipt]
    assert store.finish(receipt, result(receipt)) is True


def test_memory_deleted_parent_keeps_exact_receipt_authority(memory):
    h, ledger, row = memory
    receipt = ledger.reserve(row["id"], OWNER, row["generation"], "call", arguments(), allocation())
    del h.rows[row["id"]]
    del h.frames[row["id"]]
    assert ledger.list_unsettled() == [receipt]
    with pytest.raises(store.CopilotDelegationNotFound):
        ledger.list_outcomes(row["id"], OWNER)
    forged = {**receipt, "generation": identifier()}
    with pytest.raises(store.CopilotDelegationConflict):
        ledger.finish(forged, result(forged))
    assert ledger.finish(receipt, result(receipt)) is True
    assert ledger.finish(receipt, result(receipt)) is False
    assert ledger.list_unsettled() == []
    assert ledger.rows[receipt["receipt_id"]]["cleanup_joined"] is True
    assert not h.rows and not h.frames


def test_memory_malformed_unsettled_receipt_fails_instead_of_partial_inventory(memory):
    _, ledger, row = memory
    first = ledger.reserve(row["id"], OWNER, row["generation"], "first", arguments(), allocation())
    second = ledger.reserve(row["id"], OWNER, row["generation"], "second", arguments(), allocation())
    ledger.rows[second["receipt_id"]]["session_id"] = "malformed"
    with pytest.raises(store.CopilotDelegationError):
        ledger.list_unsettled()
    assert first["receipt_id"] in ledger.rows
