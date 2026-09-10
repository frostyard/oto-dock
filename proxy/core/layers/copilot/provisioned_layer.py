"""Explicit local service lifetime for an already provisioned Copilot bundle."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from core.layers.copilot.layer import CopilotExecutionLayer
from core.layers.copilot.provisioning import check_sdk, load
from core.layers.copilot.sandbox_home import CopilotSandboxHomes
from core.layers.copilot.session_records import CopilotSessionRecords


@asynccontextmanager
async def open_provisioned_layer(root: Path, *, forbidden_roots: tuple[Path, ...] = ()):
    """Verify on open, then own all sessions and scratch descriptors until exit.

    No installation, account selection, inference, or global registration happens
    here. The application must authenticate each request with the explicit
    Copilot config builder. Known platform mount roots are always excluded; the
    caller can add further mount roots. Per-session sandbox validation remains
    authoritative for the complete generated mount set.
    """
    import config

    excluded = (config.AGENTS_DIR, config.MCPS_DIR, config.PLATFORM_CONFIG_DIR, *forbidden_roots)
    paths = await asyncio.to_thread(load, root, forbidden_roots=excluded)
    check_sdk()
    homes = CopilotSandboxHomes(paths.homes_root)
    try:
        layer = CopilotExecutionLayer(
            runtime_path=paths.runtime_path,
            records=CopilotSessionRecords(paths.records_root, state_root=paths.state_root),
            homes=homes,
        )
        try:
            yield layer
        finally:
            # aclose joins even when the caller is repeatedly cancelled. Failed
            # owners remain claimed; closing this descriptor never erases state.
            await layer.aclose()
    finally:
        homes.close()
