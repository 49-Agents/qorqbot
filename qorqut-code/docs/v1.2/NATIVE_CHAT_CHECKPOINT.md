# Native Chat checkpoint (P2): native-session Chat backend

Branch `v1.2/native-chat`, built from the foundation-integration parent `7679f287164c0d0cfa109e6415baabb8500160df` (Operations
`138f40c64e457f0b8723b6b764e1905a69e763fc`, dashboard `3c34ab14829e9cdc963c5b3fffbea8ce44e4bf63` unchanged, no submodule moved).
Implements the P2 section of `FEATURE_PORT_DISPATCH.md` for the host (`app/`). Dashboard presentation is the later UI worker's:
the DTO contract is in `NATIVE_CHAT_DTO.md`.

Reused, not recreated: `bazarlyk.native_archive` (capture, receipts, verified reads). The v2.2 donor snapshot writer is not
ported. The Operations engine, Context, Task/Action model, role/template sources and history coverage are untouched.

## What is built (`app/bazarlyk/native_chat/`)

| Module | Role |
| --- | --- |
| `contracts.py` | Public value types: `SessionBinding`, `InputRequest`, `InputResult`, the input types/origins/states, `NativeChatError`. |
| `schema.py` | The typed host metadata contribution (`StorageContribution`, owner `native-chat`) and its DDL. |
| `store.py` | All SQL for the contribution; the transitions and counters are stated once. |
| `probe.py` | Observes the exact registered session: hook marker + `/proc` start-tick identity + tmux pane ancestry (terminal) or the held Codex thread-writer lock (Codex app-server). Starts nothing. |
| `sessions.py` | Durable exact bindings; generation advances on every new process/conversation; ends on `SessionEnd`/process gone. |
| `captures.py` | Backend capture into the archive (stat-gated, bounded batches) and reply indexing with a persistent cursor; final capture at session end. |
| `projection.py` | Provider record rules: entries, provider end-of-turn completions, application-marker provenance. |
| `adapters.py` | `TerminalAdapter` (the verified-composer transport `AgentTerminalBackend.owner_input`) and `CodexQueueAdapter` (`codex queue --thread`). |
| `arbiter.py` | The one input arbiter. `service.py`: the bounded backend thread. `reader.py`/`chat.py`/`http.py`: derived Chat. `transcripts.py`: archive reads for other consumers. |

### Exact registered native-session bindings

A binding is `native_sessions(agent, provider, conversation_id, generation, transport, process pid + start ticks, thread owner,
transcript path)`. It exists only while the probe verifies all of: the registered profile's runtime (`claude-code`/`codex`), the
hook marker for this agent and workspace, the live process by PID **and** start ticks, and for a terminal the single tmux pane
whose process tree contains it (for a Codex app-server, this conversation's held writer lock). `generation` is a host-assigned
per-agent counter independent of `inbox_watchers.generation`. A different conversation or a new
process of the same conversation, including a resumed one, is a new binding (generation + 1); the same verified process and
conversation keeps its generation; the conversation's reply identities and archive lineage are unchanged.

A binding ends only on exact evidence: the client's `SessionEnd` hook marker **from the binding's own native process** (agent,
conversation, runtime, process id and start ticks all equal the binding's — a late hook from an earlier process of the same
conversation proves nothing about the resumed one), or the verified death of the bound process or Codex thread writer, or a
different verified session replacing it. A stale end marker while the bound process is alive neither ends the binding nor
undelivers its waiting inputs.

Capture states are kept apart: a registration with transcript capture **off** is `capture_disabled` (final; never overridden by a
stray marker); capture **on** whose session has not published its transcript marker yet is `capture_not_ready` (retryable).
Chat rejects both with their own code and shows neither a composer nor a conversation until the session is `live`. A third,
separate state follows them: a live, captured session whose exact binding the backend observer has not recorded yet is
`binding_not_ready` (`can_send:false`); a state read never creates the binding. Chat sends are pinned to the binding the page saw
(`expected_binding` is required on `POST chat/messages`): the arbiter checks an exact idempotent repeat before the live session,
so a prior request keeps its receipt after the session ends, and a new request for a pin that is no longer the live session is
`terminal_changed` with nothing stored. Contract details: `NATIVE_CHAT_DTO.md`.

### One input arbiter

`InputArbiter.submit(InputRequest) -> InputResult` is the only way into a host-native session for Chat, Telegram owner input,
broadcasts, terminal-control direct messages and the future inbox nudge.

* Request: `agent_id`, `input_type` (`chat`/`telegram`/`broadcast`/`direct_message`/`inbox_nudge`/`bootstrap_recovery`), `source_key` (producer
  idempotency key), `text`, optional `expected_binding` (pin an exact session), `wait` (durable queue), `steer` (type into a
  verified empty composer while a turn runs; stored with the input so a waiting input keeps it), `sensitive` (never stored),
  `metadata`. Origin is derived from the type: `inbox_nudge` and `bootstrap_recovery` are `system` (no owner principal); `bootstrap_recovery` requires `expected_binding`.
* Result: durable `input_id`, exact `binding`, `origin`, `input_type`, `state` (`waiting`/`submitting`/`submitted`/`uncertain`/
  `failed`/`undelivered`, or `rejected`/`deferred` with a `reason` when nothing was stored/typed), transport `evidence`
  (adapter, client, composer fingerprint, a hash of the terminal identity — never screen text or message text), `marker`.
* Crash boundary: the durable `submitting` claim. From it on an input is never typed again; a crash, restart or doubtful
  outcome is `uncertain`. A deferral after the claim is `failed` with `not_typed:<reason>` and the same producer key may retry.
* A waiting Chat input is typed only into the session it was admitted for; if that session ended or another conversation took
  over it becomes `undelivered`. A different or ended session never receives it.
* System nudge path (for the backend-inbox stream): `arbiter.admit_nudge(agent, source_key, text, metadata=…)` types the text
  behind `<qorqut-system-input id="<random 128-bit>" type="inbox_nudge">…</qorqut-system-input>`; the marker id is returned in
  `InputResult.marker` and stored on the input. Chat attributes an entry as a verified system input only by that stored marker. No
  provider has a system role and no correlation is fabricated: unmarked text is just a user turn. The dispatcher is not built here.
  The owner chose immediate nudge submission while busy: `admit_nudge` requests `steer`, so the nudge is typed at once through the
  same adapter path and checks as every input (verified empty composer, the whole text echoed, one normal submit; processing is
  never confirmed unless the transport evidences it). When that is not safe — an owner's draft in the composer, a modal, an
  unreadable or changing screen, no verified session — nothing is typed: `wait=False` returns `deferred` (never success) and
  `wait=True` keeps it `waiting` for that very session. Chat and Telegram free-form text may go without the whole-text echo when the client collapses a paste; exact-session, empty-composer and post-paste mode checks still precede one Enter.

### Behavior implemented

Switching agents or opening Chat launches nothing; offline Chat is rejected (`terminal_offline`); a busy agent is steered where
the client supports it (Chat opts in) or the message waits for the same conversation; the owner's composer text is never
touched; a definitely-unsent message becomes `undelivered` at session end; an uncertain outcome is never replayed; terminal
control policy and manual lifecycle are unchanged (the launcher typed into an idle shell stays in `execute`).

Capture and reply indexing run in the backend thread whether or not Chat is open.

### Identity, rendered boundary and provenance (correction round)

* **Lineage-scoped ids.** A native id or raw offset is unique only inside one conversation, so every visible entry id and every
  completion id is the digest of `(agent, provider, native conversation, archive lineage id)` plus the record's own identity
  (`projection.Lineage`). `native_replies` is unique on that whole scope; it stores `archive_id` and no longer folds the
  conversation into the raw key. A resumed conversation keeps its lineage and so every id; never a generation, receipt, session or
  page index.
* **Rendered boundary.** `state`/`history` pages carry per-entry `completion_sequence`/`completion_id` (the durable completion
  bound to that exact record: same conversation, record position and length) and a page `rendered_boundary`: the newest completion
  sequence among the completed assistant entries the page actually returns, after projection, pagination and the turn-end rule
  below; a user-only, tool-only or empty page has none. Raw-span membership is not used. `newest_sequence` is informational. The
  read endpoint is unchanged (owner-only, monotonic, bound to an existing completion); clients acknowledge a page's
  `rendered_boundary`.
* **Turn-end-only Codex reply.** Its completion has no assistant record, so the turn-end record yields a derived assistant entry
  (full `last_agent_message` text, `raw_range` identity `<offset>#end`, the lineage-scoped id of its completion). The reader keeps it
  only when the durable index completed exactly that record (`reader.visible`): a window that starts mid-turn cannot know whether
  an assistant message earlier in the turn already completed the reply, so without the durable completion the entry is dropped, never
  shown twice or under an identity the index did not give. A turn end after an emitted candidate/final adds no entry. This stays a
  derived projection; there is no second transcript or message table.
* **Provenance.** Marker ids present in the derived page are resolved against the durable input receipts (unique `marker` index,
  at most one page of ids), restricted to system inputs of the same agent, provider and conversation (and transcript source),
  with the declared type equal to the stored type and the input past its delivery claim. Everything else is an explicitly
  unverified notice with a reason. Replies are completed only by provider
end-of-turn evidence, so narration before a tool call, tool-only records, sidechains, internal notices and owner echoes are never
replies. The owner notification projection (`reply_notifications.py`) reads `native_replies` (`gchat:<reply id>`, business
agents `nav_only`), private item-DM and Telegram parts and the independent attention preference are unchanged. Read state is a
boundary on the agent's completion sequence, owner-only, monotonic and bound to an existing completion.

## Producer inventory (every existing writer into a native client's input)

| Producer | Before | Now |
| --- | --- | --- |
| Chat runner / terminal-owned Chat queue (`global_chat/*`) | independent runner and own queue | **removed**; Chat → arbiter `chat` |
| Telegram owner input (`telegram/owner_input.py`) | `AgentTerminalBackend.owner_input` | arbiter `telegram` (sensitive payloads never stored) |
| Broadcast dispatcher (`transport/terminals.py::input_existing`) | `global_chat.broadcast_input` / `broker.submit_input` | host-native: arbiter `broadcast` with key `broadcast:<id>:<occ>:<recipient>`; sandbox/remote broker target: unchanged `broker.submit_input` |
| Terminal-control `ping` (`agents/terminal_control.py` → `AgentTerminalBackend.execute`) | `broker.submit_input` / chat ping | arbiter `direct_message` with key `terminal-action:<id>`; a definite non-delivery is recorded `failed`, not `uncertain` |
| Terminal-control `start`/`restart` | typed launcher into an idle shell | unchanged (lifecycle, not a native session) |
| Inbox notice, Claude and Codex | `inbox/service.py::deliver_notice` → `service_notice` / `codex queue` | **replaced (P5)**: system input `inbox_nudge` through `arbiter.admit_nudge` (`native_chat/nudges.py`); `deliver_notice` is gone |
| Agent-side Codex inbox watcher (`bootstrap/codex_session.py`) | `codex queue` from the agent's session | **removed (P5)** with the whole agent-side poller |
| BusinessSandbox broker (`sandbox/tmux.py`) | `submit_input`/`queue_notice` | unchanged transport for non-host targets |

`test_native_chat_producers.py` pins this inventory: a new low-level writer anywhere else fails the test.

The inbox notice and the bootstrap recovery wake are no longer special: both are system inputs of the arbiter, typed behind the
application marker, each with its own type (`inbox_nudge`, `bootstrap_recovery`; the recovery wake has `admit_recovery`, needs the exact
conversation/binding and is unrelated to inbox coverage). The hook recognizes the marker (`native_chat.projection.leading_marker`), not the notice text, so a notice
never counts as owner input. See `docs/v1.2/INBOX_NUDGES.md`.

## Removed / rewired

* Removed: `bazarlyk/global_chat/` (runner, launcher, legacy mirror, correlation, store, terminal observer, http), `tools/global_chat/service.py`
  (the runner's release recipe), the `chat` extra / `claude-agent-sdk` pin, `QORQUT_GLOBAL_CHAT` branches in the agent hooks, the
  chat-managed branches of `AgentTerminalBackend`, `reply_notifications.snapshot_replies`, and the hook-side flat transcript copy
  (`bootstrap/transcript_snapshot.py::snapshot`; the hook module still publishes the session marker). The retired runner tests,
  `docs/GLOBAL-AGENT-CHAT.md` and `docs/TERMINAL-OWNED-CHAT.md` are removed.
* Transcript consumers now read the archive: `LocalFiles.session_names/session_bytes/session_events/iter_transcripts`
  (`service/local.py`) serve the agent pane's session list/viewer and the learning programs from the committed, verified
  archive prefix (complete records only, 8 MiB bound as before). The learning source dict keeps its shape; `source_id` is now
  `native-archive/<agent>/<provider>/<conversation>`.
* HTTP: `GET/POST /api/root/agents/<uuid>/chat…` is the new `native_chat/http.py`. Stop/actions/runtime endpoints are gone.

## Schema / contribution inputs (for the schema, history-coverage and integration owners)

`composition/assembly.py::host_contributions` registers `native_chat.schema.storage_contribution()` (owner `native-chat`). The
generated `composition/schema_ddl.py`/`tables.py` are **not** edited. Seven STRICT tables, forward-only/append-only triggers
(no deletes anywhere; counters, session identity, capture cursors forward-only; inputs follow a transition guard; replies and
input events immutable; read boundaries monotonic and bound to an existing completion):

`native_agent_counters`, `native_sessions`, `native_captures`, `native_inputs` (+ unique partial index on `marker`),
`native_input_events` (append-only), `native_replies` (immutable; carries `archive_id`, unique on agent + provider + conversation +
archive lineage + identity), `native_reads`. `native_inputs` carries an immutable `steer` flag.

History-coverage inputs: `native_inputs.body`/`metadata_json`/`evidence_json` can hold owner message text (body only until
delivery; never for sensitive payloads) — treat as private payloads, not exportable by blanket dump. Replies, sessions,
captures, counters and reads hold ids, hashes, offsets, sequences and a ≤180-character preview (the preview is agent text).
Transcript bytes remain only in the archive (separate ownership). The private item-DM exclusion is unchanged.

## Dependency requests and open items

1. **Schema owner / retained-domain collector:** the seven dead `global-chat` tables (`global_chat_*`) and the `global-chat`
   group in `composition/schema_ddl.py` and `tables.py` are no longer read or written; remove them with the next coordinated
   schema edit. (The numbered v1 migration chain no longer exists in v1.3.)
2. **Final history coverage owner:** classify the seven `native_*` tables (above).
3. **Backend-inbox stream (done in P5):** `deliver_notice` and the agent-side Codex watcher are replaced by `arbiter.admit_nudge`;
   the hook recognizes the marker; `InputResult.marker` is the stored provenance id.
4. **Dashboard:** adopt `NATIVE_CHAT_DTO.md` (entries have no owner/system origin of their own; `inputs[]` carries delivery state).
5. **Release/installation:** nothing was deployed. The retired `qorqut-global-chat.service` unit (if installed) must be removed by
   the release process; the installed-host probe stubs `native_chat` instead of `global_chat`.
6. The host-assigned monotonic session generation is approved; explicit capture-disabled registrations stay disabled and Chat
   distinguishes `capture_disabled` from `capture_not_ready`; the `deliver_notice` rewrite and the removal of its
   hook-recognizer/poller flow are one change. That retained bypass has no permission to remain in the final system.
   Sandbox/remote input adapters stay a separately identified retained transport; nothing here extends host-native support
   claims to them.

## Limits

* Codex completion rules (final-answer phase, turn-end candidate, `last_agent_message`) follow the Codex shapes in the existing v1
  tests; no real Codex transcript fixture exists, and no real Claude or Codex client was run (synthetic panes and transcripts).
* Chat needs transcript capture on and a host-native session; sandbox/remote targets and capture-off agents have no Chat (other
  producers still reach a verified host terminal). A pre-existing archive-less history is not imported.
* The first indexing of an existing transcript makes the newest historic reply per agent unread, as the previous snapshot did.
* Archive reads verify the committed prefix each call (cached per receipt in-process); very long conversations page with a
  bounded window.
* Broadcasts to host-native agents are tightened: they need a verified native session and a ready composer (previously any attached mirror).
* Owner deletion of a transcript, cross-agent search and the dashboard are out of scope.
* Legacy tests that cannot be collected on this baseline (`bazarlyk.contracts` is gone) and mention the removed paths were only
  adjusted textually where they named removed symbols: `test_dual_cli.py`, `test_codex_cli_contract.py`,
  `test_global_learning_real_chats.py`, `test_reply_notifications.py`.

## Focused evidence (synthetic only)

`tests/test_native_chat_*.py` with `tests/native_chat_support.py` (a real assembled host over temporary data, a stateful fake
tmux pane that writes the user record on Enter, a fake `codex queue`, real archive): submission for Claude and Codex, stale pinned
session / session change before the first input, busy-steer vs defer, preserved composer text, uncertain-never-replayed,
crash-after-claim recovery, deferral-after-claim retry, offline rejection, undelivered at session end and on conversation change,
capture-off and capture-not-ready, system-marker provenance and lookalikes (an old nudge beyond 100 later inputs, a marker copied
into another conversation, a never-delivered input's marker, a relabelled type), conversations sharing a raw offset or a reused
provider item id, per-page rendered boundaries (older page / newer reply / older conversation / user-only page / a reply the index
has not reached), the turn-end-only Codex reply as a visible full-text entry, its deduplication against an earlier final answer
including a bounded page that starts at the turn end, a late `SessionEnd` from an
earlier process, a busy nudge and an occupied-composer deferral, closed-Chat capture → eligible reply → read → restart, stable identity on
resume, split/partial/sidechain/notice exclusions, concurrent newer reply vs read boundary, owner-only and business ownership,
archive growth/final capture/integrity, paging, transcript consumers, HTTP/DTO shape, the producer inventory, Telegram/broadcast/
ping rewiring, and the pure provider record rules.

## Telegram delivery reconciliation

The backend capture loop reconciles non-sensitive uncertain Telegram inputs even with Chat closed.
Only an exact native user entry in the bound agent/provider/conversation and transcript path,
after admission and before the bound session ended, proves arrival. Assistant quotations, tool
results, partial matches and missing timestamps do not. The bounded scan also checks already
archived evidence after an upgrade or restart. Confirmation appends evidence to the immutable native transport outcome, confirms the Telegram
producer receipt, removes the duplicate pending body, and records the archive receipt and offset.
Chat presents confirmed arrival as submitted while exposing the original transport state separately.
It does not claim processing, resend input, send a Telegram notice, or acknowledge inbox work.
Sensitive inputs cannot be reconciled by retained body because no such body is stored.
