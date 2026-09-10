"""One-use, account-bound model inventory; no native session or history owner."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path

from .credentials import AccountScopeKind, CopilotAccountScope
from .lease import CopilotLeaseGuard
from .runtime import SDK_VERSION, SandboxedCopilotRuntime
from .session_state import PrivateCopilotSessionState


class CopilotCatalogError(RuntimeError):
    """A sanitized catalog or cleanup failure."""


def _text(value):
    return type(value) is str and 0 < len(value) <= 256 and value == value.strip() and value.isprintable()


def normalize_models(response):
    """Validate the pinned wire response before SDK str()/float() coercion."""
    if type(response) is not dict or type(response.get("models")) is not list or len(response["models"]) > 200:
        raise CopilotCatalogError("Copilot model inventory is invalid")
    rows, seen = [], set()
    for model in response["models"]:
        if (type(model) is not dict or not _text(model.get("id")) or not _text(model.get("name"))
                or type(model.get("capabilities")) is not dict or model["id"] in seen):
            raise CopilotCatalogError("Copilot model inventory is invalid")
        seen.add(model["id"])
        policy = model.get("policy")
        if policy is None:
            policy = "unconfigured"
        elif type(policy) is dict and _text(policy.get("state")):
            policy = policy["state"] if policy["state"] in {"enabled", "disabled", "unconfigured"} else "unknown"
        else:
            raise CopilotCatalogError("Copilot model inventory is invalid")
        billing = model.get("billing")
        if billing is not None and type(billing) is not dict:
            raise CopilotCatalogError("Copilot model inventory is invalid")
        multiplier = billing.get("multiplier") if billing is not None else None
        if (multiplier is not None and (type(multiplier) not in (int, float)
                or not 0 <= multiplier <= 1000 or not math.isfinite(multiplier))):
            raise CopilotCatalogError("Copilot model inventory is invalid")
        rows.append({"id": model["id"], "name": model["name"], "policy": policy,
                     "available": policy in {"enabled", "unconfigured"}, "multiplier": multiplier})
    return rows


class CopilotCatalogOwner:
    """Attached to the layer reservation before acquiring any owned resource."""

    def __init__(self, config, *, runtime_path, state_root):
        self._config, self._runtime_path, self._state_root = config, Path(runtime_path), Path(state_root)
        self._state = self._runtime = self._guard = None
        self._operation = self._watcher = self._closing = None
        self._context = self._snapshot = None
        self._invalid = self._started = False
        self._closed_event = asyncio.Event()
        self.rows = None

    def prepare(self):
        from core.session.session_state import get_session_security

        scope = self._config.scope
        if (type(scope) is not CopilotAccountScope or scope.kind is not AccountScopeKind.PERSONAL
                or scope.user_sub != self._config.user_sub):
            raise CopilotCatalogError("Copilot catalog requires a personal account")
        self._context = get_session_security(self._config.platform_session_id)
        if self._context is None:
            raise CopilotCatalogError("Copilot catalog requires registered authority")
        self._snapshot = deepcopy(self._context)
        self._state = PrivateCopilotSessionState.create(self._state_root)
        for relative in ("home", "mcps", "agents/catalog/workspace", "agents/catalog/knowledge"):
            (self._state.path / relative).mkdir(mode=0o700, parents=True)
        return self._state.path / "home"

    def _valid(self):
        from core.session.session_state import get_session_security

        context = get_session_security(self._config.platform_session_id)
        return (not self._invalid and self._closing is None and context is self._context
                and context == self._snapshot and (self._guard is None or self._guard.valid is True)
                and (not self._started or self._runtime.alive is True))

    def _check(self):
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        if not self._valid() or asyncio.get_running_loop().time() >= self._deadline:
            raise CopilotCatalogError("Copilot catalog ownership is unavailable")

    def _invalidate(self):
        self._invalid = True
        if self._operation is not None and not self._operation.done():
            self._operation.cancel()
        self._begin_close()

    async def _watch(self):
        try:
            while self._closing is None:
                await asyncio.sleep(0.1)
                if not self._valid():
                    self._invalidate()
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            self._invalidate()

    async def start(self, sandbox):
        self._deadline = asyncio.get_running_loop().time() + 35
        self._operation = asyncio.create_task(self._discover(sandbox))
        self._operation.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        failed = False
        try:
            async with asyncio.timeout(35):
                await self._operation
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # A revoked lease cancels the owned RPC, not the HTTP caller.
            # Keep that internal cancellation inside the sanitized API path.
            failed = True
        except Exception:
            failed = True
        if failed:
            raise CopilotCatalogError("Copilot model discovery is unavailable")

    async def _discover(self, sandbox):
        from core.sandbox.sandbox import SandboxBuilder

        self._check()
        # Preserve the resolver's approved egress/runtime assets, but never
        # expose existing agent, user, MCP, credential or history directories.
        cfg = replace(
            sandbox, role="viewer", username="", agent_name="catalog", is_admin_agent=False,
            host_agents_dir=self._state.path / "agents", host_mcps_dir=self._state.path / "mcps",
            host_claude_dir=self._state.path / "home", config_visible=False, knowledge_rw=False,
            mount_shared=False, external=False, external_home="", isolated_config_home=True,
            knowledge_libraries=[], extra_ro_binds=[], mcp_dir_binds=[], mcp_sandbox_mounts=[],
        )
        builder = SandboxBuilder(cfg)
        self._watcher = asyncio.create_task(self._watch())
        self._guard = await CopilotLeaseGuard.acquire(self._config.account_id, self._config.scope,
                                                     on_invalid=self._invalidate)
        self._check()
        self._runtime = SandboxedCopilotRuntime(
            builder, runtime_path=self._runtime_path, working_directory=self._state.path / "agents/catalog/workspace",
            sandbox_state_directory=self._state.sandbox_destination, session_state=self._state,
            credential=self._guard.credential, environment={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
            startup_timeout=20, shutdown_timeout=5,
        )
        client = await self._runtime.start()
        self._started = True
        self._check()
        auth = await client.get_auth_status()
        self._check()
        if getattr(auth, "isAuthenticated", None) is not True:
            raise CopilotCatalogError("Copilot catalog authentication is unavailable")
        await self._guard.authorize()
        self._check()
        # SDK 1.0.13 public list_models() coerces IDs/names with str() and
        # multipliers with float(). Use the identical raw RPC through this
        # pinned adapter so malformed values cannot become selectable rows.
        if SDK_VERSION != "1.0.13" or getattr(client, "_client", None) is None:
            raise CopilotCatalogError("Copilot catalog adapter is unavailable")
        response = await client._client.request("models.list", {}, timeout=10)
        self._check()
        rows = normalize_models(response)
        await self._guard.authorize()
        self._check()
        self.rows = rows

    @property
    def alive(self):
        try:
            return self._started and self._valid()
        except Exception:
            return False

    async def wait_closed(self):
        await self._closed_event.wait()
        await asyncio.shield(self._closing)

    def _begin_close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
            self._closing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            self._closed_event.set()
        return self._closing

    async def _close(self):
        for task in (self._operation, self._watcher):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        failed = False
        for owned in (self._runtime, self._guard):
            if owned is not None:
                try:
                    await owned.close()
                except (Exception, asyncio.CancelledError):
                    failed = True
        if self._runtime is not None and (self._runtime.alive is not False or self._runtime.forced_cleanup is not False):
            failed = True
        if not failed and self._state is not None:
            try:
                self._state.discard()
                self._state = None
            except Exception:
                failed = True
        if failed:
            raise CopilotCatalogError("Copilot catalog cleanup is incomplete")
        self._runtime = self._guard = self._context = self._snapshot = None

    async def close(self):
        task = self._begin_close()
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise CopilotCatalogError("Copilot catalog cleanup is incomplete") from None
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
