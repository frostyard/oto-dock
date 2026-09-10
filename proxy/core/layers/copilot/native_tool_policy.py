"""Pinned Linux native-tool pre-execution policy for an explicit tool subset.

Native approval caches can bypass permission requests. This hook calls the
same OtoDock authority before execution and always returns an explicit decision;
the pinned SDK treats an exception/None from a hook as no restriction.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

from core.layers.copilot.host_tools import CopilotDelegationTool, DELEGATE_TOOL
from core.layers.copilot.reasoning import valid_reasoning_effort
from core.layers.copilot.permissions import _path, _text

SUPPORTED_NATIVE_TOOLS = frozenset({"bash", "create", "edit", "view", "glob", "grep"})
_SCHEMAS = json.loads(Path(__file__).with_name("native_tool_schemas.json").read_text())
_DENY = {"permissionDecision": "deny", "permissionDecisionReason": "OtoDock did not authorize this native tool"}
_SESSION_OPTIONS = frozenset({
    "model", "reasoning_effort", "streaming", "on_event", "enable_session_store", "session_limits", "system_message", "managed_settings",
})


class NativePolicySessionError(RuntimeError):
    """Sanitized guarded-session failure; its runtime owner must close on failure."""


def _structure(value, *, property_map=False):
    if isinstance(value, dict):
        return {key: _structure(item, property_map=key in {"properties", "$defs", "definitions", "patternProperties"})
                for key, item in value.items()
                if property_map or key not in {"description", "instructions"}}
    if isinstance(value, list):
        return [_structure(item) for item in value]
    return value


def _valid(value, schema):
    """Validate the small pinned JSON-schema vocabulary, denying extra fields."""
    if "anyOf" in schema:
        return any(_valid(value, branch) for branch in schema["anyOf"])
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        return (isinstance(value, dict) and not value.keys() - properties.keys()
                and set(schema.get("required", ())) <= value.keys()
                and all(_valid(item, properties[key]) for key, item in value.items()))
    if kind == "array":
        return (isinstance(value, list) and len(value) <= 128
                and all(_valid(item, schema["items"]) for item in value))
    if kind == "string":
        valid = isinstance(value, str) and len(value) <= 1_048_576 and "\x00" not in value
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind == "integer":
        valid = type(value) is int
    elif kind == "number":
        valid = type(value) in (int, float) and math.isfinite(value)
    else:
        return False
    return valid and ("enum" not in schema or value in schema["enum"])


def _search_pattern(value):
    # The shared authority checks a search root, not paths embedded in a glob.
    # Admit only patterns confined to that root; broader syntax needs its own
    # qualified path projection instead of silently authorizing another tree.
    return _text(value) and not value.startswith("/") and ".." not in value and "\\" not in value


def project_native_tool(name, args, *, working_directory):
    """Project only the reviewed subset; unknown capabilities remain denied.

    This first policy subset deliberately rejects multi-root searches, shell ID
    reuse and explicit async/detached shell modes. Even sync native Bash can
    outlive initial_wait: background ownership remains a separate engine gate.
    """
    if (not isinstance(name, str) or name not in SUPPORTED_NATIVE_TOOLS
            or not _path(working_directory) or not _valid(args, _SCHEMAS[name])):
        return None
    if name == "bash":
        if (not _text(args["command"], 32768) or not _text(args["description"], 100)
                or "shellId" in args or args.get("mode", "sync") != "sync"
                or args.get("detach", False) is not False
                or not 10 <= args.get("initial_wait", 10) <= 600):
            return None
        return "Bash", {**deepcopy(args), "cwd": working_directory}
    if name in {"create", "edit", "view"}:
        if not _path(args["path"]):
            return None
        if name == "create":
            return "Write", {"file_path": args["path"], "content": args["file_text"]}
        if name == "edit":
            if "old_str" not in args or "new_str" not in args or not args["old_str"]:
                return None
            return "Edit", {"file_path": args["path"], "old_string": args["old_str"],
                            "new_string": args["new_str"]}
        view_range = args.get("view_range")
        if view_range is not None and (len(view_range) != 2 or view_range[0] < 1
                                      or (view_range[1] != -1 and view_range[1] < view_range[0])):
            return None
        return "Read", {"file_path": args["path"], **deepcopy({
            key: value for key, value in args.items() if key != "path"
        })}
    paths = args.get("paths", working_directory)
    if isinstance(paths, list):
        if len(paths) != 1:
            return None
        paths = paths[0]
    if not _path(paths) or not _text(args["pattern"]):
        return None
    if name == "glob" and not _search_pattern(args["pattern"]):
        return None
    if name == "grep":
        if "glob" in args and not _search_pattern(args["glob"]):
            return None
        for key in ("-A", "-B", "-C", "head_limit"):
            if key in args and (type(args[key]) is not int or not 0 <= args[key] <= 1_000_000):
                return None
        if "type" in args and not _text(args["type"], 100):
            return None
    return ("Glob" if name == "glob" else "Grep"), {
        "path": paths, **deepcopy({key: value for key, value in args.items() if key != "paths"}),
    }


class CopilotNativeToolPolicy:
    def __init__(self, bridge, *, enabled_tools: frozenset[str], delegation=None):
        if (not isinstance(enabled_tools, frozenset) or not enabled_tools
                or not enabled_tools <= SUPPORTED_NATIVE_TOOLS):
            raise ValueError("An explicit supported Copilot native tool subset is required")
        if delegation is not None and (type(delegation) is not CopilotDelegationTool or delegation.bridge is not bridge):
            raise ValueError("Invalid trusted Copilot delegation binding")
        self.delegation = delegation
        self.bridge = bridge
        self.enabled_tools = enabled_tools
        self._session_claimed = False

    def validate_catalog(self, catalog: dict) -> None:
        """Check actual tools.list structural schemas before create/resume.

        Descriptions are not authorization; types, required fields, enums and
        property structure must match the reviewed pin. The runtime owner also
        checks SDK/runtime versions. This does not admit additional tools.
        """
        if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
            raise ValueError("Invalid Copilot native tool catalog")
        selected = {}
        for tool in catalog["tools"]:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ValueError("Invalid Copilot native tool catalog")
            name = tool["name"]
            if self.delegation is not None and name == DELEGATE_TOOL:
                raise ValueError("Copilot host tool collides with native catalog")
            if name in self.enabled_tools:
                if name in selected or _structure(tool.get("parameters")) != _SCHEMAS[name]:
                    raise ValueError("Copilot native tool schema changed")
                selected[name] = True
        if set(selected) != self.enabled_tools:
            raise ValueError("Copilot native tool catalog is incomplete")

    def session_options(self, **options) -> dict:
        """Mandatory options for a native-only empty-mode session owner.

        Install on every create/resume; never merge untrusted overrides over
        these fields. The owner must use the pinned empty-mode sandbox runtime,
        validate its catalog and bind the bridge's native session ID before work.
        Source filters alone cannot prevent an SDK custom tool explicitly
        overriding a builtin. Nonempty external tools and protected option
        overrides are therefore forbidden by the guarded session methods.
        """
        if options.keys() - _SESSION_OPTIONS:
            raise ValueError("Copilot native session policy options cannot be overridden")
        if "reasoning_effort" in options and (options["reasoning_effort"] is None
                or not valid_reasoning_effort(options["reasoning_effort"])):
            raise ValueError("Copilot reasoning effort is invalid")
        return {
            **options,
            "available_tools": [f"builtin:{name}" for name in sorted(self.enabled_tools)]
                + ([f"custom:{DELEGATE_TOOL}"] if self.delegation is not None else []),
            "tools": [self.delegation.sdk_tool()] if self.delegation is not None else [], "mcp_servers": {},
            "hooks": {"on_pre_tool_use": self.on_pre_tool_use},
            "on_permission_request": (self.on_permission_request if self.delegation is not None
                                      else self.bridge.on_permission_request),
            "on_user_input_request": self.bridge.on_user_input_request,
            "enable_config_discovery": False, "enable_file_hooks": False,
            "enable_host_git_operations": False,
        }

    async def create_session(self, client, *, session_id: str | None = None, **options):
        """Create with mandatory native-only configuration and checked schemas.

        Use a fresh policy/bridge per native session. The caller owns the pinned
        sandbox runtime and must close it on failure/cancellation; this helper
        does not take ownership of startup, uncertain RPCs or process cleanup.
        """
        configured = self.session_options(**options)
        native_id = uuid.uuid4().hex if session_id is None else session_id
        return await self._open_session(client, native_id, configured, resume=False)

    async def resume_session(self, client, session_id: str, **options):
        """Resume only owned history created with this same native-only profile.

        The SDK omits empty tool/MCP lists on the wire, so these options cannot
        convert arbitrary MCP/custom/terminal history into this profile. The
        session owner must retain and verify that provenance with its private
        state. Pending work is not automatically restarted, and failure never
        falls back to a new session.
        """
        configured = self.session_options(**options)
        configured["continue_pending_work"] = False
        return await self._open_session(client, session_id, configured, resume=True)

    async def _open_session(self, client, session_id, options, *, resume):
        if self._session_claimed or not _text(session_id, 256):
            raise NativePolicySessionError("Copilot native policy session is unavailable")
        self._session_claimed = True
        try:
            from copilot.rpc import ToolsListRequest

            # Bind before awaiting any startup activity; hooks cannot be
            # credited to a different native or platform session.
            self.bridge.bind_sdk_session(session_id)
            async with asyncio.timeout(5):
                catalog = await client.rpc.tools.list(ToolsListRequest(model=options.get("model")), timeout=5)
            self.validate_catalog(catalog.to_dict())
            async with asyncio.timeout(15):
                if resume:
                    session = await client.resume_session(session_id, **options)
                else:
                    session = await client.create_session(session_id=session_id, **options)
            if not self.bridge.matches_sdk_session(session.session_id):
                raise NativePolicySessionError("Copilot native session identity changed")
            return session
        except Exception:
            pass
        raise NativePolicySessionError("Copilot native policy session could not be opened")

    async def on_permission_request(self, request, invocation):
        # Native admission alone cannot execute a host capability. The fixed
        # handler reauthorizes through Oto immediately before dispatch, once.
        from copilot.rpc import PermissionDecisionApproveOnce
        if self.delegation is not None and self.delegation.admit_permission(request, invocation):
            return PermissionDecisionApproveOnce()
        return await self.bridge.on_permission_request(request, invocation)

    async def on_pre_tool_use(self, request, invocation):
        def project():
            if (not isinstance(original, dict) or request != original
                    or not self.bridge.matches_sdk_session(original.get("sessionId"))
                    or original.get("workingDirectory") != self.bridge.working_directory
                    or original.get("toolName") not in self.enabled_tools
                    or original.keys() - {"sessionId", "timestamp", "workingDirectory", "toolName", "toolArgs"}):
                return None
            return project_native_tool(original["toolName"], original.get("toolArgs"),
                                       working_directory=self.bridge.working_directory)

        try:
            original = deepcopy(request)
            if (self.delegation is not None and isinstance(original, dict)
                    and original.get("sessionId") == invocation.get("session_id")
                    and original.get("workingDirectory") == self.bridge.working_directory
                    and not original.keys() - {"sessionId", "timestamp", "workingDirectory", "toolName", "toolArgs"}
                    and self.delegation.admit(original.get("toolName"), original.get("toolArgs"), invocation)):
                return {"permissionDecision": "allow"}
            allowed = await self.bridge.authorize_operation(project, invocation, require_bound_session=True)
            if allowed is True and request == original and project() is not None:
                return {"permissionDecision": "allow"}
        except (asyncio.CancelledError, Exception):
            pass
        # The SDK catches hook exceptions and returns None, which is permissive.
        # Keep all refusals explicit and payload-free, including cancellation.
        return dict(_DENY)
