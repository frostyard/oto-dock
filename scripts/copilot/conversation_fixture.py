"""In-memory storage seam for opt-in runtime probes, never application storage.

PostgreSQL transaction behavior is qualified by the proxy database tests. This
fixture retains rows across service replacement so live probes can exercise the
real authentication, native history, generation and HTTP boundaries without a DB.
"""

from copy import deepcopy
from datetime import datetime, timezone
import threading

from storage import copilot_conversation_store as contract


class MemoryConversations:
    def __init__(self):
        self.rows, self.frames = {}, {}
        self.lock = threading.RLock()

    def _row(self, cid, owner, generation=None):
        row = self.rows.get(cid)
        if row is None or row['user_sub'] != owner:
            raise contract.CopilotConversationNotFound()
        if generation is not None and row['generation'] != generation:
            raise contract.CopilotConversationConflict()
        return row

    def _update(self, row, **fields):
        row.update(fields)
        row['revision'] += 1
        row['updated_at'] = datetime.now(timezone.utc).isoformat()

    def _append(self, row, event):
        _, size = contract._encoded(event)
        if (row['event_count'] >= contract.MAX_EVENTS
                or row['event_bytes'] + size > contract.MAX_BYTES):
            raise contract.CopilotConversationLimit()
        row['event_count'] += 1
        row['event_bytes'] += size
        self.frames[row['id']].append({**deepcopy(event), 'seq': row['event_count']})

    def create(self, conversation_id, owner_sub, **fields):
        with self.lock:
            if conversation_id in self.rows:
                raise contract.CopilotConversationConflict()
            now = datetime.now(timezone.utc).isoformat()
            row = dict(id=conversation_id, user_sub=owner_sub, **fields, revision=1,
                       state='open', title='New conversation', created_at=now, updated_at=now,
                       turn_active=False, last_turn_complete=False, event_count=0, event_bytes=0)
            self.rows[conversation_id], self.frames[conversation_id] = row, []
            return deepcopy(row)

    def get(self, cid, owner):
        with self.lock:
            row = self.rows.get(cid)
            return deepcopy(row) if row and row['user_sub'] == owner else None

    def list_conversations(self, owner, limit=20, offset=0):
        with self.lock:
            rows = sorted((row for row in self.rows.values() if row['user_sub'] == owner),
                          key=lambda row: (row['updated_at'], row['id']), reverse=True)
            return deepcopy(rows[offset:offset + limit])

    def events(self, cid, owner):
        with self.lock:
            self._row(cid, owner)
            return deepcopy(self.frames[cid])

    def begin_turn(self, cid, owner, generation, text):
        with self.lock:
            row = self._row(cid, owner, generation)
            if row['state'] != 'open' or row['turn_active']:
                raise contract.CopilotConversationConflict()
            self._append(row, {'type': 'user', 'content': text})
            self._update(row, turn_active=True, last_turn_complete=False,
                         title=text[:128] if row['event_count'] == 1 else row['title'])

    def append_event(self, cid, owner, generation, event):
        with self.lock:
            row = self._row(cid, owner, generation)
            if row['state'] != 'open' or not row['turn_active']:
                raise contract.CopilotConversationConflict()
            self._append(row, event)
            self._update(row)

    def finish_turn(self, cid, owner, generation):
        with self.lock:
            row = self._row(cid, owner, generation)
            if row['state'] != 'open' or not row['turn_active']:
                raise contract.CopilotConversationConflict()
            self._append(row, {'type': 'turn_complete'})
            self._update(row, turn_active=False, last_turn_complete=True)

    def claim_resume(self, cid, owner, expected_revision, generation):
        with self.lock:
            row = self._row(cid, owner)
            if row['state'] != 'closed' or not row['last_turn_complete'] or row['revision'] != expected_revision:
                raise contract.CopilotConversationConflict()
            self._update(row, state='open', generation=generation)
            return deepcopy(row)

    def finish_close(self, cid, owner, generation, resumable):
        with self.lock:
            row = self._row(cid, owner, generation)
            ready = resumable and row['last_turn_complete'] and not row['turn_active']
            self._update(row, state='closed' if ready else 'incomplete')
