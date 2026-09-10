"""Account setup HTTP boundaries over the real encrypted store; GitHub is mocked."""
import asyncio
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from api.auth import copilot_accounts as api
from auth.providers import UserContext, get_current_user
from services.engines.copilot_identity import InvalidCopilotToken, CopilotIdentityUnavailable
from storage import copilot_account_store as store, subscription_store

TOKEN = 'gho_api_fixture_secret'


@pytest.fixture
def setup(monkeypatch):
    user = [UserContext('user-admin', 'admin@test.com', 'Admin', 'admin')]
    identity = [SimpleNamespace(principal_id='github:user:123', login='octocat', token=TOKEN, expires_at=None)]
    calls = []

    async def validate(token):
        calls.append(token)
        if isinstance(identity[0], Exception):
            raise identity[0]
        return identity[0]

    monkeypatch.setattr(api, 'validate_user_token', validate)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: user[0]
    with TestClient(app) as client:
        yield client, user, identity, calls


def connect(client):
    response = client.post('/v1/copilot/accounts', json={'token': TOKEN})
    assert response.status_code == 201, response.text
    assert TOKEN not in response.text
    return response.json()['account']


def test_connect_list_replace_and_disconnect_are_owned_masked_operations(setup):
    client, user, identity, calls = setup
    account = connect(client)
    assert account['principal_id'] == 'github:user:123' and account['label'] == 'octocat'
    assert account['use_personal'] and not account['contribute_platform']
    assert client.get('/v1/copilot/accounts').json() == {'accounts': [account]}
    duplicate = client.post('/v1/copilot/accounts', json={'token': TOKEN})
    assert duplicate.status_code == 409
    user[0] = UserContext('user-viewer', 'viewer@test.com', 'Viewer', 'member')
    assert client.get('/v1/copilot/accounts').json() == {'accounts': []}
    before = len(calls)
    assert client.post(f"/v1/copilot/accounts/{account['id']}/reconnect", json={
        'token': TOKEN, 'expected_revision': account['revision'],
    }).status_code == 404
    assert len(calls) == before  # Ownership is checked before external validation.
    assert client.delete(f"/v1/copilot/accounts/{account['id']}").status_code == 404
    user[0] = UserContext('user-admin', 'admin@test.com', 'Admin', 'admin')
    identity[0] = SimpleNamespace(principal_id='github:user:123', login='renamed-login',
                                  token='ghu_replacement_fixture', expires_at=None)
    replaced = client.post(f"/v1/copilot/accounts/{account['id']}/reconnect", json={
        'token': 'ghu_replacement_fixture', 'expected_revision': account['revision'],
    })
    assert replaced.status_code == 200 and TOKEN not in replaced.text
    assert replaced.json()['account']['revision'] != account['revision']
    assert replaced.json()['account']['id'] == account['id']
    assert replaced.json()['account']['label'] == account['label']
    assert client.delete(f"/v1/copilot/accounts/{account['id']}").status_code == 204
    assert client.get('/v1/copilot/accounts').json() == {'accounts': []}


def test_reconnect_cannot_switch_identity_or_overwrite_stale_revision(setup):
    client, _, identity, calls = setup
    account = connect(client)
    before = len(calls)
    stale = client.post(f"/v1/copilot/accounts/{account['id']}/reconnect", json={
        'token': TOKEN, 'expected_revision': str(uuid.uuid4()),
    })
    assert stale.status_code == 409 and len(calls) == before
    identity[0] = SimpleNamespace(principal_id='github:user:999', login='someone-else', token=TOKEN, expires_at=None)
    changed = client.post(f"/v1/copilot/accounts/{account['id']}/reconnect", json={
        'token': TOKEN, 'expected_revision': account['revision'],
    })
    assert changed.status_code in (409, 422)
    assert store.get_owned_account(account['id'], 'user-admin')['revision'] == account['revision']


def test_delete_during_provider_validation_cannot_recreate_target(setup, monkeypatch):
    client, _, identity, _ = setup
    account = connect(client)

    async def validate(_token):
        await asyncio.to_thread(store.delete_owned_account, account['id'], 'user-admin')
        return identity[0]

    monkeypatch.setattr(api, 'validate_user_token', validate)
    response = client.post(f"/v1/copilot/accounts/{account['id']}/reconnect", json={
        'token': TOKEN, 'expected_revision': account['revision'],
    })
    assert response.status_code == 404
    assert client.get('/v1/copilot/accounts').json() == {'accounts': []}


def test_disable_reconnect_and_explicit_enable_preserve_setting_intent(setup):
    client, _, _, _ = setup
    account = connect(client)
    url = f"/v1/copilot/accounts/{account['id']}"
    assert client.patch(url, json={'status': 'disabled'}).status_code == 200
    response = client.post(url+'/reconnect', json={'token': TOKEN, 'expected_revision': account['revision']})
    assert response.status_code == 200 and response.json()['account']['status'] == 'disabled'
    assert client.patch(url, json={'status': 'active'}).json()['account']['status'] == 'active'


@pytest.mark.parametrize('principal', [None,
    UserContext('user-admin', 'admin@test.com', 'Agent', 'admin', is_api_key=True),
    UserContext('user-admin', 'admin@test.com', 'Agent', 'admin', session_id='session'),
])
def test_unauthenticated_and_agent_principals_cannot_manage_accounts(setup, principal):
    client, user, _, calls = setup
    user[0] = principal
    expected = 401 if principal is None else 403
    assert client.get('/v1/copilot/accounts').status_code == expected
    assert client.post('/v1/copilot/accounts', json={'token': TOKEN}).status_code == expected
    target = f'/v1/copilot/accounts/{uuid.uuid4()}'
    assert client.patch(target, json={'status': 'disabled'}).status_code == expected
    assert client.delete(target).status_code == expected
    assert client.post(target+'/reconnect', json={
        'token': TOKEN, 'expected_revision': str(uuid.uuid4()),
    }).status_code == expected
    assert calls == []


def test_member_cannot_contribute_account_to_platform(setup):
    client, user, _, _ = setup
    user[0] = UserContext('user-viewer', 'viewer@test.com', 'Viewer', 'member')
    account = connect(client)
    response = client.patch(f"/v1/copilot/accounts/{account['id']}", json={'contribute_platform': True})
    assert response.status_code == 403
    assert not store.get_owned_account(account['id'], 'user-viewer')['contribute_platform']


@pytest.mark.parametrize('payload', [
    {'token': {'nested': TOKEN}}, {'token': TOKEN, 'owner_sub': TOKEN},
    {'token': TOKEN, 'label': {'secret': TOKEN}}, {'token': TOKEN, 'expires_at': TOKEN},
])
def test_validation_never_echoes_secret_input(setup, payload, caplog):
    client, _, _, calls = setup
    response = client.post('/v1/copilot/accounts', json=payload)
    assert response.status_code == 422
    assert TOKEN not in response.text and TOKEN not in caplog.text and calls == []


def test_malformed_json_never_echoes_request_body(setup):
    client, _, _, calls = setup
    response = client.post('/v1/copilot/accounts', content='{"token":"'+TOKEN+'",',
                            headers={'content-type': 'application/json'})
    assert response.status_code == 422 and TOKEN not in response.text and calls == []


@pytest.mark.parametrize('malformed_json', [False, True])
def test_request_validation_does_not_retain_secret_body_in_exception_context(malformed_json):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        'user-admin', 'admin@test.com', 'Admin', 'admin',
    )
    observed = []

    @app.exception_handler(HTTPException)
    async def handler(_request, error):
        observed.append(error)
        return JSONResponse({'detail': error.detail}, status_code=error.status_code)

    kwargs = ({'content': '{"token":"'+TOKEN+'",', 'headers': {'content-type': 'application/json'}}
              if malformed_json else {'json': {'token': {'private': TOKEN}}})
    with TestClient(app) as client:
        response = client.post('/v1/copilot/accounts', **kwargs)
    assert response.status_code == 422 and TOKEN not in response.text
    assert len(observed) == 1
    assert observed[0].__context__ is None and observed[0].__cause__ is None


@pytest.mark.parametrize(('failure', 'status'), [
    (InvalidCopilotToken(TOKEN), 422), (CopilotIdentityUnavailable(TOKEN), 503),
])
def test_provider_failures_are_sanitized_and_do_not_persist(setup, failure, status, caplog):
    client, _, identity, _ = setup
    identity[0] = failure
    response = client.post('/v1/copilot/accounts', json={'token': TOKEN})
    assert response.status_code == status
    assert TOKEN not in response.text and TOKEN not in caplog.text
    assert client.get('/v1/copilot/accounts').json() == {'accounts': []}


@pytest.mark.parametrize('prefix', ['/v1/users/me', '/v1/admin'])
@pytest.mark.parametrize('method', ['put', 'delete'])
def test_generic_subscription_routes_cannot_bypass_copilot_management(setup, prefix, method):
    from api.admin import execution_layers

    client, _, _, _ = setup
    account = connect(client)
    client.app.include_router(execution_layers.router)
    url = f"{prefix}/execution-layers/codex-cli/subscriptions/{account['id']}"
    response = getattr(client, method)(url, **({'json': {'use_personal': False}} if method == 'put' else {}))
    assert response.status_code == 404
    assert subscription_store.get_subscription(account['id'])['use_personal']
