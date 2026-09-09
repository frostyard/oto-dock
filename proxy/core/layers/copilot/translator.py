"""Pure translation of Copilot SDK 1.0.13 serialized session events.

Input is a wire-shaped dictionary (SDK ``SessionEvent.to_dict()``), not an SDK
object. This module only imports the stdlib and Oto's CommonEvent contract.
It performs no RPC, permission decisions, credential access or registration.

One instance belongs to one session, including its replay window. Keep it alive
across turns: event/message/tool tombstones prevent replayed output. The future
session supervisor must bound session lifetime and persist replay cursors before
using this for durable recovery. These in-memory tombstones are not exactly-once
delivery across a process restart.

Only text, reasoning, ordinary tool display, errors and a reconciled idle
boundary are implemented. Other top-level types emit a payload-free diagnostic
once per event type/session; redundant streaming progress is suppressed.
subagent-owned events are filtered, not flattened into the main conversation.
Permission, usage, goals, background lifecycle and native workflow mappings need
their own adapters and verified recordings before they can be enabled.
"""

from dataclasses import dataclass

from core.events.common_events import (
    CommonEvent, DONE, ERROR, SYSTEM, TEXT, THINKING,
    TOOL_INPUT, TOOL_RESULT, TOOL_USE,
)


# SDK 1.0.13 generated/session_events.py defines streaming_delta as only a
# cumulative byte count. Tool-call deltas are partial input; execution_start
# supplies the complete arguments used by the ordinary tool display. Neither
# needs another UI event alongside the translated text/reasoning/tool events.
_REDUNDANT_PROGRESS = frozenset({
    "assistant.streaming_delta", "assistant.tool_call_delta",
})


@dataclass
class _TextState:
    text: str = ""
    started: bool = False
    final: bool = False


class CopilotEventTranslator:
    """Translate a single session's serialized, camelCase Copilot events.

    Malformed supported events raise ``ValueError`` rather than inventing IDs
    or successful completions. The caller must fail the affected stream clearly.
    Pass untrusted error text through the platform's secret redactor before
    translating or forwarding it; this pure translator cannot know credentials.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._text: dict[tuple[str, str], _TextState] = {}
        self._tools: dict[str, str] = {}
        self._completed_tools: set[str] = set()
        self._early_results: dict[str, dict] = {}
        self._turn_ids: set[str] = set()
        self._turn_boundaries: set[tuple[str, str]] = set()
        self._reported_unmapped: set[str] = set()
        self._idle_id: str | None = None
        self._generation = 0
        self._settled_generation = -1

    @staticmethod
    def _string(data: dict, key: str, *, nonempty: bool = False) -> str:
        value = data.get(key)
        if not isinstance(value, str) or (nonempty and not value):
            raise ValueError(f"Copilot event requires string {key}")
        return value

    @staticmethod
    def _system(subtype: str, **data) -> CommonEvent:
        return CommonEvent(SYSTEM, {"subtype": subtype, **data})

    def translate(self, event: dict) -> list[CommonEvent]:
        if not isinstance(event, dict):
            raise ValueError("Copilot event must be a serialized dictionary")
        event_id = self._string(event, "id", nonempty=True)
        kind = self._string(event, "type", nonempty=True)
        data = event.get("data")
        if not isinstance(data, dict):
            raise ValueError("Copilot event requires object data")
        if event_id in self._seen:
            return []

        # parentId is the *event* parent, not proof of subagent ownership.
        # agentId and parentToolCallId are the published ownership signals.
        if event.get("agentId") or data.get("parentToolCallId"):
            # Invisible in the main transcript does not mean settled. A child
            # event racing an idle reconciliation invalidates that observation.
            self._idle_id = None
            self._seen.add(event_id)
            return []

        result = self._translate(kind, data, event_id)
        # Diagnostics and repeated idle/turn-end notifications do not establish
        # another turn. Otherwise usage/background notifications arriving after
        # DONE can manufacture a second completion with no new work.
        if result and kind in {
            "assistant.message_delta", "assistant.message",
            "assistant.reasoning_delta", "assistant.reasoning",
            "tool.execution_start", "tool.execution_complete", "session.error",
        }:
            self._generation += 1
        self._seen.add(event_id)
        return result

    def _translate(self, kind: str, data: dict, event_id: str) -> list[CommonEvent]:
        if kind in {
            "assistant.message_delta", "assistant.message",
            "assistant.reasoning_delta", "assistant.reasoning",
        }:
            reasoning = kind.startswith("assistant.reasoning")
            item_id = self._string(
                data, "reasoningId" if reasoning else "messageId", nonempty=True,
            )
            final = not kind.endswith("_delta")
            content = self._string(data, "content" if final else "deltaContent")
            if final and data.get("chunkCount", 1) not in (None, 1):
                raise ValueError("Copilot chunked final messages require a verified adapter")
            state = self._text.get(("reasoning" if reasoning else "message", item_id))
            if state is not None and state.final:
                return []
            self._idle_id = None
            return self._translate_text(item_id, content, reasoning=reasoning, final=final)

        if kind == "tool.execution_start":
            tool_id = self._string(data, "toolCallId", nonempty=True)
            name = self._string(data, "toolName", nonempty=True)
            if tool_id in self._tools:
                if self._tools[tool_id] != name:
                    raise ValueError("Copilot toolCallId changed tool name")
                return []
            self._idle_id = None
            self._tools[tool_id] = name
            arguments = data.get("arguments")
            events = [CommonEvent(TOOL_USE, {"name": name, "tool_id": tool_id})]
            events.append(CommonEvent(TOOL_INPUT, {
                "name": name, "tool_id": tool_id, "summary": "",
                "tool_input": arguments if isinstance(arguments, dict) else None,
            }))
            pending = self._early_results.pop(tool_id, None)
            if pending is not None:
                events.extend(self._tool_result(tool_id, pending))
            return events

        if kind == "tool.execution_complete":
            tool_id = self._string(data, "toolCallId", nonempty=True)
            if not isinstance(data.get("success"), bool):
                raise ValueError("Copilot tool result requires boolean success")
            if tool_id in self._completed_tools:
                return []
            self._idle_id = None
            if tool_id not in self._tools:
                # Preserve out-of-order results; never invent a tool name or
                # show a completion that the UI cannot attach to a tool block.
                self._early_results.setdefault(tool_id, data.copy())
                return []
            return self._tool_result(tool_id, data)

        if kind == "session.error":
            message = self._string(data, "message")
            self._idle_id = None
            return [CommonEvent(ERROR, {"message": message})]

        if kind == "session.idle":
            # Idle is a candidate boundary, not proof background work settled.
            self._idle_id = event_id
            return [self._system("copilot_idle", event_id=event_id,
                                 aborted=data.get("aborted") is True)]

        if kind in {"assistant.turn_start", "assistant.turn_end"}:
            turn_id = self._string(data, "turnId", nonempty=True)
            boundary = (kind, turn_id)
            if boundary in self._turn_boundaries:
                return []
            self._turn_boundaries.add(boundary)
            if turn_id not in self._turn_ids:
                self._turn_ids.add(turn_id)
                self._generation += 1
            self._idle_id = None
            return [self._system("copilot_turn_boundary", event_type=kind,
                                 turn_id=data["turnId"])]

        # Unknown events cannot be assumed irrelevant to a pending idle probe.
        # In particular, background_tasks_changed carries no inline task state.
        # Invalidate even when presentation of this type has been suppressed:
        # diagnostic deduplication must never deduplicate new activity itself.
        self._idle_id = None
        if kind in _REDUNDANT_PROGRESS or kind in self._reported_unmapped:
            return []
        self._reported_unmapped.add(kind)
        return [self._system("copilot_unmapped_event", event_type=kind)]

    def _translate_text(
        self, item_id: str, content: str, *, reasoning: bool, final: bool,
    ) -> list[CommonEvent]:
        key = ("reasoning" if reasoning else "message", item_id)
        state = self._text.setdefault(key, _TextState())
        if state.final:
            return []
        if final and not content.startswith(state.text):
            # A conflicting snapshot cannot be appended without corrupting the
            # output. Report it explicitly instead of repeating the final text.
            return [self._system("copilot_content_mismatch", item_id=item_id,
                                 content_type=key[0])]
        suffix = content[len(state.text):] if final else content
        events = []
        if suffix:
            if reasoning and not state.started:
                events.append(CommonEvent(THINKING, {"phase": "start"}))
            state.started = True
            state.text += suffix
            events.append(CommonEvent(
                THINKING if reasoning else TEXT,
                {"phase": "delta", "text": suffix} if reasoning else {"content": suffix},
            ))
        if final:
            state.final = True
            if reasoning and state.started:
                events.append(CommonEvent(THINKING, {"phase": "end", "text": ""}))
        return events

    def _tool_result(self, tool_id: str, data: dict) -> list[CommonEvent]:
        self._completed_tools.add(tool_id)
        payload = {"name": self._tools[tool_id], "tool_id": tool_id,
                   "is_error": not data["success"]}
        result = data.get("result")
        error = data.get("error")
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            payload["result_content"] = result["content"]
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            payload["result_content"] = error["message"]
        return [CommonEvent(TOOL_RESULT, payload)]

    def settle_idle(self, event_id: str, *, background_settled: bool) -> list[CommonEvent]:
        """Complete a still-current idle candidate after external reconciliation.

        The owner must establish that native tasks and their follow-up turns
        have settled and no permission/tool result is pending. Passing False or
        a stale event ID is harmless. SDK idle/turn-end alone is not evidence.
        No DONE is emitted while an ordinary tool is still open or a tool
        completion still awaits its start.
        """
        if (background_settled is not True or self._idle_id != event_id
                or self._early_results
                or self._tools.keys() - self._completed_tools
                or self._settled_generation == self._generation):
            return []
        self._idle_id = None
        self._settled_generation = self._generation
        return [CommonEvent(DONE)]
