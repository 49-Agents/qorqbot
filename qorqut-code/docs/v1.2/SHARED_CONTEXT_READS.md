# Shared Context reads checkpoint (P4A): the published graph read adapter and the host read consumers

Branch `v1.2/shared-context`. Two source checkpoints: `cd9dab2` (the reader, its typed operation contribution and tests, parent `7679f287164c0d0cfa109e6415baabb8500160df`, the foundation integration checkpoint) and the follow-up commit on top of it (published-ancestry corrections, the final Context path, direct host registration and the replaced read consumers). Only synthetic repositories and instances built under a pytest temporary directory ran. No operational Context, business file, transcript, registration, credential, model, provider or network was used, and no user instance, native agent or server was started. Pins are unchanged: Operations `138f40c64e457f0b8723b6b764e1905a69e763fc`, Context `24f374450b5c62ba0c498dc5ef143c51491ad399`, dashboard `3c34ab14829e9cdc963c5b3fffbea8ce44e4bf63`, template `d056f217bd5a20b04599444b1452c1f645ebdfb3`. **This is the read side only. It is not the finished Context integration:** the old Context writers and their queue, typed edits, filing receipts and costs, and the learning and maintenance writers are the next P4 assignment (see *Remaining writer, lifecycle and manifest inputs*).

## What exists

`app/bazarlyk/shared_context/` is the one published-graph read engine of the host. `register_business_domains` constructs `SharedContextReader.for_instance(paths)` and installs `shared_context_contribution(reader)` directly with `install_contribution` (no wrapper, mode selector or second registration). `ContextBaseOperations` no longer registers `context_home`, `context_object`, `context_expand`, `context_find` or `context_changes`; its `context_filings`, `context_upload_map`, `context_filing_costs`, `context_instruct` and `context_edit` are unchanged. The reader is also on `BusinessDomains.shared_context` and `documents.shared_context` for the other host read consumers. There is no fallback to the old reader.

| File | Role |
| --- | --- |
| `reader.py` | `SharedContextReader(repository, runtime)` and `for_instance(paths)`: the one read adapter. Methods `status`, `home`, `object`, `source`, `history`, `reference`, `find`, `expand`, `changes`, `count_nodes`, `close`. |
| `graph.py` | `PublishedGraph`: bounded, JSON-plain views of one immutable pinned `Snapshot`. |
| `cursor.py` | Opaque continuation tokens that pin a revision. |
| `schemas.py` | Request bounds and the explicit JSON Schemas (Draft 2020-12) of the five operations' inputs and outputs. |
| `operations.py` | `SharedContextOperations(reader)` and `shared_context_contribution(reader)`: the typed `CatalogContribution` (owner `shared-context`). |
| `text.py` | `render_home(dto)` and `PublishedContextText(reader)`: bounded text of one `context_home` page for human and agent bootstrap, plus the reader's `status`. |

Tests: `app/tests/shared_context_support.py` (synthetic graph builder, no tests), `test_shared_context_reader.py` (reader, 30 cases) and `test_shared_context_operations.py` (host composition and consumers, 20 cases).

## Published ancestry and format

Every revision a read uses, whether the captured published head or one a cursor or `revision` argument names, must equal that head or be its ancestor. This is checked before a cached graph is reused and before an uncached one is built, so a commit that exists in the repository but was never published (an orphan commit, a draft branch, a commit rewound away from the published ref) is a `validation_error` on `cursor` or `revision`, never a ready view; a cached revision that has since left the published ancestry is refused the same way. `status` readiness validates the published head's configuration once per head with the engine's own `config_version` (format 2 or 3 with the published-ref configuration), not merely that a Git head exists; an unsupported format version is `unavailable/unsupported_format` and a malformed or non-matching configuration is `unavailable/invalid_repository`. A format 2 history reads as it is and simply has no notes; the first format 3 publication upgrades it, which the reader never does.

## Construction and the not-ready states

The reader is inert on construction: it validates two absolute, traversal-free, separate paths and touches nothing. `SharedContextReader.for_instance(paths)` uses `InstancePaths.context` and `InstancePaths.context_runtime`. Every read opens existing, initialized state only and never initializes, repairs or creates anything (the runtime directory is never created, the repository is never written; tests compare a file fingerprint before and after).

An answer that is not a graph is a successful result with `status` other than `ready`, `revision: null` and a `reason`; it is never shaped like an empty graph:

| `status` | `reason.code` | Meaning |
| --- | --- | --- |
| `not_initialized` | `repository_missing` | the repository location does not exist |
| `not_initialized` | `repository_empty` | an existing empty directory |
| `not_initialized` | `no_published_revision` | a Git repository with no refs |
| `unavailable` | `path_not_trusted` | a repository or runtime path that is, or passes through, a symbolic link |
| `unavailable` | `not_a_context_repository` | a file, a directory with unrelated files and no `.git`, a Git repository whose root differs from the location, or one with refs but no published ref |
| `unavailable` | `invalid_repository` | a published ref whose tree is not a readable graph |
| `unavailable` | `unsupported_format` | a graph written in a format other than the engine's readable formats (2 and 3) |
| `unavailable` | `runtime_binding_mismatch` / `runtime_binding_invalid` | the repository is bound to another runtime directory, or its binding file is unreadable (an absent binding is fine: published Git objects alone are authoritative) |
| `unavailable` | `git_unavailable`, `search_unavailable`, `reader_closed` | Git could not run; the scratch index could not be built; the reader was closed |

An initialized graph with no published nodes or edges answers `status: ready` with `empty: true`, zero node/edge totals (events can be non-zero after withdrawals) and a real 40-character `revision`. Tests prove that this differs from a missing repository both at the reader and through the Application.

## Responses, pinning and cursors

Every `ready` response names the published `revision` it was read at (the engine's published head, or the revision a cursor pinned). Reads use the pinned engine's `Snapshot` (pure Git objects at one commit): they do not take the engine's writer lock, do not read the pending draft or any run record, write nothing in the Context runtime, and cannot be blocked by, or block, a publication.

Paged reads (`home`, `find`, `expand`, `changes`, the object's history and the source's text) return `page: {limit, returned, has_more, next_cursor}`. A cursor is base64url JSON `{v, b, r, o}`: format version, a canonical hash binding the operation and every non-paging argument, the full Git revision, and the offset (at most 1,000,000). A cursor presented with another operation, other arguments or object, another revision, a different version, extra fields or a malformed or oversized body is a `validation_error` on field `cursor` (or `history_cursor`/`text_cursor`). A well-formed cursor whose revision is not in the published history is also a `validation_error`. A later publication therefore never reorders or drops what a cursor promised, and a fresh read without a cursor sees the new head. The token grants nothing: any registered agent can continue another's page, because the content is the same for all.

The explicit `revision` parameter exists on the reader methods for host-internal use. The operation schemas do not accept one: callers pin only through cursors.

## DTOs

Node, edge, note and source IDs are the engine's native five-character IDs; `ref` is `node:ID`, `edge:ID`, `note:ID` or `source:ID`. A bare ID is unambiguous because engine IDs are unique across nodes, edges, notes, sources and events.

- Compact entity (home, find, expand): `{kind, id, ref, summary, attributes, truncated}`. Summaries are cut at 600 characters, attributes at 40 names, 20 members and 200 characters per text member; `truncated` says a cut happened. The full record is available from `context_object`.
- Relationship: an edge entity plus `from`/`to` endpoints `{kind, id, ref, summary (200 characters), truncated}`, and, when read around a node, `direction` (`out`, `in`, `self`, relative to the node it was reached from) and `depth`.
- Source summary: `{id, ref, title, source_kind, origin, origin_ref, created_at}`. A source entity adds `input_mode`, `withdrawn` and a 300-character `excerpt` of its committed text.
- `context_home`: `{status, revision, empty, totals{nodes, edges, sources, events}, nodes[], page}`. Nodes are ordered by incident-edge count descending, then ID, and each carries `relationship_count` and at most five relationships (`relationships_truncated`).
- `context_object` by node: the full `object` record, `relationship_count`, at most 25 relationships, `support` (the committed events that currently support it, each with commit, event ID, publication time, input mode and source summary), and `history` pages newest first with `before`/`after` stored records, explanation, citations, `support_after` and developments. By edge: the record with endpoints, `support` and `history`. By source: the source record, a page of its committed text (`content.text`, `total_characters`, `next_cursor`), `supports` (the current objects it supports) and `history`.
- `context_find`: literal word/prefix search over published summaries, attributes, note text and committed source text, filtered by `kind` (`node`, `edge`, `note`, `source`) and `attributes` (a note has no attributes, so an attribute filter never returns one); empty query browses. The engine's index returns at most 1,000 hits per query, so a page that reaches that window reports `page.window_exhausted: true`.
- `context_expand`: node only; depth 1-3, `direction` out/in/both; nearest edges first, each edge once, with `total` and a pinned page.
- `context_changes` (owner feed): events newest first with `submitted_by`, source path, operation, up to 25 attributed changes (`change_count`, `changes_truncated`), `handoff_disposition` and the parsed Action envelope (`action_ref`, `revision_ref`, `tags` with their business IDs) when an operational Action produced the event.

**Notes.** A note is a separate record of the published Context (`notes/<id>.json`: exact text and zero or more node/edge subject links), never a graph vertex: it is not in `home` totals or ordering, adjacency, `context_expand` (a note reference is a validation error there) or `count_nodes`, and notes without subjects are ordinary notes.

- Compact note (in `context_find` and a source's `supports`): `{kind: "note", id, ref, text (600 characters), truncated, subject_count}`.
- `context_object` by note (`note:ID` or a bare ID): `object` `{kind, id, ref, text}` with the exact full text, `subjects` `{count, items, truncated}` (at most 50 items, each `{kind, id, ref, status, summary, truncated}` where `status` is `current` or `removed`; a removed subject keeps its reference and has a `null` summary, and no longer opens as an object), `support` (the committed events supporting it) and `history` pages newest first whose `before`/`after` are stored notes `{kind, id, ref, text, objects[{kind, id, ref}]}`. A note read has no backlinks from nodes or edges; they are not listed on the node's or edge's own read.
- `context_changes` entries may list `kind: "note"` changes and `ref` accepts a note reference to scope the feed; `context_object` on a source lists notes beside nodes and edges in its `supports`.
- A history cursor pins its revision exactly as for nodes: a later publication never reorders or drops what was promised.

The schemas in `schemas.py` close every object (`additionalProperties: false`), so a drifting DTO fails the Application's output validation (`invalid_result`) instead of leaking a field. Real responses from the synthetic graph are validated against them in the tests.

## Operations contribution

`shared_context_contribution(reader)` returns a `CatalogContribution(owner="shared-context")` with five `CatalogEntry` values equal to the deployed v1 metadata (the same entries the host catalog already declares) and five read descriptors:

| Name | Exposure | Owner only | Inputs |
| --- | --- | --- | --- |
| `context_home` | mcp, dashboard, worker | no | `limit` 1-60 (30), `cursor` |
| `context_object` | mcp, dashboard, worker | no | `ref`, `history_limit` 1-25 (10), `history_cursor`, `text_limit` 1-64000 (16000), `text_cursor` |
| `context_find` | mcp, dashboard, worker | no | `query` (at most 2000), `kind`, `attributes`, `limit` 1-100 (20), `cursor` |
| `context_expand` | mcp, dashboard, worker | no | `ref`, `depth` 1-3, `direction`, `limit` 1-200 (50), `cursor` |
| `context_changes` | dashboard, worker | yes | `ref`, `limit` 1-100 (20), `cursor` |

All five are `effect_kind="read"`, no Signoff, scope kinds `business` and `global`, `global_capability="read_global_context"`, and v1's namespace resolver (`business:<id>` when a business is selected, otherwise `global`). The handler arguments are only the DTO fields: nothing takes an agent, principal, business or revision from the arguments (unknown fields, including `agent_id`, `business_id`, `principal_id`, `scope` and `revision`, fail input validation). `context_changes` additionally refuses any non-human principal in its handler, as v1 did.

## Authorization evidence (real catalog, host policy resolver and Application)

The operations test module builds the synthetic host-foundation instance (owner, two businesses, a registered business agent each, plus a registered Global agent) through the product host registration (no registry wrapper), initializes the graph at `InstancePaths.context` / `context_runtime` with the engine's own writer API, and calls through `Application.invoke`:

- Two business agents of different businesses receive byte-identical results for `context_home`, `context_find`, `context_object` (node and edge) and `context_expand`, equal to the reader's direct answer and to the owner's and a Global agent's.
- Scoped operational reads stay denied. A published source's `origin_ref` is a plain string (`task:<id>` of a task in the other business); the other business's agent still gets `not_found` from `tasks_get`, `tasks_list` with `scope: global` is `forbidden`, a registered agent claiming another business is refused, and a foreign business task title never appears in a Context response.
- `context_changes` is owner-only: the owner reads it; both business agents (over mcp, dashboard or worker) and a non-owner human member (in their own business and in one they do not belong to) are `forbidden`. None of the change feed's field names (`submitted_by`, `action`, `handoff_disposition`, `path`, `changes`) appear in any agent-facing read.
- A registered agent's scope is derived by the host resolver from its registration: supplying no business, a global capability claim, or another business changes nothing for it, and an unregistered agent or human principal is refused.
- A pending draft (a staged node, source and event) is absent from `home`, `find`, `object`, `expand`, `changes` and source lists; reads leave the repository and runtime byte-identical (size and modification time of every file) and `git status` clean.
- A cursor from one agent continues for another and keeps its revision after the draft is published; the same query with other arguments, a tampered cursor or a forged-revision cursor is a `validation_error`.
- An absent repository answers `not_initialized` through the Application while an initialized empty graph answers `ready` with `empty: true`; nothing is created by the former.

## Design choices that matter to the next step

- **Search does not use `Context.search`.** That method takes the engine's non-blocking writer lock, writes runtime markers and rebuilds the shared runtime index to the requested revision, so an older pinned revision would thrash it and a read could contend with publication. The reader instead builds a process-private scratch index with the engine's own `project` and `SQLiteIndex` for the pinned snapshot (two revisions cached, removed on `close` or when the reader is garbage collected). The Context runtime is never opened for writing.
- **Snapshot cost.** A snapshot parses the whole published tree at one revision (four are cached). Every request also runs a few read-only Git commands to confirm the repository and its head. Cost therefore grows with the graph, not with the page. Large graphs and the cache sizes are the knobs for a later measurement; none was taken here beyond the synthetic 30-spoke hub case.
- **Handoff notes are not read.** The event's handoff note (an agent-to-agent propagation note) is not exposed; only its disposition appears in the owner feed.
- **Published evidence.** History entries expose each change's committed explanation, citations, `support_after` and developments. These are committed Context content, which every registered agent may read under the accepted rules.
- **Action evidence is committed source text.** An accepted Action revision is a source whose committed text is a JSON envelope (`action_ref`, `revision_ref`, `pointers`, `text`, `tags`). Per Manager's review it stays readable as published evidence to every registered agent through `context_find` (excerpt) and `context_object` (text pages); no field is stripped, the structured `action` field of the feed stays owner-only, and dereferencing a raw operational source stays independently authorized. P3 is being corrected to freeze ordinary tag snapshots `{id, name, business_id: null}`; this adapter infers no owning scope or global history from a tag's business and returns whatever the envelope committed.
- **`origin_ref` is data.** It is returned as the string the source recorded and is never resolved or proxied; the protected operational source is only reachable through its own operation and its own authorization.
- **Source withdrawal.** A withdrawn source keeps its summary and history, reports `withdrawn: true`, has no content, and does not count in `totals.sources`.

## Final Context path and the host read consumers

**Path.** *(Superseded by `CONTEXT_COMPOSITION.md`: the published graph is `operations/context-graph`, and Global authored Context sources keep `operations/context/`; the old host consumers listed below are retired there.)* `operations/context` is reserved exclusively for the published graph. The four global authored shared-instruction documents (`agent-foundation.md`, `operating/business-agent.md`, `operating/ecosystem-agent.md`, `ecosystem.md`) are seeded by `bootstrap/seed.py` at `operations/files/instructions/<same relative filename>` and registered with kind `shared_instruction`, namespace `global` and the same `bootstrap_key`; the document row path is `files/instructions/<relative>`. There is no fallback to the old global `context/` location and no data was moved. Per-business `hq/context/{product,icp,brand,positioning}.md` stay. `documents/library.py` `eligible()` now accepts global rows under `files/instructions/` as well as `context/` (the latter still holds the remaining global writers' `context/_people`, `_inbox`, `_dumps`, `_maps`). The synthetic composed host initializes the graph in the empty `InstancePaths.context` after seeding, which the earlier seed collision made impossible.

**Consumers replaced** (all read the same pinned DTO; none reads the old living brief):

| Consumer | Change |
| --- | --- |
| `service/main.py` | `resolver.context_home` and `owner_bootstrap.context_home` are `PublishedContextText(runtime.domains.shared_context).home_text`; `owner_bootstrap.context_status` is its `status`. The `context_orientation` binding is removed. |
| `agents/resolver.py` | No orientation; the authored Product/ICP/Brand documents are not bootstrap sources at all (neither injected nor required); the cache key still includes the home text (a publication changes the effective configuration, as a changed home did before). |
| `agents/context.py` | "About The Owner" and the per-business "Latest:" excerpts are removed. There are no Core Product/ICP/Brand sections: the authored pillar documents are ordinary scoped sources read through their document APIs, not an always-injected brief or a fallback when the graph cannot be read. "Context Home" is the bounded graph text with source `context:published`. The guidance names the published graph and revision and no longer carries the `context_edit`/`context_instruct` maintenance sentence. |
| `agents/briefing.py` | The "Context maintenance" group is removed; "Context base" is "Shared Context". |
| `owner_bootstrap.py` | The one brief is the graph text, `context_home` with source `context:published` (bounded by the character budget); the authored pillar briefs are no longer read, so `briefs` is `[]` when the provider is absent, empty or not ready and `missing` says which (`not_configured`, `unavailable`, `not_initialized`). The pillar documents remain only as registered-metadata rows of `context_index`. The maintenance guidance item is removed. |
| `owner_sessions.py` | Unchanged: it only lists operation names, which are unchanged. |
| `domains/business_briefing.py` | The `brief` section (living brief and notes) is replaced by `context`: `status`, `revision`, `empty`, `totals`, the six most connected nodes as `{ref, summary}` and `more`, from `context_home {"limit": 6}`. A graph that is not ready reports its `status` and `reason` instead. |
| `domains/items/adapters.py` | `context_navigation_get`'s `counts.person` is the published graph's nodes whose `basic_entity_type` is `person` (`reader.count_nodes`, after the existing `context_find` authorization). When the graph is not readable it is `null` (unknown) and there is no fallback to the old people items or document store. The result gains `context = {status, reason, revision}`. Story, table and product counts are unchanged. See the UI contract below. |
| `domains/document_history.py` | The `context_object`/`context_brief` sources of `document_history_get` and `business_history_list source=context` are removed: Context history is the published Git graph's, read with `context_object` and `context_changes`. |
| `domains/contextbase/operations.py` | The five read registrations and their `read()` branches are removed. |

The old `room_limit`/`recent` arguments, `timeline_*` / `attr_history_*` options and `action_kind` are not reinterpreted: the actual callers use `limit`/`cursor` and the published IDs. Source-document reads and the owner diagnostic keep their own handlers' authorization (`context_changes` stays an owner view).

Decisions:

1. The authored Product/ICP/Brand documents are not injected beside the graph and are not a fallback; they stay ordinary scoped sources. Existing tests that required them (`Core Product` sections, `brief:*` in `missing`, the pillar-edit conflict) now assert their absence; the legacy mandatory-source edit tests use the registered global Universal Foundation instead.
2. The people count is `basic_entity_type == "person"` in the published graph; if another definition is wanted, only `HostNavigation._people` changes.
3. The Context sources were removed from `document_history` rather than mapped onto `context_changes`: the cursors, pages and per-business scoping are different contracts and a facade was not wanted.
4. `documents.shared_context` is set as a plain attribute because `DocumentOperations` (outside the allowed files) declares no field for it; `BusinessDomains.shared_context` is the typed handle.
5. The home text has no Markdown heading of its own, so it nests under the host's "Context Home" section.

**Navigation UI contract.** `context_navigation_get` returns

```
{"navigation_only": true,
 "counts": {"story": int, "table": int, "product": int, "person": int | null},
 "context": {"status": "ready" | "not_initialized" | "unavailable", "reason": null | {"code", "message"},
             "revision": <full published revision the count was read at> | null}}
```

`person: 0` means a ready graph that genuinely has no `person` node. `person: null` means unknown: render it as unknown (not zero, not a stale number) and use `context.status` / `context.reason.code` for the explanation (`not_initialized`: no revision is published yet; `unavailable`: the repository cannot be read, or `not_configured` when no reader is installed). The focused cases cover a missing graph (`null`, `not_initialized`), an initialized empty graph (`0`, `ready`), an unreadable repository and an absent reader (`null`, `unavailable`).

## Remaining writer, lifecycle and manifest inputs (not done here)

1. **Old Context writer replacement and its dependents** stay as they were: `ContextBaseService` (built in `service/main.py` and attached to `domains.context_base`), the queue driver (`background/contextbase.py`, `background/integration.py`), `context_instruct` and `context_edit` (deterministic typed edits), filing receipts and costs (`context_filings`, `context_upload_map`, `context_filing_costs`, `domains/costs.py`, `domains/platform_actions/delivery.py`), `ideas.context_base`, `business_setup.context_exists`, and the learning and maintenance writers (`nightly_maintenance...context_base_path`, `maintenance/history_baseline.py`). They write the old store, not the published graph; the registered Context reads no longer show anything they file. The exact replacement of each is the next P4 assignment after the reviewed P3 queue. The ordered queue driver and its source ownership are P4B, documented in `SHARED_CONTEXT_WRITER.md`; the replacements listed here are what remains after it.
2. **Mixed read/writer compatibility in `domains/documents/operations.py`** (outside the allowed files): `read()` serves `context_list`, `context_get`, `context_search`, `context_people_list` and `context_people_get` from the old base through `service.legacy_read` when the filer mode is `base`, and `prepare` for `context_document_update` calls `legacy_read("context_get")` for its content-hash check. These are the document compatibility surface that sits on the old writer's store; they need the narrow separation when the writer is replaced.
3. **Global document writers still target `operations/context/`**: `documents/operations.py` (`context/_people`, `_inbox`, `_dumps`, `_maps`) and `transport/compatibility.py` `context_view` (`path.startswith("context/")`). They conflict with the graph repository location and must move or be retired with the writer.
4. **Backup and versioning**: `storage/recovery.ROOT_AUTHORED` lists `"context"` while `SKIP_DIRECTORIES` skips `.git`, so the nested Context repository's history is not captured by operational backups; the Context repository and its runtime need their own capture (and `files/` already holds the relocated instructions).
5. **Lifecycle**: `SharedContextReader.close()` must be called at service shutdown (no lifetime step does so; `service/lifetime` was out of scope). Nothing initializes the graph: the Context repository is initialized by the later composition or migration step, and until then the reads answer `not_initialized`.
6. **Dependency manifest** (a concrete request for the host/manifest owner): declare the Context engine distribution `qorqut-context` (import `qorqut_context`) at the accepted pin `24f374450b5c62ba0c498dc5ef143c51491ad399` in the host package dependencies. `app/pyproject.toml` still declares only the old `qorqut-contextbase`/`qorqut-learning` URLs in its `contextbase` extra (untouched). The host now imports `qorqut_context` at startup through `bazarlyk.domains`, so it is required, not optional. Checks here used a disposable wheel-equivalent install of the exact pin.
7. **Legacy tests that assert the retired behavior but do not collect in this layout** (they import `bazarlyk.contracts` or `bazarlyk.storage.Database`, which no longer exist, and the real `contextbase` package is not installed): `test_agent_configuration.py`, `test_bootstrap_orientation.py`, `test_business_briefing.py`, `test_service_entrypoint.py`, `test_owner_session_bootstrap.py`, `test_owner_session_mcp.py`, `test_shared_history.py`, `test_context_base.py`, `test_context_retrieval.py` and `test_storage_maintenance.py` (the pre-v1.2 MCP invariance harness and its recorded snapshots were retired in v1.3). Their retired assertions were edited to the new contract by reading, but they could not run here and are not claimed as passing.

## Integration facts and limits

- **No living brief or per-business engine.** There is one published graph and no per-business store. `home` is a node page, not the old brief/room pack.
- **Drafts and typed edits are not part of this reader.** Typed-edit verbs and source-metadata representation were not chosen.
- **Size and concurrency.** Snapshot and scratch-index state are each guarded by a lock, but concurrent use was not stress-tested and there is no measured limit for very large graphs; see *Snapshot cost*. The resolver calls the reader once per configuration resolution (a cached graph, a few read-only Git commands).

## Focused checks run

Environment: a disposable virtual environment (Python 3.12.3) with the exact pinned Context (`24f3744`) and Operations (`138f40c`), each installed from an independent no-hardlinks clone at the pin (the installed package directories are byte-identical to the clones), plus the host's declared dependencies from an existing local wheelhouse (`qorqut-business-docs-versioning` 0.2.0, the `service` extra, `mcp` and `openpyxl`) and an independent clone of the template at `d056f21` (the template submodule directory is empty in this worktree; the tests read it through `QORQUT_TEST_TEMPLATE_ROOT`, or skip when it is empty). Run from `app/` with `QORQUT_TEST_WORKSPACE_ROOT=/absolute/path/to/workspace`:

```
python -m pytest tests/test_shared_context_reader.py tests/test_shared_context_operations.py -p no:cacheprovider
```

Result: 86 passed (30 reader, 20 operations and consumers, 36 host-foundation). The host-foundation cases that seed instructions and create agents through the new path (`test_host_foundation_policy.py`, `test_host_retained_operations.py`, `test_host_neutral_items.py`, `test_host_foundation_paths.py`, `test_host_foundation_transactions.py`) were also run with the template injected. No model, provider or network was used, and no broad suite or installed-foundation gate was re-run.
