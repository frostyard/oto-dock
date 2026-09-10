"""Conservative native cancellation candidates, not side-effect-free proof.

The supervisor may use these only for matching accepted abort/interrupt with
authoritative settled snapshots. Callback rejection and ordinary native denial
do not retire a tool here. Payloads may carry secrets; retain digests and opaque
correlation IDs, never raw permission arguments, messages, or result contents.
"""

from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class _Request:
    digest: str
    tool_id: str | None
    sequence: int


def _identifier(value):
    return value if isinstance(value, str) and value.strip() and len(value) <= 1024 else None


def _digest(data):
    try:
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()).hexdigest()
    except Exception:
        return None


class CopilotPermissionEvents:
    def __init__(self):
        self._requests: dict[str, _Request] = {}
        self._completions: dict[str, str] = {}
        self._latest: dict[str, str] = {}
        self._starts: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self._sequence = 0
        self._failed = False

    @property
    def cancelled_tool_ids(self) -> frozenset[str]:
        return frozenset(self._cancelled)

    def _contradiction(self):
        self._failed = True
        self._cancelled.clear()
        raise ValueError("Contradictory Copilot permission events")

    def observe(self, kind: str, data: dict) -> None:
        if self._failed:
            raise ValueError("Copilot permission event tracking is invalid")
        if kind not in {"permission.requested", "permission.completed", "tool.execution_start"}:
            return
        if not isinstance(data, dict):
            self._contradiction()
        self._sequence += 1
        if kind == "tool.execution_start":
            tool_id = _identifier(data.get("toolCallId"))
            if tool_id:
                self._starts[tool_id] = self._sequence
                self._cancelled.discard(tool_id)
            return

        request_id = _identifier(data.get("requestId"))
        if request_id is None:
            details = data.get("permissionRequest") if kind == "permission.requested" else data
            if isinstance(details, dict):
                self._cancelled.discard(_identifier(details.get("toolCallId")))
            return
        digest = _digest(data)
        if digest is None:
            self._contradiction()
        if kind == "permission.requested":
            previous = self._requests.get(request_id)
            if previous is not None:
                if previous.digest != digest:
                    self._contradiction()
                return
            details = data.get("permissionRequest")
            tool_id = _identifier(details.get("toolCallId")) if isinstance(details, dict) else None
            self._requests[request_id] = _Request(digest, tool_id, self._sequence)
            if tool_id:
                self._cancelled.discard(tool_id)
                self._latest[tool_id] = request_id
            # A previously seen completion remains consumed. A later duplicate
            # cannot turn an out-of-order/unknown request into cancellation proof.
            return

        previous = self._completions.get(request_id)
        if previous is not None:
            if previous != digest:
                self._contradiction()
            return
        self._completions[request_id] = digest
        tool_id = _identifier(data.get("toolCallId"))
        requested = self._requests.get(request_id)
        if tool_id:
            self._cancelled.discard(tool_id)
        if requested is None:
            return
        if requested.tool_id:
            self._cancelled.discard(requested.tool_id)
        if tool_id is None or requested.tool_id is None:
            return
        if requested.tool_id != tool_id:
            self._contradiction()
        result = data.get("result")
        if (isinstance(result, dict) and result.get("kind") == "cancelled"
                and self._latest.get(tool_id) == request_id
                and self._starts.get(tool_id, 0) <= requested.sequence):
            self._cancelled.add(tool_id)
