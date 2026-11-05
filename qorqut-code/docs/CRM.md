# CRM In The Host

A CRM is an ordinary Table with `table_kind: crm`. Its entries point at existing records (Tasks, Stories,
Customers, Email Tasks) and add a stage and Table-local cells; the record itself is never copied, converted or
given a parent. The CRM tables, entry rules and operations belong to the Operations Domain package
(`qorqut_operations.crm`). This document is the **host's** part: how that Domain is composed, who may call what,
the aggregate activity read, the native Focus and private-conversation seams, and what is recorded or exported.

This is not the retired host campaign or marketing layer. No campaign rule, attribution, reply routing, finance,
mailbox orchestration, contact matching or dataset import exists here, and no live provider is ever called.

## Composition

`bazarlyk.domains.register_business_domains` builds the Domain once, after Tasks, Tables and the neutral items
exist and **before the registry freezes**: `CRMOperations(application, tables, tasks, items, clock=…)`, then
`tables.bind_crm(crm)` so the Table service reads CRM rows through it, then `crm.register(registry)`. It is exposed
as `runtime.domains.crm`. The host adds one operation of its own, `crm_activity_list`
(`bazarlyk.domains.crm.CRMActivityOperations`, `runtime.crm_activity`). There is no substitute CRM
implementation: the host imports the Domain's `CATALOG_ENTRIES`, `TABLES`, `IMMUTABLE_FIELDS`, `SCHEMA`,
`CLASSIFICATIONS` and `storage_contribution()` and never keeps a second copy of its schema.

| Concern | Where |
|---|---|
| Catalog and exposure | `composition/catalog.py`: the Domain's entries join `ENGINE_ENTRIES`; `crm_activity_list` is a host entry (`read`, no Signoff form). Each name is declared once. |
| Storage ownership | `composition/schema.py` composes `crm.storage_contribution()` (`crm_entries`, `crm_activity_events`) beside the other contributions, so it is part of the target fingerprint. |
| Change capture | `composition/capture.py`: feed-only triggers writing **identity columns only** (`business_id`, `id`); cells, summaries and bodies are never captured. |
| Change feed | `service/changes.py`: `crm_entries` is a `tables` change, `crm_activity_events` a `tasks` change, and the typed item `table` is `tables`. |
| History and export | `operations_history/catalog.py` classifies both tables explicitly (`host.separate_history`). `storage/exports.py` exports `crm_entries` (local cells and scoped references) and deliberately **not** `crm_activity_events`: recorded external bodies stay out of broad Git history. Normal database backups retain both. |

## Operations and policy

| Operation | Effect | Bounded by (existing operation) |
|---|---|---|
| `crm_entries_list` | read | `tables_rows_list` |
| `crm_entries_add`, `crm_entries_update`, `crm_entries_remove` | records | `tables_rows_write` |
| `tables_thread_get`, `tables_follow` | read / preference | `tables_get` |
| `tables_comment` | records | `tables_rows_write` |
| `crm_activity_list` | read | `tasks_get` |
| `crm_activity_record` | records | `tasks_comment` |

Whether a write can be staged as a Signoff is declared by the Domain's own catalog entry, not here. `composition/policy.py` owns the mapping (`CRM_PREREQUISITES`) as a rule that runs before an agent's override is
consulted. For each name the agent's own override, else its role default, else the prerequisite's own decision is
combined with the **prerequisite's effective decision** by strength: deny > signoff > allow. So an `allow` on the new
name never lifts a denial or Signoff requirement of the base operation, an explicit `deny` on the new name always
wins, and a name with no grant of its own inherits the base decision exactly. A Signoff requirement the new
operation cannot stage is a denial, never an allow. The base operation is resolved in the caller's own scope, so
the Global Table rule (only the standing Global roles hold Table defaults; anyone else needs an explicit grant)
applies to the Global call rather than the business rule. Effective-configuration discovery evaluates both scopes
for the Table-based names, so what an agent is told it may do matches what the call will do. A Table comment is
Table **write** authority; it never borrows Task comment, schema or archive authority. Membership changes and
recorded events keep the Normal Signoff policy of the operation they build on.

The roles repository grants the nine names by default only to Ordubashy; the permanent worker receives
`crm_activity_list` and `crm_activity_record` alone. Source defaults grant nothing live.

## Aggregate activity: `crm_activity_list`

`crm_activity_list(task_id, include_linked?, cursor?, limit?)` returns `{record_ref, items, next_cursor, as_of}`
for one authorized record, newest first. It merges the Domain's `local_activity` (the record's own discussion,
recorded observations and CRM-membership developments) with the events of sources the record is **directly and
explicitly** linked to:

- Task links in either direction, `concerns` / `related_to` relationships in either direction, and typed references
  in the record's own comments. Nothing is inferred and nothing is followed recursively (a linked Task's own links
  contribute nothing). Candidates are deduplicated by their full scoped identity. **There is no cap** on the number of
  linked sources, on the discovery rows examined or on the length of any history; each is read in stable keyset
  batches, so a link, source or version is never silently dropped however many exist.
- Each source is opened with the **caller's current authority on every page**, and the read is admitted exactly as a
  direct call of the source's own read operation is (`bazarlyk/read_admission.py`: the operation's exposure, the Global
  capability, the policy decision and, for a delegated assignment, the explicit in-boundary record selector - not the
  policy decision alone). One that is absent, archived away or no longer readable is skipped without a trace: no
  label, count or placeholder. Another business's Task needs the caller's own access to that business; a Global Task
  needs the Global read capability.
- **Linked discussion is its own read.** The text, labels, count and paging position of a source's discussion appear
  only while the caller may *also* read that discussion in the source's own scope: `tasks_thread_get` for a Task (its
  comments and the system entries written beside them are one thread), `signoffs_notes_list` for a Signoff's notes and
  `actions_thread_get` for an Action's thread. A caller who may read the record but not its discussion still receives
  the source's non-discussion developments (a Signoff's proposal, decision and application, an Action's record, a
  document's versions) and nothing of the discussion. A delegated worker therefore reaches a linked Task's discussion
  only when its assignment allows `tasks_get` and `tasks_thread_get` **and** names that Task; a Signoff or Action
  read takes no record selector and so is never reachable by a delegated worker. Nothing here changes the permission
  engine or widens access.
- Events keep the source's canonical identity, **actor** and time. A system development carries the principal whose
  change produced it (the entry's author, else the principal that recorded it), and a relayed comment keeps its
  speaker plus the recording agent and the relay kind; no actor is invented when the source holds none, and the relayed
  statement text and session identifiers are never copied. An Action is ordered by when it happened and cut off by
  when it was recorded; Signoff developments are its proposal, decision and application plus its notes (notes of a
  `whatsapp_send` Signoff are not repeated); document events are version metadata only.
- **Never read**: private owner conversations, native session stores, retired email stores, whole-Project activity
  and any provider. `whatsapp_send` message text and mailbox contents are not fetched.

Paging. Every stream is ordered by `(instant, event id)` descending, and a page is the best `limit` events strictly
older than the last emitted position. Each source offers at most its own newest `limit + 1` events and the sources
are visited one after another keeping only the best candidates, so no history is held in memory; the cost of a page is
proportional to the number of linked sources and their documents' version indexes, not to any history length. A
document's versions are paged by the database in the same canonical order. The Domain's local stream is resumed at the
**last emitted `(instant, event id)`**, not at a count of rows previously shown: the window that holds the first
unconsumed event is read again from the *current* data, cut at that position, and read on window after window until
enough events are found or the stream is exhausted. An event that becomes unreadable between pages therefore never
makes a different one disappear or repeat, and one that becomes visible behind the position is not inserted into a
listing that has moved past it. The Domain's stream must be ordered by `(instant, event id)`; a window that is not is
refused as `internal_error` rather than reordered. The cursor is small: a version, a binding hash (operation, caller,
record, `include_linked`), a fingerprint of the **complete** direct-link set, the first page's cutoff `as_of`, the last
emitted position and the Domain stream's resume state. A cursor of another record or caller, or one with a missing or
mistyped field, is `validation_error`; a changed link set is `revision_conflict` (start again). Events recorded after
`as_of` are excluded, including **backdated** ones, so a later page can never gain or reorder an event. `limit` is
1-100 (default 50).

`crm_activity_list` and `crm_activity_record` are served by the authenticated Global operation route
(`/api/root/operations/<name>`, `scope: global`) with the same restrictions as the other Global operations (no
business option); the route grants no authority, and neither name is an owner-session capability. The Domain returns
`channel_required` for the installation owner recording Global activity. Project Tasks retain their existing owner exception.

The read is pure. It writes no row, acknowledges no inbox event, marks no conversation read, creates no receipt
and sends nothing; tests compare every table's row count and `PRAGMA data_version` before and after.

## Focus, private conversations and Projects

- **Focus.** `table` is a native Focus source, context and reference kind. A Table card resolves through the same
  item resolver as any other source and carries `resolved_source.table_kind` (`general` or `crm`) so a client can
  choose its expansion; a CRM and a general Table share one identity. `note_count` counts only the Table's team
  comments and is itself a discussion read: for a caller who may read the Table but not `tables_thread_get` in the
  Table's scope the count is omitted (the card and its independent private channel remain), and the decision is made
  per call. Task and Signoff cards are unchanged. A conversation read routes the team thread through `tables_thread_get`; a reply is prepared as
  `tables_comment` (and the installation owner keeps the existing channel rule: shared Table discussion is not the
  owner's channel, so an owner reply is `channel_required`). Archiving the **card** (`focus_set_archived`) is
  presentation only and leaves the Table untouched; presenting the Table again restores the same card. Table archive
  is the separate existing Table operation. No parent edge or wrapper Task is written for a pinned Table.
- **Private conversations.** `owner_conversation_*` accepts `source.type: 'table'` (business or Global). It is the
  same single owner + Ordubashy channel: null peer, source key `source:<business|global>:table:<id>`, discovery,
  read/unread, replay and authorization as for a Task, re-authorizing `tables_get` on every call. Private text is
  never reposted into the team thread, never exported, and never part of activity.
- **Projects.** A Table linked into a Project's inventory has `can_thread: true`.
- Ordinary Task, Signoff, Reference and Project cards are unchanged.

## Verification status

The focused host suites are `test_crm_host_policy.py`, `test_crm_host_composition.py`, `test_crm_host_activity.py`,
`test_crm_host_activity_boundaries.py` (discussion boundary, delegated record boundary, actors, Global route),
`test_crm_host_activity_scale.py` (no source/discovery/version ceilings, stable local resume, cursor validation),
`test_crm_host_surfaces.py` and `test_crm_host_upgrade.py` (see [CRM-UPGRADE.md](CRM-UPGRADE.md)). These pass with the
integrated `qorqut_operations.crm` package and its real schema, readers and handlers. The fallback in
`tests/crm_domain_double.py` is inactive when that package is present. Tests that simulate an event disappearing between
pages explicitly wrap `local_activity`; ordinary reads and cursor progression use the real Domain.

`test_crm_integration_flows.py` exercises atomic Task creation/membership, exact idempotency replay, all existing Task
subtypes, shared projections and local fields, removal/revival, prepared Signoff revalidation/application, canonical
Inbox delivery, sticky unfollow, private/shared separation and full database backup/restore.
`test_crm_integration_browser.py` drives the real dashboard and disposable composed Host in both Global and Project scopes:
create/add/edit, history, the owner channel rule, Table/member pins and expandable Focus navigation. External requests are
blocked. The existing Project composition browser regression also passes with Tables' new private Chat.

This is synthetic local qualification; no mailbox provider, live data migration or deployment is part of it.
