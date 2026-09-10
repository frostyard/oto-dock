"""One host-owned delegation capability; no model-selected code or credentials."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import re

from core.layers.copilot.permissions import map_permission

DELEGATE_TOOL = "oto_delegate"
DELEGATE_CANONICAL = "mcp__delegation-mcp__delegate"
DELEGATE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["agent", "name", "prompt"],
    "properties": {
        "agent": {"type": "string", "minLength": 1, "maxLength": 256},
        "name": {"type": "string", "minLength": 1, "maxLength": 100},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 16384},
    },
}


def _slug(value):
    return (type(value) is str and ".." not in value
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", value) is not None)


def valid_delegation_targets(targets):
    return (type(targets) is tuple and len(targets) <= 64
            and all(_slug(target) for target in targets) and len(set(targets)) == len(targets))


def delegation_profile(targets):
    return {"version": 1, "tool": DELEGATE_TOOL, "schema": deepcopy(DELEGATE_SCHEMA), "targets": list(targets)}


def valid_delegate_args(args, targets):
    if (type(args) is not dict or set(args) != {"agent", "name", "prompt"}
            or not _slug(args["agent"]) or args["agent"] not in targets
            or type(args["name"]) is not str or not args["name"].strip()
            or args["name"] != args["name"].strip()
            or len(args["name"]) > 100 or not args["name"].isprintable()
            or type(args["prompt"]) is not str or not args["prompt"].strip() or "\0" in args["prompt"]):
        return False
    try:
        return len(args["prompt"].encode("utf-8")) <= 16384
    except UnicodeError:
        return False


class CopilotDelegationTool:
    """Internal guarded binding; the execution callback is the authority gate."""

    def __init__(self, *, targets, handler, bridge, callbacks, authorize):
        if (not valid_delegation_targets(targets) or not targets
                or not callable(handler) or not inspect.iscoroutinefunction(handler)
                or not callable(authorize)):
            raise ValueError("Explicit Copilot delegation binding is required")
        self.targets, self.bridge = targets, bridge
        self._handler, self._callbacks, self._authorize = handler, callbacks, authorize

    def sdk_tool(self):
        from copilot.tools import Tool
        return Tool(name=DELEGATE_TOOL, description="Delegate a bounded task to an authorized repository or QA agent.",
                    parameters=deepcopy(DELEGATE_SCHEMA), handler=self.execute,
                    overrides_built_in_tool=False, skip_permission=False)

    def admit(self, name, args, invocation):
        return (isinstance(invocation, dict) and name == DELEGATE_TOOL and valid_delegate_args(args, self.targets)
                and self.bridge.matches_sdk_session(invocation.get("session_id"))
                and self.bridge._valid(invocation))

    def admit_permission(self, request, invocation):
        projected = map_permission(request, working_directory=self.bridge.working_directory,
                                   custom_tools={DELEGATE_TOOL: DELEGATE_CANONICAL})
        kind = request.get("kind") if isinstance(request, dict) else getattr(request, "kind", None)
        return (kind == "custom-tool" and projected is not None and projected[0] == DELEGATE_CANONICAL
                and self.admit(DELEGATE_TOOL, projected[1], invocation))

    async def execute(self, invocation):
        from copilot.tools import ToolResult

        try:
            native_id, call_id, name = invocation.session_id, invocation.tool_call_id, invocation.tool_name
            original = deepcopy(invocation.arguments)
            context = {"session_id": native_id}

            def valid():
                return (type(call_id) is str and 0 < len(call_id) <= 256 and call_id.isprintable()
                        and call_id == call_id.strip()
                        and invocation.session_id == native_id and invocation.tool_call_id == call_id
                        and invocation.tool_name == name and invocation.arguments == original
                        and self.admit(name, original, context))

            async def invoke():
                if not valid():
                    raise ValueError()
                await self._authorize()
                if not valid() or not await self.bridge.authorize_operation(
                    lambda: (DELEGATE_CANONICAL, deepcopy(original)) if valid() else None,
                    context, require_bound_session=True,
                ):
                    raise ValueError()
                await self._authorize()
                if not valid():
                    raise ValueError()
                arguments = deepcopy(original)
                result = await self._handler(call_id, arguments)
                await self._authorize()
                if (not valid() or arguments != original or type(result) is not str
                        or not result.strip() or "\0" in result or len(result.encode("utf-8")) > 65536):
                    raise ValueError()
                return result

            if not valid():
                raise ValueError()
            result = await self._callbacks.run(call_id, invoke)
            return ToolResult(text_result_for_llm=result, result_type="success")
        except (asyncio.CancelledError, Exception):
            return ToolResult(text_result_for_llm="Delegation did not complete successfully.", result_type="failure",
                              error="OtoDock delegation is unavailable")
