# Private Settings retrieval

`environment_get({"key": "PRIVATE_CONTEXT"})` returns one value from the current
scope's Settings. It is provider-neutral: tokens, identifiers and private context
are all ordinary strings. No API adapters, HTTP proxy or database migration
are introduced. Missing keys return `found: false, value: null`; an empty stored
value returns `found: true, value: ""`. Responses are capped at 16 KB.

The operation is denied by default for every agent. An owner can explicitly set
`environment_get: allow` with the existing `permissions_set` workflow. The grant
covers ordinary non-reserved keys in that scope; private overlays additionally
require the registered role to be allowed for the key.
Explicit denial and current membership/capabilities are rechecked on every call.
Reads do not support signoff. No role pins or live permissions change on deploy.

Global calls require the existing `read_global_context` capability as well as
the operation grant. Business calls require active business membership and read
only that business's own Settings. Unlike `environment_list`, value reads never
inherit Global Settings into a business scope. Global agents must select Global
to retrieve Global values. Callers cannot supply an arbitrary scope in arguments.
Human callers must be owners of the selected scope. System workers are rejected.
Reserved runtime identity bindings remain unreadable.

Values are intentionally returned to the model and may persist in client/model
context and saved transcripts. Do not treat this as a server-side secret broker.
Grant access narrowly; retrieve only needed values. Do not copy private values to
tasks, messages, notes, logs, source control or generated configuration. This tool
does not encrypt the existing Settings store or make it suitable for storing card
security codes. Access to a card/account/token does not authorize a payment,
outbound message or any other external action.

Owner-directed code sessions are business-bound human-owner calls. Their
`environment_list` metadata contains only the selected business's ordinary
Settings and owner-readable private bindings; Global variable names are not
inherited. Their one-key `environment_get` calls likewise read only that
business and may include the owner's private values. Bootstrap and listings
never contain credential values, and owner-session credentials or delegation
grants cannot be issued or edited through these Settings operations.

Use `environment_list` when only presence or key names are needed. If a value is
only used by an existing server-side operation, prefer that operation so the value
never needs to enter model context. Native shell variable injection is unrelated
to this tool, so the read works equally through Claude, Codex CLI or app-server MCP.

## Role-private overlays (backend configuration)

Store `.env.private.json` alongside the scope's `.env`, outside source
control, with mode 0600 and a protected parent directory. Example (synthetic):

```json
{"PRIVATE_CONTEXT": {"value": "example", "allowed_roles": ["ordubashy"]}}
```

Entries accept arbitrary string content, not just API tokens. An empty role list
denies all agents. Role IDs come from the current registered profile, never tool
arguments, names, UUID allowlists or folder names. Malformed configuration fails
closed; symlinks are rejected. No UI controls or automatic operation grants exist.
The scope owner can retrieve these values through `environment_get`.

Private overlays are scope-local: Global private values never inherit into
business discovery, retrieval or terminals. For an allowed role, an overlay wins
over the same ordinary `.env` key. Other roles still receive the ordinary value
if one exists, but cannot discover or retrieve the private replacement. To make
a key exclusively private, remove its ordinary `.env` entry as a separate,
reviewed configuration change. The existing Settings UI edits only `.env`.

New agent terminals receive their role's local overlay. Owner workspace shells
and background environment consumers do not receive private overlays. Existing
terminals retain their original environment; restarting the service does not
revoke values already delivered to a process or transcript. Recreate affected
sessions when changing access. This is application authorization, not OS isolation:
unrestricted processes running as the same Unix user can read private files.
