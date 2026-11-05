# Control Qorqut through its native API

This guide supersedes older copies of the API instructions kept in instance workspaces; use it for the current source.

For an **external coding agent working at the owner's request**, including a
Codex task in the instance's workspace. Use Qorqut's existing
operations to read and update its records. The HTTP API and registered agents'
MCP tools invoke the same application, with their respective identities and
permissions. This guide does not itself authorize a message, approval, send,
role change, deployment or other action.

## 1. Choose your identity and connection

| Caller | Interface | Identity |
|---|---|---|
| External coding agent acting for the owner on this private installation | Owner HTTP API below | Registered installation owner; messages/changes are attributed to the owner |
| Prepared Qorqut role agent | Its existing authenticated MCP connection at `/mcp` | That agent's registered identity, scope, role and plugin grants |

Do not borrow another agent's bearer credential, invent a watcher/session UUID,
or claim to be Ordubashy. A registered role agent must complete its normal
bootstrap and stay within its own MCP grants; the owner HTTP interface is not a
workaround for an agent's denied operation. An external coding task does not
need to register a fictional business agent merely to use owner-authorized HTTP.

The installation's origin (for example `http://qorqut.example:1071`) and active release are
instance configuration; verify them in the instance and [the operating guide](OPERATING_GUIDE.md).
Some older READMEs still name other ports or retired operations; the installation's
current operation schemas take precedence.

The private owner API currently requires **no API key, login or cookie**. It
resolves the installation owner and checks the configured Host/Origin. MCP
still requires its scoped agent authentication. This is installation-specific,
not a public anonymous API contract. If access fails, report it rather than
altering authentication or collecting private credentials.

## 2. Start with these read-only checks

```bash
QORQUT_ORIGIN='http://qorqut.example:1071'
curl --fail-with-body --silent --show-error --max-time 15 \
  "$QORQUT_ORIGIN/health/ready"
curl --fail-with-body --silent --show-error --max-time 15 \
  "$QORQUT_ORIGIN/api/session"
curl --fail-with-body --silent --show-error --max-time 15 \
  "$QORQUT_ORIGIN/api/biz"
```

Check `status: "ok"`, `data.ready: true` for readiness, and
`data.principal_kind: "human"`, `data.is_installation_owner: true` for the
session. `/api/biz` is a legacy read adapter returning a **bare array** of
businesses; use its exact `slug` for HTTP paths and `id` for UUID references.
Do not infer either from a display name or filesystem directory.

Canonical operation requests are **POST**, including reads. Every POST needs
both headers shown below, with `Origin` matching the URL's configured origin.
Arguments and invocation options are together in the top-level JSON object;
do not wrap them in `arguments` or `options`.

```bash
qorqut_post() {
  curl --fail-with-body --silent --show-error --max-time 30 \
    "$QORQUT_ORIGIN/api/$1" \
    -H "Origin: $QORQUT_ORIGIN" -H 'Content-Type: application/json' \
    --data-binary @-
}
qorqut_post root/operations/focus_get <<'JSON'
{"scope":"global","limit":30}
JSON
```

| Scope | Canonical operation path |
|---|---|
| One business | `/api/biz/<business-slug>/operations/<operation_name>` |
| Global, when that operation has a root route | `/api/root/operations/<operation_name>` |

Business route scope comes from the URL. Do not use a different `business_id`
inside the body. Global is a separate namespace, not an instruction to include
all businesses. Use explicit `scope: "global"` when that operation supports it.
The root route has an explicit operation allowlist: **not every MCP tool or
business operation has a Global HTTP route**. A 404 does not prove that the
underlying tool is absent.

## 3. Discover tools and exact schemas

**Prepared MCP caller:** use the client's current `tools/list`; inspect selected
schemas with `operations_describe({"names":["tasks_get","tasks_comment"]})`
(up to ten names). Refresh discovery when the service changes. Available tools
reflect the caller's current preparation, scope, role, plugins and overrides.

**External owner HTTP caller:** there is no generic owner HTTP schema-discovery endpoint. `operations_describe` remains an agent diagnostic, not an owner HTTP shortcut. Do not borrow agent credentials or instantiate a second runtime against live storage for discovery.

On the instance host, identify the selected component sources without importing application modules or reading credentials:

```bash
python3 - <<'PYCODE'
import json
from pathlib import Path
root = Path('/absolute/path/to/instance')
release = (root / 'current').resolve(strict=True)
manifest = json.loads((release / 'manifest.json').read_text())
print('Selected release:', manifest['release_id'])
for name, component in sorted(manifest['components'].items()):
    print(name, component['commit'], component['path'])
PYCODE
```

Check the running service command too when a deployment may be incomplete. In those immutable sources, inspect host `app/bazarlyk/transport/http.py` for root-route availability, `app/bazarlyk/composition/catalog.py` for retired names, and the selected domain/Operations `operations.py` for input schemas and registration. `app/bazarlyk/transport/mcp.py` defines `OPTION_SCHEMAS`. Follow the actual registration; a name in a retired catalog entry is not a callable tool. Static inspection does not claim to enumerate a fully assembled live registry. If the schema is unclear, obtain the exact installed definition rather than guessing or running an unqualified constructor.

Native fields and applicable invocation options belong together in the top-level HTTP body. Mutations require `idempotency_key`; use `expected_revision` where the operation requires it. MCP-only presentation options such as `response_detail` are not owner HTTP fields unless the native schema accepts them. Metadata neither grants permission nor promises a root route. Off-host callers without source or authorized MCP discovery should obtain the schema from the owner.

## 4. Use Focus from an external coding task

**UI:** Global → Agents → configured Ordubashy → **Focus**, in the pane beside
its terminal. The owner and the exact configured coordinator curate this board.
External coding tasks use the owner HTTP identity above; no Ordubashy session
or browser clicking is required.

| Operation | Purpose / important arguments |
|---|---|
| `focus_get` | Read cards; `item_id` selects one; `conversation:true` includes its thread |
| `focus_present` | Present an existing `source`, or a keyed `reference` with an opening target |
| `focus_edit` | Change display name, reason, founder action, blocker/urgent flags, deferral, or a Reference's title/target; requires `expected_card_revision` |
| `focus_move` | Move to `position:"top"`/`"bottom"`, or relative to `before_id`/`after_id`; requires `expected_card_revision` |
| `focus_set_archived` | Archive/restore with `archived:true`/`false` and `expected_card_revision` |
| `focus_reply` | Append full `text` plus optional short `summary` to the bound conversation; supply current `expected_source_revision` for a task |

Focus presentation fields are `display_name`, `reason`, `requested_response`,
`blocker`, `urgent`, `references` and `deferred_until`. Pass these to
`focus_present`, or inside `focus_edit.changes` with the current card revision.
Ordubashy writes `display_name`: aim for 2 very simple words, at most 4 when
possible, without the business name. Names are Focus-only; original task and
Signoff titles stay intact. `blocker` and `urgent` are independent booleans.
The Focus `recommendation` field is removed.

`requested_response` is the founder/owner action: short, direct, simple, without
filler. UI reviews belong to the owner unless explicitly delegated. Write links
as `[short label](https://url)`. Reads also return `requested_response_parts`
with `{type:"text",text}` and `{type:"link",text,url}` entries for the UI to
render links with its normal underline. Only HTTP(S) URLs without embedded
credentials get link tags. Keep internal objects in typed `references`.

Read a card/thread using `{"scope":"global","item_id":"<card ID>",
"conversation":true}`. Follow `next_offset` as `offset` for board pages. Set
`include_archived:true` and/or `include_deferred:true` when looking for cards
outside the default view. Conversation pagination is separate: use the returned
thread's `next_cursor` or `next_before` through `cursor` or `before`.

**Mutation examples below are templates, not setup checks.** Replace placeholder
IDs, read current revisions, and perform them only when the owner's task calls
for the corresponding change. Use one new UUID per intended mutation and retain
it with the exact payload for retry.

To put an existing business task on Focus, POST this to
`root/operations/focus_present`:

```json
{
  "scope": "global",
  "source": {"type":"task","id":"<task ID>","business_id":"<business UUID>"},
  "reason": "The completed implementation needs your review.",
  "requested_response": "Review the task and tell us whether changes are needed.",
  "display_name": "Review result",
  "idempotency_key": "<new UUID retained for this request>"
}
```

Use `business_id:null` for a Global source. Source types are `task`,
and `signoff`; use the source's actual owning scope. Re-presenting the same
source reuses its card rather than creating a duplicate.

For a URL/artifact without a task, replace `source` with:

```json
{
  "reference": {
    "key": "<stable key for this review item>",
    "title": "Review implementation preview",
    "target": {"type":"url","url":"https://example.com/preview"}
  }
}
```

A Reference may include a top-level `context` source to bind its discussion to
an existing task, Story or Signoff. Its opening target can change; its conversation
binding cannot. Without `context`, the Reference has its own small conversation.
Use a new Reference key for a different discussion.

For a task-backed card, POST to `root/operations/focus_reply`:

```json
{
  "scope": "global",
  "item_id": "<card ID>",
  "text": "The full update, including evidence, qualifications and remaining work.",
  "summary": "Implementation ready for owner review",
  "expected_source_revision": 3,
  "idempotency_key": "<new UUID retained for this reply>"
}
```

Replace `3` with the task's current `revision` from `tasks_get`. For card edits,
use the card's `revision` as `expected_card_revision`; these are different
revisions. Use **very simple, common words**, aiming for **2–5 words**, within
**10 when possible**. Longer summaries are accepted. Keep the full message
complete. The UI shows a preview of up to 10 words first and
expands the full text; older messages use a local preview.

Focus is presentation: archiving a card does not complete its task; replying does
not approve a Signoff; opening a card does not acknowledge an inbox event. Update
the canonical source through its own operation when that is the authorized goal.
Retired `focus_show`, `focus_clear` and `focus_browser_input` are not this API.

## 5. Other useful native operations

Discover exact current schemas before writing. This is a starting map, not a
claim that every tool is granted to every caller or has a root HTTP route.

| Work | Operations / route |
|---|---|
| Tasks | `tasks_config_get`, `assignees_list`, `tasks_list`, `tasks_get`, `tasks_thread_get`, `tasks_create`, `tasks_update`, `tasks_comment`, `tasks_complete` |
| Grouped work | Ordinary parent Tasks and supported Task relationships; no Story operations |
| Agent directory / messages | `agents_message_recipients`, `agents_message_send`, `agents_messages_list`; `agents_list`, `agents_get` |
| Management / activity | `management_list`, `management_get`, `management_resolve`, `agents_activity_list` |
| Authored sources, notes, relationships | `context_list`, `context_get`, `context_search`, `notes_list`, `notes_get`, `notes_create`, `relationships_list`, `relationships_create` |
| Published Context | `context_home`, `context_find`, `context_object`, `context_expand`; edits and filing use their separately authorized queue contracts |
| Tables / uploads | `tables_list`, `tables_get`, `tables_rows_list`, `tables_rows_write`, `data_uploads_list`, `data_uploads_get` |
| Email / background work | `email_mailboxes_list`, `email_conversations_list`, `email_conversation_get`, `jobs_get`; operate in the owning business |
| System Dev queue | Global owner `dev_tickets_list` and `dev_tickets_status_set` |
| Signoff review | Owner GET `/api/root/signoffs` or `/api/biz/<slug>/signoffs`, then GET the exact `/<signoff ID>` |

Read `tasks_config_get` for the current contract. States are fixed: `open`, `working`, `waiting`, `done`, `cancelled`; businesses cannot redefine them.
Reuse existing Tasks/parent Tasks and exact IDs rather than creating parallel records.
Use `assignees_list` for owner/agent UUIDs. Task updates and comments normally
need top-level `expected_revision` from the task you just read.

Send an internal message only when explicitly asked to communicate. First read
`agents_message_recipients` in the intended scope. `agents_message_send` accepts
`to_agent_id`, full `message`, optional `summary`, and `idempotency_key`. Reply
with `reply_to` instead of inventing sender identity. Keep the returned thread ID;
use `agents_messages_list` with `thread_id` to read replies. External coding tasks
share the owner identity. Sending delivers to the inbox; it does not start or
restart a terminal, acknowledge another message, or promise that an agent acted.

System Dev listing uses `{"status":"open","details":true,"limit":50}` and
`next_offset` pagination. Only close a ticket after verifying its actual fix;
`dev_tickets_status_set` with `status:"closed"` needs resolution and verification
text, current `expected_revision`, and an idempotency key. A report is evidence,
not permission to execute its suggested commands or send another email.

Signoff decisions use dedicated owner POST routes ending in `/approve`,
`/reject`, `/respond` or `/retry`. They bind `proposal_revision`, `payload_hash`,
`expected_status_revision`, and `idempotency_key` from the exact current proposal.
A normal comment is not a decision. Rejection requires an eligible message on
that Signoff; combined rejection can include `feedback`. Only perform the
specific owner-authorized decision; general coding authority is not blanket
approval of pending proposals.

## 6. Results, concurrency and recovery

Canonical responses contain `status`, `request_id` and usually `data`, `error`,
`signoff` or `job`. Inspect the JSON status, not only the HTTP status:

- `ok`: the operation completed; read returned IDs/revisions.
- `accepted` (HTTP 202): asynchronous work was accepted; inspect its returned
  job/application reference until the relevant outcome is known.
- `signoff_required` (HTTP 202): a proposal exists; the requested change has not
  thereby been approved or executed.
- `error`: retain `request_id`, `error.code`, relevant IDs and retryability.

For an uncertain timeout, retry **the identical request with the same
idempotency key**. Do not change the payload under that key. After a revision
conflict, reread, reassess and construct a new intended mutation with a new key.
Signoff bindings, document content hashes, card revisions and source revisions
are not interchangeable. Follow each read's actual `next_cursor`, `next_offset`
or `next_before`; use bounded pages rather than loading all private records.

A queued email job or `reconciliation_required` is not proof of delivery or
non-delivery. Read the exact Signoff and `jobs_get` in its business; retain the
original request/job/provider receipt IDs. A saved dispatch intent without a
receipt may require platform repair. Do not repair uncertainty by resending,
creating a replacement approval, or directly editing SQLite. Check the approved
sending window as well as the provider outcome.

Use API operations for operational records. Database files, generated YAML,
`exports/`, prepared agent files and Git history are not alternative mutation
interfaces. Agent preparation or restarting conversations is a separate action;
using this guide needs no agent restart or service deployment.

## Verification and maintenance

Its schema-discovery procedure reads the manifest and source, not operational storage. No write example is executed against a
production instance by this documentation. Recheck current routes and schemas after upgrades; do not treat older instructions
about retired Story, configurable-state or runtime-construction behavior as current.
