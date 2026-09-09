# Triggers — Webhook Automation

Triggers are HTTP webhooks fired by external systems (GitHub, Stripe, Linear, IoT devices, Zapier, etc.). Each trigger optionally fires a task, a notification, or both. They're the bridge between "something happened in another system" and "do something on this platform". The full call shapes, vendor-subscription workflow, payload placeholders, external-system setup and worked examples are in the skill `triggers-guide` — load it with the Skill tool before creating or editing a trigger.

## When to use which tool

| User says | Tool to use |
|---|---|
| "Notify me when my PR is merged" | `create_trigger` (scope=user, notify-only) |
| "When the deploy webhook fires, run my code-review task" | `create_trigger` (scope=user, with task_id) |
| "Set up an alert for when the production server goes down" | `create_trigger` (scope=agent, manager-only) |
| "Show me my triggers" / "What webhooks do I have?" | `list_triggers` |
| "Pause / resume / delete trigger X" | `pause_trigger` / `resume_trigger` / `delete_trigger` |
| "Change the title of trigger X" / "Change which task X runs" | `edit_trigger` |
| "Test trigger X" | `fire_trigger` (sends a test payload, no real webhook needed) |

## Rules

- **Two scopes**: `scope='user'` (default) = personal automations, only the creator (and admin) can edit/delete, notifications go to the creator. `scope='agent'` (manager+ only) = business events affecting the whole team, notifications can broadcast to all agent users. Personal request (their PR, their ticket) → user; business automation that must outlive them → agent, and only if you can confirm they have manager role on this agent.
- **Trigger ↔ task**: a real LLM task on webhook = a task with `task_type='trigger'` (no schedule, no run_at) plus `create_trigger(task_id=...)`. Trigger and task MUST match on scope, agent and creator. `{{placeholder}}` tokens in the task prompt and notify title/body are substituted from the webhook body's top-level keys.
- **Trigger ↔ notification**: lightweight alerts that need no LLM use the inline `notify` block on the trigger — much cheaper than a task. `task_id` and `notify` can be combined.
- **Vendor-subscribed triggers (OAuth)**: the user subscribes to events in the dashboard; `list_subscriptions()` shows `events=<…>` — those are the ONLY valid `event_type` values. Copy the exact string; never invent variants (`"comment"`, `"comment.create"`) — a wrong `event_type` is rejected. `subject.type` is the per-event action (`create`, `opened`), not the resource. Vendor delivery is async: change the filter once, post one test action, wait ~10–15 s, check `fired_count` with `get_trigger` — never rapid-fire tests while flipping the filter.
- **After `create_trigger` succeeds**, explain the setup to the user: the full webhook URL is `https://<your-platform-host><webhook_path>`; they mint an API key in the dashboard (user: User Settings → API Keys, tick `triggers`; agent: Agent Settings → API Keys — shown ONCE), configure the external system to POST JSON with `Authorization: Bearer otok_<key>`, and test with `fire_trigger(id, body={...})` from the chat.
- **`debounce_seconds`** coalesces a chatty source into one fire per window (e.g. 60 for GitHub pushes).
- `pause_trigger` makes the webhook 404 until resumed; `delete_trigger` is permanent (the external system must be reconfigured to bring it back); static triggers cannot be deleted.
- **Security**: webhook fires authenticate with their own scoped API key, shown once — if lost or leaked, mint a new one and revoke the old (revocation is immediate). User-scoped triggers can only notify the creator, never other users.
