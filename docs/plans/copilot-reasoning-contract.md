# Copilot conversation reasoning effort

New local Copilot conversations can select an advertised reasoning effort after
loading the selected account's model catalog. **Model default** omits the SDK
option. It does not pin the provider's currently advertised default, which may
change between resumes. An explicit selection is fixed for the conversation.

## Public contract

Create accepts optional `reasoning_effort`: `null`, `low`, `medium`, `high`,
`xhigh` or `max`. Omission and null mean Model default. Unknown values and wrong
types return 422 before storage or runtime startup. Model inventory rows add
`reasoning_efforts` and nullable `default_reasoning_effort`; only advertised,
reviewed levels become selectable. Model policy still controls availability.

The raw inventory accepts at most 32 unique printable effort strings, each at
most 256 characters. Capability flags must be exact booleans. Nonempty effort
metadata without the reasoning capability, malformed fields and defaults absent
from the advertised list reject the inventory. Optional raw fields may be
omitted; explicit null is invalid under the pinned runtime schema. Unknown
future levels are filtered from the public list, and an unknown advertised
default becomes null. The SDK 1.0.13 raw inventory adapter is shared by catalog
discovery and session startup to avoid lossy public SDK conversions.

The dashboard offers Model default first and does not automatically select the
vendor default. Model/account/agent/catalog changes reset the choice. Saved and
active conversations display their immutable effort. Resume accepts only the
existing revision field, so an effort override returns 422. Reading history
does not start a runtime or refresh model metadata.

## Persistence and native runtime

An additive nullable PostgreSQL column stores the selection with a constraint
on the five reviewed levels. Old rows remain null, with their transcripts and
revisions unchanged. Store updates cannot mutate the choice. Cold resume reads
the original selection from owned conversation metadata.

Before native create or resume, the runtime inventory must still advertise the
explicit selection for the original model. Unsupported selections fail without
falling back to a different effort. The reviewed SDK option is supplied only
for explicit selections and is included in the private profile digest. Changing
or removing it cannot resume that profile. Omitted effort retains the exact
legacy digest input and SDK behavior, preserving older default histories.

The migration is idempotent and does not rewrite old rows. Older application
code cannot resume a new explicit-effort profile because its digest differs;
rolling back the application does not provide explicit-effort resume support.

## Qualification boundary

See the [results](copilot-reasoning-results.md) and [parity matrix](copilot-parity.md).
This is per-conversation effort selection for the personal-account local chat
preview. Persisted user/agent defaults, mid-conversation switching, general
engine registration, shared payers, usage accounting, attachments, queues,
MCP/delegation, remote/terminal workflows and organization automation remain
separate work. No running deployment is changed.
