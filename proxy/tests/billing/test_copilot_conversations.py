"""PostgreSQL owner/generation isolation and atomic bounded Copilot history."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import uuid

import pytest

from storage import agent_store, copilot_conversation_store as store, schema
from storage.pg import get_conn


OWNER = "user-admin"
OTHER = "user-viewer"


def identifier():
    return str(uuid.uuid4())


@pytest.fixture
def conversation():
    agent_store.create_agent("copilot-history", "Copilot history fixture")
    return store.create(identifier(), OWNER, agent="copilot-history", account_id=identifier(),
                        model="fixture-model", permission_mode="default",
                        platform_session_id=identifier(), generation=identifier())


def begin(row, text="First user prompt"):
    return store.begin_turn(row["id"], OWNER, row["generation"], text)


def append(row, event=None):
    return store.append_event(row["id"], OWNER, row["generation"],
                              event or {"type": "text", "content": "response"})


def finish(row):
    return store.finish_turn(row["id"], OWNER, row["generation"])


def ready(row):
    begin(row)
    append(row)
    finish(row)
    return store.finish_close(row["id"], OWNER, row["generation"], True)


def test_lifecycle_order_metadata_and_no_generic_chat_rows(conversation):
    row = conversation
    assert row["state"] == "open" and row["revision"] == 1
    assert row["user_sub"] == OWNER and row["event_count"] == row["event_bytes"] == 0
    assert row["turn_active"] is False and row["last_turn_complete"] is False
    assert store.events(row["id"], OWNER) == []
    first = begin(row, " A first\n message ")
    assert first["title"] == "A first message" and first["revision"] == 2
    assert first["turn_active"] and not first["last_turn_complete"]
    append(row, {"type": "tool_use", "id": "tool-1", "name": "view", "input": {"path": "/workspace/file"}})
    append(row, {"type": "tool_result", "tool_use_id": "tool-1", "content": "file contents"})
    ended = finish(row)
    events = store.events(row["id"], OWNER)
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert [event["type"] for event in events] == ["user", "tool_use", "tool_result", "turn_complete"]
    assert ended["revision"] == 5 and ended["event_count"] == 4
    assert ended["last_turn_complete"] and not ended["turn_active"]
    encoded = [json.dumps({k: v for k, v in event.items() if k != "seq"}, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode() for event in events]
    assert ended["event_bytes"] == sum(map(len, encoded))
    closed = store.finish_close(row["id"], OWNER, row["generation"], True)
    assert closed["state"] == "closed" and closed["revision"] == 6
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM chats").fetchone()["count"] == 0
        assert conn.execute("SELECT COUNT(*) AS count FROM chat_messages").fetchone()["count"] == 0


def test_owner_scoped_reads_and_all_mutations_conceal_other_users(conversation):
    row = conversation
    assert store.get(row["id"], OTHER) is None
    assert store.list_conversations(OTHER) == []
    operations = [
        lambda: store.events(row["id"], OTHER),
        lambda: store.begin_turn(row["id"], OTHER, row["generation"], "attack"),
        lambda: store.append_event(row["id"], OTHER, row["generation"], {"type": "text", "content": "attack"}),
        lambda: store.finish_turn(row["id"], OTHER, row["generation"]),
        lambda: store.finish_close(row["id"], OTHER, row["generation"], False),
        lambda: store.claim_resume(row["id"], OTHER, row["revision"], identifier()),
    ]
    for operation in operations:
        with pytest.raises(store.CopilotConversationNotFound) as error:
            operation()
        assert error.value.__context__ is None
    assert store.get(row["id"], OWNER) == row


def test_resume_changes_generation_and_stale_mutations_cannot_touch_winner(conversation):
    closed = ready(conversation)
    new_generation = identifier()
    resumed = store.claim_resume(closed["id"], OWNER, closed["revision"], new_generation)
    assert resumed["state"] == "open" and resumed["generation"] == new_generation
    assert resumed["platform_session_id"] == conversation["platform_session_id"]
    assert resumed["account_id"] == conversation["account_id"]
    assert resumed["revision"] == closed["revision"] + 1
    for operation in [
        lambda: begin(conversation), lambda: append(conversation), lambda: finish(conversation),
        lambda: store.finish_close(closed["id"], OWNER, conversation["generation"], False),
    ]:
        with pytest.raises(store.CopilotConversationConflict):
            operation()
    assert store.get(closed["id"], OWNER) == resumed
    begin(resumed, "next turn")
    assert [event["seq"] for event in store.events(closed["id"], OWNER)] == [1, 2, 3, 4]


def test_resume_compare_and_swap_has_one_concurrent_winner(conversation):
    row = ready(conversation)
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        try:
            return store.claim_resume(row["id"], OWNER, row["revision"], identifier())
        except store.CopilotConversationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store.get(row["id"], OWNER) == winners[0]


def test_concurrent_begin_turn_appends_exactly_one_user_message(conversation):
    barrier = threading.Barrier(2)

    def start():
        barrier.wait(timeout=5)
        try:
            return begin(conversation)
        except store.CopilotConversationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: start(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert store.events(conversation["id"], OWNER) == [{"type": "user", "content": "First user prompt", "seq": 1}]


@pytest.mark.parametrize("state", ["open", "no_turn", "active", "explicit_incomplete"])
def test_only_closed_completed_conversations_may_resume(conversation, state):
    row = conversation
    if state == "active":
        begin(row)
    elif state == "explicit_incomplete":
        begin(row)
        finish(row)
    if state != "open":
        row = store.finish_close(row["id"], OWNER, row["generation"], state != "explicit_incomplete")
        assert row["state"] == "incomplete"
    with pytest.raises(store.CopilotConversationConflict):
        store.claim_resume(row["id"], OWNER, row["revision"], identifier())


def test_stale_revision_and_reused_generation_cannot_reopen(conversation):
    row = ready(conversation)
    for revision, generation in [(row["revision"] - 1, identifier()), (row["revision"], row["generation"])]:
        with pytest.raises(store.CopilotConversationConflict):
            store.claim_resume(row["id"], OWNER, revision, generation)
    assert store.get(row["id"], OWNER) == row


def test_duplicate_create_never_rebinds_metadata_or_platform_session(conversation):
    row = conversation
    for cid in [row["id"], identifier()]:
        with pytest.raises(store.CopilotConversationConflict):
            store.create(cid, OTHER, agent=row["agent"], account_id=identifier(), model="other-model",
                         permission_mode="plan", platform_session_id=row["platform_session_id"], generation=identifier())
    assert store.get(row["id"], OWNER) == row


def test_event_insert_and_lifecycle_failure_roll_back_together(conversation, monkeypatch):
    original = store._update

    def unavailable(*args, **kwargs):
        raise RuntimeError("private database failure and payload")

    monkeypatch.setattr(store, "_update", unavailable)
    with pytest.raises(store.CopilotConversationError) as error:
        begin(conversation, "must roll back")
    assert "private" not in str(error.value) and error.value.__context__ is None
    monkeypatch.setattr(store, "_update", original)
    assert store.get(conversation["id"], OWNER) == conversation
    assert store.events(conversation["id"], OWNER) == []


@pytest.mark.parametrize("event", [
    {"type": "done"}, {"type": "user", "content": "spoofed"}, {"type": "turn_complete"},
    {"type": "thinking"}, {"type": "text", "seq": 999},
    {"type": "text", "content": float("nan")}, {"type": "text", "content": float("inf")},
    {"type": "text", "input": {1: "not a JSON object key"}},
    {"type": "text", "content": ("tuple",)}, {"type": "text", "content": "nul\x00value"},
])
def test_malformed_and_unapproved_events_cannot_change_history(conversation, event):
    prior = begin(conversation)
    with pytest.raises(store.CopilotConversationError):
        append(prior, event)
    assert store.get(prior["id"], OWNER) == prior
    assert len(store.events(prior["id"], OWNER)) == 1


def test_frame_and_cumulative_encoded_byte_caps(conversation):
    row = begin(conversation)
    overhead = len(json.dumps({"type": "text", "content": ""}, separators=(",", ":")).encode())
    with pytest.raises(store.CopilotConversationLimit):
        append(row, {"type": "text", "content": "x" * (store.MAX_FRAME_BYTES - overhead + 1)})
    assert store.get(row["id"], OWNER) == row
    while row["event_bytes"] < store.MAX_BYTES:
        size = min(store.MAX_FRAME_BYTES, store.MAX_BYTES - row["event_bytes"])
        row = append(row, {"type": "text", "content": "x" * (size - overhead)})
    assert row["event_bytes"] == store.MAX_BYTES
    with pytest.raises(store.CopilotConversationLimit):
        finish(row)
    assert store.get(row["id"], OWNER) == row
    assert row["turn_active"] and not row["last_turn_complete"]


def test_event_count_cap_never_records_false_completion(conversation):
    row = begin(conversation)
    payload = '{"type":"text","content":"x"}'
    with get_conn() as conn:
        conn.execute("""INSERT INTO copilot_conversation_events (conversation_id,seq,payload,created_at)
                        SELECT %s, seq, %s, %s FROM generate_series(2,999) AS seq""",
                     (row["id"], payload, row["created_at"]))
        conn.execute("UPDATE copilot_conversations SET event_count=999,event_bytes=event_bytes + %s WHERE id=%s",
                     (998 * len(payload.encode()), row["id"]))
        conn.commit()
    row = append(row)
    assert row["event_count"] == 1000
    with pytest.raises(store.CopilotConversationLimit):
        finish(row)
    assert len(store.events(row["id"], OWNER)) == 1000
    assert store.get(row["id"], OWNER) == row


def test_pagination_is_owner_scoped_and_stable_on_timestamp_ties(conversation):
    ids = [conversation["id"]]
    for owner in [OWNER, OTHER, OWNER]:
        row = store.create(identifier(), owner, agent=conversation["agent"], account_id=identifier(),
                           model="model", permission_mode="default", platform_session_id=identifier(), generation=identifier())
        if owner == OWNER:
            ids.append(row["id"])
    with get_conn() as conn:
        conn.execute("UPDATE copilot_conversations SET updated_at=%s", (conversation["updated_at"],))
        conn.commit()
    expected = sorted(ids, reverse=True)
    assert [row["id"] for row in store.list_conversations(OWNER, 2)] == expected[:2]
    assert [row["id"] for row in store.list_conversations(OWNER, 2, 2)] == expected[2:]
    assert len(store.list_conversations(OTHER)) == 1


def test_schema_initializer_is_idempotent_and_preserves_history(conversation):
    begin(conversation)
    prior = store.get(conversation["id"], OWNER)
    with get_conn() as conn:
        schema.init_copilot_conversations(conn)
        schema.init_copilot_conversations(conn)
        conn.commit()
    assert store.get(conversation["id"], OWNER) == prior
    assert len(store.events(conversation["id"], OWNER)) == 1


def test_account_disconnect_preserves_owned_history(conversation):
    from core.layers.copilot.credentials import CredentialKind
    from storage import copilot_account_store

    account = copilot_account_store.create_account(
        OWNER, "github:user:123", CredentialKind.USER_TOKEN, "gho_offline_fixture", None)
    with get_conn() as conn:
        conn.execute("UPDATE copilot_conversations SET account_id=%s WHERE id=%s",
                     (account["id"], conversation["id"]))
        conn.commit()
    begin(conversation)
    copilot_account_store.delete_owned_account(account["id"], OWNER)
    assert store.get(conversation["id"], OWNER)["account_id"] == account["id"]
    assert len(store.events(conversation["id"], OWNER)) == 1


def test_inactive_or_closed_turn_never_accepts_duplicate_output(conversation):
    for operation in [lambda: append(conversation), lambda: finish(conversation)]:
        with pytest.raises(store.CopilotConversationConflict):
            operation()
    closed = ready(conversation)
    for operation in [lambda: begin(closed), lambda: append(closed), lambda: finish(closed),
                      lambda: store.finish_close(closed["id"], OWNER, closed["generation"], False)]:
        with pytest.raises(store.CopilotConversationConflict):
            operation()
    assert store.get(closed["id"], OWNER) == closed


def test_user_byte_cap_accepts_boundary_and_rejects_unicode_overflow(conversation):
    with pytest.raises(store.CopilotConversationLimit):
        begin(conversation, "🌲" * 16385)
    assert store.get(conversation["id"], OWNER) == conversation
    row = begin(conversation, "🌲" * 16384)
    assert row["turn_active"] and row["event_count"] == 1


@pytest.mark.parametrize("operation", ["begin_turn", "events"])
def test_locked_history_reads_and_mutations_time_out_without_changes(conversation, monkeypatch, operation):
    monkeypatch.setattr(store, "_DB_TIMEOUT_MS", 100)
    with get_conn() as blocker:
        blocker.execute("SELECT id FROM copilot_conversations WHERE id=%s FOR UPDATE", (conversation["id"],))
        started = time.monotonic()
        with pytest.raises(store.CopilotConversationError) as failure:
            if operation == "events":
                store.events(conversation["id"], OWNER)
            else:
                begin(conversation)
        assert time.monotonic() - started < 3
        assert str(failure.value) == "Copilot conversation request is unavailable"
        assert failure.value.__context__ is None
    assert store.get(conversation["id"], OWNER) == conversation
    assert store.events(conversation["id"], OWNER) == []
    # A timed-out borrower has rolled back and remains usable afterward.
    assert begin(conversation)["turn_active"] is True


@pytest.mark.parametrize("rollback", [False, True])
def test_transaction_timeouts_reset_on_the_same_connection(monkeypatch, rollback):
    from contextlib import contextmanager

    monkeypatch.setattr(store, "_DB_TIMEOUT_MS", 100)
    with get_conn() as conn:
        query = "SELECT current_setting('statement_timeout') AS statement, current_setting('lock_timeout') AS lock"
        prior = conn.execute(query).fetchone()
        conn.commit()

        @contextmanager
        def borrowed():
            yield conn

        monkeypatch.setattr(store, "get_conn", borrowed)
        with store._connection() as selected:
            assert selected is conn
            assert conn.execute(query).fetchone() == {"statement": "100ms", "lock": "100ms"}
            if rollback:
                conn.rollback()
            else:
                conn.commit()
        assert conn.execute(query).fetchone() == prior


def test_slow_statement_is_cancelled_and_rolled_back(monkeypatch):
    from psycopg.errors import QueryCanceled

    monkeypatch.setattr(store, "_DB_TIMEOUT_MS", 100)
    started = time.monotonic()
    with pytest.raises(QueryCanceled):
        with store._connection() as conn:
            conn.execute("SELECT pg_sleep(5)")
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("entity", ["users", "agents"])
def test_owner_and_agent_deletion_cascade_isolated_history(conversation, entity):
    begin(conversation)
    with get_conn() as conn:
        if entity == "users":
            conn.execute("DELETE FROM users WHERE sub=%s", (OWNER,))
        else:
            conn.execute("DELETE FROM agents WHERE slug=%s", (conversation["agent"],))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS count FROM copilot_conversation_events").fetchone()["count"] == 0
    assert store.get(conversation["id"], OWNER) is None


# These validation checks run without PostgreSQL as well as in the full suite.
@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (True, 0), (20, -1), (20, 10001), (20, False)])
def test_validation_pagination_rejects_before_database(monkeypatch, limit, offset):
    calls = []

    def forbidden():
        calls.append(True)
        raise AssertionError("Database must not be opened for invalid input")

    monkeypatch.setattr(store, "get_conn", forbidden)
    with pytest.raises(store.CopilotConversationError) as error:
        store.list_conversations(OWNER, limit, offset)
    assert not calls and error.value.__context__ is None


@pytest.mark.parametrize("event", [
    {"type": "text", "content": float("nan")}, {"type": "text", "value": {1: "bad key"}},
    {"type": "text", "value": {"bad": object()}}, {"type": "text", "value": (1,)},
    {"type": "text", "value": "\x00"},
])
def test_validation_json_is_strict_and_finite(event):
    with pytest.raises(store.CopilotConversationError):
        store._encoded(event)


def test_validation_unicode_frame_size_uses_encoded_bytes():
    with pytest.raises(store.CopilotConversationLimit):
        store._encoded({"type": "text", "content": "🌲" * 65536})


@pytest.mark.parametrize("operation", ["create", "begin", "append", "finish", "close", "resume"])
@pytest.mark.parametrize("generation", [None, "", True, "not-a-uuid"])
def test_validation_mutations_require_explicit_uuid_generation(monkeypatch, operation, generation):
    calls = []

    def forbidden():
        calls.append(True)
        raise AssertionError("Invalid generation must not reach the database")

    monkeypatch.setattr(store, "get_conn", forbidden)
    cid = identifier()
    operations = {
        "create": lambda: store.create(cid, OWNER, agent="agent", account_id=identifier(), model="model",
                                        permission_mode="default", platform_session_id=identifier(), generation=generation),
        "begin": lambda: store.begin_turn(cid, OWNER, generation, "text"),
        "append": lambda: store.append_event(cid, OWNER, generation, {"type": "text", "content": "value"}),
        "finish": lambda: store.finish_turn(cid, OWNER, generation),
        "close": lambda: store.finish_close(cid, OWNER, generation, True),
        "resume": lambda: store.claim_resume(cid, OWNER, 1, generation),
    }
    with pytest.raises(store.CopilotConversationError) as error:
        operations[operation]()
    assert not calls and error.value.__context__ is None


def test_agent_filter_precedes_pagination_and_never_crosses_owner(conversation):
    agent_store.create_agent('other-history', 'Other history')
    # Fill newer rows on a different agent before the original owner's row.
    for owner, agent in [(OWNER, 'other-history'), (OTHER, 'copilot-history')]:
        store.create(identifier(), owner, agent=agent, account_id=identifier(), model='model',
                     permission_mode='default', platform_session_id=identifier(), generation=identifier())
    rows = store.list_conversations(OWNER, limit=1, agent='copilot-history')
    assert [row['id'] for row in rows] == [conversation['id']]
    assert store.list_conversations(OWNER, limit=1, offset=1, agent='copilot-history') == []
    assert store.list_conversations(OWNER, agent='absent-agent') == []
    assert len(store.list_conversations(OWNER)) == 2


@pytest.mark.parametrize('agent', ['', '../escape', 'bad/agent', 'bad\\agent', 'a' * 65, True])
def test_validation_agent_filter_rejects_before_database(monkeypatch, agent):
    def forbidden():
        pytest.fail('Invalid agent must not open storage')
    monkeypatch.setattr(store, 'get_conn', forbidden)
    with pytest.raises(store.CopilotConversationError):
        store.list_conversations(OWNER, agent=agent)


@pytest.mark.parametrize('effort', [None, 'low', 'medium', 'high', 'xhigh', 'max'])
def test_reasoning_effort_is_immutable_metadata_across_turn_and_resume(conversation, effort):
    row = store.create(identifier(), OWNER, agent=conversation['agent'], account_id=identifier(),
                       model='reasoning-model', permission_mode='default',
                       platform_session_id=identifier(), generation=identifier(), reasoning_effort=effort)
    assert row['reasoning_effort'] == effort
    closed = ready(row)
    claimed = store.claim_resume(row['id'], OWNER, closed['revision'], identifier())
    assert claimed['reasoning_effort'] == effort
    assert store.get(row['id'], OWNER)['reasoning_effort'] == effort
    assert next(item for item in store.list_conversations(OWNER) if item['id'] == row['id'])['reasoning_effort'] == effort


def test_reasoning_column_migration_preserves_existing_rows_and_events(conversation):
    row = ready(conversation)
    events = store.events(row['id'], OWNER)
    with get_conn() as conn:
        # Recreate the pre-effort schema without replacing its saved data.
        conn.execute('ALTER TABLE copilot_conversations DROP COLUMN reasoning_effort')
        schema.init_copilot_conversations(conn)
        schema.init_copilot_conversations(conn)
        conn.commit()
    assert store.get(row['id'], OWNER) == {**row, 'reasoning_effort': None}
    assert store.events(row['id'], OWNER) == events


def test_database_rejects_unreviewed_reasoning_level(conversation):
    from psycopg.errors import CheckViolation

    with pytest.raises(CheckViolation):
        with get_conn() as conn:
            conn.execute('UPDATE copilot_conversations SET reasoning_effort=%s WHERE id=%s',
                         ('unreviewed', conversation['id']))
    assert store.get(conversation['id'], OWNER)['reasoning_effort'] is None


@pytest.mark.parametrize('effort', ['', 'auto', 'minimal', ' high', 'HIGH', True, 1, [], {}])
def test_validation_reasoning_effort_rejects_before_database(monkeypatch, effort):
    def forbidden():
        pytest.fail('Invalid reasoning must not reach the database')
    monkeypatch.setattr(store, 'get_conn', forbidden)
    with pytest.raises(store.CopilotConversationError):
        store.create(identifier(), OWNER, agent='agent', account_id=identifier(), model='model',
                     permission_mode='default', platform_session_id=identifier(), generation=identifier(),
                     reasoning_effort=effort)


def usage_report():
    return dict(type='usage', event_id=identifier(), reported_model='actual-reported-model',
                input_tokens=0, output_tokens=12, cache_read_tokens=None, cache_write_tokens=None,
                reasoning_tokens=3, reported_nano_aiu=1.25)


def test_usage_after_completed_turn_preserves_completion_and_owned_attribution(conversation):
    begin(conversation)
    append(conversation)
    finished = finish(conversation)
    report = usage_report()
    assert store.append_usage(conversation['id'], OWNER, conversation['generation'], report) is True
    row = store.get(conversation['id'], OWNER)
    assert not row['turn_active'] and row['last_turn_complete']
    assert row['revision'] == finished['revision'] + 1
    for key in ('user_sub', 'agent', 'account_id', 'model', 'platform_session_id', 'generation'):
        assert row[key] == conversation[key]
    assert store.events(row['id'], OWNER)[-1] == {**report, 'seq': row['event_count']}
    closed = store.finish_close(row['id'], OWNER, row['generation'], True)
    assert closed['state'] == 'closed'
    with pytest.raises(store.CopilotConversationConflict):
        store.append_usage(row['id'], OWNER, row['generation'], usage_report())


def test_concurrent_duplicate_usage_has_one_atomic_insert(conversation):
    report = usage_report()
    barrier = threading.Barrier(2)

    def writer():
        barrier.wait(timeout=3)
        return store.append_usage(conversation['id'], OWNER, conversation['generation'], report)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: writer(), range(2)))
    assert sorted(results) == [False, True]
    row = store.get(conversation['id'], OWNER)
    assert row['event_count'] == 1 and row['revision'] == conversation['revision'] + 1
    assert store.events(row['id'], OWNER) == [{**report, 'seq': 1}]
    with pytest.raises(store.CopilotConversationConflict):
        store.append_usage(row['id'], OWNER, row['generation'], {**report, 'input_tokens': 1})
    assert store.get(row['id'], OWNER) == row


def test_usage_dedup_survives_resume_and_rejects_other_owner_or_generation(conversation):
    report = usage_report()
    assert store.append_usage(conversation['id'], OWNER, conversation['generation'], report)
    closed = ready(conversation)
    resumed = store.claim_resume(closed['id'], OWNER, closed['revision'], identifier())
    assert store.append_usage(resumed['id'], OWNER, resumed['generation'], report) is False
    assert store.get(resumed['id'], OWNER) == resumed
    with pytest.raises(store.CopilotConversationConflict):
        store.append_usage(resumed['id'], OWNER, conversation['generation'], usage_report())
    with pytest.raises(store.CopilotConversationNotFound):
        store.append_usage(resumed['id'], OTHER, resumed['generation'], usage_report())
    assert len([event for event in store.events(resumed['id'], OWNER) if event['type'] == 'usage']) == 1


def test_usage_shares_history_bound_and_duplicate_does_not_consume_capacity(conversation, monkeypatch):
    monkeypatch.setattr(store, 'MAX_EVENTS', 1)
    report = usage_report()
    assert store.append_usage(conversation['id'], OWNER, conversation['generation'], report)
    saved = store.get(conversation['id'], OWNER)
    assert store.append_usage(conversation['id'], OWNER, conversation['generation'], report) is False
    with pytest.raises(store.CopilotConversationLimit):
        store.append_usage(conversation['id'], OWNER, conversation['generation'], usage_report())
    assert store.get(conversation['id'], OWNER) == saved


@pytest.mark.parametrize('fields', [
    {'input_tokens': True}, {'input_tokens': -1}, {'output_tokens': 1.5},
    {'reported_nano_aiu': float('nan')}, {'reported_nano_aiu': float('inf')},
    {'event_id': 'invalid'}, {'seq': 1}, {'account_id': 'untrusted'}, {'cost_usd': 0},
])
def test_validation_usage_rejects_before_database(monkeypatch, fields):
    def forbidden():
        pytest.fail('Invalid usage must not reach the database')
    monkeypatch.setattr(store, 'get_conn', forbidden)
    with pytest.raises(store.CopilotConversationError):
        store.append_usage(identifier(), OWNER, identifier(), {**usage_report(), **fields})
