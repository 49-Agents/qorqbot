# Unresolved learning supersession

This extends the existing DeepSeek comparison in `learnings.extract`. It does
not introduce another engine, provider, routine or approval agent. The comparison
can reconcile newly extracted findings with older findings, or compare the
pending queue when there are no changed sources to extract.

## Activation

`learnings_extract` accepts `supersession_mode`:

- Omitted: preserve existing reconciliation behavior, including reversible
  same-day consolidation. No new automatic rejection behavior is enabled.
- `preview`: record proposed cross-day replacements on the surviving finding,
  with eligibility, model assessments and the original evidence. Do not reject
  the older finding. This is a normal extraction job, not a read-only operation;
  new findings and legacy duplicate consolidation may still be recorded.
- `apply`: automatically reject eligible older unresolved findings with feedback
  `Superseded by <replacement ID>: <reason>`. Direct invocation of this mode is
  owner-only. An owner-configured routine can use the same argument. No live
  routine configuration is changed by installing this code.

Start with preview and review its suggestions before enabling apply. A later
apply run can compare already-pending findings without resetting scan cursors or
re-extracting transcripts. Repeated identical preview relations are not appended.
Comparisons retain the current 200,000-character input bound and shared job cost
budget; if comparison cannot run, candidates remain available and the run records
the error. This is not exhaustive retrieval over an arbitrarily large queue.

## Safety and history

Only owner-supported, strictly later source-event evidence may supersede a
finding. Creation/extraction timestamps are not chronological evidence. The
model must assess the same subject, complete and explicit resolution, and a
resolved replacement state, with exact quote snippets from both findings.
Partial, pending and uncertain replacements cannot trigger automatic rejection.
Questions and uncertainty tagged as facts can both be resolved. Explicit complete
replacements of old plans/rules also qualify. Matching a name alone is not enough:
business, contract, time period and conditions must agree. Scope and observed
agent/role boundaries are checked in code.

Semantic assessments remain fallible model judgments; the gates are not proof
that two contracts or claims are equivalent. Ambiguity belongs in preview/review.
Accepted/rejected or previously owner-reviewed findings, work/filing in progress,
conflicts, and ambiguous resolution chains are protected. Candidate revisions and
both endpoints are checked again when the frozen batch is prepared for writing.

No finding is deleted and the replacement is not approved. The older finding
stays in the normal Rejected queue with `superseded_by`, structured assessment,
source references and decision history. The owner can reopen it normally or use
the replacement's existing Keep both operation. Restored findings are protected
from further automatic reconciliation. No migration or agent role grants change.

## Verification

Focused tests cover cross-day uncertainties, preview/apply, replaying a pending
queue without new extraction, protected decisions, source chronology, scope,
partial assessments, persistence and restoration in Global and business scopes.
`tests/test_learning_supersession_provider.py` is opt-in real DeepSeek execution
with synthetic contract examples and no live queue writes.

The first four-call provider check incorrectly superseded a partial payment.
The response contract now independently classifies replacement state, and code
rejects partial/pending/uncertain states even when other model flags say complete.
This failure is retained here rather than treating the test as a blind accuracy
estimate. Real-provider examples do not justify enabling apply without inspecting
actual queue previews.

After the added state gate, all four real-provider cases passed: full payment
qualified; partial payment, another contract and a promise did not. A separate
read-only DeepSeek preview of 58 actual unresolved Global findings proposed no
relationships. Nothing was applied to the production queue. That sample currently
provides no positive real supersession example; the positive case remains synthetic.
