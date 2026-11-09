# Internal agent messaging

Direct messages are part of Qorqut core and its agent inbox, not a role, plugin,
context document or task. All roles, including custom roles, receive the tools
and mandatory usage instructions. Explicit owner tool overrides remain effective.

## Tools and delivery

- `agents_message_recipients`: discover active agents in the selected scope.
- `agents_message_send`: supply `to_agent_id` and `message`, or `reply_to` and
  `message`. Use a stable `idempotency_key` for retries of the same send.
- `agents_messages_list`: read your sent and received history. Optional `thread_id`,
  `counterpart_id`, `after` and `limit` select a thread, one current counterpart or a
  page. Follow `next_cursor`; default page size is 20, maximum 100.

### Owner-directed code sessions

An owner-directed session may use these same three operations through the
`/mcp/owner` route, but only in the credential's bound business. The sender is
the installation owner; the session is not an agent, does not receive an inbox
or watcher, and cannot use Global messaging or `all_businesses=true`. Bootstrap
identifies the configured Ordubashy when that agent is active in the business.
Object-attached owner conversations remain a separate private channel; direct
messages are for objectless coordination and replies.

Message bodies sent through these operations contain 1–16,000 characters and cannot be whitespace-only. The stored body maximum is 24,000 characters so Ordubashy's private notice of an accepted owner message to a permanent worker (the complete original, at most 20,000 characters, plus a short wrapper) fits in one row.
Sender identity comes from the authenticated connection; callers cannot override
it. Replies keep the business/Global scope and go to the current counterpart.
Each message has an immutable ID, thread ID, optional parent ID, sender, recipient,
body and timestamp. History includes the current recipient's acknowledgement time
and outcome, when present. There is no automatic deletion or expiry.

### Current participants

The original `sender_*`/`recipient_*` fields attribute authorship and never change.
`current_sender_id`/`current_recipient_id` (with names) route the conversation:
visibility, history, reply destination, inbox delivery and acknowledgement, and
management and delegated routing use them plus business scope. `counterpart_id` is
the current participant other than the caller; `counterpart_id` as a history filter
returns whole threads with that counterpart. A reply goes to the parent's current
counterpart after present-day scope and contact checks, and `to_agent_id`, if given,
must equal it; reply/thread links are unchanged and no retired identity is resolved.
A message whose two original participants are now one current agent is read-only
history: readable within its business, never delivered or notified, and a reply to
it fails with `invalid_transition`. Self-reminders keep one original participant and
are delivered normally. Writers set current equal to original participants; only
reviewed maintenance changes them.

Sending records the message and agent inbox delivery in one transaction. Retrying
with the same idempotency key does not deliver it twice. A reply does not
acknowledge its parent. Inbox reads and history reads never acknowledge anything;
agents continue to acknowledge exact handled event IDs using their watcher UUID.
The inbox event includes the full message, so no context-source lookup is needed.

Business messages require both participants to have current access to the chosen
business. Ordubashy selects the relevant business to contact its business agent;
the business agent can reply in that same business. Global messages require both participants to have
Global context access. Inactive recipients cannot receive new messages. Current
scope revocation hides history/delivery from the affected agent; unrelated agents
cannot read or reply to a thread. This does not provide an external public inbox.

A message is communication, not a new permission grant or automatic assignment.
Recipients retain their current responsibilities and approvals, defer messages
while working with the owner, and reply when useful rather than automatically
replying to every acknowledgement.

Delivery is immediate and durable, including when the recipient is offline.
The existing Claude watcher checks immediately and then every ten minutes,
repeating its advisory nudge while items remain pending. This neither launches
Claude nor injects terminal input. A stopped session receives its pending messages
when it next starts/checks. No new model runtime adapter is introduced.

The owner pane beside the terminal shows direct messages with sender, timestamp,
reply indication and an expandable plain-text body. Inspecting them does not
acknowledge them.

## Keyless local coding-agent access

Registered agents use their existing MCP connection: no new key to configure.
Local coding agents can use the installation's existing trusted owner HTTP API.
That API already identifies callers as the installation owner and checks allowed
Host/Origin values. This change does not remove MCP authentication or add a public
anonymous endpoint. Local owner-API messages are attributed to the owner, not to
an invented registered agent or a caller-supplied name.

For business scope, POST JSON to:

```
/api/biz/<business-slug>/operations/agents_message_recipients
/api/biz/<business-slug>/operations/agents_message_send
/api/biz/<business-slug>/operations/agents_messages_list
```

For Global scope use `/api/root/operations/<same-operation-name>`. Include
`Content-Type: application/json` and `Origin` equal to the configured service
origin, for example `http://127.0.0.1:1049`. No API key or cookie is needed on this
trusted installation. A send body is:

```json
{
  "to_agent_id": "<recipient UUID>",
  "message": "Please review the supplier update and report your findings.",
  "idempotency_key": "<unique stable key for this send>"
}
```

The local helper wraps these same API operations; it does not access SQLite or
another agent's credentials. Run with the installed core interpreter:

```sh
/absolute/path/to/venv/bin/python -I -m qorqut.inbox.client --business '<project UUID>' recipients
/absolute/path/to/venv/bin/python -I -m qorqut.inbox.client --business '<project UUID>' send --to '<agent UUID>' --message-file /tmp/message.txt --idempotency-key '<stable send UUID>'
/absolute/path/to/venv/bin/python -I -m qorqut.inbox.client --business '<project UUID>' history --thread-id '<returned thread UUID>'
```

`--message-file -` reads stdin; `--global` replaces `--business`. `--url` selects
another configured service origin. Reuse the same key after an uncertain timeout;
do not generate a new key until sending a genuinely different message. To reply,
use `send --reply-to '<message UUID>'` instead of `--to`.

Claude can reply to an owner-origin message with `reply_to`; the service infers
the owner recipient. Local coding agents read that reply through message history.
Owner replies do not require creating a fictional agent inbox or watcher for the
owner. Multiple local coding tasks share the owner identity: retain your returned
thread ID to follow your own conversation. This helper does not wake Codex or
another local coding agent automatically.

## Activation

This change builds on `feat/internal-agent-inbox`. Merge/deploy that core-default
change with this change, plus the dashboard update, through the normal authorized
release process. Migration 036 adds immutable message history; take the usual
backup before migration. Pending events and subscriptions remain intact.
Reprepare existing agents and refresh their bootstrap context to advertise the
new tools and instructions. No new role package, API key or personal Claude
configuration is required for messaging.

## Verification

Isolated validation covered 84 distinct messaging/inbox/configuration/policy/
bootstrap backend checks, 29 storage and recovery checks, 110 dashboard pane
checks, and the real browser inbox integration. The browser sent a message through
the owner HTTP API and verified expanded plain-text rendering, escaping and
existing Story/note/Global navigation. The MCP integration verified an agent reply
to a keyless owner-API message; the local helper exercised the same HTTP routes.
No production message or real Claude session was used.

The main backend run passed 79 checks; five package-source checks initially lacked
the sibling agent-roles fixture path. After supplying that path, all five plus
three startup checks passed. Storage tests were run after importing the normal
service runtime because direct standalone storage-maintenance collection exposes
a pre-existing circular import, also reproduced on the parent branch. Those 29
checks passed. This change does not modify that unrelated import ordering.

## Ordubashy directory

Ordubashy's profile is required to include every registered business. Business
creation extends that scope and its memberships in the same transaction, and
ordinary profile edits cannot narrow it. Its live bootstrap briefing therefore
includes the business agent directory: one business agent per business, each
titled Onbashy, so rows are told apart by agent and business ID. Refresh with
`agents_message_recipients(all_businesses=true, role_id="business-agent", scope="global")`.
It also includes the Dev directory, one Dev peer per business that has adopted
it, each titled Dev (`role_id="dev"`). Ordubashy routes implementation to Dev
through these messages and tasks; it never controls Dev's terminal.
Results include agent ID, role, business ID and slug, plus the businesses checked
so a business without an agent is visible. Use the returned business ID as the send
operation's `business_id` option, and omit `scope="global"` for that business send.
To read the conversation with one business agent, call `agents_messages_list` with
`counterpart_id` set to its agent ID in that business.
The local helper supports `--global recipients --all-businesses --role-id business-agent`
and `history --counterpart-id <agent_id>`.
Future businesses require the usual explicit profile scope grant; discovery does
not silently expand authority.
