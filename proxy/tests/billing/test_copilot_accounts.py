"""PostgreSQL account isolation and CAS tests; no inference credentials/network."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from core.layers.copilot.credentials import (
    CopilotAccountScope as Scope, CredentialKind as Kind, CredentialUnavailableError,
)
from storage import copilot_account_store as store, subscription_store
from storage.credential_store import decrypt_secret, encrypt_secret
from storage.pg import get_conn


TOKEN = "gho_offline_fixture_only"


def account(owner="user-admin", principal="github:123", **kwargs):
    return store.create_account(owner, principal, Kind.USER_TOKEN, TOKEN,
                                time.time() + 3600, **kwargs)


def raw(account_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM execution_layer_subscriptions WHERE id = %s",
                            (account_id,)).fetchone()


def replace(account_id, revision, **kwargs):
    args = dict(owner_sub="user-admin", expected_revision=revision,
                principal_id="github:123", kind=Kind.USER_TOKEN,
                token="ghu_refreshed_fixture", expires_at=time.time() + 7200)
    args.update(kwargs)
    return store.replace_credential(account_id, **args)


def denied(account_id, scope):
    with pytest.raises(CredentialUnavailableError) as failure:
        store.read_credential(account_id, scope)
    assert TOKEN not in str(failure.value)
    assert failure.value.__context__ is None


def test_storage_is_encrypted_and_public_record_masked():
    row = account()
    assert "credential_data_enc" not in row
    assert TOKEN not in repr(row)
    assert row["has_credentials"] is True
    assert row["contribute_platform"] is False
    assert row["layer"] == "copilot-cli" and row["provider"] == "github"
    saved = raw(row["id"])
    assert TOKEN not in saved["credential_data_enc"]
    blob = json.loads(decrypt_secret(saved["credential_data_enc"]))
    assert blob["copilot"]["token"] == TOKEN
    credential = store.read_credential(row["id"], Scope.personal("user-admin"))
    assert credential.token == TOKEN and TOKEN not in repr(credential)
    denied(row["id"], Scope.platform())


def test_three_users_two_accounts_no_implicit_borrow_or_fallback():
    first = account("user-admin")
    second = account("user-viewer", "github:456")
    assert store.read_credential(first["id"], Scope.personal("user-admin")).principal_id == "github:123"
    assert store.read_credential(second["id"], Scope.personal("user-viewer")).principal_id == "github:456"
    for row, users in [(first, ["user-viewer", "user-viewer2"]),
                       (second, ["user-admin", "user-viewer2"])]:
        for user in users:
            denied(row["id"], Scope.personal(user))
    denied("missing", Scope.personal("user-admin"))
    denied(first["id"], None)


def test_platform_requires_explicit_flag_and_current_admin():
    row = account(contribute_platform=True, use_personal=False)
    assert store.read_credential(row["id"], Scope.platform()).account_id == row["id"]
    denied(row["id"], Scope.personal("user-admin"))
    with get_conn() as conn:
        conn.execute("UPDATE users SET role = 'member' WHERE sub = 'user-admin'")
        conn.commit()
    denied(row["id"], Scope.platform())


@pytest.mark.parametrize("owner", ["user-viewer", "missing", ""])
def test_unqualified_owner_cannot_contribute(owner):
    with pytest.raises(CredentialUnavailableError):
        account(owner, contribute_platform=True)
    assert subscription_store.list_subscriptions(layer="copilot-cli") == []


@pytest.mark.parametrize("change", ["disable", "personal_off", "shared_off", "delete", "owner_delete"])
def test_reread_detects_scope_status_and_deletion(change):
    row = account(contribute_platform=True)
    scope = Scope.platform() if change == "shared_off" else Scope.personal("user-admin")
    store.read_credential(row["id"], scope)
    if change == "disable":
        subscription_store.update_subscription(row["id"], status="disabled")
    elif change == "personal_off":
        subscription_store.update_subscription(row["id"], use_personal=False)
    elif change == "shared_off":
        subscription_store.update_subscription(row["id"], contribute_platform=False)
    elif change == "delete":
        subscription_store.delete_subscription(row["id"])
    else:
        with get_conn() as conn:
            conn.execute("DELETE FROM users WHERE sub = 'user-admin'")
            conn.commit()
    denied(row["id"], scope)


def test_refresh_cas_updates_revision_preserves_identity_and_scope():
    row = account()
    old = store.read_credential(row["id"], Scope.personal("user-admin"))
    new = replace(row["id"], old.revision)
    assert new.revision != old.revision and new.token != old.token
    assert store.read_credential(row["id"], Scope.personal("user-admin")) == new
    assert raw(row["id"])["contribute_platform"] is False
    with pytest.raises(CredentialUnavailableError):
        replace(row["id"], old.revision)


def test_generic_credential_mutation_changes_snapshot_even_without_revision_bump():
    row = account()
    old = store.read_credential(row["id"], Scope.personal("user-admin"))
    blob = subscription_store.get_credential_data(row["id"])
    blob["copilot"]["token"] = "ghu_generic_replacement_fixture"
    subscription_store.update_credential_data(row["id"], blob)
    new = store.read_credential(row["id"], Scope.personal("user-admin"))
    assert new.revision == old.revision
    assert new != old  # The caller must pin the whole snapshot, not revision alone.


def test_same_owner_distinct_principals_coexist_without_reconnect_guessing():
    first = account()
    second = account(principal="github:456")
    assert first["id"] != second["id"]
    with pytest.raises(CredentialUnavailableError) as failure:
        account()  # Duplicate identity must not silently replace either token.
    assert failure.value.__context__ is None
    assert len(subscription_store.list_subscriptions(layer="copilot-cli")) == 2


@pytest.mark.parametrize("override", [
    {"owner_sub": "user-viewer"}, {"principal_id": "github:other"},
    {"kind": Kind.INSTALLATION_TOKEN, "token": "ghs_fixture"},
    {"expires_at": 0}, {"token": "ghp_classic_unsupported"},
])
def test_refresh_cannot_change_owner_identity_kind_or_install_bad_material(override):
    row = account()
    old = store.read_credential(row["id"], Scope.personal("user-admin"))
    with pytest.raises(CredentialUnavailableError):
        replace(row["id"], old.revision, **override)
    assert store.read_credential(row["id"], Scope.personal("user-admin")) == old


def test_concurrent_refresh_only_one_revision_wins():
    row = account()
    old = store.read_credential(row["id"], Scope.personal("user-admin"))
    barrier = threading.Barrier(2)

    def contender():
        barrier.wait(timeout=5)
        try:
            return replace(row["id"], old.revision)
        except CredentialUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: contender(), range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store.read_credential(row["id"], Scope.personal("user-admin")) == winners[0]


def test_refresh_expired_or_disabled_account_does_not_reenable_it(monkeypatch):
    row = account()
    old = store.read_credential(row["id"], Scope.personal("user-admin"))
    with monkeypatch.context() as clock:
        clock.setattr(store.time, "time", lambda: old.expires_at)
        denied(row["id"], Scope.personal("user-admin"))
        fresh = replace(row["id"], old.revision)
    subscription_store.update_subscription(row["id"], status="disabled")
    replace(row["id"], fresh.revision)
    assert raw(row["id"])["status"] == "disabled"
    denied(row["id"], Scope.personal("user-admin"))


def test_installation_expiry_and_requested_runway(monkeypatch):
    monkeypatch.setattr(store.time, "time", lambda: 1000)
    row = store.create_account("user-admin", "installation:7", Kind.INSTALLATION_TOKEN,
                               "ghs_fixture", 1100, contribute_platform=True)
    credential = store.read_credential(row["id"], Scope.platform(), min_runway=99)
    assert credential.kind is Kind.INSTALLATION_TOKEN
    with pytest.raises(CredentialUnavailableError):
        store.read_credential(row["id"], Scope.platform(), min_runway=100)


@pytest.mark.parametrize("corruption", ["json", "version", "kind", "principal", "revision", "token",
                                        "layer", "provider", "auth_type", "ciphertext"])
def test_malformed_or_mistyped_storage_fails_closed(corruption):
    row = account()
    saved = raw(row["id"])
    blob = json.loads(decrypt_secret(saved["credential_data_enc"]))
    if corruption in {"layer", "provider", "auth_type"}:
        with get_conn() as conn:
            conn.execute(f"UPDATE execution_layer_subscriptions SET {corruption} = %s WHERE id = %s",
                         ("unrelated", row["id"]))
            conn.commit()
    else:
        changes = {"version": True, "kind": "unknown", "principal": "other",
                   "revision": "invalid", "token": {"raw": TOKEN}}
        if corruption in changes:
            key = "principal_id" if corruption == "principal" else corruption
            blob["copilot"][key] = changes[corruption]
        encrypted = encrypt_secret("not json" if corruption == "json" else json.dumps(blob))
        if corruption == "ciphertext":
            encrypted = TOKEN
        with get_conn() as conn:
            conn.execute("UPDATE execution_layer_subscriptions SET credential_data_enc = %s WHERE id = %s",
                         (encrypted, row["id"]))
            conn.commit()
    denied(row["id"], Scope.personal("user-admin"))


def test_database_failure_is_sanitized_without_raw_exception_context(monkeypatch):
    def unavailable():
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(store, "get_conn", unavailable)
    denied("account-id", Scope.personal("user-admin"))


@pytest.mark.asyncio
async def test_store_backed_guard_pins_generation_and_observes_owner_revocation():
    import asyncio
    from core.layers.copilot.lease import CopilotLeaseGuard

    row = await asyncio.to_thread(account)
    invalidated = asyncio.Event()
    guard = await CopilotLeaseGuard.acquire(
        row['id'], Scope.personal('user-admin'), on_invalid=invalidated.set,
        check_interval=0.01,
    )
    try:
        pinned = guard.credential
        assert pinned.account_id == row['id'] and pinned.principal_id == 'github:123'
        await guard.authorize()
        await asyncio.to_thread(subscription_store.update_subscription,
                                row['id'], use_personal=False)
        await asyncio.wait_for(invalidated.wait(), 2)
        with pytest.raises(CredentialUnavailableError):
            await guard.authorize()
        with pytest.raises(CredentialUnavailableError):
            _ = guard.credential
    finally:
        await guard.close()
