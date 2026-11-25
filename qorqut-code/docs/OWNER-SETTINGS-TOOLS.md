# Settings for Ordubashy

The owner's Settings page has one owner-only dashboard route per part, and each of those refuses every agent. Two tools give
the configured Ordubashy the same reads and changes, so the owner can ask Ordubashy to change a setting instead of opening
Settings: `settings_get` and `settings_set` (`app/qorqut/domains/settings_tools.py`).

How Ordubashy uses them is his role skill `owner-settings` (`qorqut-role-owner-settings`, Ordubashy role 1.86.0). Like every
role skill it is a native skill in his folder: the client lists its name and one-line description at every start and loads
the body only when he opens it. Nothing else about settings is in his start-up instructions; the tool descriptions list the
settings and their values and point to the skill.

## Who may call them

Only the configured Ordubashy, in person, at Global. The policy denies both tools to every other role before any role file
or override is read (`owner_settings_ordubashy_only`), and the handler proves the caller against the installation record on
every call; a delegated identity is refused. An explicit deny on Ordubashy is respected.

## What they reach

`settings_get` returns the page as it is now, by section: `connections` (extensions and their status, the agent browser,
websites signed in), `agents` (how agents run, each agent's client, model and effort, Claude Code and Codex, folder trust,
Testing mode), `display` (what Ordubashy last set for theme, text size and alerts) and `about` (version, folders, the Qorqbot
account). A part that cannot be read says `unavailable` without hiding the others. No key, password or token is returned.

`settings_set` changes one setting. Each calls the function behind the owner's own route, with the installation owner's
identity, so the same checks and refusals apply:

| Setting | Owner route it mirrors |
| --- | --- |
| `extension_key` | `/root/extensions/<id>/key` (OpenRouter, treg) |
| `agent_options` | `/root/agents/<id>/chat/options` |
| `agent_client` | `/root/agent-clients` |
| `agent_run_mode` | `/root/agents/runtime` |
| `agent_folder_trust` | `/root/agent-trust` |
| `testing_mode` | `/root/testing-mode` |
| `website_sign_out` | `/root/browser-accounts` |
| `agent_browser` | `/root/browser-setup` (retry) |
| `timezone` | `/root/operational-timezone` |
| `appearance`, `notifications` | the browser's own choice (see below) |
| `account` | `/root/account/{start,verify,logout}` |

The change runs as the operation's external step, outside the write transaction, because several of these functions take
their own locks or ask a provider; the receipt is written afterwards. A replay with the same idempotency key returns the
receipt without running the change again.

Environment variables keep their own tools (`environment_list`, `environment_get`, `environment_set`).

## Keys

A key never travels through these tools. `extension_key` names the Global Settings (Environment) variable that holds the
key, and the service reads it there and hands it to the extension's own `change_key`, which checks it with the provider before
saving. Arguments, plans, receipts and results hold the variable's name only. A key the owner gives Ordubashy is saved with
`environment_set` first.

## Theme, text size and alerts

These live in each browser (`js/appearance.js`, `js/desktop-notify.js`). `settings_set` stores the request in the owner's
preference folder (`dashboard_preferences.display`): every field with its own revision. `GET /root/display-preferences`
serves it, read only. Each browser (`js/display-sync.js`) applies a field once, when it first sees that revision, and from
then on the value is that browser's own choice: a later change in Settings stays until Ordubashy sets the field again. A
browser never asks for notification permission by itself; an alert turned on before the owner allowed notifications there
stays off, and the page says so.

Every `settings_set` bumps the `owner_settings` change kind, so open dashboards apply display choices, reread Testing mode
and the voice input status at once.
