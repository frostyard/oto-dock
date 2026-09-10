# Copilot authentication contract

Verified on 2026-09-09 against installed Python SDK **1.0.13**, bundled runtime
**1.0.83**, protocol **3**, and the official sources linked below. This documents
the foundation contract; it does not enable accounts in the application or claim
that installation-token inference has been live-tested.

## Explicit credential channels

| Credential | Runtime channel | Local expiry contract |
| --- | --- | --- |
| OAuth user, GitHub App user, fine-grained PAT | SDK `github_token` | Check known expiry; unknown user-token expiry does not establish a guaranteed lifetime |
| GitHub App installation | Child environment `COPILOT_GITHUB_TOKEN`; SDK token absent | Require known expiry and reject at or beyond the deadline |
| No credential | Neither channel | Only for development transport checks; no ambient login fallback |

The SDK converts its explicit user token into `COPILOT_SDK_AUTH_TOKEN` plus an
`--auth-token-env` flag. The value is not put in argv. Installation tokens must
not use that option. GitHub currently documents installation tokens with a
one-hour lifetime, an installation with Copilot Requests permission, organization
enablement, and an All repositories access requirement. Refresh requires a new
token and child runtime restart. These prerequisites are not configured by this
change. [GitHub server authentication](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/server-to-server-tokens)

`SandboxedCopilotRuntime` accepts an explicit `CopilotCredential`, mutually
exclusive with the legacy `github_token` development-probe argument. It checks
local usability before sandbox construction and again before SDK construction.
It rejects authentication in caller environment before adding the credential's
selected channel. Typed credentials also require a `PrivateCopilotSessionState`.
After validating its ownership and allocation identity, the runtime appends a
trusted bind to `/var/lib/otodock/copilot` after the builder's other mounts. It
rejects overlap with runtime assets. Community MCP mount permissions are unchanged.
It always uses `use_logged_in_user=False`, `mode="empty"`, and the mandatory
sandbox. Closing drops its credential, token, and client references while leaving
the caller-owned private state available for sequential resume.
Reference removal does not claim to zero immutable Python strings in memory.

Caller environment also rejects `GITHUB_COPILOT_API_TOKEN` and `COPILOT_API_URL`:
these select the documented direct API authentication route. No credential may
silently fall back to stored CLI, keychain, GitHub CLI, or inherited environment
credentials. [GitHub authentication priority](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/authenticate)

## Refresh and resume

The application owns user-token storage and OAuth refresh; possession of a
nonexpired token does not prove remote validity or entitlement.
[GitHub OAuth lifecycle](https://docs.github.com/en/copilot/how-tos/copilot-sdk/setup/github-oauth)

SDK 1.0.13 has an experimental session `github_token_provider`. The public Python
documentation says `expiresIn` must be positive, but the pinned runtime's
`schemas/api.schema.json`, definition `GitHubTokenAcquireResult`, requires a
minimum of **3601 seconds**, exceeding its one-hour preflight refresh threshold.
The installed generated RPC comment agrees. Do not use this provider for
one-hour installation tokens or short synthetic application leases. Initial
acquisition occurs on create/resume; idle refresh happens before a subsequent
credential-consuming operation, without a background timer.
[Official Python SDK documentation](https://github.com/github/copilot-sdk/blob/main/python/README.md)

The bounded first proof should restart with a newly acquired explicit user
credential after fully closing the old writer, then resume the same isolated
session with callbacks, permissions, and policy supplied again. Reusing an
existing token under a new application lease proves restart mechanics, not
OAuth refresh, real expiry handling by GitHub, or installation authentication.

User-token connection now verifies a stable GitHub.com user identity through the
fixed REST `/user` endpoint; see the [connection preview](copilot-connect-results.md).
This remains separate from Copilot model entitlement and real token refresh.

## Identity, entitlement, and redaction

The following details come from the installed SDK and bundled runtime schema:

- Client `get_auth_status()` returns authenticated state, auth type, host, login,
  and a freeform status message. Experimental session
  `session.rpc.git_hub_auth.get_status()` adds optional `copilot_plan`.
- Neither status supplies a stable GitHub user ID, expiry, membership proof, or
  authoritative billing organization. Bind accounts using separately verified
  identity; a login label alone is insufficient.
- `list_models()` returns capabilities, optional policy and billing metadata.
  SDK results cache until disconnect. Recheck under the replacement credential;
  successful authentication alone does not establish selected-model entitlement.
- Experimental `set_credentials()` immediately installs supplied credentials
  without validation; account metadata resolution is asynchronous and best-effort.
  Its success must not be treated as verification.
- Tokens stay out of credential repr and runtime argv. Do not serialize credential
  dataclasses, SDK options, environment, callbacks, or raw auth objects. Project
  auth/model status into an allowlist; omit freeform errors/status messages from
  public evidence. Secrets can appear in arbitrary upstream exception text.

Offline runtime tests cover exact user/installation/no-auth channel selection,
prelaunch expiry rejection, the second expiry check, ambient override rejection,
argument conflicts, reference cleanup, and repr/argv secrecy. These tests use a
fake SDK and do not make inference requests or validate installation entitlement.

## Sandboxed account restart result

The [bounded account probe](../../scripts/copilot/account_probe.py) passed in
**15.617 seconds** with SDK 1.0.13/runtime 1.0.83 and two `gpt-5-mini` prompts.
[Sanitized evidence](evidence/copilot-account-restart.json) records:

- A first typed user credential completed one no-tool marker turn, then its
  runtime closed before the next writer started.
- Reacquiring the same selected GitHub token under a new synthetic repository
  revision and starting a new sandbox preserved the private history. The resumed
  second turn recalled the random marker without receiving it in the new prompt.
- Both turns revalidated the credential source through the supervisor's admission
  callback. Changing the observed generation while idle independently closed the
  second runtime; no third inference was needed.
- A third runtime used the same state with no credential. Its auth status was
  unauthenticated: no stored login fallback was observed.
- All three runtimes closed normally without forced cleanup. No native tools or
  permission requests occurred, and private state was explicitly discarded.

This test uses an in-memory synthetic account repository and one actual user
token. It does not test PostgreSQL account persistence, two distinct live accounts,
OAuth refresh, installation tokens, or billing. Runtime client auth status did
not expose a usable login in this run; token equality was checked internally,
without exporting the token or claiming independent identity verification.

The test caught an integration defect: passing private state through an MCP
`SandboxMount` silently dropped it because the host source was outside the
agent/MCP install trees. The trusted internal runtime bind fixes that boundary;
the account proof then confirmed an actual persisted `events.jsonl` and cold
resume after a completed turn. A session with no real turn is not a valid cold
history proof. Runtime tests now cover missing private state, invalid ownership,
fixed destination, overlap rejection, mount order, and state survival on close.
