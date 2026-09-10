# Copilot local provisioning and configuration contract

This opt-in local preview supplies a repeatable setup and service lifetime for
the previously qualified execution layer. It does not register Copilot, expose
a chat route, or change a running deployment. Linux x86_64 with glibc is the
only admitted provisioning target; SDK 1.0.13 and runtime 1.0.83 remain pinned.

## Installation

Install the normal proxy dependencies first, then the optional supplement in
that same interpreter:

```bash
python -m pip install --no-deps --require-hashes -r proxy/requirements-copilot.txt
python -m pip check
```

The supplement adds the SDK, python-dateutil and six, preserving the proxy's
pydantic/httpx pins. Probe-only psutil is not a production dependency. Neither
this install nor opening the provisioned layer downloads native runtime assets.

Obtain the exact [official release archive](https://github.com/github/copilot-cli/releases/download/v1.0.83/github-copilot-1.0.83-linux-x64.tgz)
separately. Its SHA256 is
`888f8fbb4575c335afba4a8863c647ef04f81e5124c7c794bdcaee90c5fa4503`,
verified against the official release digest and
[SHA256SUMS.txt](https://github.com/github/copilot-cli/releases/download/v1.0.83/SHA256SUMS.txt).
The installer checks this source-pinned digest before extraction, then checks
all 30 retained hostless assets against their individual source-pinned hashes,
sizes and modes. It preserves the wrapper, runtime.node, search helpers and
adjacent assets from the already qualified SDK layout; it excludes the full
native CLI and the SDK's generated cache marker.

Run setup as the proxy service user. Choose an existing private parent directory
(owned by that user, mode 0700) outside all workspaces and sandbox mounts. The
installation directory itself must be absent or already fully valid. For example,
with an administrator-prepared `/srv/otodock-copilot-private` and downloaded
archive at `/srv/downloads/github-copilot-1.0.83-linux-x64.tgz`:

```bash
python scripts/copilot/provision_local.py initialize \
  --root /srv/otodock-copilot-private/local \
  --archive /srv/downloads/github-copilot-1.0.83-linux-x64.tgz \
  --forbid-root /srv/otodock-data/agents \
  --forbid-root /srv/otodock-config
python scripts/copilot/provision_local.py check \
  --root /srv/otodock-copilot-private/local \
  --forbid-root /srv/otodock-data/agents \
  --forbid-root /srv/otodock-config
```

Use absolute paths with no symlink components; supply every additional sandbox
mount root with `--forbid-root`. The example paths are placeholders for the
installation's actual paths. `check` reads the bundle and SDK metadata; it does
not prove sandbox prerequisites, account entitlement, model access or inference.
The underlying `initialize`/`load` functions are standard-library-only and can
stage assets before the optional SDK is installed; the CLI also checks the SDK.

Initialization builds a private temporary sibling, bounds archive size/member
count/expanded size, rejects unsafe members, and atomically publishes with
Linux `RENAME_NOREPLACE`. Existing partial directories, wrong modes, symlinks,
hard links, unexpected runtime entries and changed assets fail validation.
No existing user directory is chmodded, overwritten, repaired or removed.
Concurrent initializers cannot replace one another; a losing publication can
be retried after the winner finishes. Reinitializing a valid installation is a
read-only check and does not require retaining the input archive.

The root contains separate `records`, `state`, `homes` and `runtime` directories.
Loading verifies their roots and all runtime assets without traversing session
contents. Individual records/histories/homes retain their own existing checks
when opened. Private root paths must remain under exclusive host control;
verification is not a defense against a malicious proxy service user changing
files after validation. There is no automatic history repair, garbage collection,
upgrade, rollback or stale staging cleanup after a host crash.

## Authenticated configuration

`build_copilot_agent_config` accepts the `UserContext` produced by existing human
cookie authentication, an agent slug, explicit account ID/scope, model, native
tool allowlist and supported permission mode. Passing a fabricated Python object
is not authentication; the future route must supply its authenticated context.
API keys, agent/session JWTs, external identities and unsupported profiles fail.

It re-reads the current user, per-agent role, agent visibility and knowledge
attachments from storage, then checks them again before returning. A removed
user/access assignment, admin-only mismatch or changed preparation snapshot
fails closed. A personal payer must belong to the driving user. Explicit platform
borrowing requires that user's current `allow_platform_auth` toggle and the
account store's contribution/current-admin-owner eligibility. There is no
fallback payer. Credential reads verify eligibility but no token or credential
revision enters the returned config; runtime startup acquires a fresh watched
lease. Account eligibility can change after config creation, and configuration
checks are not a general mechanism for revoking a running user's authorization.

The security context preserves the human identity independently of mount scope,
including shared-only agent workspaces and attached knowledge policy. Model and
tools are explicit; runtime preflight remains responsible for model availability.
The builder intentionally selects this local profile and does not consult remote
routing or fall back from an unavailable target.

Instructions include `config/agent.md` (legacy `prompt.md` fallback), recursive
`config/context` Markdown/text documents, mounted personal `context/*.md`, native
tool availability and the existing permission context. Reads use directory file
descriptors, reject links/special files, and enforce a 256 KiB aggregate source
budget plus tree depth/entry limits. Unsafe sources fail instead of silently
omitting instructions. MCP configs, credentials, automatic skills, delegation
and generic prompt generation are not materialized by this builder.

## Service lifetime

`open_provisioned_layer(root, forbidden_roots=...)` is an async context manager.
It verifies the bundle outside the event loop, checks SDK metadata, rejects
known platform mount roots plus caller exclusions, and constructs the layer with
its private record/history/home roots. Each actual session still verifies its
complete generated sandbox mounts before runtime startup.

Context exit calls `layer.aclose()`, which immediately seals that instance's
admission and closes every captured session generation concurrently. New starts
and existing-session turns fail once closing begins. Repeated caller cancellation
still joins cleanup before propagating. Failure in one owner does not prevent
cleanup of others; failed owners retain inactive resource claims and the layer
stays sealed. Other layer instances retain their own sessions. Scratch file
descriptors close after cleanup settles without deleting history or claims.

This provides explicit application-lifetime ownership, not an idle eviction
policy. Generic dashboard queued messages/attachments, authorized resume
selection, engine registration, MCP/delegation, remote execution and terminal
workflows remain separate qualification gates.
