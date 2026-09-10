"""Human-owned Copilot account setup; GitHub identity is not entitlement proof."""
from __future__ import annotations

import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool

from auth.providers import UserContext, get_current_user, require_auth
from core.layers.copilot.credentials import CredentialUnavailableError
from services.engines.copilot_identity import (
    CopilotIdentityUnavailable, InvalidCopilotToken, validate_user_token,
)
from storage import copilot_account_store as accounts


class _PrivateRequestRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default errors include raw input, including token
                # fields and entire bodies for malformed JSON. Never echo them.
                pass
            # Leaving the exception suite drops the raw body's exception
            # context too, including for application-level HTTP error handlers.
            raise HTTPException(422, "Invalid Copilot account request")
        return handler


router = APIRouter(route_class=_PrivateRequestRoute)


class _Request(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class ConnectRequest(_Request):
    token: SecretStr = Field(min_length=1, max_length=4096)
    label: str = Field(default='', max_length=100, strict=True)


class ReconnectRequest(_Request):
    token: SecretStr = Field(min_length=1, max_length=4096)
    expected_revision: UUID


class UpdateRequest(_Request):
    label: str | None = Field(default=None, max_length=100, strict=True)
    use_personal: StrictBool | None = None
    contribute_platform: StrictBool | None = None
    status: Literal['active', 'disabled'] | None = None


def _owner(user):
    user = require_auth(user)
    if user.is_api_key or user.session_id:
        raise HTTPException(403, "User authentication required (not API key)")
    return user


async def _store(operation, *args, **kwargs):
    error = None
    try:
        return await asyncio.to_thread(operation, *args, **kwargs)
    except accounts.CopilotAccountNotFoundError:
        error = (404, "Copilot account not found")
    except accounts.CopilotAccountConflictError:
        error = (409, "Copilot account changed or is already connected; refresh and select it again")
    except accounts.CopilotAccountInvalidError:
        error = (422, "Invalid Copilot account change")
    except CredentialUnavailableError:
        error = (503, "Copilot account storage is unavailable")
    raise HTTPException(*error)


async def _validate(token):
    error = None
    try:
        return await validate_user_token(token.get_secret_value())
    except InvalidCopilotToken:
        error = (422, "GitHub rejected this user token or its token type is unsupported")
    except CopilotIdentityUnavailable:
        error = (503, "GitHub account validation is temporarily unavailable; try again")
    raise HTTPException(*error)


@router.get('/v1/copilot/accounts')
async def list_accounts(user: UserContext | None = Depends(get_current_user)):
    user = _owner(user)
    return {'accounts': await _store(accounts.list_owned_accounts, user.sub)}


@router.post('/v1/copilot/accounts', status_code=201)
async def connect_account(req: ConnectRequest, user: UserContext | None = Depends(get_current_user)):
    user = _owner(user)
    identity = await _validate(req.token)
    account = await _store(
        accounts.connect_user_account, user.sub, identity.principal_id,
        identity.token, identity.expires_at, label=req.label.strip() or identity.login,
    )
    return {'account': account}


@router.post('/v1/copilot/accounts/{account_id}/reconnect')
async def reconnect_account(account_id: UUID, req: ReconnectRequest,
                            user: UserContext | None = Depends(get_current_user)):
    user = _owner(user)
    selected = await _store(accounts.get_owned_account, str(account_id), user.sub)
    if selected['revision'] != str(req.expected_revision):
        raise HTTPException(409, "Copilot account changed; refresh and select it again")
    identity = await _validate(req.token)
    account = await _store(
        accounts.connect_user_account, user.sub, identity.principal_id,
        identity.token, identity.expires_at, account_id=str(account_id),
        expected_revision=str(req.expected_revision),
    )
    return {'account': account}


@router.patch('/v1/copilot/accounts/{account_id}')
async def update_account(account_id: UUID, req: UpdateRequest,
                         user: UserContext | None = Depends(get_current_user)):
    user = _owner(user)
    if req.contribute_platform is True and user.role != 'admin':
        raise HTTPException(403, "Only administrators can contribute a Copilot account")
    account = await _store(accounts.update_owned_account, str(account_id), user.sub,
                           **req.model_dump(exclude_unset=True))
    return {'account': account}


@router.delete('/v1/copilot/accounts/{account_id}', status_code=204)
async def disconnect_account(account_id: UUID, user: UserContext | None = Depends(get_current_user)):
    user = _owner(user)
    await _store(accounts.delete_owned_account, str(account_id), user.sub)
    return Response(status_code=204)
