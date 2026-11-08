# Telegram connector instructions

Optional. Not selected by any default role.

## What the Telegram package supplies

- Skill `telegram-owner-communication`: `skills/telegram-owner-communication/SKILL.md` in this
  directory, unchanged from the former default skill (published name
  `qorqut-role-telegram-owner-communication`). Only Ordubashy selected it.
- Brief section: `BRIEF-SECTION.md`, the paragraph that used to sit in Ordubashy's default brief.
  Append it to the brief where the package composes one, keeping the 10,000-character limit.
- Role grants for Ordubashy only, each `allow`: `telegram_send`, `telegram_history`,
  `telegram_delivery_get` and `telegram_send_file`. No other role held them.

The skill grants no tool. The host must register the four operations before a role manifest may
grant them; an unregistered operation in `default_tool_policy` fails validation, which is why the
default `role.yaml` no longer lists them.

## Contract summary (former README text, unchanged)

Telegram: Ordubashy grants `telegram_send`, `telegram_history`, `telegram_delivery_get` and
`telegram_send_file`; no other role holds any of them. The tool sends one authorized file to
the owner's own Telegram topic as a downloadable `document` or a displayed `photo` (static
JPEG or PNG). The shared `skills/telegram-owner-communication/` skill documents the closed
contract: `source` is a workspace file (relative path under `exports/` or `downloads/`) or a
stored document with an explicit `business_id` (`null` for Global), `expected_sha256` is
required, the framework `idempotency_key` makes retries safe, `caption` is plain text of at
most 1024 characters and 1024 UTF-16 code units, and `reply_to` may name a message in the
agent's own topic; there is no chat, topic, token, URL, bytes or file-id argument. Receiving
media is unchanged: an owner attachment still reaches the agent as its caption plus an
undelivered-attachment marker. The host allows only the currently configured, active Global
Ordubashy bound to the owner's topic to call it (a role name or an added permission on
another agent is not enough) and enforces every source, size, format and delivery gate; the
skill grants nothing.
