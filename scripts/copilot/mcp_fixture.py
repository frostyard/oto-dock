#!/usr/bin/env python3
"""Tiny stdio MCP fixture: one fixed response, no shell or arbitrary file tools."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

MARKER = "OTO_MCP_FIXTURE_OK"
TOKEN_NAMES = (
    "COPILOT_SDK_AUTH_TOKEN", "COPILOT_CONNECTION_TOKEN",
    "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
)


def token_presence(env: dict[str, str]) -> dict[str, bool]:
    """Only report presence; never serialize token values."""
    return {name: bool(env.get(name)) for name in TOKEN_NAMES}


def handle(message: dict, audit: Path) -> dict | None:
    if "id" not in message:
        return None
    method = message.get("method")
    response = {"jsonrpc": "2.0", "id": message["id"]}
    if method == "initialize":
        response["result"] = {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "oto-fixture", "version": "1.0.0"},
        }
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": [{
            "name": "marker", "description": "Return the fixed compatibility marker.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            # Leave readOnlyHint absent so this probe observes the permission
            # callback instead of relying on automatic read-only approval.
        }]}
    elif method == "tools/call" and message.get("params", {}).get("name") == "marker":
        if message["params"].get("arguments") not in (None, {}):
            response["error"] = {"code": -32602, "message": "No arguments accepted"}
        else:
            with audit.open("a") as stream:
                stream.write(json.dumps({"call": "marker", "token_presence": token_presence(os.environ)}) + "\n")
            response["result"] = {"content": [{"type": "text", "text": MARKER}]}
    else:
        response["error"] = {"code": -32601, "message": "Method or tool not found"}
    return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    for line in sys.stdin:
        try:
            response = handle(json.loads(line), args.audit)
        except (ValueError, TypeError, KeyError):
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Invalid request"}}
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
