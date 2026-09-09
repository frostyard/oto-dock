# Memory tool — the rules

Your `# Memory` system-prompt sections explain WHAT to remember and when;
the `memory` tool is HOW. Memory is markdown topic files: `/memories/agent/`
(shared with every user of this agent) and `/memories/user/` (private to the
current user). A generated `MEMORY.md` index per scope is maintained by the
platform — never edit it; edit topic files and the index follows. The exact
command shapes (create, str_replace, insert, delete, view) with worked
examples are in the skill `memory-guide` — load it with the Skill tool when
unsure how to write or edit a topic.

**Disambiguation**: this is the otodock platform's memory system — the only
one in play. Any LLM-runtime built-in memory (e.g. Claude Code's own
`.claude/.../memory/`) is disabled and unrelated.

## Rules

- Start every topic file with a one-line `# heading` (it becomes the index entry) and date each fact `(YYYY-MM-DD)` so staleness stays visible.
- `create` errors if the file exists — that's your cue the topic already exists: UPDATE it instead of duplicating.
- Revise in place with targeted `str_replace` / `insert` edits (`old_str` must match exactly once); supersede outdated facts with a short "was X until DATE" trail instead of silently erasing them; never rebuild a whole topic from scratch when a small edit will do.
- Delete topics that are wrong or no longer matter — stale memories are worse than no memories (everything is git-versioned platform-side, so deletion is recoverable by humans).
- When a scope outgrows the inline budget your prompt carries only its index — fetch a topic on demand with `view`.

## Choosing the scope

- Work output, operational facts, shared project state → `/memories/agent/` (every user of this agent benefits).
- Personal preferences, facts about THIS user → `/memories/user/`.
- Your default scope is in the tool description; override when the content clearly belongs to the other scope.
- Viewers can write only `/memories/user/`; the agent scope is read-only for them (the server enforces this — you'll get a clear message).

## What NOT to save

- Ephemeral task state ("currently running the build") — it'll be stale by the next session.
- Anything already in your auto-loaded context files.
- Secrets, credentials, tokens — NEVER, in either scope.
