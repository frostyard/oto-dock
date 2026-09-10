"""Current storage authorization feeds a real native layer/security boundary."""

import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace
import uuid

import pytest

from auth.providers import UserContext
from core.config import copilot_config_builder as builder
from core.layers.copilot.credentials import CopilotAccountScope, CopilotCredential, CredentialKind
from core.layers.copilot.layer import CopilotExecutionLayer


@pytest.fixture
def facts(monkeypatch, tmp_path):
    human = UserContext(sub="alice", email="stale@example.com", name="Old name", role="admin",
                        agents=["demo"], agent_roles={"demo": "manager"})
    user = {"sub": "alice", "username": "alice", "email": "current@example.com",
            "display_name": "Current Alice", "role": "member", "allow_platform_auth": True}
    agent = {"slug": "demo", "admin_only": False, "collaborative": True, "default_scope": "user"}
    scenario = SimpleNamespace(human=human, user=user, agent=agent, roles={"demo": "viewer"},
                               libraries=[], reads=[], authority_reads=0, on_user_read=None)

    def get_user(sub):
        assert sub == "alice"
        scenario.authority_reads += 1
        if scenario.on_user_read:
            scenario.on_user_read()
        return scenario.user

    def credential(account_id, scope):
        scenario.reads.append((account_id, scope))
        return CopilotCredential(account_id, "github:user:1", "generation", CredentialKind.USER_TOKEN,
                                 "gho_fixture_secret_never_in_config")

    monkeypatch.setattr(builder.database, "get_user", get_user)
    monkeypatch.setattr(builder.database, "get_user_agent_roles", lambda _: scenario.roles)
    monkeypatch.setattr(builder.agent_store, "get_all_agents", lambda: [scenario.agent] if scenario.agent else [])
    monkeypatch.setattr(builder.agent_store, "get_agent", lambda _: scenario.agent)
    monkeypatch.setattr(builder.db_knowledge_libraries, "attachments_for_consumer", lambda _: scenario.libraries)
    monkeypatch.setattr(builder.copilot_account_store, "read_credential", credential)
    monkeypatch.setattr(builder.config, "AGENTS_DIR", tmp_path)
    agent_dir = tmp_path / "demo"
    (agent_dir / "config/context").mkdir(parents=True)
    (agent_dir / "config/agent.md").write_text("You are the repository maintainer.")
    (agent_dir / "config/context/guide.md").write_text("Use the existing project conventions.")
    personal = agent_dir / "users/alice/context"
    personal.mkdir(parents=True)
    (personal / "preferences.md").write_text("Alice's private preferences.")
    scenario.agent_dir = agent_dir

    def forbidden(*args, **kwargs):
        raise AssertionError("Generic prompt construction must not run")

    monkeypatch.setattr(builder.config, "build_agent_prompt", forbidden)
    return scenario


async def build(facts, **options):
    return await builder.build_copilot_agent_config(**{
        "user": facts.human, "agent_name": "demo", "account_id": "account-one",
        "account_scope": CopilotAccountScope.personal("alice"), "model": "fixture-model",
        "enabled_tools": frozenset({"view", "bash"}), **options,
    })


@pytest.mark.asyncio
async def test_current_authority_replaces_stale_cookie_roles_and_config_contains_no_credentials(facts):
    result = await build(facts)
    assert result.security_context.role == "viewer"
    assert not result.security_context.config_visible
    assert result.security_context.display_name == "Current Alice"
    assert result.security_context.email == "current@example.com"
    assert result.user_sub == "alice"
    assert "repository maintainer" in result.system_prompt
    assert "project conventions" in result.system_prompt
    assert "Alice's private preferences" in result.system_prompt
    assert "Native tools" in result.system_prompt and "bash, view" in result.system_prompt
    assert "Session Context" in result.system_prompt
    assert "gho_fixture_secret" not in repr(asdict(result))
    assert result.credential_env == {} and not result.mcp_config_path and not result.extra_env
    assert result.subscription_id == "" and result.subscription_user_sub is None
    assert facts.authority_reads == 2
    assert facts.reads == [("account-one", CopilotAccountScope.personal("alice"))] * 2
    validated = CopilotExecutionLayer._validate(str(uuid.uuid4()), result)
    assert validated.model == "fixture-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("collaborative,default_scope,role", [
    (True, "user", "viewer"), (True, "agent", "editor"),
    (False, "user", "manager"), (False, "agent", "viewer"),
    (False, "agent", "manager"),
])
async def test_visibility_scope_and_knowledge_permissions_follow_current_rows(facts, collaborative, default_scope, role):
    facts.agent.update(collaborative=collaborative, default_scope=default_scope)
    facts.roles["demo"] = role
    facts.libraries = [{"source_agent": "library", "subdir": "guides", "writable": False}]
    result = await build(facts)
    context = result.security_context
    shared_only = not collaborative and default_scope == "agent"
    assert context.username == "alice"
    assert context.mount_username == ("" if shared_only else "alice")
    assert context.session_scope == ("agent" if shared_only else "user")
    assert context.mount_shared is (collaborative or default_scope == "agent")
    assert context.config_visible is (role == "manager")
    assert context.knowledge_libraries == (("library", "guides", False),)
    assert context.knowledge_rw is False
    assert ("Alice's private preferences" in result.system_prompt) is not shared_only


@pytest.mark.asyncio
async def test_platform_payer_is_explicit_and_does_not_change_human_mount_identity(facts):
    result = await build(facts, account_scope=CopilotAccountScope.platform(), resume=True,
                         client_type="sse", permission_mode="plan")
    assert result.account_scope == CopilotAccountScope.platform()
    assert result.user_sub == result.security_context.username == "alice"
    assert result.resume is True and result.client_type == "sse" and result.permission_mode == "plan"
    assert all(scope == CopilotAccountScope.platform() for _, scope in facts.reads)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, None, 1, "true"])
async def test_platform_auth_toggle_must_be_current_exact_true(facts, flag):
    facts.user["allow_platform_auth"] = flag
    with pytest.raises(builder.CopilotConfigError):
        await build(facts, account_scope=CopilotAccountScope.platform())
    assert facts.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"is_api_key": True}, {"is_api_key": 0}, {"session_id": "native-session"},
    {"agent": "demo"}, {"external_claim": "phone:caller"}, {"external_channel": "phone"},
    {"sub": "api-key"}, {"sub": "session:fixture"}, {"sub": ""},
])
async def test_non_cookie_and_external_principals_fail_before_storage(facts, changes):
    with pytest.raises(builder.CopilotConfigError):
        await build(facts, user=replace(facts.human, **changes))
    assert facts.authority_reads == 0 and facts.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"agent_name": "../other"}, {"account_id": ""}, {"model": ""},
    {"account_scope": CopilotAccountScope.personal("bob")}, {"account_scope": None},
    {"permission_mode": "auto"}, {"permission_mode": "bypassPermissions"},
    {"client_type": "task"}, {"client_type": "phone"}, {"client_type": "interactive"},
    {"resume": 1}, {"enabled_tools": frozenset()}, {"enabled_tools": {"view"}},
    {"enabled_tools": frozenset({"web_fetch"})},
])
async def test_unsupported_inputs_fail_before_storage(facts, options):
    with pytest.raises(builder.CopilotConfigError):
        await build(facts, **options)
    assert facts.authority_reads == 0 and facts.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted_user", "deleted_agent", "revoked", "admin_only", "unknown_role",
                                        "invalid_visibility", "invalid_username", "forged_admin_assignment"])
async def test_stale_or_malformed_authority_never_grants_access(facts, change):
    if change == "deleted_user":
        facts.user = None
    elif change == "deleted_agent":
        facts.agent = None
    elif change == "revoked":
        facts.roles = {}
    elif change == "admin_only":
        facts.agent["admin_only"] = True
    elif change == "unknown_role":
        facts.user["role"] = "unknown"
    elif change == "invalid_visibility":
        facts.agent["collaborative"] = "false"
    elif change == "invalid_username":
        facts.user["username"] = "../other"
    else:
        facts.roles["demo"] = "admin"
    with pytest.raises(builder.CopilotConfigError):
        await build(facts)
    assert facts.reads == []


@pytest.mark.asyncio
async def test_current_admin_can_access_unassigned_admin_agent(facts):
    facts.human.role = "member"
    facts.user["role"] = "admin"
    facts.agent["admin_only"] = True
    facts.roles = {}
    result = await build(facts)
    assert result.security_context.role == "admin" and result.security_context.is_admin_agent


@pytest.mark.asyncio
async def test_authority_change_during_preparation_rejects_result(facts):
    def change():
        if facts.authority_reads == 2:
            facts.roles["demo"] = "editor"

    facts.on_user_read = change
    with pytest.raises(builder.CopilotConfigError):
        await build(facts)
    assert len(facts.reads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["account", "identity", "knowledge", "prompt"])
async def test_failures_are_sanitized_without_fallback_or_raw_context(facts, monkeypatch, seam):
    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture-secret-provider-error")

    target, name = {
        "account": (builder.copilot_account_store, "read_credential"),
        "identity": (builder.database, "get_user"),
        "knowledge": (builder.db_knowledge_libraries, "attachments_for_consumer"),
        "prompt": (builder, "_documents"),
    }[seam]
    monkeypatch.setattr(target, name, unavailable)
    with pytest.raises(builder.CopilotConfigError) as error:
        await build(facts)
    assert str(error.value) == "Copilot configuration is unavailable"
    assert error.value.__context__ is None and error.value.__cause__ is None


@pytest.mark.asyncio
async def test_cancellation_propagates_without_constructing_a_session(facts, monkeypatch):
    async def cancelled(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(builder.asyncio, "to_thread", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await build(facts)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["symlink", "oversize", "missing_persona"])
async def test_unavailable_or_out_of_scope_prompt_material_is_rejected(facts, change, tmp_path):
    if change == "symlink":
        target = tmp_path / "private.txt"
        target.write_text("private host data")
        (facts.agent_dir / "users/alice/context/escape.md").symlink_to(target)
    elif change == "oversize":
        (facts.agent_dir / "config/agent.md").write_text("x" * 262145)
    else:
        (facts.agent_dir / "config/agent.md").unlink()
    with pytest.raises(builder.CopilotConfigError):
        await build(facts)


@pytest.mark.asyncio
async def test_account_revocation_during_preparation_is_rechecked(facts, monkeypatch):
    original = builder.copilot_account_store.read_credential

    def revoke(*args):
        if facts.reads:
            raise RuntimeError("revoked credential")
        return original(*args)

    monkeypatch.setattr(builder.copilot_account_store, "read_credential", revoke)
    with pytest.raises(builder.CopilotConfigError):
        await build(facts)
    assert facts.authority_reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [
    {"source_agent": "../other", "subdir": "", "writable": False},
    {"source_agent": "library", "subdir": "../../outside", "writable": False},
    {"source_agent": "library", "subdir": "/absolute", "writable": False},
    {"source_agent": "library", "subdir": "guides", "writable": "false"},
])
async def test_invalid_knowledge_snapshot_cannot_widen_native_path_authority(facts, row):
    facts.libraries = [row]
    with pytest.raises(builder.CopilotConfigError):
        await build(facts)
    assert facts.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["agent", "config", "context", "persona", "nested_doc", "personal_root"])
async def test_native_document_reader_never_follows_symlink_source_forms(facts, tmp_path, source):
    import shutil

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "agent.md").write_text("outside persona")
    (outside / "secret.md").write_text("outside context")
    target = {
        "agent": facts.agent_dir,
        "config": facts.agent_dir / "config",
        "context": facts.agent_dir / "config/context",
        "persona": facts.agent_dir / "config/agent.md",
        "nested_doc": facts.agent_dir / "config/context/guide.md",
        "personal_root": facts.agent_dir / "users/alice",
    }[source]
    if target.is_dir():
        shutil.rmtree(target)
        target.symlink_to(outside, target_is_directory=True)
    else:
        target.unlink()
        target.symlink_to(outside / "secret.md")
    with pytest.raises(builder.CopilotConfigError):
        await build(facts)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
async def test_nonregular_or_shared_inode_prompt_source_is_rejected(facts, tmp_path, kind):
    import os

    document = facts.agent_dir / "config/context/unsafe.md"
    if kind == "hardlink":
        outside = tmp_path / "private.md"
        outside.write_text("host-private context")
        os.link(outside, document)
    else:
        os.mkfifo(document)
    with pytest.raises(builder.CopilotConfigError):
        await asyncio.wait_for(build(facts), 1)


@pytest.mark.asyncio
async def test_descriptor_reader_stays_with_opened_directory_after_path_swap(facts, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "agent.md").write_text("escaped-host-persona")
    original = builder.os.open
    replaced = False

    def swap(name, flags, *args, **kwargs):
        nonlocal replaced
        fd = original(name, flags, *args, **kwargs)
        if name == "config" and not replaced:
            replaced = True
            (facts.agent_dir / "config").rename(facts.agent_dir / "original-config")
            (facts.agent_dir / "config").symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(builder.os, "open", swap)
    result = await build(facts)
    assert replaced
    assert "repository maintainer" in result.system_prompt
    assert "escaped-host-persona" not in result.system_prompt


@pytest.mark.asyncio
async def test_context_budget_is_shared_across_persona_and_all_documents(facts):
    (facts.agent_dir / "config/agent.md").write_text("a" * 140000)
    (facts.agent_dir / "config/context/guide.md").write_text("b" * 140000)
    with pytest.raises(builder.CopilotConfigError):
        await build(facts)


@pytest.mark.asyncio
async def test_legacy_persona_and_recursive_context_supported_without_mcp_helpers(facts, monkeypatch):
    (facts.agent_dir / "config/agent.md").rename(facts.agent_dir / "config/prompt.md")
    nested = facts.agent_dir / "config/context/nested"
    nested.mkdir()
    (nested / "notes.txt").write_text("Nested native instructions")

    def forbidden(*args):
        raise AssertionError("Generic file loader is not a safe native boundary")

    monkeypatch.setattr(builder.config, "_read_agent_files", forbidden)
    result = await build(facts)
    assert "repository maintainer" in result.system_prompt
    assert "Nested native instructions" in result.system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("subdir", ["a//b", "a/./b", "bad\x00path"])
async def test_noncanonical_knowledge_paths_rejected(facts, subdir):
    facts.libraries = [{"source_agent": "library", "subdir": subdir, "writable": False}]
    with pytest.raises(builder.CopilotConfigError):
        await build(facts)


@pytest.mark.asyncio
async def test_legacy_nullable_library_root_normalizes_without_widening(facts):
    facts.libraries = [{"source_agent": "library", "subdir": None, "writable": False}]
    result = await build(facts)
    assert result.security_context.knowledge_libraries == (("library", "", False),)


@pytest.mark.asyncio
async def test_saved_history_access_does_not_require_credentials_or_read_instructions(facts, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('History must not read credentials or instructions')
    monkeypatch.setattr(builder.copilot_account_store, 'read_credential', forbidden)
    monkeypatch.setattr(builder, '_documents', forbidden)
    await builder.authorize_copilot_history(facts.human, 'demo')
    assert facts.authority_reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['role', 'agent', 'user', 'admin_only'])
async def test_saved_history_access_rechecks_current_storage_authority(facts, change):
    if change == 'role':
        facts.roles.clear()
    elif change == 'agent':
        facts.agent = None
    elif change == 'user':
        facts.user = None
    else:
        facts.agent['admin_only'] = True
    with pytest.raises(builder.CopilotConfigError, match='conversation access is unavailable'):
        await builder.authorize_copilot_history(facts.human, 'demo')


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'is_api_key': True}, {'session_id': 'session'}, {'agent': 'demo'},
    {'external_claim': 'phone:caller'}, {'sub': 'api-key'}, {'sub': 'session:synthetic'},
])
async def test_saved_history_rejects_nonhuman_authority_before_storage(facts, changes):
    with pytest.raises(builder.CopilotConfigError):
        await builder.authorize_copilot_history(replace(facts.human, **changes), 'demo')
    assert facts.authority_reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "low", "medium", "high", "xhigh", "max"])
async def test_reasoning_effort_is_explicitly_mapped_into_layer_config(facts, effort):
    result = await build(facts, reasoning_effort=effort)
    assert result.effort == (effort or "")
    selected = CopilotExecutionLayer._validate(str(uuid.uuid4()), result)
    assert selected.reasoning_effort == effort


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["", "minimal", "HIGH", True, 1, ["high"], {}])
async def test_invalid_reasoning_effort_fails_before_account_lookup(facts, effort):
    with pytest.raises(builder.CopilotConfigError):
        await build(facts, reasoning_effort=effort)
    assert not facts.reads
