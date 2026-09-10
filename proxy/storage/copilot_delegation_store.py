"""Durable delegation receipts and joined outcomes, independent of parent turns.

Only trusted server code allocates child identities and finalizes a receipt
AFTER joining worker cleanup. A receipt is not a public API authorization token.
"""

from copy import deepcopy
import json
import re
import uuid

from storage import copilot_conversation_store as conversations

CopilotDelegationError = conversations.CopilotConversationError
CopilotDelegationNotFound = conversations.CopilotConversationNotFound
CopilotDelegationConflict = conversations.CopilotConversationConflict
CopilotDelegationLimit = conversations.CopilotConversationLimit

_ALLOCATION = frozenset({"task_id", "run_id", "session_id", "chat_id"})
_RECEIPT = frozenset({"receipt_id", "conversation_id", "user_sub", "source_agent", "generation",
                      "tool_id", "prompt_digest", "agent", "name", *_ALLOCATION})
_RESULT = frozenset({"tool_id", "task_id", "run_id", "chat_id", "agent", "name", "status", "output", "execution_created"})
_STATUSES = frozenset({"completed", "failed", "cancelled", "limit_exceeded"})
MAX_DELEGATIONS = 32
MAX_PUBLIC_BYTES = 4 * 1024 * 1024
MAX_UNSETTLED = 10000


def _allocation(value):
    if (type(value) is not dict or set(value) != _ALLOCATION
            or type(value["task_id"]) is not str or re.fullmatch(r"dyn-[0-9a-f]{32}", value["task_id"]) is None
            or type(value["run_id"]) is not str or re.fullmatch(r"run-[0-9a-f]{12}", value["run_id"]) is None
            or not conversations._uuid(value["session_id"])
            or value["chat_id"] != "task-" + value["run_id"]):
        raise CopilotDelegationError()


@conversations._safe
def validate_receipt(receipt):
    if type(receipt) is not dict or set(receipt) != _RECEIPT:
        raise CopilotDelegationError()
    conversations._identity(receipt["conversation_id"], receipt["user_sub"], receipt["generation"])
    _allocation({key: receipt[key] for key in _ALLOCATION})
    if (not conversations._uuid(receipt["receipt_id"])
            or not conversations._text(receipt["tool_id"])
            or type(receipt["prompt_digest"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", receipt["prompt_digest"]) is None
            or not conversations.config.is_safe_agent_name(receipt["source_agent"])
            or not conversations.config.is_safe_agent_name(receipt["agent"])
            or not conversations._text(receipt["name"]) or len(receipt["name"]) > 100):
        raise CopilotDelegationError()


@conversations._safe
def validate_result(receipt, result):
    validate_receipt(receipt)
    if (type(result) is not dict or set(result) != _RESULT
            or any(result[key] != receipt[key] for key in ("tool_id", "task_id", "run_id", "chat_id", "agent", "name"))
            or type(result["status"]) is not str or result["status"] not in _STATUSES
            or type(result["execution_created"]) is not bool
            or (result["status"] == "completed" and result["execution_created"] is not True)
            or type(result["output"]) is not str or "\0" in result["output"]
            or len(result["output"].encode("utf-8")) > 16384):
        raise CopilotDelegationError()


def _receipt(row):
    return {key: row[key] for key in _RECEIPT}


def _new_receipt(row, generation, tool_id, args, allocation):
    _allocation(allocation)
    audit = conversations.delegation_request(tool_id, args)
    receipt = {"receipt_id": str(uuid.uuid4()), "conversation_id": row["id"], "user_sub": row["user_sub"],
               "source_agent": row["agent"], "generation": generation, "tool_id": tool_id,
               "prompt_digest": audit["prompt_digest"], "agent": args["agent"], "name": args["name"], **allocation}
    validate_receipt(receipt)
    return receipt, audit


def _public(row):
    terminal = json.loads(row["result_payload"]) if row["result_payload"] is not None else None
    receipt = _receipt(row)
    validate_receipt(receipt)
    if terminal is not None:
        validate_result(receipt, terminal)
    if (type(row["cleanup_joined"]) is not bool or row["cleanup_joined"] != (terminal is not None)
            or (row["finished_at"] is not None) != (terminal is not None)):
        raise CopilotDelegationError()
    return {**{key: row[key] for key in ("tool_id", "task_id", "run_id", "chat_id", "agent", "name")},
            "recovery_state": "settled" if terminal is not None else "unverified",
            "status": terminal["status"] if terminal is not None else None,
            "output": terminal["output"] if terminal is not None else None,
            "execution_created": terminal["execution_created"] if terminal is not None else None}



@conversations._safe
def reserve(cid, owner, generation, tool_id, args, allocation):
    args, allocation = deepcopy(args), deepcopy(allocation)
    conversations._identity(cid, owner, generation)
    _allocation(allocation)
    conversations.delegation_request(tool_id, args)
    with conversations._connection() as conn:
        row = conversations._row(conn, cid, owner, generation)
        conversations._open(row, active=True)
        if row.get("delegation_enabled") is not True:
            raise CopilotDelegationConflict()
        # The old audit reservation remains authoritative for legacy calls.
        duplicate = conn.execute(
            """SELECT 1 FROM copilot_delegations WHERE conversation_id=%s AND tool_id=%s
               UNION ALL SELECT 1 FROM copilot_conversation_events WHERE conversation_id=%s
               AND payload::jsonb->>'type'='delegation_request' AND payload::jsonb->>'tool_id'=%s LIMIT 1""",
            (cid, tool_id, cid, tool_id),
        ).fetchone()
        if duplicate is not None:
            return None
        count = conn.execute("SELECT COUNT(*) AS count FROM copilot_delegations WHERE conversation_id=%s",
                             (cid,)).fetchone()["count"]
        if count >= MAX_DELEGATIONS:
            raise CopilotDelegationLimit()
        receipt, audit = _new_receipt(row, generation, tool_id, args, allocation)
        counts = conversations._append(conn, row, audit)
        keys = sorted(receipt)
        conn.execute(
            "INSERT INTO copilot_delegations (" + ",".join(keys) + ",created_at) VALUES ("
            + ",".join(["%s"] * (len(keys) + 1)) + ")",
            (*[receipt[key] for key in keys], conversations._now()),
        )
        # _update commits: both the ledger INSERT and audit append precede it.
        conversations._update(conn, row, **counts)
        return deepcopy(receipt)


@conversations._safe
def finish(receipt, result):
    receipt, result = deepcopy(receipt), deepcopy(result)
    validate_result(receipt, result)
    encoded, _ = conversations._encoded(result)
    with conversations._connection() as conn:
        # Preserve the reserve/finish lock order when the parent still exists,
        # but parent deletion/revocation cannot revoke cleanup persistence. The
        # immutable receipt below is this trusted finalizer's sole authority.
        conn.execute("""SELECT id FROM copilot_conversations
                        WHERE id=%s AND user_sub=%s FOR UPDATE""",
                     (receipt["conversation_id"], receipt["user_sub"])).fetchone()
        row = conn.execute("SELECT * FROM copilot_delegations WHERE receipt_id=%s FOR UPDATE",
                           (receipt["receipt_id"],)).fetchone()
        if row is None:
            raise CopilotDelegationNotFound()
        if _receipt(row) != receipt:
            raise CopilotDelegationConflict()
        if row["result_payload"] is not None:
            if json.loads(row["result_payload"]) != result or row["cleanup_joined"] is not True:
                raise CopilotDelegationConflict()
            return False
        conn.execute("""UPDATE copilot_delegations SET result_payload=%s,cleanup_joined=TRUE,finished_at=%s
                        WHERE receipt_id=%s""", (encoded, conversations._now(), receipt["receipt_id"]))
        conn.commit()
        return True


@conversations._safe
def list_outcomes(cid, owner):
    conversations._identity(cid, owner)
    with conversations._connection() as conn:
        # Validate ownership even for an empty ledger. No lock/state/generation
        # requirement: closed, inaccessible-agent or disconnected-account history
        # remains readable to its authenticated original owner at this layer.
        exists = conn.execute("SELECT 1 FROM copilot_conversations WHERE id=%s AND user_sub=%s", (cid, owner)).fetchone()
        if exists is None:
            raise CopilotDelegationNotFound()
        rows = conn.execute("""SELECT d.* FROM copilot_delegations d JOIN copilot_conversations c
                             ON c.id=d.conversation_id AND c.user_sub=d.user_sub
                             WHERE c.id=%s AND c.user_sub=%s ORDER BY d.created_at,d.receipt_id LIMIT %s""",
                            (cid, owner, MAX_DELEGATIONS + 1)).fetchall()
        if len(rows) > MAX_DELEGATIONS:
            raise CopilotDelegationLimit()
        outcomes = [_public(row) for row in rows]
        encoded = json.dumps(outcomes, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_PUBLIC_BYTES:
            raise CopilotDelegationLimit()
        return outcomes


@conversations._safe
def list_unsettled():
    """Private startup quarantine inventory; never a public history response."""
    with conversations._connection() as conn:
        rows = conn.execute("""SELECT * FROM copilot_delegations WHERE cleanup_joined=FALSE
                             ORDER BY created_at,receipt_id LIMIT %s""", (MAX_UNSETTLED + 1,)).fetchall()
        if len(rows) > MAX_UNSETTLED:
            raise CopilotDelegationLimit()
        receipts = [_receipt(row) for row in rows]
        for receipt in receipts:
            validate_receipt(receipt)
        return receipts
