# Compact current-work updates

Write one short activity or blocker per item, aiming for about 50 characters
(roughly 6–10 words) across `summary` and `blocked_reason` together. This target
applies to session work and permanent-worker assignments. Do not repeat a blocker
in both fields. Keep IDs in reference fields and details in the linked Task or
working memory. Preserve essential meaning instead of using cryptic abbreviations.
The existing validation ceilings remain unchanged; they are not writing targets.

Examples: "Preparing six SF messages", "Waiting for message approvals", or
"Contact sync blocked; repair assigned". Remove completed items rather than turning
the current-work list into a progress history. Review and update timing is unchanged.

`agents_activity_list(session_id=<your native session UUID>)` is a focused read
for an agent's own session. It returns the current `work_revision`, canonical
visible work items and parallel `item_keys`. The exact-read `items` use the
same fields accepted by the legacy full-list write. The read still filters
items whose business or task is no longer readable. In that case, `items_complete` and
`items_replacement_safe` are false; do not use the returned items as a full-list
replacement. A focused read does not expose another agent's session, and it
does not change the normal team activity listing.

`agent_work_update` keeps its existing full-list form for compatibility. It also
accepts a small `patch` array when only some items changed:

```json
{
  "session_id": "<your native session UUID>",
  "expected_work_revision": 4,
  "patch": [
    {"action": "replace", "item_key": "<key from the focused read>",
     "item": {"business_id": "<business UUID>", "task_id": "<task ID>",
              "summary": "Verify the handoff"}},
    {"action": "add", "item": {"business_id": null,
                                  "summary": "Review the Global handoff"}}
  ]
}
```

Use `remove` with only the current `item_key` to remove an item. `add` requires
an `item`; `replace` requires both the key and new item. Patch only keys returned
for that session revision. Keys are opaque, revision-local handles, not
authorization tokens. Each write still needs the current `expected_work_revision`.
A stale revision must be reread and reassessed. The service applies each patch
to the stored list, preserving unrelated and filtered items. It rejects an
unknown key, a duplicate task link, more than ten resulting items, or an item
whose current scope/task access is unavailable. Task references are pinned to
their current task revision and authorization is checked again when applying.

An empty `patch` confirms review without changing declared progress. The
existing `items: []` form still clears all current work; use full replacement
only when the focused read says `items_replacement_safe: true` or when you have
the complete current list from another authoritative source.

Runtime presence and last-seen timestamps remain automatic. These operations
record model-declared current work; task outcomes and meaningful progress belong
in task comments or the task's ordinary workflow.
