# Host message dispatch: Manager acceptance

2026-10-09 UTC. Candidate `61a68358599becb72ea317b9f805fb05491ca67a`,
parent implementation `e2e13b96333cc740a9bd0cce10e1ed618b22e901`, implements
[D46](../HOST-MESSAGE-DISPATCH.md) and Manager amendment 299. Assigned base was
`e74369c0e214570f4a6892650f3ae59e3689787c`. Manager reviewed the full production
diff, new owning tests, schema transition and the builder's two evidence records:
[original scope](host-message-dispatch-r1.md) and
[raw-observation amendment](host-message-dispatch-raw-observation-r1.md).
The branch was clean and its remote matched the candidate.

The code stores immutable messages and linked attempt identities before external
input, shares the recorded Terminal control gate and delegates only the 51-byte
inbox notification to the existing runner. It records receipt observations
separately from handling, and preserves uncertainty when original receipts are
missing or mismatched. Idle-hook sequencing limits each run to one notification
per admitted idle event. Inspection does not create a missing runner store.
The private raw observation remains specific to an exact capture binding;
public parser/coverage gaps remain visible.

Manager independently ran, from the candidate with disposable fixtures:

- `python -m unittest messaging.test_dispatch messaging.test_migration runtime.test_input_dispatch messaging.test_transports -v`:
  **32 passed in 66.085s**. This covers host crash boundaries, uncertain/deferred
  receipts, exact body preservation, FIFO/idle sequencing, input guards, original
  receipt inspection after native exit, schema-8 upgrade/restore holds and shared
  HTTP/official-MCP typed attempts.
- `python -m unittest runner.test_shell.ShellTests.test_mapped_automation_and_shell_human_control_share_thread_and_process_gate -v`:
  **1 passed in 2.587s**, exercising actual isolated tmux/Bash thread/process
  serialization and shared human control.

Commands, environment overrides, original logs and results are in
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manager-review301/`.
The existing private pinned dependency environment was reused with the candidate
source/tests on PYTHONPATH. These are source checks, not an installed native-CLI
acceptance. Provider/composer/recorder and D48 observation proofs in the new input
fixtures are explicitly synthetic. The tmux check establishes its local boundary.

Migration 9, 17 public operations and five handlers are accepted. The independent
D48 collector method is absent from this candidate, so production preparation
conservatively refuses `capture_unavailable` until the reviewed pair is combined.
Actual pinned Claude/Codex Chat delivery remains the next connected-flow gate;
echo association, shutdown drain, child coverage, browser/VM integration and the
complete product remain outstanding. No owner session, paid inference, VM,
browser profile, global service or deployment was used. Manager integration adds
only the already committed planning/audit documents and this acceptance record.
