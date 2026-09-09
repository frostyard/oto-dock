"""DirectLLMExecutionLayer — wraps DirectSession for direct Anthropic API calls.

Translates run_direct_stream SSE dicts → CommonEvent. Does NOT modify
the DirectSession machinery (core/layers/direct/session.py) — wraps it.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import config as app_config

from core.events.common_events import (
    CommonEvent,
    TEXT, THINKING, TOOL_USE, TOOL_RESULT, DONE, ERROR, SYSTEM, METADATA,
    CONTEXT_COMPACT,
)
from core.execution_layer import ExecutionLayer, AgentConfig, LayerCapabilities
from core.session.session_state import (
    _record_session_use, cleanup_session_permission_state, register_session_state,
    set_session_mode,
)
from core.layers.direct.session import (
    DirectSession,
    create_direct_session, get_direct_session,
    close_direct_session, run_direct_stream,
)

logger = logging.getLogger("claude-proxy")


# ---------------------------------------------------------------------------
# Direct LLM SSE → CommonEvent translator
# ---------------------------------------------------------------------------

def direct_event_to_common(event: dict) -> CommonEvent | None:
    """Translate a single run_direct_stream SSE event dict into a CommonEvent.

    run_direct_stream yields dicts like: {"type": "text", "data": {...}}
    """
    etype = event.get("type", "")
    data = event.get("data", {})

    if etype == "session":
        return None  # internal plumbing, not user-visible content

    if etype == "text":
        content = data.get("content", "")
        if content:
            return CommonEvent(type=TEXT, data={"content": content})
        return None

    if etype == "thinking":
        # Same phase contract as the Codex translator: start / delta / end.
        return CommonEvent(type=THINKING, data=data)

    if etype == "tool_start":
        return CommonEvent(type=TOOL_USE, data={
            "name": data.get("name", ""),
            "tool_id": data.get("tool_use_id", ""),
        })

    if etype == "tool_end":
        return CommonEvent(type=TOOL_RESULT, data={
            "name": "",  # run_direct_stream doesn't include name in tool_end
            "tool_id": data.get("tool_use_id", ""),
            "result_preview": data.get("result_preview", ""),
        })

    if etype == "metadata":
        return CommonEvent(type=METADATA, data={
            **data,
            "cost_is_delta": True,  # Direct LLM emits per-turn cost, not cumulative
        })

    if etype == "context_compact":
        return CommonEvent(type=CONTEXT_COMPACT, data=data)

    if etype == "done":
        return CommonEvent(type=DONE, data={})

    if etype == "error":
        return CommonEvent(type=ERROR, data={
            "message": data.get("message", "Unknown error"),
        })

    # Unknown event type — pass through as system
    return CommonEvent(type=SYSTEM, data={"subtype": etype, **data})


# ---------------------------------------------------------------------------
# Direct LLM layer capabilities
# ---------------------------------------------------------------------------

_DIRECT_CAPABILITIES = LayerCapabilities(
    name="direct-llm",
    display_name="Direct LLM API",
    supports_resume=False,
    supports_permissions=True,
    supports_plan_mode=False,
    supports_todos=False,
    supports_subagents=False,
    supports_context_compression=False,  # future: Anthropic compaction API
    supports_control_commands=False,
    supports_mcps=True,
    permission_modes=["default", "acceptEdits", "dontAsk"],
    control_commands=[],
    models=app_config.get_layer_models("direct-llm"),
    effort_levels=["low", "medium", "high", "xhigh", "max"],
    effort_changeable_mid_session=False,
    compression_threshold_pct=None,
    mcp_delivery="proxy_managed",
    mcp_config_format=None,
    providers=[
        {"id": "anthropic", "label": "Anthropic", "requires_key": True},
        {"id": "openai", "label": "OpenAI", "requires_key": True},
        {"id": "groq", "label": "Groq", "requires_key": True},
        # Local providers reach the operator's own network — unavailable on
        # hosted OtoDock (no operator LAN). Gated off at import on cloud.
        *([] if app_config.OTODOCK_CLOUD else [
            {"id": "ollama", "label": "Ollama", "requires_key": False},
            {"id": "openai_compatible", "label": "OpenAI-compatible endpoint", "requires_key": False},
        ]),
    ],
)


# ---------------------------------------------------------------------------
# DirectLLMExecutionLayer
# ---------------------------------------------------------------------------

def _plain_text(message: dict) -> str:
    """The visible text of a stored message, whatever provider shape it has:
    a string content; the ``text`` blocks of a content list (Anthropic blocks
    and the chat-shape blocks share that shape — images, tool_use /
    tool_result / thinking / server-tool blocks are dropped); an OpenAI
    assistant dict's ``content`` string. "" when nothing visible remains."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        ]
        return "\n\n".join(p for p in parts if p)
    return ""


def _text_only_history(messages: list[dict]) -> list[dict]:
    """The history as plain text turns — what a chat keeps when it switches to
    another provider (the same shape the DB resume builds). Tool calls, tool
    results, reasoning items and images are provider-shaped and dropped; the
    user's and the assistant's visible text stays. Consecutive same-role
    turns (an assistant's text before and after a tool loop) merge, and
    leading assistant turns go (every provider wants the user to speak
    first)."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _plain_text(m)
        if not text:
            continue
        if not out and role != "user":
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    return out


def _rebuild_history_from_db(session, session_id: str, chat_id: str = "") -> None:
    """Reconstruct conversation history from DB chat_messages.

    Direct LLM sessions are in-memory only — lost on proxy restart or idle
    reap. When resuming, we rebuild the messages list from persisted
    user/assistant messages so the LLM sees the full conversation context.

    If *chat_id* is provided, look up the chat directly (works after restart
    when the session_id in the DB was just updated to a new value).
    """
    from storage import database as task_store
    # Prefer chat_id lookup (works after restart when session_id changed)
    if chat_id:
        chat = task_store.get_chat(chat_id)
    else:
        chat = task_store.get_chat_by_session(session_id)
    if not chat:
        logger.info(f"Direct session resume: no chat found for session {session_id}")
        return

    messages_db = task_store.get_chat_messages(chat["id"])
    rebuilt: list[dict] = []
    for m in messages_db:
        role = m.get("role", "")
        content = m.get("content", "")
        if role in ("user", "assistant") and content:
            rebuilt.append({"role": role, "content": content})

    # Deferred tools the model already discovered in this chat: re-load them
    # so it can keep calling them without another tool_search. A tool call
    # persists as its OWN row (role "event", event_type "tool", the block in
    # event_data — stream_pump._serialize_turn_rows), never inside the
    # assistant text row.
    catalog = getattr(session, "catalog", None)
    if catalog is not None:
        used: list[str] = []
        for m in messages_db:
            if m.get("role") != "event" or m.get("event_type") != "tool":
                continue
            try:
                block = json.loads(m.get("event_data") or "{}")
            except ValueError:
                continue
            name = block.get("name") if isinstance(block, dict) else None
            if isinstance(name, str) and name.startswith("mcp__") and name not in used:
                used.append(name)
        if used:
            new_defs = catalog.load(used)
            session.tools.extend(new_defs)
            resident = {t.get("name") for t in session.tools}
            gone = [n for n in used if n not in catalog.deferred and n not in resident]
            logger.info(
                f"Direct session resume: re-loaded {len(new_defs)} deferred tool(s) "
                f"from chat {chat['id'][:8]}"
                + (f"; {len(gone)} no longer available: {gone}" if gone else "")
            )

    # If the previous turn was aborted, drop trailing aborted messages
    # (the aborted user message + any partial assistant responses saved
    # by the pump during streaming). The dashboard will re-inject the
    # cancelled turn context as part of the next user message — if we
    # kept those messages here, the LLM would see them twice.
    if chat.get("last_turn_aborted") and rebuilt:
        last_user_idx = -1
        for i in range(len(rebuilt) - 1, -1, -1):
            if rebuilt[i]["role"] == "user":
                last_user_idx = i
                break
        if last_user_idx >= 0:
            dropped = len(rebuilt) - last_user_idx
            rebuilt = rebuilt[:last_user_idx]
            logger.info(
                f"Direct session resume: dropped {dropped} message(s) from "
                f"aborted turn (will be re-injected as cancelled context)"
            )

    if rebuilt:
        # Estimate tokens: ~4 chars per token. If history is too large
        # relative to context window, keep only recent messages.
        context_window = app_config.get_model_context_window(session.model)
        estimated_tokens = sum(len(m.get("content", "")) for m in rebuilt) // 4
        # Reserve 40% for system prompt + tools + response
        max_history_tokens = int(context_window * 0.6)
        KEEP_MESSAGES = 6
        if estimated_tokens > max_history_tokens and len(rebuilt) > KEEP_MESSAGES:
            dropped = len(rebuilt) - KEEP_MESSAGES
            rebuilt = rebuilt[-KEEP_MESSAGES:]
            logger.info(
                f"Direct session resume: truncated {dropped} old messages "
                f"(~{estimated_tokens} tokens > {max_history_tokens} limit)"
            )
        session.messages = rebuilt
        logger.info(
            f"Direct session resume: rebuilt {len(rebuilt)} messages "
            f"from chat {chat['id'][:8]} for session {session_id[:8]}"
        )


def direct_mcp_policy(client_type: str) -> tuple[bool, int]:
    """``(enable_http_transport, tool_timeout)`` for a direct session by
    client type. Chats, tasks and meetings get the sidecar HTTP MCPs
    (file-tools, video-tools, browser…) and the chat call cap; a phone call
    stays stdio-only with the short cap — a document render behind a 300 s
    timeout must never hang a caller."""
    from core.layers.direct.mcp import TOOL_CALL_TIMEOUT, TOOL_CALL_TIMEOUT_CHAT
    if client_type == "phone":
        return False, TOOL_CALL_TIMEOUT
    return True, TOOL_CALL_TIMEOUT_CHAT


class DirectLLMExecutionLayer(ExecutionLayer):
    """Execution layer wrapping the Direct Anthropic API path.

    Delegates to the existing DirectSession functions
    (core/layers/direct/session.py) — does not modify them.
    """

    # Track active streaming tasks for abort support
    _active_streams: dict[str, asyncio.Task] = {}

    async def start_session(
        self, session_id: str, config: AgentConfig,
    ) -> None:
        """Create a direct LLM session with MCP servers.

        The permission mode + security context are registered BEFORE the MCP
        processes spawn: the session JWT minted into their env derives its
        external claim from the live context. A failed start drops the
        registration again (see the CLI layer).
        """
        register_session_state(session_id, config.permission_mode, config.security_context)
        try:
            await self._start_session_impl(session_id, config)
        except BaseException:
            cleanup_session_permission_state(session_id)
            raise

    async def _start_session_impl(
        self, session_id: str, config: AgentConfig,
    ) -> None:
        # Fail CLOSED: a local agent's stdio MCPs MUST run sandboxed +
        # network-isolated. The config builders always set this; empty means an
        # omission — refuse rather than run MCPs un-sandboxed. (The LLM call
        # itself is proxy-side; this guards the MCP subprocesses.)
        if not config.sandbox_host_claude_dir:
            raise RuntimeError(
                f"refusing to start a local Direct-LLM session for agent "
                f"'{config.agent_name}' without a sandbox dir — local agents "
                f"must run sandboxed + network-isolated."
            )
        phone_mode = config.client_type == "phone"
        # An EXTERNAL session (a phone caller who is not a platform user):
        # the MCP set is filtered by the same rule the builder applied
        # (mcp_registry.session_exclusion_reason), here for the per-MCP dir
        # binds and again inside the manager's own config rebuild.
        from core.session.external_identity import external_home_of, is_external_ctx
        external = is_external_ctx(config.security_context)
        extra = config.extra_env or {}
        # Provider-aware: subscription_pool sets _API_KEY/_PROVIDER/_ENDPOINT_URL
        # on direct-llm sessions (see services/engines/subscription_pool.py — the
        # "else" branch keyed on execution_path).
        api_key = extra.get("_API_KEY")
        provider = extra.get("_PROVIDER", "anthropic")
        endpoint_url = extra.get("_ENDPOINT_URL")

        # Build bwrap sandbox for MCP subprocesses (mirrors CLI layer pattern).
        # Each stdio MCP gets wrapped individually in bwrap so it sees the
        # same restricted mount namespace as CLI MCPs.
        sandbox_builder = None
        if config.sandbox_host_claude_dir:
            from core.sandbox.sandbox import SandboxBuilder, SandboxMount, resolve_sandbox_config
            from services.mcp import mcp_registry as mcp_reg

            ctx = config.security_context
            # Resolve MCP sandbox mounts from manifest declarations.
            # is_remote=False (fail-closed default, explicit here): LOCAL bwrap
            # mount path — satellite_only device MCPs never mount locally.
            mcp_mounts: list[SandboxMount] = []
            assigned_mcps = mcp_reg.get_agent_mcps(config.agent_name, is_remote=False) or []
            for manifest in assigned_mcps:
                for m in getattr(manifest, "sandbox_mounts", []):
                    # Host is allowlisted to the agent / mcps tree in the sandbox
                    # builder; only the ${mcp_dir} template is offered.
                    host = m.host.replace("${mcp_dir}", str(manifest.mcp_dir))
                    mcp_mounts.append(SandboxMount(host=host, sandbox=m.sandbox, mode=m.mode))

            # Per-MCP dir binds: Direct-LLM has no MCP config FILE
            # (proxy-managed delivery), so derive the stdio dirs from the
            # assigned set directly. ``.uv-python`` rides along for venvs
            # whose interpreter is uv-fetched.
            stdio_dirs = [
                str(manifest.mcp_dir)
                for manifest in mcp_reg.filter_manifests_for_session(
                    assigned_mcps,
                    contexts={"phone"} if phone_mode else set(),
                    external=external,
                )
                if manifest.server.transport == "stdio"
            ]
            if stdio_dirs:
                _uv = app_config.MCPS_DIR.resolve() / ".uv-python"
                if _uv.is_dir():
                    stdio_dirs.append(str(_uv))

            sandbox_cfg = resolve_sandbox_config(
                role=ctx.role if ctx else "viewer",
                username=ctx.mount_username if ctx else "",
                agent_name=config.agent_name,
                is_admin_agent=ctx.is_admin_agent if ctx else False,
                host_claude_dir=Path(config.sandbox_host_claude_dir),
                user_sub=config.user_sub,
                mcp_sandbox_mounts=mcp_mounts,
                config_visible=ctx.config_visible if ctx else None,
                mount_shared=ctx.mount_shared if ctx else True,
                knowledge_rw=bool(getattr(ctx, "knowledge_rw", False)) if ctx else False,
                mcp_dir_binds=stdio_dirs,
                external=external,
                external_home=external_home_of(ctx),
            )
            sandbox_builder = SandboxBuilder(sandbox_cfg)

        enable_http, tool_timeout = direct_mcp_policy(config.client_type)
        session = await create_direct_session(
            session_id=session_id,
            agent_name=config.agent_name,
            phone_mode=phone_mode,
            api_key=api_key,
            provider=provider,
            endpoint_url=endpoint_url,
            credential_env=config.credential_env or None,
            system_prompt=config.system_prompt,
            sandbox_builder=sandbox_builder,
            enable_http_transport=enable_http,
            tool_timeout=tool_timeout,
            external=external,
        )
        # The builtin file tools resolve against this session's mount table
        # (the same decisions the MCP subprocesses get from bwrap).
        session.sandbox_cfg = sandbox_builder.cfg if sandbox_builder else None
        # The scope signal for credential acquisition (here + on provider switch
        # in change_model): a real user_sub ⇒ user-scope (own subs, then borrowable
        # admin APIs only); empty ⇒ agent-scope (full platform pool). Set ONCE here
        # from the resolved creds identity and never blanked for a user chat — else
        # change_model would widen scope. The resolver also normalises "" defensively.
        session.user_sub = extra.get("_USER_SUB", "")
        # Override model and effort if specified
        if config.model:
            session.model = config.model
        if config.effort:
            session.effort = config.effort
        # (permission mode + security context were registered before the start)
        # Register session metadata (agent name, client type) — needed by
        # location bridge and other hooks that look up sessions by agent name
        _record_session_use(session_id, client_type=config.client_type, agent=config.agent_name)
        # Reconstruct conversation history from DB when resuming a direct session
        # (direct sessions are in-memory — lost on proxy restart, idle reap, etc.)
        # Also rebuild when chat_id is provided (restart with new session_id).
        if config.resume or config.chat_id:
            _rebuild_history_from_db(session, session_id, chat_id=config.chat_id)
        # Bind subscription for pool cleanup
        if config.subscription_id:
            from services.engines.subscription_pool import bind_session
            bind_session(
                session_id, config.subscription_id,
                layer="direct-llm", user_sub=config.subscription_user_sub,
            )

    async def send_message(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        """Send message and yield CommonEvents translated from SSE dicts.

        Kwargs:
            barge_in_chars: int — for phone barge-in annotation
            inject_time: bool — prepend current datetime to message (per-turn).
                The session-start system prompt also has a datetime, but it
                goes stale on long-lived sessions. Per-turn injection mirrors
                CLI/Codex behaviour.
            images: list[dict] | None — chat-attached photos. Each entry is
                ``{"base64": str, "media_type": str}``. When non-empty, the
                user message body is built as a content-block list with the
                text block first, then one image block per entry (formatted
                via the provider adapter's ``format_image_content_block``).
                Direct LLM has no built-in Read tool — images are attached
                directly to the API request (Anthropic + OpenAI vision).
        """
        session = await get_direct_session(session_id)
        if not session:
            yield CommonEvent(type=ERROR, data={"message": "Session not found"})
            return

        barge_in_chars = kwargs.get("barge_in_chars")
        inject_time = kwargs.get("inject_time", False)
        images = kwargs.get("images")

        # Register the consumer task so abort() can cancel it. Unlike CLI/Codex,
        # Direct LLM has no subprocess to kill — cancellation must propagate
        # through the async task to close HTTP streams and stop tool execution.
        task = asyncio.current_task()
        if task is not None:
            self._active_streams[session_id] = task

        # Caller must hold session_lock() — do NOT acquire here (deadlock).
        session.touch()
        try:
            async for sse_event in run_direct_stream(
                session, message, barge_in_chars=barge_in_chars,
                inject_time=inject_time, images=images,
            ):
                event = direct_event_to_common(sse_event)
                if event is not None:
                    yield event
        finally:
            # Clear only if still ours (a new send_message may have registered)
            if self._active_streams.get(session_id) is task:
                self._active_streams.pop(session_id, None)

    async def abort(self, session_id: str) -> bool:
        """Cancel the active streaming task.

        Direct LLM has no subprocess — cancellation propagates through the
        async task: the producer's CancelledError unwinds through the SDK's
        stream iterator, closing the HTTP connection and any in-flight MCP
        tool execution. Safe to call even if no stream is active. Never
        graceful: the direct session rebuilds history from the DB and relies
        on the cancelled-context injection (see the resume drop above).
        """
        task = self._active_streams.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        return False

    async def close_session(self, session_id: str) -> None:
        """Close the direct session and its MCP servers."""
        # Writeback refreshed OAuth tokens before closing MCPs
        from core.credentials.credential_writeback import writeback_credential_dirs
        await writeback_credential_dirs(session_id)
        await close_direct_session(session_id)
        # A closed session must not keep a live permission context (see the
        # CLI layer).
        cleanup_session_permission_state(session_id)
        # Release subscription + concurrency slot
        from services.engines.subscription_pool import release_subscription
        release_subscription(session_id)
        from core.concurrency import release_chat_slot
        release_chat_slot(session_id)

    async def respond_permission(
        self, session_id: str, request_id: str, approved: bool,
    ) -> None:
        """Resolve a pending permission request from the tool execution loop."""
        from core.session.session_state import resolve_permission
        resolve_permission(request_id, approved)

    async def change_model(
        self, session_id: str, model: str,
    ) -> None:
        """Update the session's model, switching provider if needed."""
        session = await get_direct_session(session_id)
        if not session:
            return

        new_provider = app_config.get_model_provider(model)
        if new_provider != session.provider:
            # Provider changed — acquire new subscription, release old
            from core.layers.providers.registry import get_adapter
            from services.engines import subscription_pool
            import asyncio as _asyncio

            old_provider = session.provider
            subscription_pool.release_subscription(session_id)
            sub_handle = await _asyncio.to_thread(
                subscription_pool.acquire_subscription,
                "direct-llm", session.user_sub or None,
                provider=new_provider,
            )
            if sub_handle:
                session.provider = sub_handle.provider
                if sub_handle.auth_type == "relay":
                    # Hosted relay: the token is provider-agnostic but the endpoint
                    # is per-provider — mint + re-point for the new provider.
                    creds = subscription_pool.relay_llm_credentials(
                        sub_handle.provider, session.user_sub,
                    )
                    session.api_key = creds[0] if creds else None
                    session.endpoint_url = creds[1] if creds else None
                else:
                    session.api_key = sub_handle.api_key
                    session.endpoint_url = sub_handle.endpoint_url
                subscription_pool.bind_session(
                    session_id, sub_handle.subscription_id,
                    layer="direct-llm", user_sub=session.user_sub or "",
                )
                # The history is provider-shaped (OpenAI tool_calls / reasoning
                # items / tool messages, Anthropic content blocks / tool_result
                # messages, provider image blocks) and the new provider would
                # 400 on it. Keep the conversation as plain text turns — what
                # the DB resume builds — and swap the provider's own server
                # tools (the client tools all carry an input_schema).
                before = len(session.messages)
                session.messages = _text_only_history(session.messages)
                session.tools = [t for t in session.tools if "input_schema" in t]
                session.tools.extend(get_adapter(session.provider).get_builtin_tools())
                logger.info(
                    f"Direct session {session_id[:8]} switched provider: "
                    f"{old_provider} → {session.provider} for model {model}; "
                    f"history kept as text ({before} → {len(session.messages)} messages)"
                )
            else:
                logger.warning(
                    f"No subscription for provider {new_provider} — "
                    f"model change to {model} may fail"
                )

        session.model = model

    async def change_mode(
        self, session_id: str, mode: str,
    ) -> None:
        """No-op — direct LLM doesn't support permission modes yet."""
        pass

    async def send_control_request(
        self, session_id: str, subtype: str, **kwargs,
    ) -> dict:
        """Handle permission mode changes (no CLI control channel)."""
        if subtype == "set_permission_mode":
            mode = kwargs.get("mode", "default")
            set_session_mode(session_id, mode)
            return {"status": "ok"}
        return {}

    # --- Capabilities ---

    @property
    def capabilities(self) -> LayerCapabilities:
        return _DIRECT_CAPABILITIES

    # --- Session access ---

    async def get_session(self, session_id: str) -> DirectSession | None:
        """Return the underlying DirectSession."""
        return await get_direct_session(session_id)

    async def is_session_alive(self, session_id: str) -> bool:
        """Direct sessions are always alive if they exist."""
        session = await get_direct_session(session_id)
        return session is not None

    # --- Session lock + lifecycle ---

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        """Wrap DirectSession.lock for multi-turn producers."""
        session = await get_direct_session(session_id)
        if session:
            async with session.lock:
                yield
        else:
            yield

    async def is_session_process_dead(self, session_id: str) -> bool:
        """Direct sessions have no process — never dead."""
        return False

    async def prepare_resume(self, session_id: str) -> None:
        """No-op — direct sessions don't resume."""
        pass

    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> bool:
        """Direct sessions can't resume (in-memory history).

        Recovery is handled via chat_id in AgentConfig instead — see
        _rebuild_history_from_db.
        """
        return False
