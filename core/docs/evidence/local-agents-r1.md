# Local agents r1 evidence

2026-10-13. Implementation on `codex/fix-local-agents-9552beea`, base
`ac90f188e81e277522650a2e988b4cf50cda9dc5`. Executes Manager decision D62
([LOCAL-AGENTS.md](../LOCAL-AGENTS.md)): ordinary agents are local folders with
their own recorded tmux shell. No delegation or internal reviewers; independent
review and integration belong to Manager.

Everything ran against disposable installations: fresh data roots, private
`HOME`, the test's own tmux sockets, dynamic ports and a private Chrome profile.
No owner session, profile, credential, database or live service was used and no
Codex or Claude process was started or logged in. Native-session fixtures used by
the owner/collector tests are **synthetic** and labeled so in the tests.

## What changed

- Schema 11 (new migration; old migrations and checksums untouched): `agents`
  rebuilt without the single-host index, lifecycle admits `active`, legacy
  ordinary `vm` rows become `host`, one `agent_project` job queued per converted
  agent. The migration is transactional with foreign keys off before `BEGIN`,
  restored on every exit; `legacy_alter_table` is on only around the rebuild
  statements, followed by `foreign_key_check` and `integrity_check`.
- `reserved()` resolves only through `installation.onbash_agent_id`; only
  `ensure_onbash` assigns it.
- Provisioning completes to `active` once the owned workspace and a bound,
  recorder-verified shell are observed, with an attributed lifecycle event; only
  the untouched `Initializing.` status becomes `Shell ready. Start a CLI in
  Terminal to connect Chat.`. Holds and uncertain effects never read as ready.
- Per-agent terminal records and managers, native setup, session owners, input
  dispatch, native collection and attention, all keyed by agent id under the one
  existing monitor thread. A domain or environment failure for one agent never
  aborts the pass; an unexpected error is raised after every agent is visited and
  still stops the observer and readiness.

## Checks

Test runs use the shared read-only test venv with `PYTHONPATH=src:tests`; the
wheel flows use a private venv. Baseline means a pristine copy of the base commit.

| Check | Result |
| --- | --- |
| `agents` (36; new `test_migration` 5, `test_activation` 11) | 35 pass; only the load-flaky concurrent create fails in a full run and passes alone (4/4) |
| `runtime` (91) | OK |
| `foundation` (26), `archive` (21), `integration` (1), `transcripts.test_native_capture` (3) | OK |
| `transports` (38, includes real-tmux `test_local_agents`) | 8 failures/errors, all in the baseline-or-flaky set below |
| `files`, `messaging`, `browser`, `conversation` | only the baseline failures below plus one baseline-flaky browser test |
| Real tmux, from source: `test_local_agents` + `test_terminal` | OK (8) |
| Installed wheel in a private venv (`installed-wheel-run.txt`) | `test_local_agents` OK; package file-identical to the source tree |
| Browser smoke (`browser/`) | two agents created in the real UI, each typed in its own Terminal; files landed in their own workspaces |

`test_local_agents` drives the real API and console process: two ordinary agents
get distinct workspaces, private homes, runtime roots, tmux sockets and shells;
creation is idempotent; the onbash pointer is unchanged; no process runs under
any shell and no provider CLI exists; marker output never crosses agents; reopen
keeps every terminal identity; damaging one agent's terminal store makes only
that agent unavailable and leaves its shell unreplaced.

## Baseline and flaky accounting

Failing identically on the pristine base copy: `files.test_references
.test_saved_entry_publishes_after_restart…`; `conversation.test_native_projection
.test_schema_six…` (extra `browser_request_id` column) and
`conversation.test_publication.test_unexpected_target_symlink…`;
`messaging.test_migration.test_schema_eight_receipts…` (pinned revision lacks the
monorepo `src` path); `transports`: `SharedFlow.test_http_sdk_parity…`,
`Disconnect.test_real_disconnect…` (mcp and http), both `test_chat_publication`
tests (the first now fails at the same later point as base),
`BrowserHTTP.test_origin_identity_legacy_headers_and_bounds` and
`HTTPBoundary.test_declared_chunked…` (an extra `native.attention_changed`
event, timing dependent on both trees).

Load flaky on a host at load average 15–25, passing alone: the registry
concurrent create, `test_worker` restart receipt ("Storage is busy"),
`browser.test_requests.test_done_cancel_race…` (failed 1 of 6 runs on each tree),
and `test_terminal.test_cookie_free_console…`, whose fifth-viewer handshake
timeout belongs to the terminal admission repair reserved to another worker
(failed in 3 of 5 combined runs, then 0 of 9 later runs).

## Limits

Restated from [LOCAL-AGENTS.md](../LOCAL-AGENTS.md): the observer visits agents
sequentially (about 0.5–0.8 s each at load average 25; views older than five
seconds read stale); preparing one shell holds the shared effect gate (first
onbash attach about 3.6 s late at load 19); absolute-path file references and
browser attachment stay onbash-only; local agents share the owner's OS account.
The UI, not editable here, still labels host agents with desktop wording and
keeps the "registered — provisioning" toast after activation.

Screenshots and driver output are kept outside Git under
`/home/alp/qorqbot-launch/qa/2026-10-13/acla-fixes-9552beea/local-agents-evidence/`.
