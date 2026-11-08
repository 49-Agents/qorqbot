# Owner-directed code sessions

Interactive Claude Code / Codex CLI conversations that the installation owner
starts inside a business's code tree. They act with the owner's own human
identity through a dedicated MCP route, read business context on demand, and
save owner-requested work through existing operations. They are not autonomous
business workers: no agent profile, roster entry, inbox, watcher, autonomous
task discovery or work loop exists for them, and existing managed agents are
unchanged. Starting or ending a session writes nothing and claims, announces or
hands off nothing; several sessions can work in one project beside its other agents
without becoming participants. This document describes the implementation contract. Merge, deployment and
live enrollment are separate release steps; a source checkout is not proof
that the running installation supports this route.

## Identity and transport

- `/mcp/owner` is a second Streamable-HTTP MCP route beside the unchanged
  registered-agent `/mcp`. Both share one transport implementation through a
  caller-policy adapter (`qorqut/transport/mcp.py`,
  `qorqut/transport/owner_mcp.py`).
- An owner-session credential is an ordinary revocable `credentials` row on
  the installation owner's human principal. Trusted `metadata_json` carries
  `purpose: owner_session`, a version, the bound business UUID, the machine
  label, and the prepared endpoint/roots as diagnostics. Clients cannot supply
  an author, principal, role or business; a registered agent credential is
  rejected on this route and an owner-session credential is rejected on
  `/mcp`. Credentials are identified by principal and purpose: retiring a
  registered agent revokes only that agent's rows, and owner-session
  revocation only touches rows with this purpose and business.
- Every request — including calls inside an established MCP session — rechecks
  credential validity and expiry, the business's `owner_sessions.enabled`
  configuration, that the business exists and is not archived, and the owner's
  active owning membership. Revocation, disablement, archival and membership
  removal therefore fail cleanly mid-session.
- All domain effects receive the owner's human identity. Ordubashy and the
  dashboard see an ordinary owner author; request/session correlation stays
  transport-internal diagnostics.

## Enablement and configuration

The optional business-configuration section is owner-managed and validated:

```json
{"owner_sessions": {"enabled": true}}
```

(`revision` is an optional positive integer for future re-preparation
bookkeeping.) Agents and workers cannot introduce or change the section.
Enable it per business through ordinary business settings
(`business_config_patch`) before issuing credentials. Deployments ship with no
section, i.e. disabled, until the owner explicitly enrolls a business.

## Operation catalog

The route exposes a fixed, explicit catalog (`qorqut/owner_sessions.py`);
newly registered operations never enter it automatically, and the catalog
binds direct calls, aliases and batch entries as well as `tools/list`.

- Orientation: `owner_session_bootstrap`, which includes only compact
  coordinator metadata and operation pointers, never task/message/secret data.
- Business reads and writes cover the explicit catalog in
  `qorqut/owner_sessions.py`: context/documents/relationships, stories,
  tables/uploads/assets, metrics/ledger/activity, business settings,
  business-only Settings metadata/value access, tasks, and direct messaging.
  Tasks include `tasks_create`, `tasks_assign`, `assignees_list`,
  `tasks_config_get`, and `tasks_config_update`; messaging includes
  `agents_message_recipients`, `agents_message_send`, and
  `agents_messages_list`.
- `context_instruct`, `stories_close`, and `stories_reopen` have an explicit
  caller-aware owner-session exposure marker. The Story lifecycle routes
  remain owner-only. Context corrections also retain the separately authorized
  registered-role MCP access introduced by the context-correction release.
- `operations_batch` with the same per-entry checks.

Business operations are bound to the configured business: explicit conflicting
`business_id` / `review_business_id` selectors are rejected, and foreign
source objects fail the existing domain ownership checks. Domain arguments
such as `scope: "global"` cannot switch tasks, Signoffs or conversations into
Global. Nested references and prepared targets retain the single-business
binding; the existing shared-context reads remain available. Excluded entirely: agent administration,
permission/grant administration, subscription/watchers, agent presence and
work tracking, role workflow, managed bootstrap/checkpoint calls, background
routines, terminal controls, credential issuance, account-wide email,
outbound sends, Signoff decisions, and every read-acknowledgement operation
(`owner_conversation_mark_read` stays dashboard-only).

Managed-agent `plans_*` operations are deliberately absent: they are agent
work plans, i.e. operational inventory. Authored planning documents live in
the context library and stay discoverable through `context_list` /
`context_search` / `context_get`.

## Bootstrap

`owner_session_bootstrap` is one bounded, side-effect-free read returning:
the bound business identity (`business`), the engineering authority,
registered repositories and code root (`engineering`, no credential values),
`briefs`, a metadata-only index of further context documents (`context_index`), a compact
`coordination` object with the configured active Ordubashy
(`coordinator_id`, `coordinator_name`, `available`; `reason` is `not_configured`
or `inactive_or_out_of_scope` when unavailable) and the message/task operation
names (`operations`), short `guidance` naming the owner by registered display
name (project direction belongs to the owner with Ordubashy; the project's notes
via `notes_list`/`notes_get`; projects have no resident agents), and
explicit `missing` diagnostics for any source that could not be read. It
contains no task, Story, Signoff, inbox, message history or lifecycle data,
starts nothing, and never claims completeness it cannot show. Sessions read
anything deeper progressively through the catalog reads.

`briefs` prefers the current context home: the same business-scoped
`home_text` provider managed agents receive, read fresh on every call. It is
then the only brief, `{"key": "context_home", "source": "context-base:<business
id>", "characters", "truncated", "text"}`, with no document ID, path, revision
or hash because it is a derived graph projection; stale authored pillar briefs
are not listed beside it. Without a configured provider, or when it returns no
home, `briefs` falls back to the authored mandatory pillar briefs with their
document IDs, revisions and content hashes. An enabled provider that returns no
home adds `{"source": "context_home", "reason": "unavailable"}` to `missing`,
so the fallback is never mistaken for current context. `character_count` sums
the returned brief text and never exceeds `character_limit`.

## Authorization and the private channel

Qorqut write authorization is an instruction contract, not a permission mode:
a sufficiently specific owner instruction ("create these tasks", "close this
Story") authorizes that operation with no second confirmation. The session asks
only for missing scope or a materially different action; tool access never
authorizes unrelated writes, and the catalog, business binding and excluded
operations are enforced by the route. An instruction in one conversation never
authorizes another conversation's mutation; there is no shared "current task" or
approved flag. Writes use current revisions and per-mutation idempotency keys —
a lost response is retried with the same key and a `revision_conflict` requires
rereading. Existing state transitions apply: tasks close with `tasks_complete`
(or a closed status) and reopen with `tasks_update` to an open status; Stories
use `stories_close`/`stories_reopen`, which refuse an already closed/open Story.
Requested changes are ordinary visible records with their usual notifications,
attributed to the owner. Concurrent sessions share the business profile but no
state; existing revision/hash checks arbitrate conflicting writes.

Freeform commentary goes only to the owner/Ordubashy private conversation
(`owner_conversation_send`), stored under the real owner principal and
delivered to Ordubashy exactly like a dashboard-authored owner note. Shared
team discussions stay closed to owner freeform text: `stories_note_add`,
committed story drafts, task note fields, Signoff comments, email comments and
focus replies all refuse the installation owner with `channel_required`, and
this route additionally rejects `stories_update` `note`/`summary` arguments,
whose shared state-change events would otherwise carry commentary into the
team timeline (add such a state-change reason through the dashboard if it is
genuinely wanted there). Formal decision feedback keeps its existing shared
paths and is not part of this feature. Owner-session reads never mark
dashboard messages read, never change Focus placement and never create work.

## One-time setup per machine

1. Enable the business: patch its configuration with
   `{"owner_sessions": {"enabled": true}}`.
2. When the code tree and installation database are on the same machine, use
   the deployed `biz-core` Python environment to run:

   ```sh
   python -m qorqut.bootstrap.owner_prepare prepare \
     --businesses-root <businesses root> \
     --business <uuid-or-slug> \
     --root /abs/path/to/business/code \
     --nested some/checkout --nested another/repo \
     --endpoint <service URL> \
     --label <machine name>
   ```

   `--nested` names each repository root inside the code tree that sessions
   will be launched from — configuration is not assumed to propagate into
   nested Git roots, so each launch root must be prepared explicitly. A
   checkout outside the code tree requires the explicit `--outside-root`
   flag. The endpoint is the existing local/Tailscale service URL; no new
   exposure is required. Server-side business identity is the business UUID;
   a remote machine's paths need not mirror the server's.

Each prepared root receives: `.qorqut-owner/` (0600 `credential`, 0700
POSIX-sh `headers` helper, `registration.json` receipt), a `qorqut-owner`
entry merged into `.mcp.json`, one marker-delimited instruction block in
`CLAUDE.md` and `AGENTS.md`, and a `.gitignore` entry for `.qorqut-owner/`.
Every prepared root also gets an owned MCP block in `.codex/config.toml`. Unrelated
MCP servers, authored instructions, ignore entries and client settings are
preserved; collisions (an existing unrelated `qorqut-owner` server, invalid
JSON/TOML, unbalanced markers, symlinked paths) abort before anything is written.
Re-running is idempotent and rotates the machine's credential. Global
Claude/Codex configuration is never touched, and no approval requirement is
added for ordinary local coding work.

## Setup on another Tailscale machine

Credential issuance takes place on Qorqut; file installation takes place on the
client. The client needs Python 3.12+ and the matching `biz-core` package and
dependencies (install the same built wheel in a local virtual environment).
It does **not** need the installation database, server filesystem layout or a
copy of business data. The installed MCP headers helper itself is POSIX shell
and does not depend on Python at runtime.

1. Enable the business as above. On **Qorqut**, create a private directory
   outside source repositories and issue a bundle using the deployed Python:

   ```sh
   install -d -m 700 "$HOME/.local/share/qorqut/owner-sessions"
   python -m qorqut.bootstrap.owner_prepare issue \
     --businesses-root /absolute/path/to/businesses-root \
     --business <uuid-or-slug> \
     --endpoint http://qorqut.example:1071 --label laptop \
     --output "$HOME/.local/share/qorqut/owner-sessions/business-laptop.json"
   ```

   The 0600 bundle contains a secret credential, business/endpoint metadata and
   the owner's registered display name for the generated text, with no
   server-local code paths. Older bundles without the name still install. The
   command prints its location, not the token. Reissuing the same
   business/label revokes that label's previous token. Choose a new output
   filename or remove a no-longer-needed transfer bundle before reissuing;
   issuance refuses to overwrite an existing file.

2. On the **client**, copy the bundle through SSH/SCP on your tailnet. Keep it
   outside the code checkout; never commit it or transfer it through Git:

   ```sh
   install -d -m 700 "$HOME/.local/share/qorqut/owner-sessions"
   scp <qorqut-host>:.local/share/qorqut/owner-sessions/business-laptop.json \
     "$HOME/.local/share/qorqut/owner-sessions/business-laptop.json"
   chmod 600 "$HOME/.local/share/qorqut/owner-sessions/business-laptop.json"
   python -m qorqut.bootstrap.owner_prepare install \
     --bundle "$HOME/.local/share/qorqut/owner-sessions/business-laptop.json" \
     --root /absolute/client/path/to/code --nested some/repository
   ```

   Use the actual client paths, which can differ from Qorqut. Installation
   validates all explicitly selected roots and merges their configuration
   locally; it does not open a database or contact the running service.

3. After successful installation, remove the transfer bundle from both
   machines when no longer needed. Credentials remain in each prepared root's
   private, ignored `.qorqut-owner/credential` file. For rotation, issue a new
   bundle on Qorqut and reinstall it on that machine. Revocation runs on Qorqut.

## Launch, reconnect, and concurrency

- Claude Code: `cd <prepared root> && claude` (plain; project `.mcp.json`
  supplies the `qorqut-owner` server).
- Codex: `cd <prepared root> && codex`. The project-local MCP configuration
  augments the normal user configuration. Do not change `CODEX_HOME`: existing
  authentication, models, permissions, plugins and user MCP settings keep their
  usual location and precedence.
- Complete each provider's ordinary project-trust/MCP approval prompts on first
  use. Codex only loads project configuration in trusted projects; Claude may
  initially show the project MCP server as pending approval. Preparation does
  not bypass those provider controls or overwrite your chosen permission mode.
- Generated instructions make `owner_session_bootstrap` the session's first
  action before project work. The owner does not need to remember a tool call.
  A failed bootstrap is diagnosed before claiming to know the business.
- Each MCP request re-authenticates; resumed CLI conversations keep working
  while their credentials remain valid. Clients may cache authorization headers
  and tool schemas: after credential rotation or a server-tool update, use the
  client's MCP restart/reload facility (or restart the client as appropriate)
  and resume the conversation. An HTTP 401 after rotation is not a lost chat.
  Multiple simultaneous conversations are expected;
  each is independent, with its own approvals and idempotency keys.

## Revocation and removal

- Rotate one machine: re-run `prepare` with the same `--label` (the previous
  credential is revoked and files rewritten).
- Disable one machine: `python -m qorqut.bootstrap.owner_prepare revoke
  --businesses-root <root> --business <uuid-or-slug> --label <machine>`
  (`--label` defaults to the current hostname).
- Disable the business: set `owner_sessions.enabled` to `false` — every
  session fails immediately with `forbidden`, without touching credentials.
- Remove the client integration:
  `python -m qorqut.bootstrap.owner_prepare remove --root <top>
  [--nested ...]` deletes only owned files and entries and restores prior
  content. Removal does not revoke credentials; revoke separately.
- Rollback of the whole feature is disable + revoke + remove. Owner-attributed
  business records made through sessions are ordinary durable records and are
  not erased.

## Troubleshooting

| Symptom | Meaning |
| --- | --- |
| HTTP 401 `unauthenticated` on connect | Missing/invalid/revoked/expired credential or a cached old header. Check the installed credential, reissue/install if needed, then reload the client's MCP connection. |
| HTTP 403 `forbidden` on every call | `owner_sessions.enabled` is false, the business is archived, or the owner membership is inactive. |
| `forbidden: This operation is not in the owner-session catalog` | The operation exists but is deliberately outside this route; use the dashboard. |
| `channel_required` | Freeform text aimed at a shared discussion; send it with `owner_conversation_send` instead. |
| `revision_conflict` | Reread the object; retry with a fresh key if the owner's instruction still applies, otherwise ask. |
| Bootstrap `missing` entries | Named sources (briefs, repositories, index) are absent or unreadable — authored context needs filling in, the session is not silently under-informed. |

## Limitations

- Prompt adherence (acting only on specific owner requests) is an instruction
  contract verified in owner-led acceptance, not a mechanical guarantee; the
  catalog, business binding and credential checks are the enforced surface. None of
  this is a filesystem sandbox against same-user shell access through another
  interface.
- Issuance/revocation require the installation database on Qorqut. Remote
  installation uses the explicit private-bundle workflow above; it is not
  automatic account enrollment and needs the local Python package for setup.
- Native preparation supports POSIX hosts with Claude Code/Codex versions that
  support the generated HTTP headers-helper settings. Windows-native setup is
  not part of this implementation; use a compatible POSIX environment.
- `stories_update` state-change reasons (`note`/`summary`) are refused on
  this route even though the dashboard allows them for the owner.
- Enable/re-prepare/revoke are explicit owner administration; a
  business-settings UI can follow later.

## Pilot

Pilot one business with both providers before broader enrollment:

1. Enable `owner_sessions` on the pilot business; run `prepare` on one
   machine with the code root and one nested repository root.
2. Launch plain `claude` at the code root: confirm the bootstrap names the
   right business, brief revisions/hashes match the library, and no task or
   inbox data appears.
3. Repeat from the nested root, and once with two concurrent conversations;
   confirm both work and stay independent.
4. Do a real piece of software/design work: confirm ordinary local coding
   proceeds under existing client settings, a specific request ("close this
   Story") is carried out without a second confirmation while unrelated writes
   are not made, a requested private note reaches Ordubashy as an ordinary
   owner message, and CLI reads leave unread dots unchanged.
5. Exercise a refusal: ask the session to post a story note — expect
   `channel_required`/catalog refusal and the private-channel fallback.
6. Launch plain Codex with its normal user home and repeat a short version of
   2–4; verify the owner's model, permissions and other tools remain available.
7. Revoke the machine label and confirm the open session fails cleanly;
   re-prepare and confirm recovery. Report behavioral observations separately
   from the mechanical API tests.

For the remote pilot, issue on Qorqut and install on a second tailnet machine
at a different absolute path using the same top/nested-root checks. Do not
copy a database or prepared server paths to that machine. Behavioral approval
and real remote reachability are pilot results, distinct from isolated tests.

## Automated verification

With the usual test workspace/package-source configuration from the repository
test setup, run:

```sh
python -m pytest tests/test_owner_session_mcp.py \
  tests/test_owner_session_bootstrap.py tests/test_owner_session_preparation.py \
  tests/test_owner_session_final_model.py tests/test_owner_session_records.py \
  tests/test_owner_session_cli.py
```

The native CLI checks use temporary user homes and no real credentials, model
calls, live business writes, or existing CLI processes. They read the actual
Codex app-server's effective configuration and run each provider's MCP discovery
from the top and nested Git roots. Codex model/approval/sandbox settings and a
personal MCP entry must survive alongside the prepared owner MCP. These checks
were exercised with Codex 0.154.0 and Claude Code 2.1.274; they skip when their
respective native executable is not installed. Project-trust and real owner
approval behavior still belong to the pilot above.
