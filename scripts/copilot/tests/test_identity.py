"""Offline user-identity boundary tests; no SDK, GitHub account, or inference."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import functools
import json
from pathlib import Path
import sys
import traceback

from multidict import CIMultiDict
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from services.engines import copilot_identity as identity

TOKEN = "ghu_secret_fixture"
USER = {"type": "User", "id": 123, "login": "sample-user"}


def async_test(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapper


class Response:
    def __init__(self, status=200, value=USER, *, body=None, headers=(), chunks=None, error=None):
        self.status = status
        self.headers = CIMultiDict({"Content-Type": "application/json"})
        self.headers.extend(headers)
        self.body = json.dumps(value).encode() if body is None else body
        self.chunks = chunks
        self.error = error
        self.content = self
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def iter_chunked(self, _size):
        if self.error:
            raise self.error
        for chunk in self.chunks if self.chunks is not None else [self.body]:
            yield chunk


class Client:
    def __init__(self, response):
        self.response = response
        self.options = None
        self.requests = []
        self.closed = False

    def factory(self, **options):
        self.options = options
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def get(self, url, **options):
        self.requests.append((url, options))
        return self.response


@async_test
async def test_fixed_tls_request_ignores_ambient_auth_and_proxies(monkeypatch, tmp_path):
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN", "HTTPS_PROXY", "SSL_CERT_FILE"):
        monkeypatch.setenv(key, "ambient-fixture")
    monkeypatch.setenv("HOME", str(tmp_path))
    client = Client(Response())
    result = await identity.validate_user_token(TOKEN, session_factory=client.factory)
    assert result.principal_id == "github:user:123" and result.login == "sample-user"
    assert result.token == TOKEN and result.expires_at is None
    assert TOKEN not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.login = "changed"
    assert client.requests == [("https://api.github.com/user", {"allow_redirects": False, "ssl": True})]
    assert client.options["trust_env"] is False and client.options["auto_decompress"] is False
    assert client.options["headers"]["Authorization"] == "Bearer " + TOKEN
    assert "ambient-fixture" not in repr(client.options)
    assert client.options["timeout"].total == 10
    assert client.closed and client.response.closed


@async_test
@pytest.mark.parametrize("token", [None, "", "ghp_classic", "ghs_installation", "ghu_", " ghu_x", "ghu_x\n", "ghu_" + "x" * 4096])
async def test_unsupported_tokens_never_create_http_client(token):
    def forbidden(**_):
        pytest.fail("Invalid token reached network construction")
    with pytest.raises(identity.InvalidCopilotToken) as exc:
        await identity.validate_user_token(token, session_factory=forbidden)
    assert exc.value.__context__ is None


@async_test
@pytest.mark.parametrize("status,error", [
    (401, identity.InvalidCopilotToken), (403, identity.CopilotIdentityUnavailable),
    (429, identity.CopilotIdentityUnavailable), (500, identity.CopilotIdentityUnavailable),
    (302, identity.CopilotIdentityUnavailable), (307, identity.CopilotIdentityUnavailable),
    (304, identity.CopilotIdentityUnavailable),
])
async def test_http_errors_and_redirects_are_sanitized_and_not_followed(status, error):
    response = Response(status, {"message": TOKEN}, headers=[("Location", "https://other.example/" + TOKEN),
                                                            ("X-RateLimit-Remaining", "0")])
    client = Client(response)
    with pytest.raises(error) as exc:
        await identity.validate_user_token(TOKEN, session_factory=client.factory)
    assert exc.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(exc.value))
    assert len(client.requests) == 1 and response.closed and client.closed


@async_test
@pytest.mark.parametrize("value", [[], {}, {**USER, "id": True}, {**USER, "id": 0},
                                    {**USER, "id": -1}, {**USER, "id": "123"},
                                    {**USER, "login": "bad/login"}, {**USER, "login": "bad\n"},
                                    {**USER, "login": "x" * 40}, {**USER, "login": None}])
async def test_malformed_identity_cannot_become_a_principal(value):
    with pytest.raises((identity.InvalidCopilotToken, identity.CopilotIdentityUnavailable)):
        await identity.validate_user_token(TOKEN, session_factory=Client(Response(value=value)).factory)


@async_test
async def test_nonuser_identity_rejected_but_enterprise_managed_login_allowed():
    with pytest.raises(identity.InvalidCopilotToken):
        await identity.validate_user_token(TOKEN, session_factory=Client(Response(value={**USER, "type": "Bot"})).factory)
    user = await identity.validate_user_token(TOKEN, session_factory=Client(Response(value={**USER, "login": "mona-cat_octo"})).factory)
    assert user.login == "mona-cat_octo"


@async_test
@pytest.mark.parametrize("response", [
    Response(body=b"not-json " + TOKEN.encode()),
    Response(chunks=[b"x" * 32768, b"x" * 32769]),
    Response(headers=[("Content-Length", "65537")]),
    Response(headers=[("Content-Encoding", "gzip")]),
    Response(error=TimeoutError(TOKEN)),
    Response(error=OSError(TOKEN)),
])
async def test_malformed_oversize_encoded_or_failed_stream_is_bounded_and_redacted(response):
    client = Client(response)
    with pytest.raises(identity.CopilotIdentityUnavailable) as exc:
        await identity.validate_user_token(TOKEN, session_factory=client.factory)
    assert exc.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(exc.value))
    assert response.closed and client.closed


@async_test
@pytest.mark.parametrize("header", ["2030-01-01 00:00:00 UTC", "2030-01-01 01:00:00 +0100"])
async def test_authoritative_expiration_header_is_normalized(monkeypatch, header):
    monkeypatch.setattr(identity.time, "time", lambda: 0)
    result = await identity.validate_user_token(TOKEN, session_factory=Client(Response(headers=[(identity.EXPIRATION_HEADER, header)])).factory)
    assert result.expires_at == datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()


@async_test
@pytest.mark.parametrize("headers,error", [
    ([(identity.EXPIRATION_HEADER, "2000-01-01 00:00:00 UTC")], identity.InvalidCopilotToken),
    ([(identity.EXPIRATION_HEADER, "never")], identity.CopilotIdentityUnavailable),
    ([(identity.EXPIRATION_HEADER, "2030-99-01 00:00:00 UTC")], identity.CopilotIdentityUnavailable),
    ([(identity.EXPIRATION_HEADER, "2030-01-01 00:00:00")], identity.CopilotIdentityUnavailable),
    ([(identity.EXPIRATION_HEADER, "2030-01-01 00:00:00 UTC")] * 2, identity.CopilotIdentityUnavailable),
])
async def test_bad_or_expired_header_never_becomes_unknown_lifetime(headers, error):
    with pytest.raises(error) as exc:
        await identity.validate_user_token(TOKEN, session_factory=Client(Response(headers=headers)).factory)
    assert exc.value.__context__ is None


@async_test
async def test_total_timeout_and_cancellation_close_http_resources(monkeypatch):
    class HangingResponse(Response):
        async def iter_chunked(self, _size):
            await asyncio.Event().wait()
            yield b""
    monkeypatch.setattr(identity, "TOTAL_TIMEOUT", 0.02)
    client = Client(HangingResponse())
    with pytest.raises(identity.CopilotIdentityUnavailable):
        await identity.validate_user_token(TOKEN, session_factory=client.factory)
    assert client.closed and client.response.closed
    client = Client(HangingResponse())
    task = asyncio.create_task(identity.validate_user_token(TOKEN, session_factory=client.factory))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed and client.response.closed
