# Operating guide

This is the entry point to the host's source documentation. It describes how the source
is organized and the product contracts it implements. It is not an installation guide:
see the [native distribution guide](../distribution/README.md) for the downloadable application and
[development preparation](../INSTALLATION.md) for source builds.

## Source and instance layout

- The host source is `qorqut-code`; its Operations engine, shared Context engine and
  dashboard are ordinary component directories in the publication repository
  (see [repositories](../REPOSITORIES.md)).
- An instance keeps source, configuration and data apart: reusable source and roles
  are code; `config/` holds private configuration; `operations/` holds the operational
  database (`operations/.runtime/operations.sqlite3`), project working areas
  (`operations/projects/<project>/hq/`, `code/`, and registered agents' folders), the
  published shared Context (`operations/context-graph/`) and its runtime
  (`operations/.runtime/context/`). `operations/context/` contains authored inputs,
  not the published graph. Runtime files are not source code.
- Agent UUIDs remain identity; display names and folder labels do not confer authority.
- Reusable roles live in `agent-roles`; see [repositories](../REPOSITORIES.md).
- Recovery: [recovery guide](../../app/bazarlyk/storage/RECOVERY.md). Owner API:
  [native API guide](CONTROL_VIA_API.md). Behavior: [contracts](CONTRACTS.md).

## Release worker

The release worker is source tooling under `app/bazarlyk/release_worker/`; its contract
is in [docs/release](../release/README.md). It must be activated through its versioned
installer from an immutable artifact and is never run from a development checkout.
Documentation is not release authorization: merge, worker activation, stop and deploy
steps each need their own approval.

## Current product contracts

- Agents keep their identities and operational permissions. All registered agents may
  read the shared Context; that does not grant cross-project operational writes.
- Tasks have fixed states `open`, `working`, `waiting`, `needs_input`, `done`, `cancelled`, parent
  Tasks, informational dependencies, one shared snooze timestamp, due dates, archiving,
  custom fields and `owner_request`. Parent/child states are independent. A work group is
  an ordinary parent Task; there is no separate Story record, API or destination.
- Tasks and Actions retain one owning global/project scope. Shared tags are labels, not
  scope or grants. Signoffs and routines keep their policy and lifecycle semantics.
- Chat targets the exact existing native conversation. Conversations are started and
  resumed manually in Terminal; Chat has no conversation selector. Raw native archives and
  durable read metadata back Chat history and notifications.
- The backend delivers advisory inbox nudges through the same input arbiter as Chat, with
  Chat closed too. There is no agent-side polling or Monitor setup. See
  [Inbox](../AGENT-INBOX.md).
- Context reads use the published graph; ordinary inputs, exact Action revisions and
  controlled graph edits use the ordered queue. Queue admission is not publication. See
  [Context composition](CONTEXT_COMPOSITION.md) and [graph edits](CONTEXT_GRAPH_EDIT.md).
- Private DMs are excluded from additional operational Git snapshots. Their authoritative
  stores and raw native archives remain separate. See [history coverage](HISTORY.md).

## Documentation and pins

Source pins are the exact commit and subtree trees in `SOURCE-MANIFEST.json`; installed pins are the selected release manifest.
No private development history or submodule pin is required by the ordinary publication repository.
