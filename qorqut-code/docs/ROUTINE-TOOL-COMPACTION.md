# Routine tool compaction

The MCP defaults now avoid sending reviewed Signoff proposals back after a
decision. Successful
`signoffs_reject`, `signoffs_input_cancel` and
`signoffs_input_answer_delegated` calls return the identity, proposal/status
revisions and decision/execution states. Approval can mean queued, not executed.
Execution errors, apply results and decision-message references remain visible;
they may exceed the normal 200–300-character receipt. `signoffs_get` retains
decision evidence and `response_detail=full` retains canonical write results.
Replaying a write with full detail uses the original idempotency key. HTTP/domain
results, authorization, approvals and persisted receipts are unchanged.

`focus_get(item_id=..., response_detail="summary")` reads one card's framing,
revision and typed source/reference without expanded source representations.
Use it for an explicit card-only change. Source decisions and completion claims
still need their evidence. Summary plus `conversation=true` is rejected, and
unavailable cards stay redacted. Board pagination is unchanged.

The [current-work API](AGENT-WORK-COMPACT.md) selects an exact own session and
patches individual items under its work revision. It supports both task-linked
items and owner conversations without echoing the complete current-work list.
Empty patches confirm review; unrelated or unreadable commitments stay intact.

`tasks_overview` includes each task's revision. An explicit due/tag/status edit
can reuse an existing overview or task-list row when that row contains the
required fields. Missing details, completion decisions and revision conflicts
still need a read. Merely knowing an ID or title is not completion evidence.

Startup inventory returns 16-character opaque continuation handles in the
existing `next_cursor` field. Pass them unchanged as `inventory_after`, keeping
`inventory_limit` fixed. The process-local cache stores positions, never response
bodies; handles bind agent, current scopes and page size. Its fixed lifetime is
15 minutes, with at most 4,096 entries. Replay is supported without extending the
lifetime. On `inventory_cursor_expired` after expiry, eviction or service restart,
restart without the cursor. Legacy base64 cursors remain accepted. These are
live pages, not a database snapshot, and every page still checks visibility.
The cache fits the current single-process service; a multi-process deployment
would need shared cursor storage or request affinity before using these handles.

Inbox consumers remain explicit: the present MCP connection is not proven to
identify the exact native watcher/lease. Dropping `consumer_id` would guess which
lease owns the events. Existing bounded pagination and bulk acknowledgement
remain available; a read never acknowledges an event. Notes, context documents,
message bodies and Signoff evidence reads are not shortened by this change.

## Checks

New focused checks cover short Signoff receipts, execution/failure preservation,
full-detail idempotent replay through MCP, selected-card summary/redaction and
conversation reads, cursor scope/limit/replay/expiry/capacity behavior,
overview-revision edits and stale conflicts, and exact-session work patches.
Previously accepted suites were not rerun. Skill frontmatter validation covers
the changed role, cleanup and owner-focus guidance.

## Release boundary

No migration is needed. Existing full-list work updates and legacy inventory
cursors remain compatible. Deploy core before publishing/adopting Ordubashy
1.37.2. Refresh MCP discovery and selected agent preparations for the new
selectors, schemas and shared role/cleanup guidance; do not change unrelated
pins, grants or sessions. Core-owned guidance benefits all prepared roles;
the companion owner-focus change is Ordubashy-specific. Merge, deployment,
publication, adoption and preparation remain separate release selections.
This source work has not activated any live change.
