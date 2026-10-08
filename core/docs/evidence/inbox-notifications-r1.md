# D43 inbox notification and input-layout implementation

2026-10-09 UTC. Implemented against the fixed [D43 decision](../INBOX-NOTIFICATIONS.md)
from base `0fc5ffbc71cc0036f81967e34e9b670846e1fc6e`, on
`codex/build-inbox-notifications-r1`. This is message lookup, preparation text
and pure layout evidence; actual automated delivery remains unimplemented.

`messaging.notifications.notification_text(UUID)` returns exactly the 51 ASCII
bytes of `[Qorqut inbox <canonical-lowercase-UUID>]`, without a newline or message
content. `messages_list(message_id=...)` validates the exact ID, suppresses the
implicit self-agent filter, and intersects explicit agent/thread/cursor/unhandled
filters. Existing pagination and recovery-epoch rules remain in force.

New messages containing valid UTF-8 controls retain their exact bodies and receive
the normal pending/runtime-unavailable delivery and dependency-held job. The
64-KiB storage bound remains unchanged. A disposable database written by the
actual pre-D43 source verifies that historical `unsafe_terminal_text` failed
deliveries and immutable send results replay unchanged, including after recovery
activation; no delivery job is recreated for those old failures.

Both manual and earlier managed preparation paths now generate the same exact-ID
lookup, saved sender/thread/body/handling, duplicate-work avoidance and explicit
acknowledgment instructions. Connection failure remains distinct from an explicit
session ownership conflict. Refresh checks preserve authored files and native
configuration, including upgrades from owned instruction blocks without this text.

`adapters.input_layout.classify_input` and `matches_input` inspect only supplied
snapshots for Claude 2.1.287 and Codex 0.160.0. Checks cover bottom-relative geometry,
exact row counts, cursor and mode flags, full-width Claude rules, native prompt
styling, provider footers and the bottom eight rows' busy/dialog text. Only SGR
and terminated OSC-8 delimiters are removed for plain comparison; raw SGR remains
part of placeholder/cursor proof. Unknown escapes and unexpected controls refuse
the layout. Exact matching is limited to nonempty printable ASCII that fits with
a spare cursor cell. Neither callable grants paste permission or changes runtime
readiness. Legacy offline `classify` behavior remains unchanged.

## Focused verification

All **32 focused tests passed**, with no skips. Tests used disposable data and
processes; the HTTP/MCP check starts only the existing synthetic local server.
No actual native CLI, provider inference, VM or browser was run.

Commands below use `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests` and Python
3.12.3 (`-B`) from the existing read-only dependency environment:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manual-native-codex-r1/venv/bin/python`.
Product code was imported from this worktree, not that environment's installed
core package. No dependencies were added or changed.

| Command arguments after Python `-B` | Passed | Private log |
| --- | ---: | --- |
| `-m unittest messaging.test_messages messaging.test_transports -v` | 8 | `messages.log` |
| `-m unittest discover -s tests/adapters -p test_adapters.py -k PreparationTests -v` | 13 | `preparation.log` |
| `-m unittest runtime.test_native_setup.Preparation conversation.test_publication.Publication.test_canonical_full_entries_both_agents_onbash_dates_and_saved_observation conversation.test_publication.Publication.test_restore_holds_pending_receipt_delivery_and_new_job_for_same_partition -v` | 3 | `manual-publication.log` |
| `-m unittest discover -s tests/adapters -p test_adapters.py -k composer -v` | 2 | `legacy-composer.log` |
| `-m unittest discover -s tests/adapters -p test_adapters.py -k InputLayoutTests -v` | 6 groups | `layout-final.log` |

The message/transport checks include exact control-body HTTP/MCP retrieval, Chat
and dated JSONL equality, shared schema exposure, exact ID/intersection/handling,
replay, and unchanged historical failures. The layout groups use small sanitized
lower-composer fixtures at 80×24, 100×30, 120×40 and 500×250. They cover both positive
profiles, exact notification mismatch, drafts, busy/dialog text, earlier output,
OSC-8 BEL/ST delimiters, malformed escape/control/geometry/cursor/row count,
unsupported versions, true pane flags, dim/concealed prompt prefixes and refused
multiline/collapsed layouts. `git diff --check` also passed.

The initial layout run had three assertion failures: it demanded `unknown` for
a Claude draft with an incorrect cursor, while classification already refused it
as `blocked`. The test now requires refusal in that case, retaining `unknown`
assertions for malformed empty layouts. The failed log is retained privately as
`layout-initial.log`; subsequent layout checks passed.

## Original saved-screen replay

Original read-only root:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/explore-native-input-r1`.
The original [observations](native-input-observations-r1.md) and
[Manager acceptance](native-input-manager-acceptance.md) remain unchanged.

The private driver replayed all **48** original
`{claude,codex}/evidence/*/{screen.sgr,observation.json,flags.raw}` sets. It verified
the saved screen SHA-256 and agreement of flags/cursor, retained original bytes,
dimensions, cursor and `pane_in_mode`, and rechecked all three source-file hashes
after each read. It compared only the independently saved `payloads/short.utf8`
for short-input matching; no native transcript was copied or committed.

Per Manager decision 286, every replay record has `layout_only=true`,
`unobserved_flags=["input_off","synchronized"]` and
`assumed_flags={"input_off":false,"synchronized":false}`. The original observations
did not record these two flags. **All replay matches are conditional on those
explicit assumptions**, not proof of terminal admission or live readiness.
Disposable tests independently verify that each true flag refuses input.

| Original screen names | Claude | Codex |
| --- | --- | --- |
| `draft-cleared`, `idle-100x30`, `idle-80x24`, `idle-restored-100x30`, `large-before`, `large-post`, `medium-before`, `medium-post`, `short-before`, `short-post` | Empty layout | Empty layout |
| `idle-settled-100x30` | Empty layout | No such original capture |
| `short-busy` | Busy; refused | Busy; refused |
| `human-draft`, consent/dialog/shell/exit screens, `medium-pasted`, `large-pasted` | Refused | Refused |
| `short-pasted` | Nonempty; exact saved short payload matches | Nonempty; exact saved short payload matches |

There are 21 empty-layout observations and 27 refused empty classifications.
Only each provider's `short-pasted` screen matches its original short payload.
All 48 reject the synthetic notification when compared to those original screens.
The per-screen verdicts, reasons, original hashes and assumptions are retained in
`original-replay.json`; `original-replay.log` lists every screen by verdict.

Private checks and the replay driver belong to:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-inbox-notifications-r1`.

There is no dispatch loop, attempt persistence/hash wiring, schema migration,
Application assembly, native ownership, runner input-gate, UI or parser-correlation
change here. `native_verified`, `permits_paste` and public `input_ready` remain
false. Future delivery must hash the notification bytes separately from the
message and combine actual fresh flags, identity, owner, hook, capture and human
control checks. A later normal-server flow must prove actual delivery separately.
