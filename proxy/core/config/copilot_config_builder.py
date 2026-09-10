"""Authenticated, native-only Copilot configuration; no routing or provisioning.

The caller must supply the UserContext returned by cookie authentication. This
service rechecks current authorization; it cannot authenticate a fabricated
Python object. Runtime creation still acquires and watches its scoped account.
"""

import asyncio
from copy import deepcopy
from contextlib import contextmanager
import os
import stat
from pathlib import Path
import uuid

import config
from auth.path_policy import SecurityContext, build_permission_context
from auth.providers import UserContext
from core.layers.copilot.credentials import AccountScopeKind, CopilotAccountScope
from core.layers.copilot.layer import CopilotAgentConfig, CopilotExecutionLayer
from core.layers.copilot.native_tool_policy import SUPPORTED_NATIVE_TOOLS
from core.session.visibility import available_scopes_for
from storage import agent_store, copilot_account_store, database, db_knowledge_libraries


class CopilotConfigError(RuntimeError):
    """Configuration could not be authorized or prepared; no secret detail."""


def _text(value, limit=256):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value.strip() == value and value.isprintable())


def _authority(sub, agent_name):
    # Deliberately bypass the hot agent cache for authorization and mount scope.
    user = database.get_user(sub)
    roles = database.get_user_agent_roles(sub)
    rows = [row for row in agent_store.get_all_agents() if row.get("slug") == agent_name]
    if (not isinstance(user, dict) or user.get("sub") != sub or len(rows) != 1
            or user.get("role") not in {"admin", "creator", "member"}
            or not isinstance(roles, dict)):
        raise ValueError()
    agent = rows[0]
    role = "admin" if user["role"] == "admin" else roles.get(agent_name)
    if (role not in {"admin", "manager", "editor", "viewer"}
            or (user["role"] != "admin" and role == "admin")
            or type(agent.get("admin_only")) is not bool
            or (agent["admin_only"] and user["role"] != "admin")
            or type(agent.get("collaborative")) is not bool
            or agent.get("default_scope") not in {"user", "agent"}
            or not _text(user.get("username"))
            or not config.is_safe_agent_name(user["username"])
            or any(not isinstance(user.get(key, ""), str) for key in ("display_name", "email"))):
        raise ValueError()
    libraries = db_knowledge_libraries.attachments_for_consumer(agent_name)
    attached = []
    for row in libraries:
        source, subdir, writable = row["source_agent"], row["subdir"], row["writable"]
        subdir = "" if subdir is None else subdir
        if (not isinstance(source, str) or not config.is_safe_agent_name(source)
                or not isinstance(subdir, str) or "\\" in subdir or "\x00" in subdir
                or (subdir and any(part in {"", ".", ".."} for part in subdir.split("/")))
                or (subdir and (Path(subdir).is_absolute() or ".." in Path(subdir).parts))
                or type(writable) is not bool):
            raise ValueError()
        attached.append((source, subdir, writable))
    return deepcopy((user, agent, role, tuple(attached)))


@contextmanager
def _directory(name, *, parent=None, optional=False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    except FileNotFoundError:
        if not optional:
            raise
        yield None
        return
    try:
        yield fd
    finally:
        os.close(fd)


def _documents(agent_name, username):
    """Read native instructions through directory FDs, with one total budget.

    Persona agent.md (legacy prompt.md accepted), recursive config/context
    documents, and the mounted human's top-level context/*.md are supported.
    Symlinks, hard links, special files, excessive trees, and oversized content
    are rejected. Missing optional context directories are allowed; a missing
    persona is not. No MCP discovery, home creation, or credential writes occur.
    """
    remaining = 262144
    count = 0
    documents = []

    def read(parent, name, label):
        nonlocal remaining
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > remaining:
                raise ValueError()
            data = bytearray()
            while len(data) <= remaining:
                part = os.read(fd, min(65536, remaining + 1 - len(data)))
                if not part:
                    break
                data.extend(part)
            if len(data) > remaining:
                raise ValueError()
            remaining -= len(data)
            documents.append((label, data.decode("utf-8")))
        finally:
            os.close(fd)

    def walk(fd, prefix, *, recursive, depth=0):
        nonlocal count
        names = sorted(os.listdir(fd))
        count += len(names)
        if depth > 16 or count > 4096:
            raise ValueError()
        for name in names:
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError()
            if stat.S_ISDIR(metadata.st_mode):
                if recursive:
                    with _directory(name, parent=fd) as child:
                        walk(child, prefix + name + "/", recursive=True, depth=depth + 1)
            elif Path(name).suffix.lower() in ({".md", ".txt", ".markdown"} if recursive else {".md"}):
                read(fd, name, prefix + name)

    with _directory(config.AGENTS_DIR) as root, _directory(agent_name, parent=root) as agent:
        with _directory("config", parent=agent) as settings:
            try:
                read(settings, "agent.md", "agent.md")
            except FileNotFoundError:
                read(settings, "prompt.md", "prompt.md")
            with _directory("context", parent=settings, optional=True) as context:
                if context is not None:
                    walk(context, "config/context/", recursive=True)
        if username:
            # Only directory absence is optional. A disappearing document
            # during a read fails the whole build instead of dropping notes.
            with _directory("users", parent=agent, optional=True) as users:
                if users is not None:
                    with _directory(username, parent=users, optional=True) as user:
                        if user is not None:
                            with _directory("context", parent=user, optional=True) as context:
                                if context is not None:
                                    walk(context, "Personal context: ", recursive=False)
    return documents


def _prompt(agent_name, ctx, enabled_tools):
    files = _documents(agent_name, ctx.mount_username)
    parts = [body if index == 0 else f"\n## {name}\n{body}"
             for index, (name, body) in enumerate(files)]
    parts.append("\nRespond in the same language the user uses unless they ask you to switch.")
    parts.append("\n# Native tools\nEnabled tools: " + ", ".join(sorted(enabled_tools))
                 + ". All tool operations require the current session's permission policy."
                 + " MCP servers, delegation tools, and remote execution are unavailable in this profile.")
    parts.append(build_permission_context(ctx, assigned_mcp_names=(), execution_path="copilot-cli"))
    return "\n".join(parts)


def _build(sub, agent_name, account_id, account_scope, model, permission_mode,
           client_type, resume, enabled_tools):
    facts = _authority(sub, agent_name)
    user, agent, role, libraries = facts
    if account_scope.kind is AccountScopeKind.PLATFORM and user.get("allow_platform_auth") is not True:
        raise ValueError()
    # Eligibility only: the credential is never attached to the returned config.
    copilot_account_store.read_credential(account_id, account_scope)
    available = available_scopes_for(agent["collaborative"], agent["default_scope"])
    scope = "agent" if available == ("agent",) else "user"
    ctx = SecurityContext(
        role=role, username=user["username"], agent=agent_name,
        is_admin_agent=agent["admin_only"], display_name=user.get("display_name") or "",
        email=user.get("email") or "", session_scope=scope,
        config_visible=role in {"admin", "manager"}, available_scopes=available,
        knowledge_libraries=libraries,
    )
    result = CopilotAgentConfig(
        agent_name=agent_name, user_sub=sub, account_id=account_id, account_scope=account_scope,
        model=model, permission_mode=permission_mode, client_type=client_type, resume=resume,
        enabled_tools=enabled_tools, security_context=ctx,
        system_prompt=_prompt(agent_name, ctx, enabled_tools),
    )
    # The validator is independent of runtime/root provisioning. Keep one source
    # of truth for the native profile's configuration boundary.
    CopilotExecutionLayer._validate(str(uuid.uuid4()), result)
    if _authority(sub, agent_name) != facts:
        raise ValueError()
    copilot_account_store.read_credential(account_id, account_scope)
    return result


async def build_copilot_agent_config(*, user: UserContext, agent_name: str,
                                     account_id: str, account_scope: CopilotAccountScope,
                                     model: str, permission_mode="default", client_type="dashboard",
                                     resume=False, enabled_tools=frozenset()) -> CopilotAgentConfig:
    """Build from a current human identity and an explicitly selected payer.

    Platform borrowing requires the driver's current Platform Auth toggle and
    the account store's explicit contribution/current-admin-owner eligibility.
    There is no personal-to-platform fallback. Empty tools are not a profile.
    No session is registered and no credential is serialized or provisioned.
    """
    failed = False
    try:
        if (type(user) is not UserContext or user.is_api_key is not False
                or user.session_id or user.agent or user.external_claim
                or user.external_channel or user.external_id
                or not _text(user.sub) or user.sub == "api-key" or user.sub.startswith("session:")
                or not _text(agent_name) or not config.is_safe_agent_name(agent_name)
                or not _text(account_id) or not _text(model)
                or type(account_scope) is not CopilotAccountScope
                or (account_scope.kind is AccountScopeKind.PERSONAL and account_scope.user_sub != user.sub)
                or permission_mode not in {"default", "acceptEdits", "plan", "dontAsk"}
                or client_type not in {"dashboard", "sse"} or type(resume) is not bool
                or type(enabled_tools) is not frozenset or not enabled_tools
                or not enabled_tools <= SUPPORTED_NATIVE_TOOLS):
            raise ValueError()
        return await asyncio.to_thread(
            _build, user.sub, agent_name, account_id, account_scope, model,
            permission_mode, client_type, resume, enabled_tools,
        )
    except Exception:
        failed = True
    if failed:
        raise CopilotConfigError("Copilot configuration is unavailable")
