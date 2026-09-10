"""Preview lifetime precedes generic shutdown and does not swallow startup faults."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.layers.copilot.chat_lifetime import copilot_chat_lifetime
from core.layers.copilot import provisioned_layer
from services.engines import copilot_chat


@pytest.fixture
def lifetime(monkeypatch):
    calls = []
    app = SimpleNamespace(state=SimpleNamespace())
    fixture = SimpleNamespace(app=app, calls=calls, enter_error=False, close_error=False, exit_error=False)

    @asynccontextmanager
    async def installation(root):
        assert root == Path('/private/copilot')
        calls.append('open')
        if fixture.enter_error:
            raise ValueError('fixture startup error')
        try:
            yield 'layer'
        finally:
            calls.append('exit')
            if fixture.exit_error:
                raise ValueError('private runtime failure')

    class Service:
        def __init__(self, layer):
            assert layer == 'layer'
            calls.append('service')

        async def aclose(self):
            assert app.state.copilot_chat is None
            calls.append('close')
            if fixture.close_error:
                raise ValueError('private service failure')

    monkeypatch.setattr(provisioned_layer, 'open_provisioned_layer', installation)
    monkeypatch.setattr(copilot_chat, 'CopilotChatService', Service)
    return fixture


@pytest.mark.asyncio
async def test_disabled_preview_does_not_open_or_construct_service(lifetime):
    async with copilot_chat_lifetime(lifetime.app, ''):
        assert lifetime.app.state.copilot_chat is None
    assert lifetime.calls == []


@pytest.mark.asyncio
async def test_enabled_preview_closes_service_before_installation(lifetime):
    async with copilot_chat_lifetime(lifetime.app, '/private/copilot'):
        assert lifetime.app.state.copilot_chat is not None
    assert lifetime.app.state.copilot_chat is None
    assert lifetime.calls == ['open', 'service', 'close', 'exit']


@pytest.mark.asyncio
async def test_invalid_opt_in_installation_fails_startup(lifetime):
    lifetime.enter_error = True
    with pytest.raises(ValueError, match='startup error'):
        async with copilot_chat_lifetime(lifetime.app, '/private/copilot'):
            pytest.fail('Invalid preview must not start')
    assert lifetime.calls == ['open'] and lifetime.app.state.copilot_chat is None


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['close_error', 'exit_error', 'both'])
async def test_cleanup_failure_is_sanitized_and_allows_remaining_app_shutdown(lifetime, caplog, failure):
    lifetime.close_error = failure in {'close_error', 'both'}
    lifetime.exit_error = failure in {'exit_error', 'both'}
    async with copilot_chat_lifetime(lifetime.app, '/private/copilot'):
        pass
    lifetime.calls.append('other engine and database shutdown')
    assert lifetime.calls == ['open', 'service', 'close', 'exit', 'other engine and database shutdown']
    assert 'cleanup is incomplete' in caplog.text and 'private' not in caplog.text


@pytest.mark.asyncio
async def test_body_failure_still_closes_resources_and_propagates(lifetime):
    with pytest.raises(ValueError, match='body error'):
        async with copilot_chat_lifetime(lifetime.app, '/private/copilot'):
            raise ValueError('body error')
    assert lifetime.calls == ['open', 'service', 'close', 'exit']
