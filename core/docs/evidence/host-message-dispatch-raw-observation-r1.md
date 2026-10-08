# D46 raw observation amendment

Implements Manager amendment message 299, received through the bound ACLA inbox,
and the amended D46 plan at
`601d42bf288a7713eb75d7b0df07f451091494c9` (2026-10-09). This follows D46 commit
`e2e13b96333cc740a9bd0cce10e1ed618b22e901` on
`codex/build-host-message-dispatch-r1`. The exact revision SHA and pushed status
are reported through ACLA. The original
[D46 evidence](host-message-dispatch-r1.md) remains unchanged; this record
supersedes only its aggregate native-capture prerequisite.

The Manager's fixed amendment requires D46 to consume D48's private
`NativeCollector.input_observation(run_id)` result, check `healthy`, the exact
full `transcripts.spool.RunBinding`, and `observed_at >= owner.connected_at`.
D48 alone owns the frozen observation and its memory-only accessor, including
five-second monotonic source/observation freshness. Aggregate parser/coverage
warnings must remain visible and must not be substituted for raw capture proof.
Until that method exists, D46 must refuse `capture_unavailable`. All other
dispatch requirements and acceptance limits remain unchanged.

## Changes

`runtime/input_dispatch.py` now requests that exact incumbent run's private
observation. It requires an exact-True healthy flag, all seven capture binding
fields and a timestamp at or after connection. An absent/non-callable method or
absent observation refuses input. There is no fallback to the aggregate view
and no module-load import of the not-yet-present D48 class. The caller relies on
the accessor's contracted monotonic freshness rather than inferring freshness
from wall-clock age. It still performs its own terminal recorder/health,
process/ancestry, accepted hook, primary host archive link, control/composer and
final callback checks. Receipt inspection is unchanged.

The owning fixture supplies a frozen `SyntheticRawObservation` with the exact
four-field D48 contract. It does not implement collector polling or inspect an
unfinished D48 branch. Focused cases check healthy matching raw evidence despite
both public parser and capture-coverage gaps, with pending bytes and unchanged
public observations; absent method, absent/unhealthy/non-boolean health;
pre-connection observation; every mismatched capture binding field; and a
fresh aggregate view that cannot replace an expired private observation (the
accessor returns None). Existing long-idle-hook, primary-link, final proof-loss,
native-exit receipt and other owning checks remain.

Only the input composition module, its owning test module and this new evidence
file change in this revision. `native_collector.py`, its implementation/tests,
schema, Application/Jobs, the dispatcher and historical evidence are unchanged.

## Validation and limits

Private validation root `V`:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-host-message-dispatch-r1-validation`.
Working directory:
`/home/alp/qorqut-core/worktrees/build-host-message-dispatch-r1`.
The original private Python 3.12 environment and unchanged dependencies were
reused. The focused command is:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests "$V/venv/bin/python" -m unittest \
  messaging.test_dispatch messaging.test_migration messaging.test_messages \
  messaging.test_transports runtime.test_input_dispatch -v
```

39 tests passed in 72.273 seconds. Original output: `$V/amendment-299.log`.
SHA-256: `cf120106c551a7d53beb5352b50273c0ea741633afe12dc905c22d3238b30797`.
`git diff --check` passed. This reruns the preexisting D46 input, dispatch,
recovery, migration and transport checks along with the amended cases.

Provider/recorder/composer/raw observation evidence remains explicitly synthetic;
the real disposable ancestry and existing isolated tmux evidence have the same
limits recorded in the original D46 evidence. These tests do not establish the
separately owned D48 collector's freshness implementation or actual native Chat
acceptance. The current branch deliberately refuses input until the D48 accessor
is integrated. Manager must integrate the pair before real Claude/Codex
acceptance. No D48 source implementation, unfinished actor changes, paid
inference, owner sessions, deployment or merge was accessed or performed.
