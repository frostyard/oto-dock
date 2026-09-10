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
        observed_usage, held_usage, usage_receivers = {}, [], []
        usage_after_first_close = {}
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

            async def start(self):
                client = await super().start()

                def capture(original, kind):
                    async def invoke(*values, **options):
                        assert options.get("reasoning_effort") == args.reasoning_effort
                        if args.reasoning_effort is None:
                            assert "reasoning_effort" not in options
                        report.setdefault("sdk_reasoning_options", []).append({
                            "operation": kind, "reasoning_effort": options.get("reasoning_effort"),
                            "explicit": "reasoning_effort" in options,
                        })
                        if args.verify_usage:
                            receive = options["on_event"]
                            usage_receivers.append(receive)

                            def usage_event(event):
                                raw = event if isinstance(event, dict) else event.to_dict()
                                data = raw.get("data", {})
                                if (raw.get("type") == "assistant.usage" and not raw.get("agentId")
                                        and not data.get("parentToolCallId")):
                                    event_id = raw["id"]
                                    projection = {
                                        "type": "usage", "event_id": event_id, "reported_model": data["model"],
                                        "input_tokens": data.get("inputTokens"), "output_tokens": data.get("outputTokens"),
                                        "cache_read_tokens": data.get("cacheReadTokens"),
                                        "cache_write_tokens": data.get("cacheWriteTokens"),
                                        "reasoning_tokens": data.get("reasoningTokens"),
                                        "reported_nano_aiu": (data.get("copilotUsage") or {}).get("totalNanoAiu"),
                                    }
                                    if event_id in observed_usage:
                                        assert observed_usage[event_id] == projection
                                    observed_usage[event_id] = projection
                                    if not held_usage:
                                        held_usage.append((receive, event, event_id))
                                        return
                                receive(event)
                            options["on_event"] = usage_event
                        session = await original(*values, **options)
                        if args.reasoning_effort is not None:
                            current = await client._client.request("session.model.getCurrent", {
                                "sessionId": session.session_id,
                            }, timeout=5)
                            assert current.get("reasoningEffort") == args.reasoning_effort
                            report.setdefault("runtime_reasoning_snapshots", []).append({
                                "operation": kind, "reasoning_effort": current["reasoningEffort"],
                            })
                        return session
                    return invoke

                client.create_session = capture(client.create_session, "create")
                client.resume_session = capture(client.resume_session, "resume")
                original_request = client._client.request

                async def request(method, params=None, **options):
                    if method in {"session.create", "session.resume"}:
                        assert params.get("reasoningEffort") == args.reasoning_effort
                        if args.reasoning_effort is None:
                            assert "reasoningEffort" not in params
                        report.setdefault("native_reasoning_payloads", []).append({
                            "operation": method, "reasoning_effort": params.get("reasoningEffort"),
                            "explicit": "reasoningEffort" in params,
                        })
                    result = await original_request(method, params, **options)
                    if method == "models.list" and args.reasoning_effort is not None:
                        selected = next(model for model in result["models"] if model["id"] == "gpt-5-mini")
                        assert args.reasoning_effort in selected.get("supportedReasoningEfforts", [])
                        report["selected_effort_advertised_by_real_account_catalog"] = True
                    return result
                client._client.request = request
                return client

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
                    if args.reasoning_effort is not None:
                        create["reasoning_effort"] = args.reasoning_effort
                        denied = await client.post(prefix + "/sessions", json={**create, "reasoning_effort": "unsupported"})
                        assert denied.status_code == 422 and not runtimes
                        report["invalid_effort_rejected_before_runtime"] = True
                    denied = await client.post(prefix + "/sessions", json=create, headers={"Origin": "https://invalid.example"})
                    assert denied.status_code == 403 and not runtimes
                    report["cross_origin_start_denied"] = True

                    async def verified_usage(cid):
                        async with asyncio.timeout(10):
                            while True:
                                response = await client.get(f"{prefix}/conversations/{cid}", params={"agent": agent.name})
                                assert response.status_code == 200
                                archived = response.json()
                                usage = [event for event in archived["events"] if event["type"] == "usage"]
                                actual = {event["event_id"]: {key: value for key, value in event.items() if key != "seq"}
                                          for event in usage}
                                assert len(actual) == len(usage)
                                if actual == observed_usage:
                                    return archived, actual
                                await asyncio.sleep(0.025)

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
                            if args.verify_usage:
                                assert held_usage and observed_usage
                                receive, event, held_id = held_usage[0]
                                assert history.rows[cid]["turn_active"] is False
                                receive(event)
                                receive(event)
                                checked, _ = await verified_usage(cid)
                                terminal_seq = max(event["seq"] for event in checked["events"] if event["type"] == "turn_complete")
                                held_seq = next(event["seq"] for event in checked["events"]
                                                if event["type"] == "usage" and event["event_id"] == held_id)
                                assert held_seq > terminal_seq
                                report["held_real_usage_persisted_after_turn_completion"] = True
                                report["duplicate_real_usage_deduplicated"] = True
                            old_handle = sid
                            assert (await client.delete(f"{prefix}/sessions/{sid}")).status_code == 204
                            archived = (await client.get(f"{prefix}/conversations/{cid}", params={"agent": agent.name})).json()
                            assert archived["conversation"]["can_resume"] is True
                            if args.reasoning_effort is not None:
                                assert archived["conversation"]["reasoning_effort"] == args.reasoning_effort
                                assert history.rows[cid]["reasoning_effort"] == args.reasoning_effort
                                report["explicit_effort_persisted_in_owned_conversation_metadata"] = True
                            assert [event for event in archived["events"] if event["type"] != "usage"][-1]["type"] == "turn_complete"
                            if args.verify_usage:
                                _, actual = await verified_usage(cid)
                                usage_after_first_close.update(actual)
                                report["reported_usage_after_first_close"] = len(actual)
                            assert any(event["type"] == "permission_prompt" for event in archived["events"])
                            assert [event["seq"] for event in archived["events"]] == list(range(1, len(archived["events"]) + 1))
                            listing = (await client.get(prefix + "/conversations", params={"agent": agent.name})).json()
                            assert [item["id"] for item in listing["conversations"]] == [cid]
                            assert len(runtimes) == 1 and not runtimes[0].alive
                            report["saved_transcript_read_does_not_start_runtime"] = True
                            # Route-bound reads/resume must not expose or mutate a
                            # conversation when the URL belongs to another agent.
                            before = history.get(cid, human["sub"])
                            denied = await client.get(f"{prefix}/conversations/{cid}", params={"agent": "different-agent"})
                            assert denied.status_code == 404
                            denied = await client.post(f"{prefix}/conversations/{cid}/resume",
                                                       params={"agent": "different-agent"},
                                                       json={"revision": archived["conversation"]["revision"]})
                            assert denied.status_code == 404
                            assert history.get(cid, human["sub"]) == before and len(runtimes) == 1
                            report["wrong_agent_route_cannot_read_resume_or_mutate"] = True
                            report["agent_bound_history_read_and_resume"] = True
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
                            archived = (await client.get(f"{prefix}/conversations/{cid}", params={"agent": agent.name})).json()
                            if args.verify_usage:
                                before_runtime_count = len(runtimes)
                                _, actual = await verified_usage(cid)
                                assert actual == usage_after_first_close and len(runtimes) == before_runtime_count
                                report["persisted_usage_survives_service_replacement_without_runtime"] = True
                            revision = archived["conversation"]["revision"]
                            if args.reasoning_effort is not None:
                                before = history.get(cid, human["sub"])
                                rejected = await client.post(f"{prefix}/conversations/{cid}/resume", params={"agent": agent.name},
                                                             json={"revision": revision, "reasoning_effort": "high"})
                                assert rejected.status_code == 422 and len(runtimes) == 1
                                assert history.get(cid, human["sub"]) == before
                                report["resume_effort_override_rejected_before_startup"] = True
                            rejected = await client.post(f"{prefix}/conversations/{cid}/resume", params={"agent": agent.name}, json={"revision": revision - 1})
                            assert rejected.status_code == 409 and len(runtimes) == 1
                            resumed = await client.post(f"{prefix}/conversations/{cid}/resume", params={"agent": agent.name}, json={"revision": revision})
                            assert resumed.status_code == 201
                            assert resumed.json()["conversation_id"] == cid
                            sid = resumed.json()["session_id"]
                            assert sid != old_handle
                            if args.verify_usage:
                                before = history.get(cid, human["sub"])
                                usage_receivers[-1](held_usage[0][1])
                                await app.state.copilot_chat._flush_usage(app.state.copilot_chat._entries[platform_sid])
                                assert history.get(cid, human["sub"]) == before
                                _, actual = await verified_usage(cid)
                                assert actual == usage_after_first_close
                                report["cross_runtime_replayed_usage_deduplicated_without_revision_change"] = True
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
                    partial = (await client.get(f"{prefix}/conversations/{cid}", params={"agent": agent.name})).json()
                    assert partial["conversation"]["can_resume"] is False
                    assert any(event["type"] == "text" for event in partial["events"])
                    rejected = await client.post(f"{prefix}/conversations/{cid}/resume", params={"agent": agent.name}, json={"revision": partial["conversation"]["revision"]})
                    assert rejected.status_code == 409
                    report["interrupted_transcript_readable_but_not_resumable"] = True
                    if args.verify_usage:
                        _, actual = await verified_usage(cid)
                        assert all(actual.get(key) == value for key, value in usage_after_first_close.items())
                        assert len(actual) > len(usage_after_first_close)
                        report["reported_usage_after_resume_and_interrupt"] = len(actual)
                        report["persisted_usage_matches_observed_native_events"] = True
                        report["reported_token_totals"] = {
                            key: sum(event[key] for event in actual.values() if event[key] is not None)
                            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
                        }
                        report["missing_metric_observation_counts"] = {
                            key: sum(event[key] is None for event in actual.values())
                            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "reported_nano_aiu")
                        }
                    await new_session()
                    revoked = session_ids[-1]
                    roles.clear()
                    async with asyncio.timeout(20):
                        while get_owned_session(revoked) is not None:
                            await asyncio.sleep(0.05)
                    report["database_role_revocation_closed_idle_session"] = True
                    assert {item["operation"] for item in report["sdk_reasoning_options"]} == {"create", "resume"}
                    assert {item["operation"] for item in report["native_reasoning_payloads"]} == {"session.create", "session.resume"}
                    report["native_create_and_cold_resume_effort_match"] = True
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
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max"), default=None)
    parser.add_argument("--verify-usage", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = {"result": "failed", "sdk_version": "1.0.13", "runtime_version": "1.0.83",
              "live_turn_limit": 3, "flow_deadline_seconds": 240, "actual_cookie_authentication": True,
              "actual_http_sse": True, "actual_authenticated_config_builder": True,
              "actual_chat_service": True, "actual_platform_permission_authority": True,
              "actual_private_history_and_resume_locks": True,
              "postgresql": False, "storage": "controlled user, agent and account reads; in-memory conversation store"}
    report["reasoning_effort"] = args.reasoning_effort
    report["verify_usage"] = args.verify_usage
    if args.verify_usage:
        report["usage_fixture"] = "first real usage callback held until idle; same event replayed twice locally and once after cold resume"
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
