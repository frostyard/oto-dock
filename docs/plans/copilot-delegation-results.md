# Copilot fixed host delegation tool qualification

The pinned SDK 1.0.13/runtime 1.0.83 executed the guarded `oto_delegate` through a real `SandboxBuilder` runtime and `CopilotLocalSession`. The model received a controlled worker's report; a new runtime resumed the private history and recalled that report. A changed delegation roster was rejected before another runtime started. Separate model turns proved that a held approval could be denied without dispatch and that hard-closing a held worker joined its callback. These observations qualify the fixed host-tool boundary, not a real scheduler child or end-to-end CTO/repository/QA workflow.

## Recorded observations

| Case | Observation | Evidence |
| --- | --- | --- |
| Successful model dispatch | One native pre-tool hook, one Oto authority decision, one controlled worker invocation; parent response contained the worker's private fixture marker. | [History run](evidence/copilot-host-tool-history-rpc-limit.json) |
| Cold resume | A fresh runtime and credential revision resumed the same privately locked history and recalled the worker result. Different target roster rejected before runtime launch. | [History run](evidence/copilot-host-tool-history-rpc-limit.json) |
| Held denial | One native hook and one held authority decision; zero worker calls. The owner then stopped the controlled turn explicitly. | [Controls run](evidence/copilot-host-tool-controls.json) |
| Held worker shutdown | One authorized worker started; hard-close cancelled and joined it, drained the callback registry, and stopped the runtime. | [Controls run](evidence/copilot-host-tool-controls.json) |

Native permission callbacks were not invoked on these observed paths: the explicit pre-tool hook admitted the known host tool, and its handler performed the actual Oto authorization. Thus the success path prompted the controlled authority once. Malformed permission requests and managed/bypass/skip flags are covered by deterministic tests; these runs do not independently qualify every native enterprise-policy path.

All three recorded runs stopped their runtimes normally, without forced cleanup, and closed their credential observers. Across them, six model turns used five sequential runtime instances. Each invocation had a 180-second outer deadline, 45-second turn deadlines, and the factory's 30-credit native session limit. Only the controls run is labeled globally passed; the earlier reports retain their original failed outcomes.

## Separate scheduler qualification

The live probe's worker is an inert Python callback returning a private marker or waiting for cancellation. It never invokes the scheduler and therefore does not demonstrate a real Claude/Codex repository or QA worker.

The separate [owned worker integration tests](../../proxy/tests/tasks/test_copilot_worker.py) exercise the real scheduler with controlled execution layers and storage fixtures. They cover target authorization, scope/configuration selection, admission, publication, cancellation, retained cleanup ownership, and engine factory startup capture without inference. Those deterministic checks qualify composition and failure paths; they are not live mixed-provider credential or end-to-end multi-repository evidence. Consolidated test and CI results are recorded separately below when available.

## Negative observations and limits

The [initial run](evidence/copilot-host-tool-initial-retry.json) successfully dispatched the tool, then the model retried the denied tool five times despite instructions not to retry. No denied call dispatched a worker, but the turn reached its deadline. Model compliance with a no-retry instruction is not an execution boundary. The final controls phase explicitly closes the owner after establishing denial.

The history run completed its success and resume assertions, then attempted to use `session.rpc.tools.execute` for deterministic negative cases. That RPC reached the pre-tool hook but did not reach the controlled host authority, so the fixture timed out. Its subsequent cleanup was normal. This RPC is not qualified as an alternate route to the guarded model-originated host callback. The executable now separates `history` and `controls` phases and uses real model turns for both.

The authority decisions, account repository, and worker were controlled fixtures. Actual context registration, exact owner checks, credential leases, SDK callbacks, private profile/history, and sandbox lifecycle remained active. No child model was started, no scheduler worker was created, and no PostgreSQL, dashboard approval, cross-agent access or task-result delivery claim follows from this probe. The report contains counts and booleans, with no token, account identity, native session ID, prompt, worker result, or marker.

## Reproduction

Run each phase explicitly with the proxy environment plus the pinned optional SDK dependencies and a previously verified local provisioned runtime. Each phase permits at most two model turns and two runtime launches:

```sh
python scripts/copilot/delegation_tool_probe.py \
  --provisioned-root /path/to/private/install \
  --phase history --live --use-gh-token --output /tmp/copilot-host-history.json
python scripts/copilot/delegation_tool_probe.py \
  --provisioned-root /path/to/private/install \
  --phase controls --live --use-gh-token --output /tmp/copilot-host-controls.json
```

The script selects the existing GitHub token in memory and creates disposable platform configuration, workspaces, and private records. It neither edits the provisioned installation nor uses a running deployment. The checked-in history evidence comes from the earlier combined run, whose successful history assertions and later RPC limitation are preserved above; the separated history phase was not rerun solely to relabel that evidence.


## Local validation

- 1,320 offline Copilot tests plus 153 subtests passed.
- 994 dashboard tests across 131 files, TypeScript and production build passed.
- 183 service, real permission-authority and owned-lane regression tests passed.
- 32 real-scheduler/controlled-engine worker tests passed, including queued and
  active manual cancellation, ongoing revocation, failed startup, late builder
  cleanup, subscription ownership, and native constructor capture.
- 287 API/configuration tests and 77 storage validation cases passed during
  implementation; an additional legacy prompt/profile regression also passed.
- Ruff and diff checks passed. PostgreSQL transaction/schema/concurrency tests
  are included in the full CI gate; no local PostgreSQL was available.

Full CI status is recorded in the pull request and tracking issue for its exact
head commit. These tests do not change the live qualification limits above.


The initial CI run stopped at parallel pytest collection because one new route
parameter included a random session UUID. Both HTTP fixture IDs are now stable;
all 176 API tests passed with four local xdist workers after the fix. Production
code was unaffected by this collection failure.
