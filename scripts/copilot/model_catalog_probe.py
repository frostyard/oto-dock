#!/usr/bin/env python3
"""Zero-turn account catalog proof through real cookies, HTTP, and sandbox runtime.

User/agent/account database reads and conversation storage are explicit fixtures.
No production configuration changes, native sessions, or inference are permitted.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

from probe import selected_token


async def run(args, report):
    with tempfile.TemporaryDirectory(prefix="otodock-model-catalog-") as directory:
        root = Path(directory)
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        import config
        import httpx
        import uvicorn
        from copilot import CopilotClient
        from fastapi import FastAPI
        from api.agents.copilot_chat import router
        from auth.providers import create_session_jwt
        from core import concurrency
        from core.layers.copilot import catalog, provisioning
        from core.layers.copilot.chat_lifetime import copilot_chat_lifetime
        from core.layers.copilot.credentials import CopilotCredential, CredentialKind, CredentialUnavailableError
        from core.sandbox.sandbox import netns_preflight
        from core.session.owned_sessions import owned_session_ids
        from core.session import session_state as state
        from services.mcp import mcp_registry
        from services.engines import copilot_chat
        from storage import database, agent_store, db_knowledge_libraries, copilot_account_store
        from conversation_fixture import MemoryConversations

        agent = root / "data/agents/model-catalog-probe"
        for name in ("workspace", "knowledge", "config"):
            (agent / name).mkdir(parents=True)
        (agent / "config/agent.md").write_text("Catalog fixture; no inference is authorized.")
        (agent / "workspace/sentinel.txt").write_text("unchanged")
        human = {"sub": "catalog-user", "username": "catalog-user", "name": "Catalog user",
                 "display_name": "Catalog user", "email": "catalog@example.invalid", "role": "member"}
        other = {**human, "sub": "other-user", "username": "other-user", "role": "admin"}
        agent_row = {"slug": agent.name, "admin_only": False, "collaborative": False, "default_scope": "agent"}
        credential = CopilotCredential("e29cfd83-bba9-41ac-8e45-d4f9e949d583", "synthetic-github-principal",
                                       "fixture-revision", CredentialKind.USER_TOKEN, selected_token())
        revoked = False
        held, joined = asyncio.Event(), asyncio.Event()
        originals, runtimes, platform_ids = [], [], []
        history = MemoryConversations()
        paths = provisioning.load(args.provisioned_root)
        roots = (paths.records_root, paths.state_root, paths.homes_root)

        def allocations():
            return tuple(tuple(sorted(str(path.relative_to(base)) for path in base.rglob("*"))) for base in roots)

        baseline = allocations()
        baseline_claims = owned_session_ids()

        def replace(module, name, value):
            originals.append((module, name, getattr(module, name)))
            setattr(module, name, value)

        def read_credential(account_id, scope):
            if revoked or account_id != credential.account_id or scope.user_sub != human["sub"]:
                raise CredentialUnavailableError("Fixture account unavailable")
            return credential

        original_service = copilot_chat.CopilotChatService
        original_runtime = catalog.SandboxedCopilotRuntime

        class Service(original_service):
            def __init__(self, layer):
                super().__init__(layer, store=history)
                original = layer.list_models

                async def models(sid, configuration):
                    platform_ids.append(sid)
                    return await original(sid, configuration)
                layer.list_models = models

        async def forbidden_session(*args, **kwargs):
            report["native_session_calls"] += 1
            raise RuntimeError("Native sessions are prohibited in this catalog probe")

        class Runtime(original_runtime):
            def __init__(self, *values, **options):
                if len(runtimes) >= 2:
                    raise RuntimeError("Catalog runtime limit reached")
                super().__init__(*values, **options)
                runtimes.append(self)
                self.probe_index = len(runtimes)

            async def start(self):
                client = await super().start()
                original_request = client._client.request

                async def request(method, params=None, **options):
                    if method.startswith("session."):
                        report["native_session_calls"] += 1
                        raise RuntimeError("Native session RPC prohibited")
                    result = await original_request(method, params, **options)
                    if method == "models.list":
                        report["real_catalog_rpc_count"] += 1
                        if self.probe_index == 2:
                            held.set()
                            try:
                                await asyncio.Event().wait()
                            except asyncio.CancelledError:
                                joined.set()
                                raise
                    return result
                client._client.request = request
                return client

        replace(config, "get_jwt_expiry_hours", lambda: 8)
        replace(database, "get_user", {human["sub"]: human, other["sub"]: other}.get)
        replace(database, "get_user_agent_roles", lambda _: {agent.name: "manager"})
        replace(database, "get_user_default_agent", lambda _: agent.name)
        replace(agent_store, "get_all_agents", lambda: [dict(agent_row)])
        replace(agent_store, "get_agent", lambda slug: dict(agent_row) if slug == agent.name else None)
        replace(db_knowledge_libraries, "attachments_for_consumer", lambda _: [])
        replace(copilot_account_store, "read_credential", read_credential)
        replace(mcp_registry, "resolve_sandbox_egress", lambda *a, **kw: (["1"], []))
        replace(catalog, "SandboxedCopilotRuntime", Runtime)
        replace(copilot_chat, "CopilotChatService", Service)
        replace(CopilotClient, "create_session", forbidden_session)
        replace(CopilotClient, "resume_session", forbidden_session)
        netns_preflight()
        concurrency.init()

        @asynccontextmanager
        async def lifespan(app):
            async with copilot_chat_lifetime(app, str(args.provisioned_root)):
                yield

        app = FastAPI(lifespan=lifespan)
        app.include_router(router)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        pending = None
        try:
            async with asyncio.timeout(90):
                while not server.started:
                    if serving.done():
                        await serving
                        raise RuntimeError("Catalog HTTP server did not start")
                    await asyncio.sleep(0.01)
                cookie = create_session_jwt(human["sub"], human["email"], human["name"], human["role"])
                other_cookie = create_session_jwt(other["sub"], other["email"], other["name"], other["role"])
                endpoint = "/v1/copilot/chat/models"
                body = {"agent": agent.name, "account_id": credential.account_id}
                async with httpx.AsyncClient(base_url=base, cookies={"session": cookie}, headers={"Origin": base}, timeout=50) as client:
                    denied = await client.post(endpoint, json=body, headers={"Origin": "https://invalid.example"})
                    assert denied.status_code == 403 and not runtimes
                    report["cross_origin_denied"] = True
                    denied = await client.post(endpoint, json={**body, "account_id": "d0c87453-5e92-4a15-a65b-8d64ee7dca2f"})
                    assert denied.status_code in {403, 404, 503} and not runtimes
                    report["foreign_account_denied_before_runtime"] = True
                    async with httpx.AsyncClient(base_url=base, cookies={"session": other_cookie}, headers={"Origin": base}) as foreign:
                        denied = await foreign.post(endpoint, json=body)
                        assert denied.status_code in {403, 404, 503} and not runtimes
                    report["other_admin_cannot_borrow_selected_account"] = True
                    response = await client.post(endpoint, json=body)
                    report["catalog_http_status"] = response.status_code
                    assert response.status_code == 200
                    rows = response.json()["models"]
                    assert 0 < len(rows) <= 200
                    assert all(set(row) == {"id", "name", "available", "policy", "multiplier"} for row in rows)
                    selected = [row for row in rows if row["id"] == "gpt-5-mini"]
                    report["model_count"] = len(rows)
                    report["gpt_5_mini_present"] = bool(selected)
                    report["gpt_5_mini_selectable"] = bool(selected and selected[0]["available"])
                    assert selected
                    assert len(runtimes) == 1 and not runtimes[0].alive
                    assert allocations() == baseline and not app.state.copilot_chat._entries
                    assert not history.rows and not history.frames
                    report["first_response_after_runtime_and_private_state_cleanup"] = True
                    pending = asyncio.create_task(client.post(endpoint, json=body))
                    await asyncio.wait_for(held.wait(), 40)
                    assert len(runtimes) == 2 and runtimes[-1].alive
                    revoked = True
                    response = await asyncio.wait_for(pending, 15)
                    report["revoked_request_http_status"] = response.status_code
                    assert response.status_code == 503 and joined.is_set()
                    assert response.json() == {"detail": "Copilot chat preview is unavailable"}
                    report["revocation_error_sanitized"] = True
                    assert not app.state.copilot_chat._entries
                    report["account_revocation_cancels_held_real_catalog_response"] = True
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            server.should_exit = True
            await asyncio.wait_for(serving, 25)
            sock.close()
            report["runtime_count"] = len(runtimes)
            report["normal_cleanup"] = all(not runtime.alive and not runtime.forced_cleanup for runtime in runtimes)
            report["all_claims_released"] = owned_session_ids() == baseline_claims
            report["all_contexts_released"] = all(state.get_session_security(sid) is None for sid in platform_ids)
            report["no_new_private_allocations"] = allocations() == baseline
            report["conversation_rows_created"] = len(history.rows)
            report["actual_agent_workspace_unchanged"] = (agent / "workspace/sentinel.txt").read_text() == "unchanged"
            for module, name, value in reversed(originals):
                setattr(module, name, value)
            assert report["normal_cleanup"] and report["all_claims_released"] and report["all_contexts_released"]
            assert report["no_new_private_allocations"] and report["conversation_rows_created"] == 0
            assert report["native_session_calls"] == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provisioned-root", required=True, type=Path)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "live_turn_limit": 0, "runtime_limit": 2, "flow_deadline_seconds": 120,
              "actual_cookie_http_authentication": True, "actual_authenticated_config_builder": True,
              "actual_chat_service_and_owned_sandbox_runtime": True, "native_session_calls": 0,
              "real_catalog_rpc_count": 0, "postgresql": False,
              "fixtures": "controlled user/agent/account reads; memory conversation store; second real models.list response held until revocation"}
    started = time.monotonic()

    async def bounded():
        async with asyncio.timeout(120):
            await run(args, report)

    try:
        asyncio.run(bounded())
        report["result"] = "passed"
    except Exception as error:
        report["error_type"] = type(error).__name__
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
