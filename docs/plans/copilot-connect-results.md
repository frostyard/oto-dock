# Copilot user-token connection contract

Verified on 2026-09-10. This slice validates a supplied GitHub user token and
returns a stable identity for account binding. It does not prove Copilot model
access, organization membership, or a billing relationship.

## Vendor contract

`GET https://api.github.com/user` identifies the authenticated token owner.
GitHub App user access tokens and fine-grained personal access tokens work with
this endpoint without additional fine-grained permissions. OAuth tokens without
`read:user` still authenticate their owner and receive the public user fields;
private profile data is unnecessary for this operation. The validator requires
the returned `type` to be `User`, a positive integer `id`, and a safe login.
[GitHub authenticated user endpoint](https://docs.github.com/en/rest/users/users#get-the-authenticated-user)

The request pins REST API version `2026-03-10`, currently supported by GitHub.
[REST API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions)

The token shape is checked with the shared Copilot `USER_TOKEN` contract before
network access: OAuth user (`gho_`), GitHub App user (`ghu_`), and fine-grained PAT
(`github_pat_`). Installation and classic PAT tokens are rejected locally for
this connection path, even where another GitHub endpoint might accept them.
[Copilot authentication token types](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/authenticate)

The login check permits ASCII letters, digits, internal hyphens and underscores,
up to 39 characters. Underscores are needed for GitHub.com enterprise-managed
users; GitHub appends the enterprise shortcode to their normalized login.
[Enterprise username rules](https://docs.github.com/en/enterprise-cloud%40latest/admin/managing-iam/iam-configuration-reference/username-considerations-for-external-authentication)

GitHub documents `GitHub-Authentication-Token-Expiration` as an API response
header indicating a personal access token's expiration date. Absence of that
header does not prove unlimited validity or supply a refresh mechanism.
[GitHub token expiration announcement](https://github.blog/changelog/2021-07-26-expiration-options-for-personal-access-tokens/)

The implementation accepts a single explicit timestamp in `YYYY-MM-DD HH:MM:SS
UTC` or numeric-offset form and normalizes it to Unix seconds. A missing header
returns `expires_at=None`; a malformed, duplicate, or timezone-ambiguous header
fails verification instead of silently producing an unknown-lifetime credential.
An already-expired timestamp rejects the token. Neither token prefix nor a
successful identity response is used to invent an expiry.

## Implementation boundary

[`copilot_identity.py`](../../proxy/services/engines/copilot_identity.py) exposes
`validate_user_token(token, *, session_factory=None)`. The factory is an internal
test seam; application calls supply only the token. Its frozen result contains
`principal_id="github:user:<numeric id>"`, login, the token excluded from repr,
and optional expiry. Callers must explicitly project this object before public
serialization; excluding repr does not remove tokens from dataclass serialization.

The service makes one request to the fixed TLS URL with certificate verification,
redirects disabled, `trust_env=False`, decompression disabled, and a requested
identity encoding. It bounds the operation to 10 seconds, socket phases to 5
seconds, and the response body to 64 KiB. It does not invoke GitHub CLI or read
ambient credentials. [aiohttp client controls](https://docs.aiohttp.org/en/stable/client_reference.html)

Unsupported input, HTTP 401, a non-user identity, and authoritative expired tokens
raise sanitized `InvalidCopilotToken` for an API-level 422. HTTP 403/429, redirects,
other unexpected statuses, transport failures, malformed data, and limit breaches
raise `CopilotIdentityUnavailable` for an API-level 503. A 403 can represent
temporary authentication blocking, so it does not definitively invalidate the
supplied credential. No response message, header, token, or upstream exception
context is exposed. Cancellation propagates while HTTP resources close.
[GitHub authentication failures](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api)

## Account setup routes and dashboard

The authenticated [`copilot_accounts` router](../../proxy/api/auth/copilot_accounts.py)
is registered by the proxy and the dashboard exposes a separate account-setup
preview under AI Engines. Copilot remains unavailable for chat; this does not
register an execution engine or qualify model entitlement.

| Method and path | Behavior |
| --- | --- |
| `GET /v1/copilot/accounts` | Masked current-user accounts, including disabled/expired accounts. |
| `POST /v1/copilot/accounts` | Validate the supplied user token, then create a personal account with sharing off. Duplicate identities require an explicit replacement. |
| `POST /v1/copilot/accounts/{id}/reconnect` | Check ownership and revision before external validation, verify the same stable GitHub identity, then atomically replace that exact revision. Preserve label and selection/status flags. |
| `PATCH /v1/copilot/accounts/{id}` | Owner-only label, personal-use, sharing and enabled/disabled controls. Sharing-on requires a current administrator. |
| `DELETE /v1/copilot/accounts/{id}` | Disconnect the owner's exact account; never delete another engine's record. |

The route accepts human user authentication, not agent session tokens or API
keys, even when they resolve to an administrator's identity. Generic subscription
mutation routes reject Copilot records by their stored layer, so a different URL
layer cannot bypass these checks. Existing engines keep their current routes.

Database writes serialize on the owner and selected account. A stale reconnect,
changed GitHub identity, duplicate connect, or target deleted during validation
fails without creating a replacement account. Login renames do not change the
stable GitHub ID. Disconnect removes OtoDock's saved credential; it does not
revoke the token at GitHub. Existing account observers detect removal, disabling,
scope changes or credential replacement within their documented observation
bounds and close the affected runtimes.

No token or ciphertext appears in account responses. Request tokens use secret
fields, and malformed-body validation returns fixed errors without retaining the
raw request in exception context. Provider/storage failures are sanitized. The
dashboard clears token input before requests, does not put token requests into
React Query's mutation cache, and projects only public account fields into a
query keyed by the current user. It does not read error bodies. Installation
accounts already provisioned internally can be displayed, disabled and removed;
this user-token connection flow cannot replace their credentials.

The setup preview is an intermediate feature. OAuth sign-in/application setup,
automatic refresh, installation-token issuance, Copilot model-access validation,
complete engine registration, policy enforcement and remote execution remain
open. A saved token does not make an engine runnable or satisfy onboarding's
existing usable-engine requirement. No deployment is included in this change.

## Verification

All **41 offline identity tests** pass, covering the request boundary, user-token
filter, redirects, 401/403/429/5xx, identity shape, enterprise logins, bounded
response handling, expiration parsing, exception redaction, timeout and
cancellation cleanup. Ruff passes.

One read-only live `/user` request used the existing explicitly selected GitHub
CLI token, captured in memory by the development harness. It succeeded and
produced a numeric stable principal and valid login. No authoritative expiration
header was available, so expiry remained unknown. Only the following booleans
were exported:

```json
{
  "read_only_user_validation_passed": true,
  "stable_numeric_principal": true,
  "login_validated": true,
  "token_absent_from_repr": true,
  "authoritative_expiry_available": false,
  "copilot_entitlement_tested": false,
  "oauth_refresh_tested": false
}
```

No account identifiers, login, token, raw headers, or response body were recorded.
No inference, app installation, OAuth application creation, credential refresh,
or deployment change occurred.
