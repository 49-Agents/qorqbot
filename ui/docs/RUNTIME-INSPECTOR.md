# Session and native-history observations in the inspector

Manager implementation plan D42, 2026-10-09. Consume the existing read-only
`runtime_get` contract from core `7a7380a4058551ae1163fd180b35b6b74ec6d43b`.
This replaces the inspector's generic Current run placeholder with core's
reported logical-session and collection observations. It does not add launch,
provider/model controls, input readiness or a new backend API.

## Read contract and state

Use the exact fields in core `runtime/models.py` (`RuntimeView`, `CurrentOwner`)
and `runtime/native_models.py` (`NativeSetupView`, `NativeCollectionView`). Add
small frontend wire types plus a focused runtime-view validator; no schema
framework or dependency. Validate UUIDs, nullable UTC timestamps, documented
state/reason enums, positive generations and nonnegative safe counters. The
current input_ready contract is literal false. Reject malformed observations,
a mismatched agent, or an owner whose conversation differs from the selected
agent. An owner recovery_epoch different from Store.epoch enters existing
recovery handling. RuntimeView has no top-level recovery epoch: rely on existing
config/event recovery and request-generation guards for ownerless observations;
do not invent one or infer a connection from it.

Discover runtime_get in the existing schema. Read it only for the selected
agent, on selection and each existing bounded visible-page poll, alongside
conversation and terminal reads. Use a per-read AbortController, generation and
selection guards matching the existing terminal reader. Abort on selection,
logout, expiry and recovery; reset the runtime cache with server-derived state.
No separate timer, stream, all-agent polling or persistent cache. Preserve Chat
drafts, history and selected Chat/Terminal view. A manual Retry may replace an
in-flight runtime read; obsolete responses must never update the current agent.

Store view/loading/error separately. On a failed or invalid response clear the
old view, show an unavailable message with Retry, and keep automatic retries in
the existing poll. A 401 uses normal session expiry; target_changed uses normal
recovery. Read failure must not disconnect a live Terminal or mutate delivery.
While refreshing, any retained view is explicitly a last-reported observation
with its timestamp and a refreshing label, never a fresh liveness assertion.

## Inspector presentation

Add a small Session section and a Native history section in the existing
inspector, replacing Current run. Keep status/activity/attention as the existing
agent fields; runtime_get does not provide working/idle, so do not infer those
states from session existence. Remove the redundant generic Capture row from
Current work; Terminal recording, native collection and dated publication each
have their own authoritative sections. Do not rewrite persisted lifecycle or
status text in the frontend.

Session shows core's last-reported connected/disconnected/unknown status in
plain language, observation time and, when present, provider/version and
connection time from current_owner. Unknown with an owner retains that owner's
identity; it is not permission to replace the session. Put native session/run IDs
and generation in a collapsed details area, not primary labels. When disconnected
say no session is connected; retain the short manual-start-in-Terminal guidance.
Do not display configured Agent.cli/model as a currently running selection.

Native history shows setup and collection observations independently, mapping
existing reasons to short ordinary-language text (waiting for Terminal/session/
source, not configured/unsupported, unavailable, recovery review or stale).
Show source count, bytes awaiting archival and bytes awaiting parsing, plus the
collection observation time when available. Blocked/gap states stay visible;
zero pending bytes must not imply complete coverage. Null observations say no
observation is available. Explain once that collected native history and terminal
recording are separate, and leave dated transcript publication in its existing
section. Neither setup ready nor collection active implies Chat delivery.

When runtime_get is absent, say session information is unavailable on this
server, issue no runtime requests and show no Retry for the absent capability.
Keep monochrome styling, existing inspector drawer/layout, escaped text and
accessible buttons. No browser/file UI or general redesign in this assignment.

## Focused checks

Extend the existing synthetic API with the exact optional runtime response and
add one compact owning Chrome group. Exercise disconnected/connected/unknown,
retained incumbent identity, setup/collection waiting/blocked/gap and counters;
one failed read then successful retry; selection or recovery while a read is
held, proving an obsolete result cannot replace the new agent; absent capability
and malformed/wrong-agent response. Reuse existing test helpers and do not copy
the complete session/Terminal suites. Inspect the new sections on desktop and
in the narrow inspector drawer, with no page exceptions or horizontal overflow.
Run the locked build, the new group and the two existing Terminal groups because
Store polling is shared. Do not call real model providers or rerun native-workflow
harnesses. These checks prove presentation against the fixed schema, not a new
integrated native workflow; Manager will review the exact committed SHA.

Own only the runtime wire declaration/validator, Store read state, inspector
presentation and necessary small CSS, the synthetic fixture/owning test, and
`docs/evidence/runtime-inspector-r1.md`. Preserve Terminal protocol/client/input,
Chat mutation behavior and dependencies. Commit/push a clean candidate with
concise evidence, actual checks, private screenshot paths and honest limits.
