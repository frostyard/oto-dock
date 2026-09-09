"""Deferred tool loading for Direct-LLM sessions.

The core MCP tool schemas alone cost ≈19k tokens per request on a fully
wired agent (79 tools over 11 servers), sent again on every turn and every
tool-loop iteration. Instead of shipping every schema, a session keeps a
RESIDENT set — the client-side builtins, the provider's own tools, and the
tools of servers whose manifest sets ``always_load`` — and DEFERS the rest:
a compact catalog in the system prompt (name + first sentence, grouped by
server) plus one client-side ``tool_search`` tool that loads schemas on
demand. Loaded tools are sticky for the session and are re-loaded from the
chat's persisted tool calls after a restart (``layer._rebuild_history_from_db``).

Client-side and provider-agnostic: identical on Anthropic, OpenAI, Groq and
local servers (Claude Code's ``ToolSearch`` and Codex's ``tool_search`` are
the same idea, done by the CLIs). ``DIRECT_LLM_TOOL_SEARCH``: ``auto``
(default — defer when the deferrable schemas exceed
``DIRECT_LLM_TOOL_SEARCH_THRESHOLD_TOKENS``), ``on``, ``off``.

Cache note: loading a tool changes the tools array, so the next call
re-prefills the prompt prefix on local servers (Anthropic re-caches) once
per load — bounded by the number of searches, never per turn.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

TOOL_SEARCH_NAME = "tool_search"
MODES = ("auto", "on", "off")
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 20
_SELECT_PREFIX = "select:"
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def server_of(name: str) -> str:
    """``mcp__<server>__<tool>`` → ``<server>``; ``""`` for anything else."""
    parts = (name or "").split("__", 2)
    return parts[1] if len(parts) == 3 and parts[0] == "mcp" else ""


def bare_tool(name: str) -> str:
    parts = (name or "").split("__", 2)
    return parts[2] if len(parts) == 3 and parts[0] == "mcp" else name


def first_sentence(text: str, limit: int = 160) -> str:
    """One catalog line from a tool description: up to the first sentence
    end or line break, capped."""
    text = " ".join((text or "").strip().split())
    if not text:
        return ""
    cut = len(text)
    for sep in (". ", "! ", "? "):
        idx = text.find(sep)
        if idx != -1:
            cut = min(cut, idx + 1)
    out = text[:cut].strip()
    if len(out) > limit:
        out = out[:limit - 1].rstrip() + "…"
    return out


# Providers whose server re-processes the WHOLE context whenever the tool
# list changes (llama.cpp / Ollama render the tools ahead of the system text,
# so a load invalidates the KV cache — measured 2026-09-06: 82 s for a 20k
# context at ≈250 tok/s prefill on the operator's GPU, versus 2–5 s for a
# call with an unchanged list). Loads on these providers are batched per
# server: one re-process per server the model needs, not one per tool.
LOCAL_PROVIDERS = frozenset({"ollama", "openai_compatible"})
# A server above this many deferred tools is NOT widened (Home Assistant's
# ~97 would park ≈25k tokens of schemas in every later call); its tools load
# exactly as matched, like on a cloud provider.
LOCAL_LOAD_MAX_TOOLS = 20


def batch_loads_for(provider: str) -> bool:
    return (provider or "").lower() in LOCAL_PROVIDERS


def estimate_tokens(objs) -> int:
    """≈ tokens of a JSON payload (bytes / 4 — the same rule of thumb the
    prompt-size log line uses)."""
    return len(json.dumps(objs, ensure_ascii=False)) // 4


def tool_search_def() -> dict:
    """The universal-format definition of the ``tool_search`` client tool."""
    return {
        "name": TOOL_SEARCH_NAME,
        "description": (
            "Load deferred tools so you can call them. The `# Deferred tools` "
            "section of your instructions lists every tool that is not loaded "
            "yet. Query forms: `select:mcp__server__tool,mcp__server__other` "
            "loads exact names; any other text is keyword-matched against the "
            "server, tool name and description (top max_results). Loaded tools "
            "stay available for the rest of the session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords, or select:<name>,<name> for exact tool names",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum tools to load for a keyword query (default {DEFAULT_MAX_RESULTS})",
                },
            },
            "required": ["query"],
        },
    }


@dataclass
class ToolCatalog:
    """The deferred set of one session: ``deferred`` (name → universal tool
    def) and what has been loaded so far. ``guides`` maps a server to the
    on-demand skill ids that document it (the ``Skill`` tool pointer)."""

    deferred: dict[str, dict]
    guides: dict[str, list[str]] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)

    # -- construction -------------------------------------------------------

    @staticmethod
    def split(tools: list[dict], always_load_servers: set[str]) -> tuple[list[dict], list[dict]]:
        """(resident, deferrable): every ``mcp__*`` tool of a server that is
        not in ``always_load_servers`` is deferrable; builtins and provider
        tools (no ``mcp__`` prefix) are always resident."""
        resident: list[dict] = []
        deferrable: list[dict] = []
        for t in tools:
            server = server_of(t.get("name", ""))
            if server and server not in always_load_servers:
                deferrable.append(t)
            else:
                resident.append(t)
        return resident, deferrable

    @staticmethod
    def should_defer(mode: str, deferrable: list[dict], threshold_tokens: int) -> bool:
        if mode == "off" or not deferrable:
            return False
        if mode == "on":
            return True
        return estimate_tokens(deferrable) > threshold_tokens  # strictly above

    # -- prompt -------------------------------------------------------------

    def catalog_text(self) -> str:
        """The ``# Deferred tools`` prompt section — byte-stable for the
        session (sorted by server, then tool name)."""
        by_server: dict[str, list[str]] = {}
        for name in sorted(self.deferred):
            by_server.setdefault(server_of(name), []).append(name)
        lines = [
            "# Deferred tools",
            "",
            "These tools exist in this session but their definitions are not "
            "loaded yet. To use one, call `tool_search` (keywords, or "
            "`select:<name>,<name>` for exact names) — it loads the definition "
            "and shows the parameters — or call the tool directly by its exact "
            "name: it is loaded on first use. Loaded tools stay available for "
            "the session, and the `Skill` tool loads the tools a guide "
            "describes. Never substitute another tool for one listed here. "
            "Tools not listed here are already loaded.",
        ]
        for server in sorted(by_server):
            guides = self.guides.get(server) or []
            head = f"**{server}**"
            if guides:
                head += " (guide: " + ", ".join(f"`{g}`" for g in guides) + " — Skill tool)"
            lines += ["", head]
            for name in by_server[server]:
                desc = first_sentence(self.deferred[name].get("description", ""))
                lines.append(f"- `{name}` — {desc}" if desc else f"- `{name}`")
        return "\n".join(lines)

    # -- search / load --------------------------------------------------------

    def _exact(self, wanted: str) -> str | None:
        wanted = wanted.strip()
        if not wanted:
            return None
        if wanted in self.deferred:
            return wanted
        # Accept `<server>__<tool>` and a bare tool name when unambiguous.
        candidates = [
            n for n in self.deferred
            if n == f"mcp__{wanted}" or bare_tool(n) == wanted
        ]
        return candidates[0] if len(candidates) == 1 else None

    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> list[str]:
        """Names of deferred tools matching ``query`` (loaded or not). The
        ``select:`` form returns exact hits in the given order; otherwise a
        small keyword scorer over server + tool name (weight 3, prefix 1) and
        description words (weight 1), best first, capped."""
        q = (query or "").strip()
        if not q:
            return []
        if q.lower().startswith(_SELECT_PREFIX):
            out: list[str] = []
            for part in q[len(_SELECT_PREFIX):].split(","):
                hit = self._exact(part)
                if hit and hit not in out:
                    out.append(hit)
            return out
        q_tokens = _tokens(q)
        if not q_tokens:
            return []
        max_results = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), MAX_RESULTS_CAP))
        scored: list[tuple[int, str]] = []
        for name, t in self.deferred.items():
            name_tokens = _tokens(server_of(name).replace("-", " ")) | _tokens(bare_tool(name).replace("_", " "))
            desc_tokens = _tokens(t.get("description", ""))
            score = 0
            for w in q_tokens:
                if w in name_tokens:
                    score += 3
                elif w in desc_tokens:
                    score += 1
                elif len(w) >= 3 and any(nt.startswith(w) for nt in name_tokens):
                    score += 1
            if score:
                scored.append((-score, name))
        scored.sort()
        return [n for _s, n in scored[:max_results]]

    def load(self, names: list[str], *, whole_server: bool = False) -> list[dict]:
        """Mark ``names`` loaded and return the definitions that were NOT
        loaded before (the caller appends them to the session's tool list).
        Unknown names are ignored. ``whole_server`` widens each name to every
        deferred tool of its server — the local-provider policy (see
        ``batch_loads_for``) — unless the server has more than
        ``LOCAL_LOAD_MAX_TOOLS`` deferred tools."""
        wanted = list(names)
        if whole_server:
            servers = {server_of(n) for n in names if n in self.deferred}
            for srv in servers:
                members = [n for n in self.deferred if server_of(n) == srv]
                if len(members) <= LOCAL_LOAD_MAX_TOOLS:
                    wanted += [n for n in members if n not in wanted]
        new_defs: list[dict] = []
        for n in wanted:
            t = self.deferred.get(n)
            if t is None or n in self.loaded:
                continue
            self.loaded.append(n)
            new_defs.append(t)
        return new_defs

    def is_loaded(self, name: str) -> bool:
        return name in self.loaded
