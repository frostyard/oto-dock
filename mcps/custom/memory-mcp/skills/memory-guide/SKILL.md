---
name: memory-guide
description: Worked examples for the memory tool — create a topic, revise a fact in place, append, delete, view a topic that was not inlined, and the exact command shapes. Load when unsure how to write or edit a memory topic file.
---

# Memory tool — worked examples

The `memory-usage` card in your instructions carries the rules (scopes,
what not to save, revise instead of duplicate). These are the command
shapes behind them. Memory is markdown topic files: `/memories/agent/`
(shared with every user of this agent) and `/memories/user/` (private to
the current user). A generated `MEMORY.md` index per scope is maintained by
the platform — never edit it; edit topic files and the index follows.

## Save a new fact (create a topic)

```
memory(
  command="create",
  path="/memories/user/preferences.md",
  file_text="# Communication preferences\n- Prefers replies in Greek (2026-06-12)\n- Wants metric units everywhere (2026-06-12)\n"
)
```

Start every topic file with a one-line `# heading` — it becomes the topic's
index entry. Date each fact `(YYYY-MM-DD)` so staleness stays visible.
`create` errors if the file exists — that's your cue the topic already
exists: UPDATE it instead of duplicating.

## Update an existing fact (revise in place)

```
memory(
  command="str_replace",
  path="/memories/user/preferences.md",
  old_str="- Wants metric units everywhere (2026-06-12)",
  new_str="- Switched to imperial units (2026-07-02; was metric until then)"
)
```

`old_str` must match exactly once. Supersede outdated facts with a short
"was X until DATE" trail instead of silently erasing them — it keeps the
history readable for you and the user. Prefer targeted `str_replace` /
`insert` edits; never rebuild a whole topic from scratch when a small edit
will do.

## Append to a topic

```
memory(
  command="insert",
  path="/memories/agent/post-history.md",
  insert_line=2,
  insert_text="- Posted launch teaser to company Instagram (2026-06-12)"
)
```

Text is inserted AFTER the given line (`insert_line=0` = top of file). Use
`view` first if unsure of the layout.

## Remove a wrong or dead memory

```
memory(command="delete", path="/memories/user/old-project.md")
```

Delete topics that are wrong or no longer matter — stale memories are worse
than no memories. Everything is git-versioned platform-side, so deletion is
recoverable by humans.

## Read a topic that wasn't inlined

When a scope outgrows the inline budget, your prompt carries only its index.
Fetch a topic on demand:

```
memory(command="view", path="/memories/agent/infrastructure.md")
```

`view` on a directory lists it: `memory(command="view", path="/memories/agent")`.
