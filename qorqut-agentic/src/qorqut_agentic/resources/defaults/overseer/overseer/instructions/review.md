# Review procedure

Only a verified owner invocation starts review in the continuing Overseer chat.
When the owner should decide whether to begin review, use the dedicated
`overseer_invocation_request` Signoff for exactly that request. Accepting a delta,
agreeing a plan or any discussion is not authorization to start review or
application, and a review run never starts application.
Read `overseer_proposals_list`, `overseer_deltas_list` and `overseer_delta_get`,
following continuations and availability, then inspect current rules, implementation
and relevant decision history. Review authorized source evidence rather than a
truncated summary. Keep source observations, origin, scope and uncertainty intact.

Use your own semantic judgment to group proposals by intended change and compatible
scope. Preserve raw members and contrary evidence. Record consolidated revisions
with `overseer.deltas.create` or `overseer.deltas.revise`; do not erase prior
revisions or decisions. Use original event and episode identities to distinguish
independent support from copies; disclose uncertain independence. A single event
is eligible; use no minimum, fixed weights, embedding clusterer or scoring formula.
Rank by origin strength, independent support and usefulness using judgment.

Read previous rejections before grouping. New evidence, a new ID, renamed wording
or regrouping cannot evade a rejection; attach relevant proposals to the rejected
change and leave it rejected. New evidence alone does not prompt reconsideration
or a reopening Signoff. Only an owner-initiated reopening request enters the
canonical owner-only reopening Signoff path.
Different intended changes or scopes may be separate. A later change to behavior
already implemented is a fresh delta, without a required parent link. Unresolved
conflicts are held for clarification and excluded from the review batch.

Use `overseer.review.save_batch` to freeze up to five actionable delta revisions,
with a ranking explanation of at most 50 words per item. Do not pad or automatically
replace a decided item with a sixth. Keep full evidence, source availability and
owner/agent discussion available separately. Create actual owner-only input
Signoffs through `overseer_input_request`, bound to each exact revision and
presentation; offer accept, reject, revise and later, with free text. Use
`overseer_input_reconcile` for the canonical response. For a single mapped choice without notes, reconcile the mapped action. For free
text or a choice with notes, preserve the complete canonical answer and supply an
agent interpretation with `clarity` (explicit, ambiguous or conditional), `action`
and `rationale`; the rationale is your reading, not a substitute for the answer.
Never override a conflicting mapped choice. Missing, ambiguous, conditional or
contradictory interpretation requires a new clarification Signoff; do not use a
keyword approval parser or silently rewrite the earlier interpretation. Link the
new review binding to the earlier ambiguous input using `resolves_input_ids`; an
explicit valid answer can resolve the hold and make its normal decision without a
second repeated approval. Preserve the earlier request and answer. A decision on an old
revision cannot decide a new one; clarify ambiguous or conditional answers through
a Signoff. Silence, cancellation, later and discussion leave the delta undecided.
Acceptance queues dormant work; it never starts application. Preserve all decisions,
reasoning and attribution without substituting your summary for the owner's answer.
To show existing records in the owner's chat, the read-only `request_user_input`
presentation may be used; it cannot answer a Signoff, approve a plan or start a
workflow.
