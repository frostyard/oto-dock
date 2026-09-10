"""In-memory recovery ledger seam; PostgreSQL transaction proofs live in tests."""

from copy import deepcopy
import json

from storage import copilot_delegation_store as contract
from storage import copilot_conversation_store as conversations


class MemoryDelegations:
    def __init__(self, history):
        self.history = history
        self.rows = {}
        self.lock = history.lock

    @conversations._safe
    def reserve(self, cid, owner, generation, tool_id, args, allocation):
        args, allocation = deepcopy(args), deepcopy(allocation)
        conversations._identity(cid, owner, generation)
        contract._allocation(allocation)
        conversations.delegation_request(tool_id, args)
        with self.lock:
            row = self.history._row(cid, owner, generation)
            conversations._open(row, active=True)
            if row.get('delegation_enabled') is not True:
                raise contract.CopilotDelegationConflict()
            existing = [value for value in self.rows.values() if value['conversation_id'] == cid]
            if (any(value['tool_id'] == tool_id for value in existing)
                    or any(value['type'] == 'delegation_request' and value['tool_id'] == tool_id
                           for value in self.history.frames[cid])):
                return None
            if len(existing) >= contract.MAX_DELEGATIONS:
                raise contract.CopilotDelegationLimit()
            if any(any(value[key] == allocation[key] for key in contract._ALLOCATION) for value in self.rows.values()):
                raise contract.CopilotDelegationConflict()
            receipt, audit = contract._new_receipt(row, generation, tool_id, args, allocation)
            before_row, before_events = deepcopy(row), deepcopy(self.history.frames[cid])
            try:
                self.history._append(row, audit)
                self.rows[receipt['receipt_id']] = {
                    **receipt, 'created_at': conversations._now(), 'result_payload': None,
                    'cleanup_joined': False, 'finished_at': None,
                }
                self.history._update(row)
            except Exception:
                self.history.rows[cid] = before_row
                self.history.frames[cid] = before_events
                self.rows.pop(receipt['receipt_id'], None)
                raise
            return deepcopy(receipt)

    @conversations._safe
    def finish(self, receipt, result):
        receipt, result = deepcopy(receipt), deepcopy(result)
        contract.validate_result(receipt, result)
        with self.lock:
            row = self.rows.get(receipt['receipt_id'])
            if row is None:
                raise contract.CopilotDelegationNotFound()
            if contract._receipt(row) != receipt:
                raise contract.CopilotDelegationConflict()
            if row['result_payload'] is not None:
                if json.loads(row['result_payload']) != result or row['cleanup_joined'] is not True:
                    raise contract.CopilotDelegationConflict()
                return False
            row.update(result_payload=conversations._encoded(result)[0], cleanup_joined=True,
                       finished_at=conversations._now())
            return True

    @conversations._safe
    def list_outcomes(self, cid, owner):
        conversations._identity(cid, owner)
        with self.lock:
            self.history._row(cid, owner)
            rows = sorted((row for row in self.rows.values() if row['conversation_id'] == cid and row['user_sub'] == owner),
                          key=lambda row: (row['created_at'], row['receipt_id']))
            if len(rows) > contract.MAX_DELEGATIONS:
                raise contract.CopilotDelegationLimit()
            result = [contract._public(row) for row in rows]
            if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > contract.MAX_PUBLIC_BYTES:
                raise contract.CopilotDelegationLimit()
            return deepcopy(result)

    @conversations._safe
    def list_unsettled(self):
        with self.lock:
            rows = [contract._receipt(row) for row in sorted(self.rows.values(), key=lambda row: (row['created_at'], row['receipt_id']))
                    if row['cleanup_joined'] is False]
            if len(rows) > contract.MAX_UNSETTLED:
                raise contract.CopilotDelegationLimit()
            for receipt in rows:
                contract.validate_receipt(receipt)
            return rows
