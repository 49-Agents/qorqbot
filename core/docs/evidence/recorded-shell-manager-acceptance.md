# Recorded shell: Manager acceptance

Accepted on 2026-10-08: `395d8315399bd9e10bba7220425ef9b46f16d05c`, based on
`f4506c705f77f9c1b6b1575f1905b95c77b7c27f`, under D36/D37 and amendment 163.
The five added implementation, test and evidence files are integrated unchanged.
Existing accepted source, dependencies, host migrations and Manager plans remain.

This accepts a local persistent Bash shell recorded through the existing PTY
recorder, plus disposable bounded terminal attachments. Launch intent and exact
specification are durable before launch. Recovery inspects the same attempt;
unknown or stopped execution does not authorize another launch. A live recorder
probe and exact socket/process/session/pane/child identities corroborate execution.
The terminal capture producer is distinct from any native agent session.

All tmux viewers are physically read-only. Human input and resizing use a separate
gate with durable viewer, control epoch and sequence checks. A consumed uncertain
input cannot be replayed. Viewer disconnect retains the human hold; takeover
cannot retract bytes already admitted. Manager/viewer close leaves execution
alive; explicit shutdown targets only the corroborated recorded session.

Fresh independent Astra MAX review 180 found no in-scope defects. It verified
all 171 candidate files against Git and the unchanged 166 base files, then
passed seven owning source checks and two focused installed checks. The installed
flows covered actual recording/reattachment and observer/control/takeover/resize/
exit. Its syscall trace verified the actual recorder/helper imports from its
separate installed package. All 59 package Python/NOTICE files and 31 dependency
pins matched; no fixture processes or paths remained.

Manager independently read all three production additions and owning tests,
including the launch-intent/claims, process recovery, live probe, input gate and
shutdown paths. The builder and review worktrees were clean at the exact candidate,
and the remote builder branch matched. Manager compared all 59 package files
across candidate Git, the independent wheel and its installed package before
running two further focused installed checks with an exact copied test file:

- Concurrent manager processes converge and exit; the same shell and an owned
  background child survive viewer disconnect/reconnect, with continuing capture.
- An actual input effect followed by lost reply consumes its sequence and cannot
  replay; capture-loop stall and recorder death close subsequent input admission.

Both passed in 3.451 seconds without skips. All loaded core modules came from the
verified installed package, all 31 lock pins matched, and the owned short
temporary root was empty and removed. Manager commands, test bytes, package/import
provenance, results and cleanup are retained in `manager-shell180/` in the recorded
build run. The independent review bundle is `review-recorded-shell-r1-piwlr_kn/`.
Its wheel SHA-256 is
`d12ea34a5cc698af7d645b89722ac3871b78871ef83739b0202539f5c7b7f470`.
Reviewer and builder approvals are separate ACLA messages 182 and 183.

This is local process/viewer restart evidence, not host activation, native
startup/inbox association, WebSocket/Terminal UI, automated Chat delivery, native
assistant ingestion/publication, service-cgroup/reboot survival, VM or browser
acceptance. Raw terminal recording is not a complete semantic transcript archive.
No provider/native CLI calls, owner data, live Qorqut sessions or global services
were used. Nothing was deployed and no full G0–G6 gate advanced.
