"""Account-bound local composition, deliberately not a registered ExecutionLayer.

The trusted caller authenticates agent access and registers the platform security
context before open(). It owns platform routing/permission state cleanup. This
component owns the credential observer, durable writer record, sandbox runtime,
native-only SDK profile and CommonEvent supervisor; no raw SDK is exposed.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import uuid

from core.layers.copilot.credentials import CopilotAccountScope, AccountScopeKind
from core.layers.copilot.lease import CopilotLeaseGuard
from core.layers.copilot.native_shells import CopilotNativeShellSession
from core.layers.copilot.native_tool_policy import CopilotNativeToolPolicy, SUPPORTED_NATIVE_TOOLS
from core.layers.copilot.permissions import bind_platform_authority, _text
from core.layers.copilot.runtime import SandboxedCopilotRuntime
from core.layers.copilot.session_records import CopilotSessionProfile, CopilotSessionRecords
from core.layers.copilot.supervisor import CopilotSessionSupervisor


class CopilotLocalSessionError(RuntimeError):
    """Sanitized local session failure, with no credential or SDK exception chain."""


@dataclass(frozen=True)
class CopilotLocalSessionConfig:
    platform_session_id: str
    account_id: str
    scope: CopilotAccountScope
    user_sub: str
    model: str
    enabled_tools: frozenset[str]
    system_prompt: str = ""

    def __post_init__(self):
        if (not all(_text(value, 256) for value in (self.platform_session_id, self.account_id, self.model))
                or type(self.scope) is not CopilotAccountScope
                or not isinstance(self.user_sub, str)
                or (self.scope.kind is AccountScopeKind.PERSONAL and self.scope.user_sub != self.user_sub)
                or not isinstance(self.enabled_tools, frozenset) or not self.enabled_tools
                or not self.enabled_tools <= SUPPORTED_NATIVE_TOOLS
                or not isinstance(self.system_prompt, str) or len(self.system_prompt) > 262144
                or "\x00" in self.system_prompt):
            raise ValueError("Invalid explicit Copilot local session configuration")


def _canonical(value):
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(item) for item in value)
    if type(value) in (str, int, float, bool, type(None)):
        return value
    raise ValueError("Unsupported Copilot ownership configuration")


class CopilotLocalSession:
    def __init__(self):
        raise TypeError("Use CopilotLocalSession.open")

    @classmethod
    async def open(cls, config: CopilotLocalSessionConfig, *, builder, runtime_path: Path,
                   records: CopilotSessionRecords, resume: bool = False, turn_timeout: float = 300):
        instance = object.__new__(cls)
        instance._config = config
        instance._guard = None
        instance._record = None
        instance._runtime = None
        instance._runtime_started = False
        instance._supervisor = None
        instance._context = None
        instance._context_snapshot = None
        instance._close_task = None
        instance._closing_started = asyncio.Event()
        instance._invalid = False
        instance._opened = False
        instance._streaming = False
        instance._completed = False
        instance._uncertain = False
        instance._provider_error = False
        instance._context_watcher = None
        instance._startup_deadline = asyncio.get_running_loop().time() + 60
        failed = False
        try:
            async with asyncio.timeout(60):
                await instance._open(builder, runtime_path, records, resume, turn_timeout)
            return instance
        except asyncio.CancelledError:
            instance._uncertain = True
            await instance.close()
            raise
        except Exception:
            failed = True
        if failed:
            instance._uncertain = True
            with suppress(Exception):
                await instance.close()
            raise CopilotLocalSessionError("Copilot local session could not be opened")

    def _context_valid(self):
        from core.session.session_state import get_session_security

        current = get_session_security(self._config.platform_session_id)
        return (current is self._context and current is not None
                and current == self._context_snapshot)

    def _check(self):
        # A dependency may suppress cancellation and return a late resource.
        # Such a return must never publish a usable owner or dispatch new work.
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        if not self._opened and asyncio.get_running_loop().time() >= self._startup_deadline:
            raise CopilotLocalSessionError("Copilot local session startup exceeded its deadline")
        if (self._invalid or self._close_task is not None or not self._context_valid()
                or self._provider_error
                or (self._supervisor is not None and self._supervisor.failure_detected)
                or (self._runtime_started and not self._runtime.alive)
                or (self._guard is not None and not self._guard.valid)):
            raise CopilotLocalSessionError("Copilot local session ownership is unavailable")

    def _authority_valid(self):
        try:
            self._check()
            return True
        except Exception:
            return False

    async def _watch_context(self):
        try:
            while self._opened and self._close_task is None:
                await asyncio.sleep(0.5)
                if self._close_task is not None:
                    return
                if not self._authority_valid():
                    self._invalidate()
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            self._invalidate()

    def _invalidate(self):
        self._invalid = True
        self._uncertain = True
        if self._supervisor is not None:
            self._supervisor.invalidate_credentials()
        # During startup, _open owns all late acquisitions and joins cleanup
        # before returning an error. An idle opened owner closes independently.
        if self._opened:
            self._begin_close()

    async def _authorize(self):
        self._check()
        await self._guard.authorize()
        self._check()

    def _receive_event(self, event):
        try:
            raw = event if isinstance(event, dict) else event.to_dict()
            if raw.get("type") == "session.error":
                self._uncertain = True
                self._provider_error = True
                raw = {**raw, "data": {"message": "Copilot provider request failed"}}
            self._supervisor.receive_event(raw)
        except Exception:
            self._uncertain = True
            # Invalid conversion is a transport failure, not a callback exception
            # the SDK may swallow while continuing to admit work.
            self._supervisor.receive_event({})
    async def _open(self, builder, runtime_path, records, resume, turn_timeout):
        from auth.path_policy import SecurityContext
        from core.sandbox.sandbox import SandboxBuilder
        from core.session.session_state import get_session_security

        if (type(self._config) is not CopilotLocalSessionConfig or type(resume) is not bool
                or type(builder) is not SandboxBuilder or type(records) is not CopilotSessionRecords
                or type(turn_timeout) not in (int, float) or not math.isfinite(turn_timeout)
                or not 0 < turn_timeout <= 1800):
            raise ValueError("Invalid Copilot local session owner")
        context = get_session_security(self._config.platform_session_id)
        if type(context) is not SecurityContext:
            raise ValueError("Copilot requires registered platform security context")
        cfg = deepcopy(builder.cfg)
        mount_user = context.username if context.session_scope == "user" else ""
        if (context.target_kind != "local" or context.session_scope not in {"user", "agent"}
                or context.principal not in {"user", "agent"}
                or cfg.external or cfg.external_home or context.external_home or context.work_cwd
                or (context.role, context.agent, context.is_admin_agent, mount_user)
                != (cfg.role, cfg.agent_name, cfg.is_admin_agent, cfg.username)
                or context.config_visible != cfg.config_visible
                or ("agent" in context.available_scopes) != cfg.mount_shared
                or context.knowledge_rw != cfg.knowledge_rw
                or tuple(context.knowledge_libraries) != tuple(cfg.knowledge_libraries)):
            raise ValueError("Copilot sandbox and platform context disagree")
        self._context, self._context_snapshot = context, deepcopy(context)
        builder = SandboxBuilder(cfg)
        cwd = builder.get_cwd()
        selected = None
        for mount in builder.workspace_mount_table():
            destination = PurePosixPath(mount.sandbox)
            if PurePosixPath(cwd).is_relative_to(destination):
                candidate = (len(destination.parts), Path(mount.host) / PurePosixPath(cwd).relative_to(destination))
                if selected is None or candidate[0] >= selected[0]:
                    selected = candidate
        if selected is None:
            raise ValueError("Copilot working directory has no sandbox mount")
        working_directory = selected[1].resolve(strict=True)
        runtime_path = Path(runtime_path).resolve(strict=True)
        command = builder.build_command_prefix(["/opt/copilot-runtime/copilot-runtime"])
        # Profile records must not be writable/readable through any supplied
        # bind. The runtime later mounts only the exact private state allocation.
        for index, option in enumerate(command):
            if option in {"--bind", "--ro-bind", "--bind-try", "--ro-bind-try"}:
                source = Path(command[index + 1]).resolve()
                for private in (records.root, records.state_root):
                    if private.is_relative_to(source) or source.is_relative_to(private):
                        raise ValueError("Copilot private records overlap sandbox mounts")
        digest = hashlib.sha256(json.dumps(_canonical({
            "sandbox": cfg, "context": context, "system_prompt": self._config.system_prompt,
            "runtime_path": runtime_path, "cwd": cwd,
        }), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self._check()
        self._guard = await CopilotLeaseGuard.acquire(
            self._config.account_id, self._config.scope, on_invalid=self._invalidate,
        )
        self._check()
        credential = self._guard.credential
        profile = CopilotSessionProfile(
            account_id=self._config.account_id, scope=self._config.scope,
            principal_id=credential.principal_id, credential_kind=credential.kind,
            platform_session_id=self._config.platform_session_id, user_sub=self._config.user_sub,
            agent_id=cfg.agent_name, workspace=str(working_directory), model=self._config.model,
            enabled_tools=self._config.enabled_tools, config_digest=digest,
        )
        self._record = (records.open(profile) if resume else records.create(
            profile, native_session_id=uuid.uuid4().hex,
        ))
        self._check()
        state = self._record.state
        self._runtime = SandboxedCopilotRuntime(
            builder, runtime_path=runtime_path, working_directory=working_directory,
            sandbox_state_directory=state.sandbox_destination, session_state=state,
            credential=credential, environment={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
            startup_timeout=20, shutdown_timeout=5,
        )
        self._supervisor = CopilotSessionSupervisor(
            pending_requests=frozenset, close_runtime=self._runtime.close,
            authorize_submission=self._authorize, rpc_timeout=15, turn_timeout=turn_timeout,
        )
        # The caller already registered authority. No model-supplied session ID
        # can select another user's route or permission queue.
        bridge = bind_platform_authority(
            self._config.platform_session_id, self._supervisor.requests, working_directory=cwd,
            owner_valid=self._authority_valid,
        )
        policy = CopilotNativeToolPolicy(bridge, enabled_tools=self._config.enabled_tools)
        client = await self._runtime.start()
        self._runtime_started = True
        self._check()
        fence = self._runtime.capture_process_fence()
        auth = await client.get_auth_status()
        self._check()
        if getattr(auth, "isAuthenticated", None) is not True:
            raise ValueError("Copilot runtime authentication unavailable")
        models = await client.list_models()
        self._check()
        if not isinstance(models, list):
            raise ValueError("Invalid Copilot model inventory")
        matching = [model for model in models if getattr(model, "id", None) == self._config.model]
        if len(matching) != 1:
            raise ValueError("Requested Copilot model unavailable")
        model_policy = getattr(matching[0], "policy", None)
        if model_policy is not None and getattr(model_policy, "state", None) not in {"enabled", "unconfigured"}:
            raise ValueError("Requested Copilot model policy is unavailable")
        # Revalidate the exact payer again immediately before session creation.
        await self._authorize()
        options = dict(model=self._config.model, streaming=True, enable_session_store=True,
                       on_event=self._receive_event, session_limits={"max_ai_credits": 30.0})
        if self._config.system_prompt:
            options["system_message"] = {"mode": "append", "content": self._config.system_prompt}
        if resume:
            session = await policy.resume_session(client, self._record.native_session_id, **options)
        else:
            session = await policy.create_session(client, session_id=self._record.native_session_id, **options)
        self._check()
        self._supervisor.bind(CopilotNativeShellSession(session, processes_settled=fence.is_settled))
        await self._authorize()
        self._opened = True
        self._context_watcher = asyncio.create_task(self._watch_context(), name="copilot-context-observer")

    async def stream(self, prompt):
        self._check()
        if not self._opened or self._streaming:
            raise CopilotLocalSessionError("Copilot local session already has a stream or is unavailable")
        self._streaming = True
        self._completed = False
        finished = False
        failed = False
        try:
            async for event in self._supervisor.stream(prompt):
                if event.type == "error":
                    self._uncertain = True
                if event.type == "done":
                    self._completed = True
                    finished = True
                yield event
        except Exception:
            failed = True
        finally:
            self._streaming = False
            if not finished:
                self._uncertain = True
                try:
                    await self.close()
                except Exception:
                    failed = True
        if failed:
            raise CopilotLocalSessionError("Copilot local turn could not be completed")

    async def _call(self, operation, *arguments):
        self._check()
        try:
            return await getattr(self._supervisor, operation)(*arguments)
        except asyncio.CancelledError:
            self._uncertain = True
            with suppress(Exception):
                await self.close()
            raise
        except Exception:
            self._uncertain = True
        with suppress(Exception):
            await self.close()
        raise CopilotLocalSessionError("Copilot local session operation failed")

    async def steer(self, prompt):
        return await self._call("steer", prompt)

    async def abort(self):
        self._check()
        # Controlled settlement is not graceful-history preservation. Keep this
        # run ineligible for durable resume until that separate contract is proven.
        self._uncertain = True
        return await self._call("abort")

    async def interrupt(self):
        self._check()
        self._uncertain = True
        return await self._call("interrupt")

    @property
    def alive(self) -> bool:
        """Whether this opened owner can still admit work; never starts cleanup.

        This synchronous observation does not depend on a caller task or event
        loop. Dispatch still performs its own fresh authorization under lock.
        """
        try:
            return bool(
                self._opened and self._close_task is None and not self._invalid
                and not self._provider_error and self._context_valid()
                and self._runtime_started and self._runtime is not None and self._runtime.alive is True
                and self._guard is not None and self._guard.valid is True
                and self._supervisor is not None and self._supervisor.failure_detected is False
            )
        except Exception:
            return False

    @property
    def closed(self) -> bool:
        """Cleanup has finished, including failure; wait_closed reports its result.

        True alone does not prove every owned resource stopped successfully.
        """
        return self._close_task is not None and self._close_task.done()

    async def wait_closed(self) -> None:
        """Observe shutdown without initiating it or owning another cleanup task.

        Cancelling this waiter never cancels cleanup or other waiters. A late
        waiter receives the same successful outcome or sanitized failure.
        """
        await self._closing_started.wait()
        task = self._close_task
        failed = False
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled() or asyncio.current_task().cancelling():
                raise
            failed = True
        except Exception:
            failed = True
        if failed:
            raise CopilotLocalSessionError("Copilot local session cleanup did not complete successfully")

    def _begin_close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            self._closing_started.set()
        return self._close_task

    async def _close(self):
        failed = False
        if self._context_watcher is not None:
            self._context_watcher.cancel()
            await asyncio.gather(self._context_watcher, return_exceptions=True)
        for owned in (self._supervisor, self._runtime, self._guard):
            if owned is not None:
                try:
                    await owned.close()
                except (asyncio.CancelledError, Exception):
                    failed = True
        if self._record is not None:
            try:
                if (not failed and self._opened and self._completed and not self._streaming
                        and not self._uncertain and not self._invalid
                        and self._supervisor is not None and not self._supervisor.failure_detected
                        and self._runtime is not None and not self._runtime.alive
                        and not self._runtime.forced_cleanup and self._context_valid()):
                    self._record.mark_ready()
            except Exception:
                failed = True
            finally:
                try:
                    self._record.close()
                except Exception:
                    failed = True
        self._opened = False
        if failed:
            raise CopilotLocalSessionError("Copilot local session cleanup is incomplete")

    async def close(self):
        task = self._begin_close()
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise CopilotLocalSessionError("Copilot local session cleanup was cancelled") from None
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
