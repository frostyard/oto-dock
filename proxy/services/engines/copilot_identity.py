"""Validate an explicitly supplied GitHub user token without claiming Copilot access."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import time

from core.layers.copilot.credentials import CopilotCredential, CredentialKind

USER_ENDPOINT = "https://api.github.com/user"
API_VERSION = "2026-03-10"
MAX_BODY_BYTES = 64 * 1024
TOTAL_TIMEOUT = 10
EXPIRATION_HEADER = "GitHub-Authentication-Token-Expiration"


class InvalidCopilotToken(ValueError):
    """Unsupported or rejected user credential; API callers may return 422."""


class CopilotIdentityUnavailable(RuntimeError):
    """Identity could not be verified; API callers may return 503."""


@dataclass(frozen=True)
class ValidatedCopilotUser:
    principal_id: str
    login: str
    token: str = field(repr=False)
    expires_at: float | None = None


def _expiration(headers) -> float | None:
    values = headers.getall(EXPIRATION_HEADER, [])
    if not values:
        return None
    if len(values) != 1 or len(values[0]) > 64:
        raise CopilotIdentityUnavailable()
    raw = values[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", raw):
        date = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}", raw):
        date = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z")
    else:
        # Present but ambiguous must not silently become an unlimited lease.
        raise CopilotIdentityUnavailable()
    expires_at = date.timestamp()
    if expires_at <= time.time():
        raise InvalidCopilotToken()
    return expires_at


async def _response_user(response, token):
    if response.status == 401:
        raise InvalidCopilotToken()
    # 403 includes rate limits and temporary authentication blocking. Avoid
    # labelling a credential invalid using freeform upstream messages.
    if response.status != 200:
        raise CopilotIdentityUnavailable()
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise CopilotIdentityUnavailable()
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
    if content_type not in {"application/json", "application/vnd.github+json"}:
        raise CopilotIdentityUnavailable()
    length = response.headers.get("Content-Length")
    if length is not None and (not length.isdecimal() or int(length) > MAX_BODY_BYTES):
        raise CopilotIdentityUnavailable()
    body = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise CopilotIdentityUnavailable()
        body.extend(chunk)
    value = json.loads(body)
    if not isinstance(value, dict):
        raise CopilotIdentityUnavailable()
    if value.get("type") != "User":
        raise InvalidCopilotToken()
    identity = value.get("id")
    login = value.get("login")
    if (type(identity) is not int or identity <= 0
            or not isinstance(login, str)
            or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,37}[A-Za-z0-9])?", login) is None):
        raise CopilotIdentityUnavailable()
    return ValidatedCopilotUser(f"github:user:{identity}", login, token, _expiration(response.headers))


async def validate_user_token(token: str, *, session_factory=None) -> ValidatedCopilotUser:
    """GET one fixed TLS endpoint; no ambient auth, redirects, retries, or inference.

    ``session_factory`` is an internal HTTP seam for deterministic tests. The
    application calls this with only the explicit token. Unknown expiry remains
    None; successful identity validation does not establish model entitlement.
    """
    invalid = False
    try:
        # Use the shared typed USER_TOKEN contract; installation and classic
        # tokens are never sent down this user-identity path.
        if not isinstance(token, str) or len(token) > 4096:
            raise InvalidCopilotToken()
        CopilotCredential("validation", "validation", "validation", CredentialKind.USER_TOKEN, token)
    except Exception:
        invalid = True
    if invalid:
        raise InvalidCopilotToken("Unsupported Copilot user token")

    import aiohttp

    failure = CopilotIdentityUnavailable
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            factory = session_factory or aiohttp.ClientSession
            async with factory(
                timeout=aiohttp.ClientTimeout(total=TOTAL_TIMEOUT, connect=5, sock_read=5),
                trust_env=False, auto_decompress=False,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                         "Accept-Encoding": "identity", "X-GitHub-Api-Version": API_VERSION,
                         "User-Agent": "Frostyard-OtoDock-Copilot"},
            ) as client:
                async with client.get(USER_ENDPOINT, allow_redirects=False, ssl=True) as response:
                    return await _response_user(response, token)
    except InvalidCopilotToken:
        failure = InvalidCopilotToken
    except Exception:
        pass
    # Raise outside the handlers: raw transport/body exceptions are not retained
    # as exception context, nor copied into user-facing error text.
    if failure is InvalidCopilotToken:
        raise InvalidCopilotToken("GitHub rejected the Copilot user token")
    raise CopilotIdentityUnavailable("GitHub identity verification is unavailable")
