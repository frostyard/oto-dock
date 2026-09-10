"""Opt-in application ownership of the local Copilot chat preview."""

from contextlib import asynccontextmanager
import logging
from pathlib import Path

logger = logging.getLogger("claude-proxy.copilot")


@asynccontextmanager
async def copilot_chat_lifetime(app, root: str):
    app.state.copilot_chat = None
    if not root:
        yield
        return
    # Disabled installations neither open a bundle nor require the SDK.
    # A configured but invalid installation fails startup explicitly.
    from core.layers.copilot.provisioned_layer import open_provisioned_layer
    from services.engines.copilot_chat import CopilotChatService

    installation = open_provisioned_layer(Path(root))
    layer = await installation.__aenter__()
    try:
        service = CopilotChatService(layer)
        app.state.copilot_chat = service
        try:
            yield
        finally:
            app.state.copilot_chat = None
            try:
                await service.aclose()
            except Exception:
                # Failed owners remain claimed; an optional preview's failure
                # must not skip shutdown of every other engine and the DB pool.
                logger.error("Copilot preview cleanup is incomplete")
    finally:
        try:
            await installation.__aexit__(None, None, None)
        except Exception:
            logger.error("Copilot provisioned layer cleanup is incomplete")
