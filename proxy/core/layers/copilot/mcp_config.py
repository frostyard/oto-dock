"""Prepare explicitly configured stdio MCPs for the Copilot SDK.

The SDK passes its inference credentials to MCP subprocesses. Every stdio
server therefore needs the existing Oto interceptor, even without broker or
path-translation markers. This is a config transform, not a sandbox boundary.
HTTP transport and native/discovered tools require separate policy adapters.
"""

from __future__ import annotations

from copy import deepcopy
import re

INFERENCE_TOKEN_NAMES = (
    "COPILOT_SDK_AUTH_TOKEN", "COPILOT_CONNECTION_TOKEN", "COPILOT_GITHUB_TOKEN",
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def wrap_stdio_servers(
    servers: dict, *, interpreter: str, interceptor_path: str,
) -> dict:
    """Return an isolated SDK config with mandatory credential stripping.

    Call after broker/path markers are injected and before create/resume. The
    interpreter and script path must refer to provisioned files in the target
    runtime's filesystem. Unknown transports fail explicitly; never silently
    drop an MCP or claim HTTP broker support. Values are never included in
    validation errors because configuration may contain credentials.
    """
    if not isinstance(servers, dict):
        raise ValueError("Copilot MCP servers must be a mapping")
    for value in (interpreter, interceptor_path):
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError("Copilot MCP interceptor executable and path are required")
    result = deepcopy(servers)
    for server in result.values():
        if not isinstance(server, dict) or server.get("type", "stdio") != "stdio":
            raise ValueError("Copilot MCP adapter currently requires stdio transport")
        command = server.get("command")
        args = server.get("args", [])
        env = server.get("env", {})
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("Copilot stdio MCP requires an executable command")
        if not isinstance(args, list) or any(not isinstance(a, str) or "\0" in a for a in args):
            raise ValueError("Copilot MCP arguments must be strings")
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not _ENV_NAME.fullmatch(k)
            or not isinstance(v, str) or "\0" in v for k, v in env.items()
        ):
            raise ValueError("Copilot MCP environment must contain valid string entries")
        strip_names = set(INFERENCE_TOKEN_NAMES)
        for key in list(env):
            if key.upper() == "OTO_STRIP_KEYS":
                for name in env.pop(key).split(","):
                    name = name.strip()
                    if name and not _ENV_NAME.fullmatch(name):
                        raise ValueError("Copilot MCP strip list contains an invalid name")
                    if name:
                        strip_names.add(name.upper())
            elif key.upper() in INFERENCE_TOKEN_NAMES:
                # Do not persist inference credentials in per-tool config.
                env.pop(key)
        env["OTO_STRIP_KEYS"] = ",".join(sorted(strip_names))
        server["env"] = env
        already_wrapped = command == interpreter and args[:2] == [interceptor_path, "--"]
        if already_wrapped:
            if len(args) < 3 or not args[2].strip():
                raise ValueError("Copilot MCP interceptor requires a child command")
        else:
            server["command"] = interpreter
            server["args"] = [interceptor_path, "--", command, *args]
    return result
