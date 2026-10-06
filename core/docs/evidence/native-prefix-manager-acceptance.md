# Native-prefix Manager acceptance

Accepted candidate: `c4ac16d6fe1963774026f43a74b5fa491b5c5f89`, based on
`617a12f1db3f0026108b9e679766eea50d67dd3d`. Review date: 2026-10-08 UTC.

The fresh independent Astra MAX reviewer found no in-scope defect and passed
9 prefix tests plus 9 unchanged spool tests from source, then the same 9 prefix
tests from its independently built installed wheel. Its package/origin evidence
and scope were inspected; these are the reviewer's runs, not Manager reruns.

Manager read D32 and the complete production diff, verified the four-file scope,
clean builder/reviewer worktrees, pushed candidate and unchanged old spool AST.
Both changed production files matched the independent wheel and installation.
Three Manager-authored source diagnostics passed on the exact candidate:

- Actual ACK/reclaim/reopen followed by full-prefix/tail hash verification,
  unchanged borrowed descriptor position and unchanged SQL state.
- Positive short reads outside SQL with a real second-connection append;
  the proof retained its originally frozen target.
- Held-unlinked descriptor verification followed by an earlier-byte rewrite
  with unchanged tail; mismatch/non-boundary refusals preserved capture health.

The Manager command was Python 3.12 with `-I -B` executing the private
`manager-prefix-r1-checks.py` and loading only this candidate's source. Temporary
fixtures were removed and descriptors/connections closed. The private run
directory retains that script, result and `manager-prefix-r1-acceptance.json`.
No product failure, test correction or extra suite rerun occurred.

Accepted only the read-only receipt view and finite borrowed-file comparison.
No native ownership, durable checkpoint, ingestion, source reconstruction, ACK,
runtime readiness, deployment or whole G0–G6 acceptance follows.
