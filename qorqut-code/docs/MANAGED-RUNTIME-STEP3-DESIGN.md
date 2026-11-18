# Step 3 (Claude side): managed headless runtime, implementation design

Paths are relative to `qorqut-code/app/qorqut/` unless they start with `../`. `../` means `qorqut-code/`. I read the plan including Step 0 results and traced the code. I ran nothing and edited nothing.

## 0. Shape of the design

- **Identity.** A managed session is one `native_sessions` row with `transport='managed'`.
  - `conversation_id` is Claude's session id.
  - `process_pid` and `process_started` are the headless `claude` process, the same process the hooks see as their `comm=claude` ancestor.
  - `owner` is `host:<host_pid>:<host_started>`. It is an existing nullable column, so no new column is needed.
  - `Selection.holds` and `same_session` need small managed branches.
- **Process model.** One detached host process per open session (`managed_runtime/host.py`). It owns `ClaudeSession` from the other agent's `claude_driver.py`, a control socket, and an event log.
- **No second path.** The service never drives claude directly. `ManagedAdapter` talks to the host, and the arbiter stays the only input path.
- **Binding source.** For managed agents the probe derives identity from the verified host record instead of a tmux pane. The existing `terminal-chat.json` marker is still read, and the hooks still run for inbox binding.

## 1. Per-agent runtime switch

**Where agent config lives today (all unsuitable for the switch):**
- `agent_profiles.profile_json`, with `runtime` = `claude-code`|`codex` meaning which CLI.
  - Schema: `agents/models.py:44-62`. It has `additionalProperties: False`, and is also validated in `qorqut-operations/.../global_agents.py` and `agents/global_scope.py:30`.
  - Mirrored to the workspace `agent.yaml` at `bootstrap/preparation.py:548`.
  - A new key would need schema edits in two packages, a profile revision bump and a re-prepare. It is portable authority, not a host-local setting.
- `.qorqut/registration.json` (`preparation.py:556`) is regenerated on every prepare.
- `native_session_owners.desired_state` is selection intent, not runtime choice.

**Decision: host-local file, no schema change.**
- Location and format: `<paths.runtime>/managed-agents/<agent_id>/mode.json` = `{"schema_version":1,"mode":"managed","changed_at":…}`. A missing file means `terminal`.
- This follows the `<runtime>/agent-browsers/<agent>/` precedent (`browser/agent_browsers.py:~75`).
- Writes use `bootstrap/client.py:167 write_json_private`, which is atomic and private. Reads are cached by mtime.
- New module `managed_runtime/mode.py` with `mode_of(paths, agent_id)` and `set_mode(paths, agent_id, mode)`.

**How it is read:**
- `native_chat/store.py:48-54 _profile(row, paths)` adds `result["execution"] = mode_of(paths, agent_id)` when `paths` is given. Every consumer that already receives `profile` sees it: the arbiter at `arbiter.py:109`, `SessionProbe.observe`, the backend pass at `service.py:95`, and `chat.py:_target`.
- Callers that pass no `paths`, such as `domains/owner_inputs.py:~320`, get the default `terminal`. They only read archives.

**How it is set:**
- New route `POST /api/root/agents/<id>/chat/runtime` `{mode}` in `native_chat/http.py`, next to `select` at `http.py:133`. It is owner-only like the rest.
- Preconditions:
  - profile role is `global-worker` (`agents/roles.py:10`);
  - profile `runtime == claude-code`;
  - no live native session (probe not `live` and `registry.current` is `None`);
  - no waiting inputs;
  - no tmux session present.
- Switching back to `terminal` requires the host to be closed. Otherwise the route returns `409 session_open`.
- The switch bumps `store.bump(agent)`.

## 2. The `managed` adapter

**What the arbiter calls today:**
- `adapter.submit(profile, session, text, *, steer, echo, checkpoint, replace_composer=False) -> OwnerInputOutcome(status, reason, receipt)`, at `arbiter.py:244-246`.
- `adapter.name` is used as `evidence.adapter` (`arbiter.py:255`).
- The adapter must call `checkpoint()` exactly once, after its last pre-input check and before its first write. A raise inside it means nothing was sent (`adapters.py:1-8`).
- The adapter is chosen by `self.adapters.get(session["transport"])` (`arbiter.py:236`). Registration is at `chat.py:96` (the `adapters or {...}` map).
- Arbiter semantics:
  - `deferred` becomes `failed not_typed:<reason>` for an attempt (`arbiter.py:264`), or requeues for Chat (`requeue=True`, `arbiter.py:262`).
  - `uncertain` is never replayed.
  - Exceptions: `DomainError/OSError/ValueError/KeyError/RuntimeError` become `uncertain` after the claim (`arbiter.py:249-252`).
- Producer flags (`InputRequest`, `contracts.py:101`):
  - `steer=True`: Chat (`chat.py:375`), nudge (`arbiter.py:339`), recovery (`arbiter.py:348`).
  - `steer=False`: Telegram, broadcast and direct messages. These are idle-only today.
  - `echo` and `replace_composer` are meaningless for managed and are ignored.

**`ManagedAdapter` in `native_chat/adapters.py`, `name = "managed"`:**
1. `handle = managed.handle(agent_id)`. If it is `None` or the host fails verification, return `DEFERRED session_closed`.
2. `hello`/`status`:
   - identity not equal to `session` (conversation, claude pid or ticks, or host identity) gives `DEFERRED stale_session`;
   - `phase=starting` gives `DEFERRED session_starting`;
   - `phase in (working, blocked)` and `not steer` gives `DEFERRED turn_running`. This is the equivalent of today's `allow_busy=steer`. Telegram, broadcast and direct messages are therefore deferred while the agent works, and Telegram's 2s to 30s backoff retries them.
3. `checkpoint()`. A raise gives `DEFERRED checkpoint_failed`.
4. `message_uuid = uuid4()`. Call host `send{text, message_uuid, steer}` (10s timeout). The driver stamps `{"type":"user","uuid":…,"origin":{"kind":"human"},…}` on every message, `/role` included. Otherwise `projection.py:408-413` drops mid-turn messages: the `queued_command` attachment is accepted only with `origin.kind=="human"`.
5. Host reply handling:
   - `ok` with `mode:"started"`, `turn_started` seen within 3s: `SUBMITTED processing_confirmed`.
   - `ok` with `mode:"started"`, not confirmed: `SUBMITTED processing_not_confirmed`.
   - `ok` with `mode:"queued"` (a mid-turn send): `SUBMITTED queued_mid_turn`.
   - `rejected` before any write to claude: `DEFERRED <session_closing|turn_running|host_busy>`.
   - Receipt is `{"client":"claude","transport":"managed","message_uuid":…,"queued":bool}`.
6. Timeout or socket loss after `send` was written.
   - Call `lookup{message_uuid, fence:true}` once, with a 2s timeout. The host records the uuid as "never accept" if it has not seen it.
   - If the host says `seen`: `SUBMITTED accepted_by_host`.
   - If the host says `unseen` and fenced: `DEFERRED host_never_received`.
   - If `lookup` also fails: `UNCERTAIN host_timeout` or `UNCERTAIN host_connection_lost`.
   - This resolves most uncertain outcomes that paste-and-verify could not.
7. Failure codes:
   - `FAILED unsupported_runtime` if the profile runtime is not claude-code.
   - `FAILED driver_unavailable` if the host reports the driver or CLI could not start.
8. Text exactly `/exit` is not sent. The adapter calls host `close{graceful}` and returns `SUBMITTED session_closing`.

**Reason-code mapping** (terminal reasons from `transport/agent_terminal_control.py:472-646`, Codex ones from `native_chat/codex_control.py:95-132`):

| Terminal / Codex | Managed |
|---|---|
| `terminal_unavailable`, `terminal_absent`, `terminal_unverified`, `terminal_unsupported`, `codex_session_closed` | `session_closed` (no verified host), `host_unverified` (wrong identity) |
| `target_busy`, `composer_busy`, `composer_modal`, `composer_not_empty` | `turn_running` (only when `not steer`), `host_busy` (the host's command lock is held) |
| `screen_changing`, `paste_failed`, `submit_interrupts` | no equivalent |
| `checkpoint_failed`, `stale_session` | same |
| `paste_unverified`, `submit_failed`, `submit_unconfirmed`, `session_changed` (uncertain) | `host_timeout`, `host_connection_lost`, `session_changed` |
| `processing_not_confirmed` | `processing_confirmed` or `processing_not_confirmed` |
| `unsupported_runtime` | same |

**No session open (the "terminal absent" equivalent):**
- `arbiter.py:175-181 _unavailable` maps `REASON_PROCESS_GONE` and `REASON_NO_MARKER` to `"terminal_offline"`. For a managed agent it returns the new code `session_closed`, which is `deferred` for an attempt.
- Make `_unavailable` an instance method that reads `profile["execution"]`, or pass a flag.
- `nudges.py:44-46 UNAVAILABLE_REASONS` must not include it. A closed session stays in the "offline" steady verdict (`nudges.py:57 STEADY`, `step` at `nudges.py:~390`) and the nudge waits for an owner-opened session.
- Telegram (`../extensions/telegram/qorqut_telegram/owner_input.py:30-35, 238-265`):
  - add `"session_closed"` to `DEFERRALS` (an identity map);
  - add a `NOTICE_REASONS["session_closed"]` text such as "its session is not open; sending a message in Chat opens it";
  - the queued-notice text at `:256-258` says "typed into the terminal when the composer is free" and needs a managed wording.

**Hook-blocked prompt.**
- The input row is already `submitted`. The host event `prompt_blocked{message_uuid, reason}` reaches the backend pass.
- New `store.annotate_input(conn, id, reason, evidence_patch)`: compare-and-set on state `submitted`, reason `prompt_blocked`, evidence `{prompt_blocked:{reason}}`, one `record_input_event`, one `bump`.
  - Do not reuse `settle_input`. It would increment `user_sequence` twice (`store.py:340-342`).
- `input_view` (`store.py:413`) exposes it, and the UI shows "Not processed: blocked (reason)".
- Correlation uses `json_extract(evidence_json,'$.message_uuid')`.

**Arbiter extras (`arbiter.py`):**
- Generalize the check at `arbiter.py:130-133`: Chat on `transport=='managed'` whose first word is a CLI-menu-only command (`/model` bare, `/resume`, `/cost`, `/usage`, `/status`, `/context`, `/permissions`, `/mcp`, `/memory`, `/agents`, `/config`, `/doctor`, `/help`, `/clear`) is refused with `rejected managed_command` before any row.
  - `/model <name>` is a candidate for a later driver `set_model`.
- `commands.delivery_text` (`commands.py:151`, trailing space to close the completion menu) is terminal-only and is not applied.
- The managed catalog (`catalog()` at `commands.py:104`, `http.py:91`) marks those commands `where:"unavailable"`. `/compact`, custom commands and skills stay `chat`. `/exit` maps to close.
- The `TERMINAL_COMMAND` message in `_REFUSALS` (`chat.py:64`) is Codex-specific and needs a managed variant.

**Uncertain outcomes.**
- `store.recover_claimed` (`store.py:372`) turns `submitting` rows into `uncertain service_restarted`. That stays: the host dedupes by uuid, but the arbiter's claim rule is unchanged.
- `delivery_reconciliation.py` confirms by text equality. Do not rely on matching the `queued_command` attachment uuid; it is unverified.

## 3. The host process

**State directory** `<runtime>/managed-agents/<agent_id>/` (0700):
- `mode.json`
- `host.lock`: held with `flock` for the host's whole life, like `supervisor.lock` (`browser/agent_supervisor.py`)
- `host.json`: schema_version, agent_id, instance, `host_pid`, `host_identity` (`boot_id:ticks`, via `browser/control.py:process_identity`), protocol, state (`starting|ready|closing|closed|failed`), `session_id`, `transcript_path`, `claude_pid`, `claude_identity`, `last_activity_at`, `exit{code,reason}`
- `token` (0600)
- `control.sock`
- `events.jsonl` (bounded; monotonic `seq`)
- `sent.jsonl` (the uuid ledger plus fenced uuids)
- `host.log`
- `launch.json` (transient, 0600)

If the full socket path exceeds 104 or 108 bytes, use the `short_temporary`/`socket_fits` pattern (`browser/agent_supervisor.py`) and record the real path in `host.json`.

**Spawn:**
- `ManagedRuntime.ensure_open(profile)` runs under a non-blocking per-agent lifecycle lock (the `agent_browsers.lifecycle_lock` pattern).
- It prepares the workspace through the same path as `transport/terminals.py:163-190` (`_prepare`, then `execution.environment_resolver`, then `agent_cli_launch.session_environment`, which holds `QORQBOT_MANAGED_CLAUDE` and PATH). Reuse `TerminalService._prepare`.
- It writes `launch.json`:
  - `executable = agent_cli_launch.executable("claude","claude")`;
  - `args = for_executable(...)["args"] + ["--settings", <ws>/.qorqut/claude-launch-settings.json]` (written at `preparation.py:566`, ecosystem scope gives `bypassPermissions`);
  - the `claude_code` system-prompt preset (Step 0);
  - the full resolved env, **never in argv or `--setenv`**, because it carries private role secrets from `service/local.py:107`;
  - `resume_session_id`, `send_role`, `idle_seconds` (default 900), the token, and the socket path.
- Launchers follow the `AgentBrowserManager` rule (`browser/agent_browsers.py`, constructor around `:495`):
  - folder installs and macOS: `subprocess.Popen([sys.executable,"-B","-m","qorqut.managed_runtime.host","--state",DIR], start_new_session=True, stdin=DEVNULL, stdout/stderr=host.log)`, as in `browser/native_launcher.py:62`;
  - Linux service installs: transient `systemd-run --user --unit=qorqut-managed-<instance>-<agent>.service --collect`, as in `agent_browsers.UserUnitLauncher.start` (`:~420`).
  - `start_new_session` alone does not survive a service restart on a systemd service (cgroup kill), so the transient unit is needed there. Reuse `outside_private_mounts`.
- The host takes `host.lock` (a second copy exits 75), reads and unlinks `launch.json`, binds the socket, and starts `ClaudeSession`.
- On `session_started` it sets `state=ready` and records `session_id` and `transcript_path`. A fresh session then gets `/role` through the host's own `send` (not an arbiter input, matching the terminal flow at `agent_terminal_control.py:~441`); a resume does not.
- Exit codes follow the supervisor convention: 0 stopped, 1 failed, 75 busy, 78 bad config.

**Control channel:**
- Unix-domain stream socket carrying NDJSON requests and responses, `{id, op, token, ...}`.
- Ops: `hello`, `status`, `send`, `lookup`, `interrupt`, `answer`, `close{graceful|now}`, `events{since}`.
- Each connection is checked on the host with `SO_PEERCRED` uid, plus the token. On the service side, peer pid equals `host_pid`. macOS: `LOCAL_PEERPID`.
- `status` returns `{phase: starting|idle|working|blocked|closing, activity, turn_started_at, pending:[{request_id,kind:permission|question,tool,input,questions}], last_turn_status, session identity}`.
- `pending` is state, not events. After a service restart it is re-read from `status`.

**Find and verify after restart:**
- `ManagedRuntime.start()` spawns nothing. It scans `managed-agents/*/`.
- A directory is "running" only if all of these hold:
  1. `host.lock` is held (a non-blocking flock fails);
  2. `process_identity(host_pid)` equals `host.json`, and `/proc/<pid>/cmdline` contains `qorqut.managed_runtime.host --state <dir>`;
  3. the peer pid of the socket equals `host_pid`;
  4. `hello` with the token returns the same agent and instance, and `claude_pid` descends from `host_pid` (the ancestor walk, 24 hops, as in `probe.py:244-249`).
- Anything else is treated as absent. A stale directory is cleaned. Nothing is killed by name or port.

**Idle close:**
- No active turn, no pending request, and no send for `idle_seconds`.
- The host sets `closing`, rejects new `send` as `session_closing`, calls `driver.close()`, and waits up to 20s for `exited`.
- Claude's `SessionEnd` hook then lets the registry end the binding (`sessions.py:147`).
- The host then escalates (terminate, then kill the group), writes `closed`, releases the lock and exits 0.

**Resume:**
- `resume_session_id` is `selection.selected(agent).conversation_id` when the provider is claude and the capture's `transcript_path` still exists.
- If resume fails, the manager reports `resume_failed`. It never silently starts a fresh conversation, which mirrors the terminal rule at `agent_terminal_control.py:~432`.
- Optional: `new_conversation:true` on `POST /messages` for an explicit fresh start.

**Shutdown:**
- A service stop does not touch hosts. `ManagedRuntime.stop()` only closes sockets.
- `close_all(reason)` is used by the owner's Close, the switch back to terminal, and the terminal-control `kill` for a managed agent.
- Add a managed leg to recovery quiescence next to `service/main.py:85-86` (`HostTerminalWriters`/`LegacyHostWriters`, `storage/host_writers.py:50-112`). It refuses with `recovery_not_quiescent` while any verified host runs. Otherwise a backup or recovery runs under live claude writers.

## 4. Changes in probe, sessions, liveness and every pane assumption

**`native_chat/probe.py`:**
- `_verify_process` (`:222-250`) runs tmux `list-panes`. A managed claude gets `None`, then `REASON_PROCESS_GONE`, so it never binds. Add a branch before `:234`: `if self._managed and (hit := self._managed.verify(profile, marker)): return hit`. It returns `{"transport":"managed","owner":"host:<pid>:<ticks>"}` and requires the host to be verified and the marker pid to be a descendant of it. Terminal agents are untouched (the hit is `None`, so the tmux path continues).
- `Observation` (`:54-68`) gets defaulted fields `activity`, `phase`, `pending`. `working` also comes from the host `phase`, alongside the hook activity file (`_working`, `:271-282`).
- Optional hardening: if the marker lacks `transcript_path`, take the path from the host status and verify it with `transcript_matches` (`:91`).
- Constructor takes `managed=None`. `NativeChat.build` (`chat.py:81-105`) passes it. Nothing else changes.

**`native_chat/sessions.py`:**
- `reconcile` (`:74-124`) binds only when `chosen.holds(observation)` (`:80-84`). `Selection.holds` (`selection.py:74-82`) requires `observation.owner is None` for a terminal-owned conversation. Add: `if observation.transport == "managed": return True` after the pid and started check.
- `_ended`/`session_gone` (`:46-50, :143-152`) use liveness and need no edit.
- `store.same_session` (`store.py:153-158`) already compares transport and owner. A resumed conversation under a new host is a new generation, which is correct.

**`native_chat/liveness.py`:**
- `of_session` (`:73-84`): `managed` returns the worse of `state(process_pid, process_started)` and `state(host_pid, host_ticks)`, with the host parsed from `owner`. Only verifiably `dead` ends the binding (the three-state rule at `:6-12`). The claude process alone being alive is not enough: an orphan whose host is gone cannot be driven.

**Schema and migration:**
- `native_chat/schema.py:59` has `CHECK(transport IN ('terminal','codex_queue'))`; widen it to include `'managed'`.
- `native_chat/contracts.py:20` `TRANSPORTS` gets `'managed'`.
- SQLite cannot alter a CHECK. Precedent exists at `migration/signoff_side_chat.py` (an in-place catalog replace; `CHANGED`).
- Add `migration/native_transport_managed.py` with `NAME`, `CHANGED={('table','native_sessions')}`, `OBJECTS=frozenset()`, `strip/forward/backward/held_rows`, and append it to `TRANSFORMS` at `migration/upgrade.py:~62`. A release then needs the offline upgrade step.
- Alternative if the migration is unwanted: keep `transport='terminal'` and encode managed as an `owner` prefix. Every raw `row["transport"]` reader (`arbiter.py:130,236`, `attachment.py:229`, `nudges.py:230`, `chat.py:155`) would then need a helper. I recommend the explicit `'managed'` value.

**Hook side.** Without these two edits a managed agent never gets an inbox binding, and its MCP calls are unbound:
- `bootstrap/native_binding.py:14-55 attest` rejects any claude with `-p`, `--print` or `--agent-id` in argv (`:36-37`). Then no `native-binding.json`, no `snapshot_transcript` (`bootstrap/claude_session.py:88-97` runs it only when `native`), and `bind_native` (`transport/http.py:~1412`) fails.
- Fix: allow it when `managed_host(workspace, pid)` is true. The host writes `<workspace>/.qorqut/managed-host.json` `{host_id, host_pid, host_ticks}`, and the helper checks that the claude pid has that host as an ancestor with matching ticks and a `qorqut.managed_runtime.host` cmdline. `claude -p` delegated workers (`bootstrap/delegated_runtime.py:181`) have no such ancestor and stay excluded.
- Write `managed_host` into the observation value. Trust remains with the service-side probe.
- `NativeBindingAdapter.verify` (`native_binding.py:167-185`) runs `PROBE` (`:59-110`), which asserts `pane and ancestry==pane` and asks tmux `list-panes`. Add a `MANAGED_PROBE` used when a host record exists: pid alive, `comm` is `claude`, and the ancestor chain reaches the recorded host. The `-p` argv assertion is waived in that script too.
- `bootstrap/bridge_binding.py:28` rejects `-p`. Apply the same `managed_host` allowance, or the MCP bridge sends no `X-Qorqut-Native-Session` or generation and inbox tools are unbound.
- `chat.py:464` (`select`) compares `latest.owner or f"{pid}:{started}"` with the binding owner. For managed, compare only pid and started.
- `chat.py:398 follow_terminal` and `chat.py:416 select` work unchanged once the above holds.

**Other places that assume a pane or a terminal:**

| Place | What breaks if untouched |
|---|---|
| `native_chat/chat.py:362 send` | requires `expected_binding` (`:370`), and Chat refuses without a live session (`arbiter._unavailable` → `terminal_offline`). A closed managed agent can never be opened. |
| `chat.py:128-180 session_view` | the label is hard-coded "Terminal"/"Codex" (`:155`), the detail says "Start or resume the agent in its terminal" (`:174`), and `launch_commands` (`:180`) tells the owner to run `claude`. |
| `chat.py:324 upload` | refuses with "Start the agent in Terminal" (`:334`) when the session is closed. |
| `native_chat/attachment.py:177-230` | calls `backend.pane_observation` and reports "not in terminal" or mismatch for a managed agent. It needs an early `UNSUPPORTED/managed` view. |
| `transport/terminals.py:163 _open_admitted`, `:270 reconnect_selected` | would open a tmux shell in a managed agent's workspace, so a second client could run beside the host. Refuse with `managed_agent`. |
| `transport/agent_terminal_control.py:106 snapshot`, `:281 validate`, `:373 _execute` | `start` and `restart` would launch `claude` in tmux and violate "only owner Chat opens". `kill` finds no tmux session. `ping` validation needs `state=='present'` and `command==client` (`:281-293`). Behaviour: `start` and `restart` refused, `kill` maps to manager `close`, `snapshot` returns a synthetic managed snapshot. |
| `agents/terminal_control.py:64 queue_message` | calls `self.backend.validate('ping', binding['terminal'])`, which fails for a managed agent, so direct messages from `inbox/messages.py:96-99` fail. Same synthetic snapshot fixes it. The actual send already goes through the arbiter (`_ping` at `agent_terminal_control.py:540`). |
| Broadcasts, `transport/terminals.py:229-310 input_existing` | `_native_input` already uses the arbiter and maps results, so closed managed gives `unavailable`. OK, but the later tmux fallback must not run for managed. |
| Telegram | see §2. |
| Nudges, `native_chat/nudges.py` and `agents/attention_delivery.py:534` | work via the arbiter. Add `session_closed` to the deferred class. Recovery wake (`nudges.py:462-482`) is pinned to the live session and works. Do not set `desired_state` from Stop. |
| Role bootstrap | the host sends `/role` itself. `terminal_control._execute` (`:~441`) is bypassed. The `QORQBOT_ROLE_SENT` shell wrapper (`preparation.py:294`) is irrelevant to managed. |
| `agent_trust.py` | not called for managed. `terminal_control._execute` calls `agent_trust.ensure` (`:~416`) only on the tmux path. Headless has no trust prompt (Step 0). |
| Slash commands, `native_chat/commands.py` | see §2. |
| "Working" indicator, `probe._working` (`:271`) | hook activity file stays valid because its owner is the claude pid. Add `phase` from the host for lower latency. |
| Dashboard terminal panel, `biz-dashboard/js/global-agents.js:336` (`mountAgentView(... terminal: {base: '/root/terminal/<id>'})`) | the pane would open a shell. Hide it for managed. The agent list/state needs an `execution` field. |
| `service/runtime.py:269-270, :520-523`, `service/main.py:85-93` | construct `ManagedRuntime`, pass it into `NativeChat.build`, and start it with `native_chat` (`step("native_chat", …)`). Do not stop hosts on shutdown. |

## 5. Chat UI and server

Existing carriers (`native_chat/http.py`, `../biz-dashboard/js/global-chat.js`):
- Events: the SSE `changed` event carries only `{revision, newest_sequence, waiting}` (`chat.py:319`). The client re-reads `GET /chat`. The backend bumps `store.bump` when the working state changes (`service.py:141-148`).
- Composer: `session.can_send` and `session.state/reason` (`global-chat.js:381-396`).
- Working indicator: `session.working`.
- Message admission: `POST /messages {text, idempotency_key, expected_binding}` (`http.py:128`, `global-chat.js:487`). The pin comes from `liveBinding().binding_id` (`global-chat.js:~503`).

**New or changed:**
- **State.** `session_view` adds `execution`, and for managed adds `managed:{open, phase, activity, turn_started_at, can_stop, pending:[{request_id, kind, tool, input_summary, questions}]}`.
  - A closed managed agent reports `can_send:true`, `binding:null`, `state:"offline"`, `reason:"session_closed"`, detail "Sending a message starts a session".
- **Send, opening a closed session.** `NativeChat.send` runs, under `arbiter.locks(agent)`: if the agent is managed, no live session exists and no pin is given, call `managed.ensure_open`, wait up to about 45s for the registry binding, then continue with the existing arbiter admission using the new binding id. A pin naming an ended session still gives `terminal_changed`. `expected_binding` becomes optional only for a managed agent with no live session (`checked_fields` at `http.py:129-131`). Opening Chat itself starts nothing.
  - The blocking wait is simplest. A durable "pending open" would be heavier, because `native_inputs.session_id` is `NOT NULL REFERENCES native_sessions` (`schema.py:92`), so a waiting row cannot exist before a binding does.
- **Stop.** New `POST /chat/interrupt {expected_binding}` calls host `interrupt`. It is not an arbiter input (a control operation under the agent lock). It is refused for terminal agents.
- **Permission and question cards.** `POST /chat/answer {expected_binding, request_id, decision}` calls host `answer`. Cards render from `session.managed.pending`. With bypass mode permission requests are rare. The cases that remain are `AskUserQuestion` and MCP approval, which Step 0 says to re-check.
- **Close.** Optional `POST /chat/close`, plus `/exit`.
- **Activity line.** The backend pass (`service.py:132-156 observe`) polls `managed.refresh(agent)` (status plus events from the cursor) and bumps the revision when `(phase, pending ids, activity)` changes. Throttle activity text to at most 1 bump per second. Also put the `managed` summary into the `changed` event `data` so activity-only changes need no full state reload.
- **Dashboard.**
  - `global-chat.js:381-440`: `sessionSummary()` and `emptyState()` get a managed branch; `startHint()` (`:414`) must not say "run claude".
  - Add a Stop button, activity line and cards.
  - The runtime toggle (new `agent-runtime.js`).
  - Hide the terminal pane for managed agents.
  - `slash-commands.js:70-76`: managed refusal text for unavailable commands.

## 6. Work packages (disjoint file ownership)

New directory `app/qorqut/managed_runtime/`. `claude_driver.py` belongs to the other agent and is read-only to all packages.

**Interface contracts (shared, defined up front so packages can run in parallel):**
- `managed_runtime/protocol.py` (WP1): the NDJSON ops above.
- `HostClient` (WP1): `hello()`, `status()`, `send(text, uuid, steer)`, `lookup(uuid, fence)`, `interrupt()`, `answer(request_id, decision)`, `close(mode)`, `events(since)`; raises `HostUnavailable`.
- `ManagedRuntime` (WP2) exposes the following. WP3 and WP4 consume it duck-typed and test against fakes:
  - `mode(agent_id)`;
  - `verify(profile, marker) -> {"transport","owner"} | None`;
  - `identity(agent_id)`;
  - `snapshot(agent_id)`;
  - `refresh(agent_id) -> changed`;
  - `drain_events(agent_id)`;
  - `ensure_open(profile, timeout)`;
  - `interrupt`, `answer`, `close`, `close_all`, `running_hosts()`.

**WP1: host, protocol, launcher.**
- Files: `managed_runtime/{__init__,state,protocol,host,client,launcher}.py`, `app/tests/test_managed_host.py`, `test_managed_protocol.py`.
- Contract: everything in §3 up to `HostClient`.
- Tests, with a fake `ClaudeSession` class:
  - lock exclusivity, identity verify;
  - uuid dedupe and fence;
  - idle close;
  - event log cursor;
  - token and peer-cred refusal;
  - restart reattach in a subprocess;
  - socket path length fallback.
- Done when: a real subprocess host with the fake driver survives the client's death, is found and verified by a fresh client, accepts `send`, `lookup`, `interrupt` and `answer`, idle-closes, and exits cleanly. Nothing in the file calls a real claude.

**WP2: manager, mode, adapter, tmux-op guards.**
- Files: `managed_runtime/{mode,manager}.py`, `native_chat/adapters.py`, `transport/terminals.py`, `transport/agent_terminal_control.py`, `agents/terminal_control.py`, `../extensions/telegram/qorqut_telegram/owner_input.py`, `app/tests/test_managed_adapter.py`, `test_managed_mode.py`, plus a Telegram test under `../extensions/telegram/tests/`.
- Contract: `mode_of/set_mode`, `ManagedRuntime` as above, `ManagedAdapter` with the §2 mapping table, and the synthetic managed snapshot.
- Tests (pattern of `tests/test_nudge_terminal_submission.py` with a fake `HostClient`): one test per reason code; steer versus non-steer; `checkpoint` called exactly once and before `send`; timeout then `lookup` resolves to submitted or deferred; `/exit`; Telegram mapping; refusal of tmux `start` and `restart`.
- Done when: every row of the mapping table is covered, and with `execution="terminal"` the existing tests for these files pass unchanged.

**WP3: session identity, binding, schema, hooks.**
- Files: `native_chat/{contracts,schema,store,probe,sessions,liveness,selection,arbiter,service}.py`, `migration/native_transport_managed.py`, `migration/upgrade.py`, `bootstrap/{native_binding,bridge_binding}.py`, `app/tests/test_managed_session_binding.py`, `test_native_transport_migration.py`.
- Contract: probe and liveness branches, `Selection.holds`, `store.annotate_input`, `store._profile` adds `execution`, `arbiter._unavailable` returns `session_closed`, the managed command refusal, `service.observe` calls `managed.refresh/drain_events` and applies `prompt_blocked`, and the `attest`, `bridge_binding` and `MANAGED_PROBE` allowance.
- Tests (patterns of `test_chat_codex_commands.py` and `test_local_session_binding.py`, fake `managed`): a managed observation binds; liveness is dead if either the host or claude is gone; `-p` is allowed only with a verified host ancestor and still refused for plain `claude -p`; migration forward/backward on a store with and without managed rows; terminal agents unchanged.
- Done when: a fake host plus a fake marker yields `Reconciled(live=True)` with `transport=="managed"`, the arbiter dispatches to the `managed` adapter, and the migration passes `inspect/apply/revert`.

**WP4: Chat facade, HTTP, service wiring.**
- Files: `native_chat/{chat,http,commands,attachment,nudges}.py`, `service/runtime.py`, `service/main.py`, `app/tests/test_managed_chat_send.py`, `test_managed_http.py`.
- Contract: `send` open flow with optional pin, `session_view` `execution` and `managed`, `interrupt`, `answer`, `close`, `runtime` routes, the `changed` event payload, the managed catalog, `attachment` early view, the `nudges` reason class, `NativeChat.build` takes `managed`, `ManagedRuntime` is started with `native_chat` and not stopped on shutdown, and a managed quiescence leg beside `service/main.py:85`.
- Tests: send to a closed managed agent opens then admits; a stale pin returns `terminal_changed`; terminal agents keep the required pin; the switch preconditions; owner-only and agent-bearer refusals; the event payload.
- Done when: an HTTP-level run with fakes goes closed agent, message, session opened, input admitted, `changed` event carries `managed.phase`.

**WP5: dashboard.**
- Files: `../biz-dashboard/js/{global-chat,chat-view,slash-commands,global-agents}.js`, new `../biz-dashboard/js/agent-runtime.js`, `../biz-dashboard/css/*` (new managed-chat css), `../biz-dashboard/index.html`, `../biz-dashboard/tests/chat-managed.test.js`.
- Contract: consumes only the JSON shapes in §5.
- Tests (node tests in the style of `chat-terminal-switch.test.js`): closed managed composer is enabled and sends without a pin; Stop appears only while working; activity line; permission and question cards post `answer`; terminal pane hidden; terminal agents render as before.
- Done when: the node tests pass and a terminal agent's Chat is unchanged.

**Sequencing:** WP1 and WP2 and WP3 and WP5 can start together on the contracts. WP4 integrates last.

## 7. Risks and what cannot be verified on this Linux host

**Cannot verify here:**
- Anything on macOS:
  - `process_info` identity and ancestry (`native_binding.py:23`);
  - `LOCAL_PEERPID`;
  - `Popen` detach versus launchd;
  - the native claude process name there.
- The driver itself. It is not written yet, so the event shapes, the `session_started` timing, and whether argv contains `-p` all depend on it.
- That the hooks fire before the first message in stream-json mode (SessionStart in streaming input).
- Whether the managed claude's `comm` is `claude` (`bootstrap/terminal_chat.py:38`; `activity_hook.py:139` already accepts `claude.exe`).
- Whether Claude passes its env, including the marker, to stdio MCP servers. The design avoids relying on it by using ancestry plus the host record.
- The `queued_command` attachment's uuid and origin shape for a mid-turn message (`projection.py:408-413`), and the exact `prompt_blocked` payload.
- `/compact` and `/clear` behaviour in stream-json input, and `--resume` of a session created under a different invocation.
- `systemd-run --user` lingering on a real service install.
- The migration against a real production store.
- Long sessions, the 240s MCP startup timeout (`preparation.py` `MCP_TIMEOUT`), and anything larger than Haiku.

**Risks:**
- Two writers on one agent: a terminal claude and a managed host. Mitigated by the switch preconditions and the refusal in `TerminalService.open`.
- The `-p` allowance in `attest` and `bridge_binding` widens a security-relevant filter. The ancestry-to-host check must be strict.
- Release coupling: the schema change needs the offline upgrade, because normal startup refuses a store without it.
- Env secrets pass through `launch.json`. It must be 0600 and unlinked by the host immediately.
- Idle close racing a Chat send. The sent row can end `undelivered session_ended`, and the owner resends.
- Provider terms: plan "Open decisions" 2 (Agent SDK with subscription sign-in) is unresolved and applies to this runtime.
- Orphan hosts and recovery quiescence need the managed leg in `service/main.py`.
- Activity bump rate versus the SSE re-read storm; throttled to 1 per second.

### Critical Files for Implementation
- /home/alp/qorqbot-public/.claude/worktrees/bridge-cse_01UgDrh92qm8SN6FwG2guf3Y/qorqut-code/app/qorqut/native_chat/arbiter.py
- /home/alp/qorqbot-public/.claude/worktrees/bridge-cse_01UgDrh92qm8SN6FwG2guf3Y/qorqut-code/app/qorqut/native_chat/probe.py
- /home/alp/qorqbot-public/.claude/worktrees/bridge-cse_01UgDrh92qm8SN6FwG2guf3Y/qorqut-code/app/qorqut/native_chat/chat.py
- /home/alp/qorqbot-public/.claude/worktrees/bridge-cse_01UgDrh92qm8SN6FwG2guf3Y/qorqut-code/app/qorqut/bootstrap/native_binding.py
- /home/alp/qorqbot-public/.claude/worktrees/bridge-cse_01UgDrh92qm8SN6FwG2guf3Y/qorqut-code/app/qorqut/browser/agent_browsers.py
