# Owner-invoked application

If the verified owner invocation explicitly selected an `application_id`, bind
that accepted work directly; otherwise select it through an owner-only input
Signoff. Selection is separate from plan agreement. Inspect its exact accepted
revision and current implementation. If already satisfied, retain concrete
evidence and use the owner-confirmed close-satisfied input; do not fabricate a commit.
Jointly save a plan identifying managed files/entries, checks, expected base and any
external development-task dependencies. The plan Signoff binds the exact plan; if
intent or scope changed, it may explicitly approve the displayed new delta revision
and plan together while preserving earlier decisions. All requested input uses
owner-only Signoffs and canonical responses. For an ambiguous plan answer, bind a
new plan question to the exact current targets and link the old hold with
`resolves_input_ids`; a valid explicit answer both resolves that hold and performs
the normal plan transition without a second repeated approval.

Prepare a separate branch and worktree with isolated state. Implement only the
agreed agentic changes; validate and check them, preserve concurrent active edits,
and prepare one squashed commit with the delta ID. Publish through the agentic
application toolkit to the active branch and existing changefeed; this is not a
repository push or package publication. Do not rewrite unrelated history. Run the
agreed quick checks against the actual merged revision. Record acceptance, merge,
check receipts and refresh separately. On failed post-merge checks retain the merge,
report failure and request the owner's next action through a Signoff; do not
implicitly repair, revert or merge twice.

Application/native-extension source changes outside the managed agentic layer become
linked development tasks under their ordinary workflow. A task's creation is not
proof of implementation; leave dependencies incomplete until delivered and checked.
An external-only delta has no fabricated agentic commit. Never push, deploy or
publish repositories as part of this workflow.
