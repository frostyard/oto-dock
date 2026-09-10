"""Explicit local ExecutionLayer adapter; intentionally absent from _LAYERS.

Authenticated callers must construct CopilotAgentConfig deliberately. Generic
config builders do not select Copilot accounts or provision this native profile.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from auth.path_policy import SecurityContext
from core.execution_layer import AgentConfig, ExecutionLayer, LayerCapabilities
from core.layers.copilot.credentials import CopilotAccountScope
from core.layers.copilot.local_session import CopilotLocalSession, CopilotLocalSessionConfig
from core.layers.copilot.sandbox_home import CopilotSandboxHomes
from core.layers.copilot.session_records import CopilotSessionRecords
from core.sandbox.sandbox import SandboxBuilder, SandboxMount, resolve_sandbox_config
from core.session import session_state as state
from core.session.owned_sessions import register_owned_session, release_owned_session


class CopilotLayerError(RuntimeError):
    """A sanitized lifecycle/configuration failure."""


_PERMISSION_MODES = ("default", "acceptEdits", "plan", "dontAsk")


@dataclass
class CopilotAgentConfig(AgentConfig):
    execution_path: str = "copilot-cli"
    account_id: str = ""
    account_scope: CopilotAccountScope | None = None
    enabled_tools: frozenset[str] = frozenset()


@dataclass
class _Entry:
    session_id: str
    config: CopilotAgentConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: CopilotLocalSession | None = None
    context: object = None
    registration_started: bool = False
    startup: asyncio.Task | None = None
    closing: asyncio.Task | None = None
    reaper: asyncio.Task | None = None
    claim: object = None


# Reserve before any await/registration, including across separately constructed
# adapters. The durable record independently excludes cross-process writers.
_claims: dict[str, _Entry] = {}
_producer_owner: ContextVar[_Entry | None] = ContextVar("copilot_producer_owner", default=None)


async def _join_cleanup(task):
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise CopilotLayerError("Copilot layer cleanup was cancelled") from None
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


class CopilotExecutionLayer(ExecutionLayer):
    def __init__(self, *, runtime_path: Path, records: CopilotSessionRecords,
                 homes: CopilotSandboxHomes):
        if type(records) is not CopilotSessionRecords or type(homes) is not CopilotSandboxHomes:
            raise CopilotLayerError("Explicit Copilot host storage is required")
        roots = (records.root, records.state_root, homes.root)
        if any(a.is_relative_to(b) or b.is_relative_to(a)
               for index, a in enumerate(roots) for b in roots[index + 1:]):
            raise CopilotLayerError("Copilot host storage roots must be separate")
        self._runtime_path = Path(runtime_path).resolve(strict=True)
        self._records, self._homes = records, homes
        self._sessions: dict[str, _Entry] = {}
        self._closing: asyncio.Task | None = None

    @property
    def capabilities(self):
        # Explicit config.resume is supported below. Generic cold-resume
        # discovery lacks payer/profile authority, so don't advertise it yet.
        return LayerCapabilities(
            name="copilot-cli", display_name="GitHub Copilot (local preview)",
            supports_permissions=True, supports_mcps=False,
            permission_modes=list(_PERMISSION_MODES),
            mcp_delivery="external_config", mcp_config_format=None,
        )

    @staticmethod
    def _validate(session_id, config):
        if type(config) is not CopilotAgentConfig:
            raise CopilotLayerError("Explicit Copilot account configuration is required")
        ctx = config.security_context
        if (type(ctx) is not SecurityContext or ctx.agent != config.agent_name
                or ctx.target_kind != "local" or ctx.principal != "user"
                or ctx.cli_session_id not in {"", session_id.lower()}
                or any(getattr(ctx, name) for name in (
                    "target_label", "target_agents_dir", "target_machine_id", "target_home_dir",
                    "target_allow_full_fs", "target_claude_runtime_root", "target_os_user",
                    "target_user_dirs", "target_device_grants", "session_allowed_roots", "work_cwd",
                    "external_channel", "external_id",
                    "external_home", "external_ephemeral", "external_verified", "external_claim"))
                or config.execution_path != "copilot-cli" or config.execution_target != "local"
                or config.client_type not in {"dashboard", "sse"}
                or not config.user_sub or config.permission_mode not in _PERMISSION_MODES
                or type(config.resume) is not bool
                or any(getattr(config, name) for name in (
                    "mcp_config_path", "credential_env", "mcp_secret_bundles", "extra_env", "effort",
                    "subscription_id", "sandbox_host_claude_dir", "codex_thread_id", "multi_value_envs",
                    "interactive", "interactive_theme", "interactive_first_prompt", "chat_id",
                    "work_cwd", "use_native_permissions",
                    "default_execution_mode", "term", "fallback_reason"))
                or config.subscription_user_sub is not None):
            raise CopilotLayerError("Unsupported Copilot local session configuration")
        return CopilotLocalSessionConfig(
            session_id, config.account_id, config.account_scope, config.user_sub,
            config.model, config.enabled_tools, config.system_prompt,
        )

    async def start_session(self, session_id, config):
        if self._closing is not None:
            raise CopilotLayerError("Copilot execution layer is closed")
        failed = False
        try:
            config = deepcopy(config)
            local = self._validate(session_id, config)
        except Exception:
            failed = True
        if failed:
            raise CopilotLayerError("Invalid Copilot local session request")
        from core.session.session_manager import has_legacy_session

        if (session_id in _claims or state.get_session_security(session_id) is not None
                or has_legacy_session(session_id)):
            raise CopilotLayerError("Copilot platform session is already owned")
        entry = _Entry(session_id, config)
        entry.claim = register_owned_session(
            session_id=session_id, engine="copilot-cli", agent=config.agent_name,
            user_sub=config.user_sub, username=config.security_context.mount_username,
            active=lambda: (self._closing is None and entry.closing is None
                            and entry.owner is not None and entry.owner.alive),
            close=lambda: self._close_entry(entry),
        )
        _claims[session_id] = self._sessions[session_id] = entry
        entry.startup = asyncio.create_task(self._start(entry, local))
        try:
            await entry.startup
            if entry.closing is not None or not entry.claim.active:
                raise CopilotLayerError("Copilot session closed during startup")
            return
        except asyncio.CancelledError:
            await self._close_entry(entry)
            raise
        except Exception:
            failed = True
        with suppress(Exception):
            await self._close_entry(entry)
        raise CopilotLayerError("Copilot execution session could not be started")

    async def _start(self, entry, local):
        config, ctx = entry.config, entry.config.security_context
        from core.session.session_manager import has_legacy_session

        if (state.get_session_security(entry.session_id) is not None
                or has_legacy_session(entry.session_id)):
            raise CopilotLayerError("Copilot platform session was claimed before startup")
        # No await between registration and capturing the actual stamped object.
        entry.registration_started = True
        try:
            state.register_session_state(entry.session_id, config.permission_mode, ctx)
        finally:
            entry.context = state.get_session_security(entry.session_id)
        state._record_session_use(entry.session_id, config.client_type, config.agent_name)
        home = self._homes.get(entry.session_id)
        sandbox = resolve_sandbox_config(
            role=ctx.role, username=ctx.mount_username, agent_name=config.agent_name,
            is_admin_agent=ctx.is_admin_agent, host_claude_dir=home, user_sub=config.user_sub,
            isolated_config_home=True,
            config_visible=ctx.config_visible, mount_shared=ctx.mount_shared,
            knowledge_rw=ctx.knowledge_rw, mcp_dir_binds=[],
            trusted_runtime_mounts=[SandboxMount(str(self._runtime_path.parent), "/opt/copilot-runtime", "ro")],
        )
        builder = SandboxBuilder(sandbox)
        command = builder.build_command_prefix([])
        for index, option in enumerate(command):
            if option in {"--bind", "--ro-bind", "--bind-try", "--ro-bind-try"}:
                source = Path(command[index + 1]).resolve()
                if (self._homes.root.is_relative_to(source)
                        or (source.is_relative_to(self._homes.root) and source != home)):
                    raise CopilotLayerError("Copilot scratch homes overlap sandbox mounts")
        entry.owner = await CopilotLocalSession.open(
            local, builder=builder, runtime_path=self._runtime_path,
            records=self._records, resume=config.resume,
        )
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        entry.reaper = asyncio.create_task(self._watch(entry))

    async def _watch(self, entry):
        try:
            with suppress(Exception):
                await entry.owner.wait_closed()
            self._begin_close(entry)
        except asyncio.CancelledError:
            return

    async def _finish(self, entry):
        failed = False
        if entry.startup is not None and not entry.startup.done():
            entry.startup.cancel()
            await asyncio.gather(entry.startup, return_exceptions=True)
        if entry.reaper is not None:
            entry.reaper.cancel()
            await asyncio.gather(entry.reaper, return_exceptions=True)
        try:
            if entry.owner is not None:
                await entry.owner.close()
        except (Exception, asyncio.CancelledError):
            failed = True
        finally:
            try:
                # A replacement registration belongs to its new owner, even if
                # it caused this runtime to be revoked. Never erase that state.
                if (entry.registration_started
                        and state.get_session_security(entry.session_id) is entry.context):
                    state.cleanup_session_permission_state(entry.session_id)
                    if not failed:
                        from core.concurrency import release_chat_slot
                        release_chat_slot(entry.session_id)
            except Exception:
                failed = True
            finally:
                if not failed and self._sessions.get(entry.session_id) is entry:
                    del self._sessions[entry.session_id]
                if not failed and _claims.get(entry.session_id) is entry:
                    del _claims[entry.session_id]
                if not failed:
                    release_owned_session(entry.claim)
        if failed:
            raise CopilotLayerError("Copilot execution cleanup is incomplete")

    def _begin_close(self, entry):
        if entry.closing is None:
            entry.closing = asyncio.create_task(self._finish(entry))
            entry.closing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return entry.closing

    async def _close_entry(self, entry):
        self._begin_close(entry)
        await _join_cleanup(entry.closing)

    async def close_session(self, session_id):
        entry = self._sessions.get(session_id)
        if entry is not None:
            await self._close_entry(entry)

    async def _finish_all(self, entries):
        results = await asyncio.gather(
            *(self._close_entry(entry) for entry in entries), return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            raise CopilotLayerError("Copilot execution cleanup is incomplete")

    async def aclose(self):
        """Seal this instance and join every captured generation before returning.

        Other layer instances keep their own sessions. A failed cleanup remains
        claimed and this instance stays sealed; a cancelled waiter still joins
        all cleanup before cancellation propagates to its caller.
        """
        if self._closing is None:
            entries = tuple(self._sessions.values())
            self._closing = asyncio.create_task(self._finish_all(entries))
            self._closing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await _join_cleanup(self._closing)

    def _live(self, session_id):
        entry = self._sessions.get(session_id)
        if (self._closing is not None or entry is None or entry.closing is not None
                or entry.owner is None or not entry.claim.active):
            raise CopilotLayerError("Copilot execution session is unavailable")
        return entry

    async def send_message(self, session_id, message, **kwargs):
        if any(key != "inject_time" or value is not False for key, value in kwargs.items()):
            raise CopilotLayerError("Unsupported Copilot message options")
        entry = self._live(session_id)
        producer = _producer_owner.get()
        if producer is not None and producer is not entry:
            raise CopilotLayerError("Copilot producer belongs to a previous session owner")
        stream = entry.owner.stream(message)
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()

    async def abort(self, session_id):
        # False means hard cleanup in ExecutionLayer. A native acknowledgement
        # alone does not prove preserved partial history, so never return True.
        await self.close_session(session_id)
        return False

    async def respond_permission(self, session_id, request_id, approved):
        self._live(session_id)
        if type(approved) is not bool or state.get_permission_request_session(request_id) != session_id:
            raise CopilotLayerError("Copilot permission request is unavailable")
        state.resolve_permission(request_id, approved)

    async def change_model(self, session_id, model):
        if self._live(session_id).config.model != model:
            raise CopilotLayerError("Copilot model changes require a new session profile")

    async def change_mode(self, session_id, mode):
        if (self._live(session_id).config.permission_mode != mode
                or state.get_session_mode(session_id) != mode):
            raise CopilotLayerError("Copilot permission mode changes are not yet supported")

    async def send_control_request(self, session_id, subtype, **kwargs):
        self._live(session_id)
        return {"error": "Unsupported Copilot control command"}

    async def get_session(self, session_id):
        entry = self._sessions.get(session_id)
        return entry.owner if entry is not None else None

    async def is_session_alive(self, session_id):
        try:
            self._live(session_id)
            return True
        except CopilotLayerError:
            return False

    @asynccontextmanager
    async def session_lock(self, session_id):
        entry = self._live(session_id)
        async with entry.lock:
            if self._live(session_id) is not entry:
                raise CopilotLayerError("Copilot execution session changed")
            token = _producer_owner.set(entry)
            try:
                yield
            finally:
                _producer_owner.reset(token)

    async def is_session_process_dead(self, session_id):
        # Unusable does not imply dead. Startup, revocation and cleanup may
        # still own processes. Failed cleanup retains its claim as a tombstone.
        return session_id not in _claims

    async def prepare_resume(self, session_id):
        if await self.is_session_alive(session_id):
            raise CopilotLayerError("Cannot prepare a live Copilot session for resume")
        await self.close_session(session_id)

    async def history_ready(self, session_id: str, owner_sub: str) -> bool:
        """Read-only private-record candidate proof, never resume admission."""
        from core.session.owned_sessions import get_owned_session

        if get_owned_session(session_id) is not None:
            return False
        ready = await asyncio.to_thread(self._records.is_ready, session_id, owner_sub)
        return ready and get_owned_session(session_id) is None

    async def can_resume_session(self, session_id, *, agent_name="", username="", external_home=""):
        # This legacy query carries no account/driver/profile authorization.
        # Explicit CopilotAgentConfig(resume=True) verifies those in the factory.
        return False
