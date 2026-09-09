# Frostyard builds and release boundaries

The Frostyard fork is experimental. Copilot parity is not implemented yet. No
Frostyard binary release channel is enabled, and these repository changes do not
modify existing installations or the service on selfie.

## Build the fork from source

Use an isolated development host or checkout, not an existing production data
directory. Follow the normal host prerequisites in the upstream installation
documentation. From this checkout, create `config.env` with a generated
`POSTGRES_PASSWORD` if it does not already exist, then run:

```bash
scripts/compose.sh up -d --build
```

The wrapper combines the source overlays with the base manifests. Proxy, init,
file-tools, and optional phone images use `ghcr.io/frostyard/*:source` tags and
`pull_policy: build`; they are built locally and do not pull or overwrite the
upstream platform image tags. Set `OTODOCK_PHONE=0` to omit phone. The phone source
override must come **after** `docker-compose.phone.yml` in a manually assembled
Compose command. The wrapper does this automatically.

The base `docker-compose.yml` and `docker-compose.phone.yml` still describe
**upstream binary installs**, preserving the upstream baseline for merges. Using
those files alone pulls upstream OtoDock and does not install this fork. The
fork's `scripts/install.sh` exits before any download, write, or host setup;
its inherited upstream implementation is retained below that guard for future
reconciliation. The upstream quick-start command in README explicitly remains
an upstream installation command.

## Publishing remains opt-in

`release-images.yml` runs only in `frostyard/oto-dock` and only when the repository
variable `FROSTYARD_PUBLISH_IMAGES` equals `true`. Do not enable it merely to run
CI. Publishing is not necessary for source development.

When deliberately enabled, a tag push or manual dispatch builds amd64 images
in `ghcr.io/frostyard`. The image tag is
`<OTODOCK_VERSION>-frostyard.<12-character-commit-SHA>`. There is no `latest` tag,
and the workflow has no upstream package destination. First publication also
requires checking package visibility and repository linkage as described in the
workflow. A commit-specific tag identifies source but is not an immutable digest;
release qualification must record image digests and test reproducibility.

Before distributing binaries, implement and test dedicated Frostyard pull
manifests, a fork installer, explicit version/update-channel selection, artifact
provenance, migration and rollback. The base upstream manifests cannot consume
Frostyard images by changing `OTODOCK_VERSION` alone: their registry namespace
also differs. These remain C9 release gates, not shipped capabilities.

## Update and dependency audit

| Surface | Current source and fork treatment |
| --- | --- |
| Platform binary installation | Upstream pull manifests retained and labeled; fork installer disabled. There is no enabled Frostyard automatic platform update feed. |
| Satellite installation and updates | The connected proxy builds a tarball from its bundled satellite source and sends it over the existing transport with a SHA-256 digest. It does not fetch the satellite package from upstream GitHub. A satellite paired to an upstream proxy still receives upstream code; use separate test machines when evaluating the fork. See `proxy/api/remote/remote_machines.py` and `satellite/transport/lifecycle_update.py`. |
| Community agents | `OtoDock/community-agents` registry and archives remain intentional upstream dependencies. |
| Community MCPs and skills | `OtoDock/community-mcps` and `OtoDock/community-skills` remain intentional upstream catalog and install/update dependencies. Community manifests may reference upstream container images. These catalogs are distinct from the platform release channel. |
| Runtime tools and base images | Existing vendor downloads and pins in `VERSIONS.md` remain unchanged. They include Claude Code, Codex, language runtimes, sandbox tools, and third-party container images. Copilot provisioning awaits C1 evidence. |
| Documentation and hosted services | Existing `otodock.io` documentation, product, and service links remain upstream references; this change does not establish Frostyard-operated equivalents. |

This audit separates platform artifacts from intentional external dependencies;
it does not claim the fork is independent of upstream services or catalogs.
