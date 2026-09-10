"""Owned Copilot permission/question callbacks onto OtoDock's shared authority.

This bridges requests that reach the SDK callback. It is not an every-tool
security gate: runtime read auto-approval, cached rules, skip_permission and
resolved_by_hook can bypass callbacks. A separately proven PreToolUse floor is
required before unrestricted production tool registration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from pathlib import PurePosixPath
import re
import uuid


class UserInputUnavailableError(RuntimeError):
    """Legacy SDK questions have no cancellation response; fail without an answer."""


def _field(request, name, default=None):
    return request.get(name, default) if isinstance(request, Mapping) else getattr(request, name, default)


def _text(value, maximum=8192):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and "\x00" not in value


def _path(value):
    # This first Linux bridge only admits explicit absolute native paths. It
    # must not normalize relative/symlink/tilde paths into a different operation.
    return (_text(value) and PurePosixPath(value).is_absolute()
            and "\\" not in value and not any(ord(c) < 32 for c in value))


def _name(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is not None


def map_permission(request, *, working_directory: str, custom_tools: Mapping[str, str] | None = None):
    """Map pinned SDK typed/snake_case requests; unknown capabilities deny.

    Shell/read-only hints and permission recommendations never confer authority.
    Custom names require an explicit host-owned binding; a tool named Read or
    Bash cannot inherit a native tool's policy by naming itself that way.
    """
    for field in ("request_sandbox_bypass", "managed_approval_required", "skip_permission"):
        value = _field(request, field)
        if value is not None and value is not False:
            return None
    kind = _field(request, "kind")
    if kind == "shell":
        command = _field(request, "full_command_text")
        if not _text(command, 32768) or not _path(working_directory):
            return None
        return "Bash", {"command": command, "cwd": working_directory}
    if kind in {"read", "write"}:
        path = _field(request, "path" if kind == "read" else "file_name")
        if not _path(path):
            return None
        args = {"file_path": path}
        if kind == "write":
            diff = _field(request, "diff")
            content = _field(request, "new_file_contents")
            if not isinstance(diff, str) or (content is not None and not isinstance(content, str)):
                return None
            args["diff"] = diff
            if content is not None:
                args["content"] = content
        return ("Read" if kind == "read" else "Write"), args
    if kind == "url":
        url = _field(request, "url")
        if not _text(url):
            return None
        return "WebFetch", {"url": url}
    if kind in {"mcp", "custom-tool"}:
        tool = _field(request, "tool_name")
        args = _field(request, "args")
        if not _name(tool) or (args is not None and not isinstance(args, dict)):
            return None
        if kind == "mcp":
            server = _field(request, "server_name")
            if not _name(server) or "__" in server:
                return None
            canonical = f"mcp__{server}__{tool}"
        else:
            canonical = (custom_tools or {}).get(tool)
            if not canonical:
                return None
        return canonical, deepcopy(args or {})
    return None


class CopilotPermissionBridge:
    def __init__(self, requests, *, decide, context_valid, working_directory: str,
                 ask=None, custom_tools: Mapping[str, str] | None = None,
                 expected_sdk_session_id: str | None = None):
        if not _path(working_directory) or not callable(decide) or not callable(context_valid):
            raise ValueError("Explicit Copilot permission authority and working directory are required")
        if ask is not None and not callable(ask):
            raise ValueError("Invalid Copilot question authority")
        self.requests = requests
        self._decide = decide
        self._ask = ask
        self._context_valid = context_valid
        self._working_directory = working_directory
        self._custom_tools = dict(custom_tools or {})
        if any(not _name(name) or not _name(canonical)
               or canonical in {"EnterPlanMode", "ExitPlanMode", "AskUserQuestion"}
               for name, canonical in self._custom_tools.items()):
            raise ValueError("Invalid trusted Copilot custom tool bindings")
        self._sdk_session_id = None
        if expected_sdk_session_id is not None:
            self.bind_sdk_session(expected_sdk_session_id)

    def bind_sdk_session(self, session_id: str):
        if not _text(session_id, 256) or (self._sdk_session_id is not None and self._sdk_session_id != session_id):
            raise ValueError("Copilot callback session binding cannot change")
        self._sdk_session_id = session_id

    def _valid(self, invocation):
        return (self._context_valid() is True
                and (self._sdk_session_id is None or (
                    isinstance(invocation, Mapping) and invocation.get("session_id") == self._sdk_session_id)))

    async def on_permission_request(self, request, invocation):
        from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

        async def decide():
            if not self._valid(invocation):
                return False
            mapped = map_permission(request, working_directory=self._working_directory,
                                    custom_tools=self._custom_tools)
            if mapped is None:
                return False
            name, args = mapped
            original = deepcopy(args)
            result = await self._decide(name, args)
            return (self._valid(invocation) and isinstance(result, dict)
                    and result.get("decision") == "allow" and args == original
                    and map_permission(request, working_directory=self._working_directory,
                                       custom_tools=self._custom_tools) == (name, original)
                    and ("updated_input" not in result or result["updated_input"] == original))

        try:
            allowed = await self.requests.run(decide)
            if allowed is True and self._valid(invocation):
                # Do not persist a runtime-wide allow rule or claim a human
                # approved an action that the platform may have auto-allowed.
                return PermissionDecisionApproveOnce()
        except (asyncio.CancelledError, Exception):
            pass
        return PermissionDecisionReject(feedback="OtoDock did not authorize this action")

    async def on_user_input_request(self, request, invocation):
        async def ask():
            if (not self._valid(invocation) or self._ask is None
                    or not isinstance(original, Mapping) or request != original):
                raise UserInputUnavailableError()
            question = original.get("question")
            choices = original.get("choices", [])
            freeform = original.get("allowFreeform", True)
            if (not _text(question) or not isinstance(choices, list) or len(choices) > 20
                    or any(not _text(choice, 500) for choice in choices)
                    or len(set(choices)) != len(choices) or type(freeform) is not bool
                    or (not choices and not freeform)):
                raise UserInputUnavailableError()
            choices = tuple(choices)
            question_id = f"copilot-{uuid.uuid4().hex}"
            questions = [{"id": question_id, "header": "Copilot", "question": question,
                          "options": [{"label": choice, "description": ""} for choice in choices],
                          "multiSelect": False, "isOther": freeform, "isSecret": False}]
            result = await self._ask(questions)
            if not self._valid(invocation) or request != original or not isinstance(result, dict):
                raise UserInputUnavailableError()
            entry = result.get(question_id)
            answers = entry.get("answers") if isinstance(entry, dict) else None
            if (not isinstance(answers, list) or not 1 <= len(answers) <= 2
                    or any(not _text(value) for value in answers)):
                raise UserInputUnavailableError()
            if len(answers) == 2:
                # Dashboard structured answers may contain one selected label
                # followed by the human's free text. Preserve both verbatim;
                # multiple selected choices are not a single-choice response.
                if not freeform or answers[0] not in choices or answers[1] in choices:
                    raise UserInputUnavailableError()
                answer = "\n".join(answers)
                if not _text(answer):
                    raise UserInputUnavailableError()
                return {"answer": answer, "wasFreeform": True}
            answer = answers[0]
            if answer not in choices and not freeform:
                raise UserInputUnavailableError()
            return {"answer": answer, "wasFreeform": answer not in choices}

        try:
            # Snapshot before the registry schedules the host wait. Neither a
            # late source mutation nor an aliased choices list may change the
            # question to which the human's answer is credited.
            original = deepcopy(request)
            answer = await self.requests.run(ask)
            if self._valid(invocation) and request == original:
                return answer
        except (asyncio.CancelledError, Exception):
            pass
        # The legacy SDK has no cancel/decline response. Never invent an empty
        # user answer; its JSON-RPC error contains only this fixed message.
        raise UserInputUnavailableError("Copilot user input is unavailable")


def bind_platform_authority(session_id: str, requests, *, working_directory: str,
                            custom_tools=None, expected_sdk_session_id=None, question_timeout=604800.0):
    """Bind only a host-owned platform session ID; resolve live context and mode.

    Caller registers session state before spawn and releases waiters/cleans state
    on close. SDK native session IDs are separate and optionally bound above.
    """
    from api.hooks.hooks import ask_user_question, decide_tool_permission, resolve_hook_route
    from core.session.session_state import get_session_security, get_session_mode, get_session_client_type

    if not _text(session_id, 256):
        raise ValueError("An explicit platform session is required")

    def snapshot():
        context = get_session_security(session_id)
        route = resolve_hook_route(session_id)
        # Keep the original alive to make object identity meaningful, and a
        # deep snapshot to detect mutations of otherwise equal live contexts.
        return (context, id(context), deepcopy(context), get_session_mode(session_id),
                get_session_client_type(session_id), route.is_meeting, route.parent_session_id,
                route.queue_session_id, get_session_mode(route.parent_session_id) if route.is_meeting else None)

    def valid():
        return get_session_security(session_id) is not None

    async def decide(name, args):
        before = snapshot()
        if before[0] is None:
            return {"decision": "deny"}
        result = await decide_tool_permission(session_id, name, args)
        return result if snapshot() == before else {"decision": "deny"}

    async def ask(questions):
        before = snapshot()
        if before[0] is None:
            return {}
        result = await ask_user_question(session_id, questions, timeout=question_timeout)
        return result if snapshot() == before else {}

    return CopilotPermissionBridge(
        requests, decide=decide, ask=ask, context_valid=valid, working_directory=working_directory,
        custom_tools=custom_tools, expected_sdk_session_id=expected_sdk_session_id,
    )
