# Copilot observed usage contract

The local personal-account chat preview captures provider usage independently
of assistant turn streaming. Live and saved conversations show **Reported usage**
for the conversation: input, output, cache-read, cache-write and reasoning token
counts, plus reported nano-AIU where available. Missing values stay unknown;
reported zero remains zero. These are observed reports, which may be partial.

## Source and units

The pinned SDK 1.0.13/runtime 1.0.83 emits ephemeral `assistant.usage` events
describing model calls. Only the reported model is required. Usage has no
guaranteed user-turn identifier or arrival order and is not replayed from native
history on cold resume. Native event UUIDs identify reports; optional API,
provider and service request IDs are not public attribution keys.

The [official SDK usage guide](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing)
and pinned runtime schema distinguish the following:

- `assistant.usage.cost` is an experimental premium-request multiplier, not
  dollars or billed AI credits. It is omitted from this adapter.
- `copilotUsage.totalNanoAiu` supplies native reported nano-AIU. The adapter
  exposes this unit without converting it to credits or money.
- Reasoning tokens are included in output tokens; the display does not add
  them again. Cache counts are presented separately without assuming how a
  provider includes them in input counts.
- `session.usage_info` describes current context occupancy. Durable
  `session.usage_checkpoint` counters are cumulative. Neither is added to the
  per-call reports.

## Public report

History events and active SSE streams may include this exact payload (history
adds its existing `seq` framing):

```json
{
  "type": "usage",
  "event_id": "8ea9bb37-d358-47c3-8af6-dc447a4d2858",
  "reported_model": "example-model",
  "input_tokens": 100,
  "output_tokens": 12,
  "cache_read_tokens": 0,
  "cache_write_tokens": null,
  "reasoning_tokens": 3,
  "reported_nano_aiu": null
}
```

The source event ID must be a canonical UUID. The model is a nonempty trimmed
printable string of at most 256 characters. Token metrics are nullable exact
integers from zero through JavaScript's maximum safe integer. Native nano-AIU
is nullable, finite, nonnegative and bounded by the same maximum. Booleans,
numeric strings, fractional token counts and additional public fields fail
validation. The SDK serializer preserves integer token types. Provider prose,
quota snapshots, credentials and opaque request IDs are not projected.

The reported model remains distinct from the conversation's selected model.
Original owner, account, agent and native platform session attribution comes
from immutable conversation metadata; it is never accepted from the provider
payload. The active writer generation is checked on every storage operation.

## Ownership and persistence

A synchronous bounded observer is installed before native create/resume and
remains attached through runtime shutdown. Usage bypasses the ordinary turn
queue and completion coordinator: a late report cannot manufacture another
DONE or become the next user's turn usage. Child-agent reports are excluded
from this native-only profile.

The observer keeps at most 1,000 event UUID tombstones. An identical repeat is
ignored; conflicting content for an existing ID fails the owner. The service
owns a 128-frame writer queue and commits reports before optional SSE delivery.
It drains queued reports before turn completion and, after native shutdown,
seals and joins the writer before final conversation closure. Idle and shutdown
reports remain readable even when no SSE consumer exists. Validation, overflow
and persistence failures close and quarantine the conversation; unresolved
cleanup or writes retain the existing ownership protections.

`append_usage` stores reports in the existing conversation-events table while
the conversation is open, either active or idle. A row lock, owner and generation
checks serialize writers. A report UUID already stored with identical content
does not add another event or change the revision, including after cold resume;
conflicting content fails. Usage preserves turn-completion flags and shares the
existing 1,000-event/1 MiB conversation limits. No schema migration is required.

Deduplication identifies observed event UUIDs, not distinct invoiced requests.
Missing events, process crashes, provider omissions and reports lost before a
durable write cannot be reconstructed from ephemeral native usage. Earlier
conversations have no retroactive usage backfill.

## Dashboard and remaining scope

The summary is separate from assistant turn content. Live delivery and saved
history merge by report UUID. Post-turn/close history refresh incorporates late
reports under the current conversation and navigation guards. New conversation,
user or agent scope clears the summary. Conflicting or malformed reports make
usage unavailable instead of presenting a misleading total. Each metric sums
only known values from unique reports; missing values remain visible, partial
metrics are labelled, and totals beyond the supported range are unavailable.

This does not implement general billing records, dollar budgets, quota/credit
enforcement, context gauges or organization-wide usage reports. Those require
additional qualification, alongside the remaining [parity work](copilot-parity.md).
See the [results](copilot-usage-results.md). No deployment is changed.
