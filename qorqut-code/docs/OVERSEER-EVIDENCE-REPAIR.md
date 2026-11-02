# Overseer evidence read dependencies

The Learning Applier role needs `agents_get`, `tasks_get`, `tasks_thread_get`,
and `actions_thread_get` in addition to the existing Action and Signoff reads.
The evidence adapter checks its current `read_agent_experience` admission and
the ordinary source operation policy. The capability alone does not grant those
operations. Role version 1.15.1 supplies the missing read defaults; existing
profile overrides, project scope and source visibility remain authoritative.
Publishing source alone does not change an existing agent's pinned role.

Episode reads first use private captured relation metadata to find potentially
connected records. They then authorize those sources, exclude Overseer's own
work, and rebuild the graph using only permitted records before returning any
content. Incoming links still connect an episode, but a denied intermediate
episode cannot expose records beyond it. Unrelated frontier records are not
re-read on every episode request. No shared authorization cache is introduced;
revocations are checked on subsequent reads, including saved frontiers.

Existing source identities, cursors, capture receipts and analysis coverage are
unchanged. A saved extraction run can continue; do not start a replacement run,
reset coverage, invent provenance or mark pending sources analyzed to clear an
error. Legacy history/provenance gaps remain visible. This change does not claim
to resolve unrelated database contention or guarantee that every large evidence
request completes within its transport budget.

Validation uses the actual role defaults, without the fixture-only allow
overrides that previously hid the missing policies. Focused tests cover task and
transcript access, explicit denial, unrelated episodes, incoming relations,
denied bridges, source revocation and existing recovery/coverage behavior.
