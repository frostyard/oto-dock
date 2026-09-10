#!/usr/bin/env python3
"""Bounded real-cookie/HTTP Copilot preview proof with controlled storage reads.

Uses a verified local installation, real builder/service/sandbox/permission
queues and real HTTP streaming. Database authority/account reads are fixtures;
no deployment, real-user authentication record or PostgreSQL data is changed.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import time

from probe import selected_token


async def run(args, report):
    with tempfile.TemporaryDirectory(prefix="otodock-copilot-http-") as directory:
        root = Path(directory)
        os.environ["PLATFORM_DATA_DIR"] = str(root / "data")
        os.environ["PLATFORM_CONFIG_DIR"] = str(root / "config")
        (root / "config").mkdir()
        (root / "config/config.env").write_text("OTODOCK_STORAGE_QUOTAS=off\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy"))
        import config
        import httpx
        from fastapi import FastAPI
        import uvicorn
        from api.agents.copilot_chat import router
        from auth.providers import create_session_jwt
        from core.layers.copilot.chat_lifetime import copilot_chat_lifetime
        from core import concurrency
        from core.layers.copilot import local_session as factory
        from core.layers.copilot.credentials import CopilotCredential, CredentialKind
        from core.sandbox.sandbox import netns_preflight
        from core.session.owned_sessions import get_owned_session
        from core.session import session_state as state
        from services.mcp import mcp_registry
        from services.engines import copilot_chat
        from conversation_fixture import MemoryConversations
        from storage import database, agent_store, db_knowledge_libraries, copilot_account_store

        agent = root / "data/agents/chat-preview-probe"
        for name in ("workspace", "knowledge", "config"):
            (agent / name).mkdir(parents=True)
        (agent / "config/agent.md").write_text("Follow the user's request exactly. Use only the requested tools.")
        human = {"sub": "preview-user", "username": "preview-user", "name": "Preview user",
                 "display_name": "Preview user", "email": "preview@example.invalid", "role": "member"}
        other = {**human, "sub": "other-user", "username": "other-user", "role": "admin"}
        roles = {agent.name: "manager"}
        agent_row = {"slug": agent.name, "admin_only": False, "collaborative": False, "default_scope": "agent"}
        credential = CopilotCredential("e29cfd83-bba9-41ac-8e45-d4f9e949d583", "synthetic-github-principal", "fixture-revision",
                                       CredentialKind.USER_TOKEN, selected_token())
        originals = []
        runtimes = []
        history = MemoryConversations()
        original_service = copilot_chat.CopilotChatService

        class Service(original_service):
            def __init__(self, layer):
                super().__init__(layer, store=history)

        def replace(module, name, value):
            originals.append((module, name, getattr(module, name)))
            setattr(module, name, value)

        def read_credential(account_id, scope):
            assert account_id == credential.account_id and scope.user_sub == human["sub"]
            return credential

        original_runtime = factory.SandboxedCopilotRuntime

        class Runtime(original_runtime):
            def __init__(self, *values, **options):
                super().__init__(*values, **options)
                runtimes.append(self)

        replace(config, "get_jwt_expiry_hours", lambda: 8)
        replace(database, "get_user", {human["sub"]: human, other["sub"]: other}.get)
        replace(database, "get_user_agent_roles", lambda sub: dict(roles) if sub == human["sub"] else {})
        replace(database, "get_user_default_agent", lambda sub: agent.name)
        replace(agent_store, "get_all_agents", lambda: [dict(agent_row)])
        replace(agent_store, "get_agent", lambda slug: dict(agent_row) if slug == agent.name else None)
        replace(db_knowledge_libraries, "attachments_for_consumer", lambda _: [])
        replace(copilot_account_store, "read_credential", read_credential)
        replace(mcp_registry, "resolve_sandbox_egress", lambda *a, **kw: (["1"], []))
        replace(factory, "SandboxedCopilotRuntime", Runtime)
        replace(copilot_chat, "CopilotChatService", Service)
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
        session_ids = []
        conversation_ids = []
        replacement = None
        try:
            async with asyncio.timeout(240):
                while not server.started:
                    if serving.done():
                        await serving
                        raise RuntimeError("Preview HTTP server did not start")
                    await asyncio.sleep(0.01)
                cookie = create_session_jwt(human["sub"], human["email"], human["name"], human["role"])
                other_cookie = create_session_jwt(other["sub"], other["email"], other["name"], other["role"])
                prefix = "/v1/copilot/chat"
                async with httpx.AsyncClient(base_url=base, cookies={"session": cookie},
                                             headers={"Origin": base}, timeout=75) as client:
                    assert (await client.get(prefix + "/status")).json() == {"available": True}
                    create = {"agent": agent.name, "account_id": credential.account_id,
                              "model": "gpt-5-mini", "permission_mode": "default"}
                    denied = await client.post(prefix + "/sessions", json=create, headers={"Origin": "https://invalid.example"})
                    assert denied.status_code == 403 and not runtimes
                    report["cross_origin_start_denied"] = True

                    async def new_session():
                        response = await client.post(prefix + "/sessions", json=create)
                        report["last_create_status"] = response.status_code
                        assert response.status_code == 201
                        sid = response.json()["session_id"]
                        cid = response.json()["conversation_id"]
                        conversation_ids.append(cid)
                        session_ids.append(history.rows[cid]["platform_session_id"])
                        return sid

                    sid = await new_session()
                    async with httpx.AsyncClient(base_url=base, cookies={"session": other_cookie},
                                                 headers={"Origin": base}) as foreign:
                        denied = await foreign.post(f"{prefix}/sessions/{sid}/turn", json={"text": "must not run"})
                        assert denied.status_code == 404
                    report["other_admin_cannot_drive_session"] = True
                    cid = conversation_ids[-1]
                    platform_sid = session_ids[-1]
                    marker = "PREVIEW_" + secrets.token_hex(10).upper()
                    permissions = 0
                    for index, prompt in enumerate((
                        f'Use create exactly once with path="/workspace/proof.txt" and file_text="{marker}". '
                        'Do not add a newline. Then reply CREATED.',
                        'Without using any tools, reply with only the exact text you wrote into proof.txt.',
                    ), 1):
                        completed = False
                        text_parts = []
                        tool_events = 0
                        async with client.stream("POST", f"{prefix}/sessions/{sid}/turn", json={"text": prompt}) as response:
                            assert response.status_code == 200
                            async for line in response.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                frame = json.loads(line[6:])
                                assert frame["type"] != "error"
                                if frame["type"] == "permission_prompt":
                                    assert frame["tool_name"] == "Write"
                                    assert frame["tool_input"] == {"file_path": "/workspace/proof.txt", "content": marker}
                                    permissions += 1
                                    approved = await client.post(f"{prefix}/sessions/{sid}/permission", json={
                                        "request_id": frame["request_id"], "approved": True,
                                    })
                                    assert approved.status_code == 204
                                if frame["type"] == "text":
                                    text_parts.append(frame["content"])
                                tool_events += frame["type"] == "tool_use"
                                completed |= frame["type"] == "turn_complete"
                        assert completed and (agent / "workspace/proof.txt").read_text() == marker
                        if index == 2:
                            assert marker in "".join(text_parts)
                        report.setdefault("turns", []).append({"turn": index, "completed": completed,
                                                              "tool_events": tool_events, "fixture_intact": True})
                        if index == 1:
                            old_handle = sid
                            assert (await client.delete(f"{prefix}/sessions/{sid}")).status_code == 204
                            archived = (await client.get(f"{prefix}/conversations/{cid}")).json()
                            assert archived["conversation"]["can_resume"] is True
                            assert archived["events"][-1]["type"] == "turn_complete"
                            assert any(event["type"] == "permission_prompt" for event in archived["events"])
                            assert [event["seq"] for event in archived["events"]] == list(range(1, len(archived["events"]) + 1))
                            listing = (await client.get(prefix + "/conversations")).json()
                            assert [item["id"] for item in listing["conversations"]] == [cid]
                            assert len(runtimes) == 1 and not runtimes[0].alive
                            report["saved_transcript_read_does_not_start_runtime"] = True
                            async with httpx.AsyncClient(base_url=base, cookies={"session": other_cookie},
                                                         headers={"Origin": base}) as foreign:
                                assert (await foreign.get(f"{prefix}/conversations/{cid}")).status_code == 404
                                assert (await foreign.get(prefix + "/conversations")).json()["conversations"] == []
                            report["other_admin_cannot_read_saved_conversation"] = True
                            # Replace the application service and provisioned layer;
                            # the HTTP server and controlled DB seam stay in place.
                            await app.state.copilot_chat.aclose()
                            replacement = copilot_chat_lifetime(app, str(args.provisioned_root))
                            await replacement.__aenter__()
                            archived = (await client.get(f"{prefix}/conversations/{cid}")).json()
                            revision = archived["conversation"]["revision"]
                            rejected = await client.post(f"{prefix}/conversations/{cid}/resume", json={"revision": revision - 1})
                            assert rejected.status_code == 409 and len(runtimes) == 1
                            resumed = await client.post(f"{prefix}/conversations/{cid}/resume", json={"revision": revision})
                            assert resumed.status_code == 201
                            assert resumed.json()["conversation_id"] == cid
                            sid = resumed.json()["session_id"]
                            assert sid != old_handle
                            assert (await client.delete(f"{prefix}/sessions/{old_handle}")).status_code == 404
                            assert get_owned_session(platform_sid) is not None and runtimes[-1].alive
                            report["cold_resume_after_service_and_layer_replacement"] = True
                            report["stale_handle_cannot_close_resumed_runtime"] = True
                            report["stale_revision_rejected_before_runtime_start"] = True

                    assert permissions >= 1
                    report["real_native_create_permission_approved_over_http"] = True
                    report["cold_resumed_followup_remembers_prior_turn"] = True
                    async with client.stream("POST", f"{prefix}/sessions/{sid}/turn", json={
                        "text": "Without tools, write 100 short numbered arithmetic facts. Start immediately.",
                    }) as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                frame = json.loads(line[6:])
                                assert frame["type"] != "error"
                                if frame["type"] == "text" and frame.get("content"):
                                    break
                        else:
                            raise RuntimeError("No partial response")
                    async with asyncio.timeout(20):
                        while get_owned_session(platform_sid) is not None:
                            await asyncio.sleep(0.05)
                    report["disconnect_closed_active_session"] = True
                    report["turns"].append({"turn": 3, "first_text_received": True, "disconnected": True})
                    partial = (await client.get(f"{prefix}/conversations/{cid}")).json()
                    assert partial["conversation"]["can_resume"] is False
                    assert any(event["type"] == "text" for event in partial["events"])
                    rejected = await client.post(f"{prefix}/conversations/{cid}/resume", json={"revision": partial["conversation"]["revision"]})
                    assert rejected.status_code == 409
                    report["interrupted_transcript_readable_but_not_resumable"] = True
                    await new_session()
                    revoked = session_ids[-1]
                    roles.clear()
                    async with asyncio.timeout(20):
                        while get_owned_session(revoked) is not None:
                            await asyncio.sleep(0.05)
                    report["database_role_revocation_closed_idle_session"] = True
        finally:
            if replacement is not None:
                await replacement.__aexit__(None, None, None)
            server.should_exit = True
            await asyncio.wait_for(serving, 30)
            sock.close()
            report["runtime_count"] = len(runtimes)
            report["normal_cleanup"] = all(not r.alive and not r.forced_cleanup for r in runtimes)
            report["all_claims_released"] = all(get_owned_session(sid) is None for sid in session_ids)
            report["all_contexts_released"] = all(state.get_session_security(sid) is None for sid in session_ids)
            for module, name, value in reversed(originals):
                setattr(module, name, value)
            assert report["normal_cleanup"] and report["all_claims_released"] and report["all_contexts_released"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provisioned-root", required=True, type=Path)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--use-gh-token", action="store_true", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "live_turn_limit": 3, "flow_deadline_seconds": 240, "actual_cookie_authentication": True,
              "actual_http_sse": True, "actual_authenticated_config_builder": True,
              "actual_chat_service": True, "actual_platform_permission_authority": True,
              "actual_private_history_and_resume_locks": True,
              "postgresql": False, "storage": "controlled user, agent and account reads; in-memory conversation store"}
    started = time.monotonic()
    try:
        asyncio.run(run(args, report))
        report["result"] = "passed"
    except Exception as error:
        report["error_type"] = type(error).__name__
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
