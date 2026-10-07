# Isolated native CLI rehearsal r1 evidence

2026-10-08 UTC. Exploration and reproduction under
[NATIVE-REHEARSAL.md](../NATIVE-REHEARSAL.md) plus Manager's amendment to
question 144 (message 145; core plan commit
`86dace75823a67da2f63cafebda68e4df4c48dee`). Base
`03c2d22f087ed332dced05bd23f4ee8ecfc80d1b` on `codex/native-rehearsal-r1`. Run
`649fed24-a7af-40d7-9d41-382ab248483a`; worker
`d6b85832-b3dc-4d8a-8b86-78a729d0f6b5`; Claude Code `claude-sonnet-5-5`, fixed
xhigh effort, no nested agents or internal reviewers. This file is the only
tracked output: no production, test, lock, plan, dependency-pin or capability
edit. A fresh independent review remains required.

`$RUN` below is the private run directory
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-rehearsal-r1-validation`.
Times are UTC clock times (HH:MM:SS.mmm) on 2026-10-08. Environment variables are
named, never valued.

## What this does and does not establish

Established: with a loopback fake provider only, the installed Claude `2.1.287`
and a privately pinned Codex `0.160.0` each started interactively under the
accepted recorder in a private tmux session, loaded the generated configuration,
connected to both loopback MCP endpoints with their distinct synthetic
credentials, accepted one prompt pasted by the accepted `TerminalRunner.attempt`
path, displayed the canned answer, wrote a native store, and resumed that exact
session under a distinct run binding. Accepted discovery ran for all four runs
only after independent process/session evidence agreed.

Not established: real inference; semantic tool use (no model requested a tool, so
no MCP tool was ever called); authentication flows; multi-line, large, concurrent
or busy-turn paste; complete transcript coverage, attachments or child sessions;
behavior of any other CLI version; production composer gating (the accepted
classifier did not classify any real empty composer as empty, see
[Prompt driving](#setup-prompts-and-the-one-prompt)); product readiness. Fixture
behavior is labeled `FIXTURE` throughout.

## Versions, paths and hashes

| Item | Path | Output | SHA-256 |
| --- | --- | --- | --- |
| Claude (installed) | `/home/alp/.nvm/versions/node/v22.22.0/bin/claude` → `…/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` | `2.1.287 (Claude Code)`; package `@anthropic-ai/claude-code` 2.1.287 | `claude.exe` `3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0` |
| Codex (installed, nvm) | `…/node_modules/@openai/codex/bin/codex.js` → vendored `…/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex` | `codex-cli 0.161.0` | launcher `61b0194f3bb6534439c8d26a3ed57d0805f84b884588b761795323eeb92fcf70`; native `9a820c17865fa825d04db416818679a9d63bd72e50835c396f496e5684626c9c` |
| Codex (installed, `/usr/bin`) | `/usr/bin/codex` | `codex-cli 0.155.1` | `0753dfe1d8b87a52436deb13eb1c549661ef4c84fee2c5aa688385eebeccb761` |
| Codex (private pin, used) | `$RUN/codex-pkg/install/node_modules/@openai/codex/bin/codex.js`; native `…/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex` | `codex-cli 0.160.0` | launcher `61b0194f…cf70` (byte-identical to the 0.161.0 launcher); native `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad` (289,101,384 bytes) |

Accepted profiles (`adapters/capabilities.py` `VERSIONS`): Claude `2.1.287`,
Codex `0.160.0`. Claude matches. Neither installed Codex matches; by code
inspection `require_profile` compares the version string for equality and
raises, so no installed-Codex preparation, launch or interactive step ran. The
pin is a labeled rehearsal of the profile version only, separate from the host's
unsupported `0.161.0`.

Pin acquisition (Manager exception, outside the namespace, package files only):
`npm install @openai/codex@0.160.0 --save-exact --ignore-scripts --no-audit
--no-fund --registry=https://registry.npmjs.org --prefix $RUN/codex-pkg/install`
via `env -i` with private `HOME`, cache, prefix and empty user/global npm config
files. Result "added 2 packages". `package-lock.json`
(`2ae2d7b4800cd43c46f5be0ef0191b13dc45f396de8f52d6f13c94a128fcc4ad`) resolves
`@openai/codex` 0.160.0 with integrity
`sha512-kEtVGzjRAYAMOwJxN39bGcna7LT3IDQgq64NNJ/dDTfu4OzZaocJcyNb5/gGJ/IVF/Vj7oK7E2m3nmTan7lpjg==`
(equal to Manager's registry metadata) and the installed
`@openai/codex-linux-x64` as `0.160.0-linux-x64`, integrity
`sha512-KI/73OqGrHmR18s7ya7E1NqV6rT0y3lxr0s8S1qR2m6zU6QRF/HlR529jALLh5vjdUnsRT4Ahoxt0axb4kY99g==`;
npm verified both on install and I did not re-download either tarball to
recompute it. Version/help ran through the accepted `capabilities.probe` inside
the namespace: `--version` → `codex-cli 0.160.0`. The `--help` and `resume --help`
outputs are byte-identical to the installed 0.161.0 outputs
(`75c90681ba095160666d9884d1eccce391be95c80da27641a3c8e1481bb02fac`,
`a69d53b274c25b7ca50fad2c5ed62bd538097033fa4092dc0b2587e8f0a50edb`), which
establishes nothing about compatibility. `/usr/bin/codex` 0.155.1 help differs.

Test tools: tmux 3.4, util-linux 2.39.3 `unshare`/`nsenter`, Linux
6.8.0-138-generic, Python 3.12.3 with `mcp` 1.30.0 in a private venv whose `.pth`
points at this worktree's `src`, Node v22.22.0.

## Containment and fixtures

- Namespace: `unshare --map-current-user --keep-caps --net --pid --fork
  --kill-child --mount-proc` running a holder that brings `lo` up and then runs a
  reaping Python PID 1. Commands enter it with `nsenter -t <init> --user --net
  --pid --mount --preserve-credentials` as uid 1000 with no capabilities. Only
  `lo` exists. `/tmp` is shared with the host mount tree (visible in cleanup).
  A first `sleep infinity` PID 1 left zombies and was replaced; teardown
  required SIGKILL of the `unshare` launcher (SIGTERM did not tear it down).
- Every process was started from `env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 …`
  with new `HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_*` under
  `$RUN/native/<provider>/`; no inherited authentication.
- Fake endpoints (all `127.0.0.1`, inside the namespace): Anthropic Messages
  `:18101`, OpenAI Responses `:18102`, core MCP `:18111`, browser MCP `:18112`.
  The two MCP servers use the official SDK (`FastMCP`, streamable HTTP at
  `/mcp`), one `readOnlyHint` tool each (`qorqut_core_probe`,
  `qorqut_browser_probe`), require their own synthetic `Authorization: Bearer`
  and `X-Qorqut-Target`, and log accept/reject. Cross-credential rejection was not
  tested. They are test services, not the browser product.
- Orchestration used accepted APIs: `ClaudeAdapter`/`CodexAdapter.prepare`,
  `.launch`, `.discover`, `.translate_hook`, `.composer`,
  `runner.process.fingerprint`, `Tmux.inspect`,
  `TerminalRunner.register_binding`/`.attempt`, `transcripts.probe.probe_recorder`
  and the recorder CLI (`python -P -m qorqut_core.transcripts.recorder --spool …
  --probe-address NAME -- <argv>`). The recorder has no env or cwd option and
  inherits the pane's, so launch env travels through `env VAR=… python …` in the
  tmux pane command; tmux was `new-session -d -s qnr-<flow> -x 120 -y 40 -c
  <workspace>` on a private socket under `/tmp/qnr-<flow>-<hex>/`.

### Fixture additions (not adapter output)

| Fixture | Provider | Detail |
| --- | --- | --- |
| Launch env | Claude | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` (synthetic), `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (applied from attempt 2 on; not isolated as necessary) |
| Seed `H/.claude/.claude.json` = `{"hasCompletedOnboarding": true}` | Claude | Written after preparation; see failed attempts |
| Directories `.claude`, `.config`, `.cache`, `.local/share` | both | The adapter names these in `LaunchSpec` env but does not create them |
| Launch env `PATH=$RUN/codex-pkg/shim:/usr/bin:/bin`, `FIXTURE_OPENAI_KEY` (synthetic) | Codex | Shim holds only symlinks `codex` → `codex.js` and `node` → nvm node, so `#!/usr/bin/env node` resolves; `exe` passed to `launch` was `$RUN/codex-pkg/shim/codex` |
| Extra argv `-c model_provider="qfake" -c model_providers.qfake={name="qfake", base_url="http://127.0.0.1:18102/v1", wire_api="responses", env_key="FIXTURE_OPENAI_KEY"}` | Codex | Appended after the adapter argv so the generated `config.toml`/`hooks.json` stay byte-identical |
| Fixture composer/readiness callbacks (`capture_ready` from a real recorder probe, `machine_hold` always false, a classifier and a paste verifier written after viewing each screen) | both | Required inputs to `TerminalRunner.attempt`; not production rules |
| Setup keystrokes (below), logged with times | both | Sent with `tmux send-keys` |
| `config.toml` restore/re-apply around the Codex resume `prepare` | Codex | See Codex resume |

## Observation 1 and 2: preparation, configuration loading, MCP, provider

Adapter output (identical in shape for fresh and resume; per-run files differ):

- Claude: `workspace/CLAUDE.md`, `workspace/.qorqut/adapters/claude/{mcp.json,settings.json,preparation.json,runs/<run>.json}`, `identity.json` in both roots, `prepare.lock`. Gaps reported: `native_mcp_discovery_unverified`, `authentication_requires_setup`, `native_hooks_unverified`. Launch argv: `claude --settings … --mcp-config … --strict-mcp-config --permission-mode bypassPermissions --session-id <uuid>` (resume: `--resume <uuid>`).
- Codex: `workspace/AGENTS.md`, `home/.codex/config.toml` (approval/sandbox keys plus two `mcp_servers` tables with `url`, `required=false`, `default_tools_approval_mode="approve"`, `http_headers`), `home/.codex/hooks.json` (8 events: PreToolUse, PermissionRequest, PostToolUse, SessionStart, SessionEnd, UserPromptSubmit, Stop, Interrupt), `workspace/.qorqut/adapters/codex/{preparation.json,runs/<run>.json}`. Launch argv: `codex --no-daemon --cd <workspace> --sandbox danger-full-access --ask-for-approval never` (resume: `codex resume <uuid> --no-daemon …`). All of these flags exist in the pinned help.
- Pre/post-prepare tree diffs and verbatim generated copies:
  `$RUN/evidence/<flow>/{tree-diff-prepare.json,generated-by-adapter/}`.

Configuration loading:

| | Claude 2.1.287 | Codex 0.160.0 (pin) |
| --- | --- | --- |
| Generated instructions in the model request | Yes: agent and conversation UUID text from `CLAUDE.md` present; `persona.md` text absent (the model never read it) | Yes: `AGENTS.md` text present; persona absent |
| MCP discovery | Both endpoints connected only after the setup dialogs finished (start of the interactive session), before first input: `POST server/discover` (protocol `2026-07-28`, HTTP 400 from the SDK server), then `initialize` (`2025-11-25`), `notifications/initialized`, `tools/list`, `prompts/list`, `resources/list`; `GET` stream; user agent `claude-code/2.1.287 (cli)` | Both endpoints connected right after the hooks dialog, before first input, and again at resume startup: `initialize` (`2025-06-18`), `notifications/initialized`, `tools/list` only; user agent `codex-mcp-client/0.160.0` |
| Credentials | Every request from both endpoints carried the endpoint's own bearer and target (`auth_ok` true for all logged requests) | Same |
| Tools in the model request | `mcp__qorqut-core__qorqut_core_probe` and `mcp__qorqut-browser__qorqut_browser_probe`, each under its own server | Not visible: the turn request has no `tools` key; tools are in a developer `additional_tools` item (14 entries: `exec`, `wait`, `request_user_input`…, `collaboration` tools). The strings `qorqut_core_probe`/`qorqut_browser_probe` appear nowhere in the request. The `exec` text says "Some deferred nested tools may be omitted from this description". Presentation to the model is therefore unproven, though `tools/list` was fetched |
| Hooks | Settings hooks ran (SessionStart, UserPromptSubmit, Stop, SessionEnd) with `QORQUT_ADAPTER_BINDING_FILE` carrying the run | Ran after trust (SessionStart, UserPromptSubmit, Stop); SessionEnd and Interrupt never fired |

Provider protocols (version and uncertainty):

- Claude: `POST /v1/messages?beta=true`, `stream: true`, selected by
  `ANTHROPIC_BASE_URL` (documented). I served the Messages SSE events
  (`message_start`, `content_block_start`, `ping`, `content_block_delta`
  `text_delta`, `content_block_stop`, `message_delta`, `message_stop`). Claude
  also issued `HEAD /api/hello` at each session start (answered 200; the first at
  launch, before the dialogs) and, for the fresh prompt, a second
  `POST /v1/messages` without tools besides the main turn (3 POSTs in total:
  two fresh, one resume). Canned reply rendered as
  `● qorqut-native-probe-complete`.
- Codex: `POST /v1/responses`, `stream: true`, `store: false`, selected by the
  documented `model_providers.<id>.base_url`/`wire_api = "responses"`/`env_key`
  keys passed as `-c`. I served Responses SSE events from memory of the public
  event names (`response.created`, `response.in_progress`,
  `response.output_item.added`, `response.content_part.added`,
  `response.output_text.delta`/`.done`, `response.content_part.done`,
  `response.output_item.done`, `response.completed` with `usage`); Codex
  accepted them on the first try. The minimum set Codex requires was not
  determined. Codex made two requests per prompt: the turn and a
  `thread_source: thread_title` request on a different thread id. No `/models`
  or other request reached the fake. With this custom provider no login screen
  appeared, so Codex authentication was not observed. Canned reply rendered as
  `• qorqut-native-probe-complete`; the bundled default model label
  `GPT-6.1-Sol` shown by the TUI was never served by a model.
- Canned strings: `qorqut-native-probe-complete` (fresh),
  `qorqut-native-probe-resume-complete` (resume), via a text file the fake reads
  per request. The resume turn request to the fake contained the earlier
  exchange (Claude and Codex).

## Observation 3: pane, recorder, native

- Claude: tmux server (ns pid 63) → recorder `python -P -m …recorder` (64, the
  pane pid, on the pane TTY) → native `claude.exe` (67, argv0 the nvm path, own
  session and process group on a different TTY, the recorder's PTY). The
  recorder's direct child is the native process.
- Codex: tmux server → recorder → `node` running the launcher (direct child,
  its own session on the recorder's PTY) → native `codex` binary (grandchild; same
  session, process group and TTY as node). Hook ancestry and the thread lock holder name the
  grandchild. The recorder probe's `child` and the `RunBinding.native` that I
  built (the pane's direct child) are the node launcher, not the native binary.
- Successful-exec evidence: no `recorder: child exec failed` text in any of the
  four raw spools; the probe observation's `child` fingerprint (pid, start
  ticks, pid-namespace inode, uid, boot id) matched `/proc` and `Tmux.inspect`
  accepted pane, recorder and native fingerprints. Each snapshot repeated a fresh
  `probe_recorder` (verified, `terminal_active`), and every probe was taken
  after launch, not carried over. Spool state in each snapshot: `healthy`, a
  growing `local_captured_offset`, `capability_evidence` "recorder-owned PTY
  output; no native semantic capture".
- Raw spool sizes: Claude fresh 8,160→8,531 bytes after stop, resume 3,131→3,502;
  Codex fresh 9,876, resume 6,468. They hold terminal bytes only.

## Setup prompts and the one prompt

Setup dialogs (screens in `$RUN/evidence/<flow>/snap-*.plain.txt`; keys and times
in `setup-keys.jsonl`):

- Claude fresh, three dialogs in order, each defaulting to the refusing option:
  workspace trust ("Is this a project you created or one you trust?", default
  `No, exit`); detected custom `ANTHROPIC_API_KEY` ("Do you want to use this API
  key?", default `No (recommended)`); Bypass Permissions warning (default `No,
  exit`). Keys: Down 15:50:57.772, Enter 15:51:13.431, Up 15:51:23.890, Enter
  15:51:25.144, Down 15:51:47.754, Enter 15:51:49.097 (gaps are my polling, not
  CLI waits). Claude resume showed none
  of them (same disposable home; I did not isolate which file carries the state).
- Codex fresh: one dialog, "Hooks need review: 8 hooks are new or changed",
  options Review / Trust all and continue / Continue without trusting (default
  Review). Down 16:09:52.846, Enter 16:09:54.740 (Trust all). No workspace-trust or login
  screen appeared. Codex resume showed the same dialog again (new run binding,
  see below); Down 16:15:15.080, Enter 16:15:16.202.

Prompt: one 66-byte single-line prompt (fresh; 90 bytes on resume) through
`TerminalRunner.attempt` with the fixture callbacks. Results for all four runs:
the attempt receipt was `submitted`, phase `finished`, reason
`processing_not_confirmed`; the fixture verifier saw the text on screen before
Enter and the CLI then processed it (the fake provider received it and the
canned answer rendered).

| Run | Attempt start→end | Paste verified | Evidence of processing |
| --- | --- | --- | --- |
| Claude fresh | 15:53:27.425→15:53:27.781 | 15:53:27.682, .714 | `history.jsonl` created 15:53:27.777; UserPromptSubmit hook 15:53:27.829; transcript first seen 15:53:27.981; Stop hook 15:53:28.037 |
| Codex fresh | 16:10:41.116→16:10:41.563 | 16:10:41.428, .464 | rollout first seen 16:10:41.530; SessionStart hook 16:10:41.628, UserPromptSubmit .687, Stop .805 |
| Claude resume | 16:03:17.780→16:03:18.264 | 16:03:18.149, .200 | UserPromptSubmit hook 16:03:18.363, Stop 16:03:18.534; transcript appends below |
| Codex resume | 16:15:46.243→16:15:46.537 | 16:15:46.468, .492 | SessionStart(resume) 16:15:46.567, UserPromptSubmit .611, Stop .705 |

Readiness and paste facts the installed CLIs actually show:

- Both TUIs enabled bracketed paste (`ESC[?2004h`) in the raw spool (Claude 9
  times with 3 resets, Codex once); the accepted `ESC[200~…ESC[201~` wire plus a
  separate Enter was accepted by both for single-line text. The pasted text
  appeared in the composer before Enter.
- Claude hides the terminal cursor (`cursor_visible` false at the composer) and
  shows `⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents` as the
  footer. Codex shows the cursor, a `› Ask Codex to do anything` placeholder
  (`ESC[1m›ESC[0m ESC[2m…ESC[0m`), a model/cwd row and `? for shortcuts`.
- The accepted `adapter.composer`/`composer.classify` never returned `empty` on a
  real empty composer. Claude: `unknown / unknown_footer_or_multiline_input`.
  Diagnostic variants of the captured screen show two independent blockers:
  the footer is not `? for shortcuts`, and the cursor is not visible
  (`cursor_not_at_empty_composer`); replacing the footer and cursor yielded
  `empty`. Codex: `blocked / composer_contains_text`. Variants show two
  blockers: the marker is preceded by `ESC[1m` so the placeholder check does not
  match, and there are two rows under the composer instead of the single
  `? for shortcuts`; changing both yielded `empty`
  (`composer-diag-*.json` marks these as altered copies, not observations).
  The classification passed only through my fixture classifiers.
- Not proven: multi-line or large paste (collapsed-paste placeholders), paste
  while a turn is running, paste after human typing, Unicode/control content,
  that submission reached the model beyond these single cases, readiness during
  setup dialogs other than by screen text, or any behavior of the paste verifier
  against a late or partial render. In the Codex raw spool the prompt text is
  not contiguous (0 literal matches) while the canned reply is, so searching the
  raw spool for pasted text would not have verified the paste.

## Observation 5: stores and session links

Claude 2.1.287 (fresh, ns pids tmux 63 / recorder 64 / native 67; session
`d97e2375-5404-4630-a515-67f23c1871ce`):

- Evidence tying process, run and session, with times:
  1. argv carries `--session-id d97e2375…` from process start (launch 15:50:40.172).
  2. `.claude/sessions/67.json` (named by the native pid) appeared when the setup
     dialogs finished (`startedAt` 15:51:49.236), 98 s before first input: `sessionId`, `procStart` equal to the process's `/proc` start ticks,
     `pidDomain` containing the pid-namespace inode `4026533040`, `tmux`
     `qnr-claude-fresh:@0.%0`, `version` `2.1.287`. A sibling
     `67.<hash>.key` file was created 0.7 s after launch (15:50:40.842), before the dialogs. Both
     were removed at exit. I could not reproduce the hash in `pidDomain`
     from the boot id (md5/sha1/sha256 tried).
  3. SessionStart hook at 15:51:49.278 carried the same `session_id` and the future
     transcript path, with ancestry `[hook, sh, 67, 64, 63, 1]` and the run's
     `QORQUT_ADAPTER_BINDING_FILE`. The transcript file did not exist yet.
- Transcript: `projects/<cwd with / and . replaced by ->/d97e2375….jsonl`,
  created after first input (history.jsonl 15:53:27.777, transcript first seen
  15:53:27.981 at 281 bytes, final 151,061 bytes, inode 20753389). 24 JSON lines; record 0
  `mode` (83 bytes at offset 0, carries `sessionId`), then `permission-mode`,
  `atis-latch`, `file-history-snapshot`, `user`, 11 `attachment`, `assistant`,
  attachment, `last-prompt`, `mode`, `permission-mode`, `atis-latch`, and two
  `system` records (`stop_hook_summary`, `turn_duration`). sha256
  `0d86705fa30ea403de57b468c3970f31e60acccea479cead7e83ae985f9801f9`.
- Accepted `ClaudeAdapter("2.1.287").discover` with the independently
  established binding returned one source (inode 20753389, size 151,061,
  `claude-jsonl-p06-p11`, ownership proof: `native_session_id` offset 0 length
  83) and gaps `child_association_contract_unverified`,
  `attachment_coverage_partial`, and 12 `unsupported_attachment_reference`.
  `translate_hook` accepted all three real payloads (idle, working, idle).
- Raw bytes versus terminal capture: the transcript is 151,061 bytes of JSONL
  and the terminal spool 8,160 bytes of rendered PTY output. The spool contains
  the prompt text once and the canned reply once, and neither the session id
  nor any persona text. The two are different artifacts.

Codex 0.160.0 (fresh, ns pids tmux 670 / recorder 671 / node 674 / native 681;
thread `01a11c47-4367-7b23-93da-768092953c1f`):

- The session UUID is not in argv. Evidence, with times:
  1. At startup after the hooks confirmation (16:09:54.810, 46 s before first input)
     the native process holds an exclusive `flock` on
     `.codex/thread-writer-locks/01a11c47-….lock` (`/proc/locks` entry for pid
     681 and the lock inode) and wrote `.codex/shell_snapshots/01a11c47-….sh`.
     The native's open descriptors name the lock and (later) the rollout.
  2. The rollout file `.codex/sessions/2026/10/08/rollout-2026-10-08T12-09-54-01a11c47-….jsonl`
     did not exist until first input: first seen 16:10:41.530 at 23,117 bytes (its name
     carries the thread creation time, 47 s earlier, local time).
  3. SessionStart hook (`source: startup`) fired at 16:10:41.628, after the rollout
     appeared, not at startup; its payload carries `session_id`, `transcript_path`
     and `model`, with ancestry `[hook, 681, 674, 671, 670, 1]`.
  4. Also: Codex's log store (`logs_2.sqlite`) tags `pid:681:<process uuid>`
     with `thread_id`, `state_5.sqlite` `threads` has the row with
     `rollout_path`, `history.jsonl` and every request's
     `x-codex-turn-metadata` repeat the id.
  The lock name, rollout descriptor filename and hook `session_id` agreed; only
  then did `discover` run.
- Rollout: 15 JSON lines, 44,580 bytes, inode 20753889, sha256
  `7402101fb96247ca91b235a9393364b4aaf39b62af412512aee8193be048d1ac`; record 0
  `session_meta` (22,818 bytes at offset 0, `id` = thread, `cli_version`
  `0.160.0`, `originator` `codex-tui`, `model_provider` `qfake`), then
  `task_started`, four developer/user `response_item`s, `world_state`,
  `turn_context`, user item, two `item_completed`, assistant item,
  `token_usage_record`, `token_count`, `task_complete`.
- Accepted `CodexAdapter("0.160.0").discover` returned one source (ownership
  proof offset 0 length 22,818), no attachments, gaps
  `child_association_contract_unverified` and `attachment_coverage_partial`;
  `translate_hook` accepted the three payloads.
- Terminal spool 9,876 bytes versus rollout 44,580 bytes; the session id and
  persona are absent from the spool.

Other native writes at startup (before any input), both CLIs:

- Claude: `.claude.json` rewritten (new inode) and `backups/.claude.json.backup.*`;
  after the first prompt `.config/mimeapps.list` and
  `.local/share/applications/claude-code-url-handler.desktop` were created.
- Codex rewrote the adapter-generated `home/.codex/config.toml` at first start
  (before any input): 675→718 bytes by appending
  `[tui]\nscreen_reader_detection_done = true`; after the hooks confirmation it
  grew to 2,847 bytes with a `[hooks.state."<home>/.codex/hooks.json:<event>:0:0"]`
  `trusted_hash` table for each of the 8 hooks. `hooks.json` was unchanged. Codex
  also created SQLite stores (`state_5`, `logs_2`, `goals_1`, `memories_1`,
  `queue_1`, `thread_history_1`), `skills/.system/`, `installation_id`, `tmp/arg0`
  entries (`apply_patch`, `codex-linux-sandbox`, …) that share one inode of
  289,101,384 bytes, `.sandbox_migration` and `history.jsonl`. I read only copies
  of the SQLite files.

## Observation 6: resume

Stop: SIGTERM to the pidfd-verified recorder pid. All tmux, recorder and native
processes were gone by the first 0.25 s poll in all four runs (Claude resume also
exited 143, Codex resume 0); the tmux server exited and its
socket file was left behind.

| | Claude fresh stop | Codex fresh stop |
| --- | --- | --- |
| Recorder child exit / spool health | 143 / `incomplete`, `last_error` "child exited unsuccessfully" | 0 / `healthy`, no error |
| Store at stop | JSONL appended 1,018 bytes (two `cost-state` records, 508 bytes each plus newline); prefix hash unchanged | rollout unchanged (44,580 bytes, same hash) |
| Hook | SessionEnd (`reason: other`) fired | none after Stop |

Claude resume (`--resume d97e2375…`, new run `88f3a0b8-0a5a-4501-9fef-de5da1596177`,
run generation 2, native pid 455, same home):

- `prepare` changed only `settings.json` (hook commands embed the new
  `QORQUT_ADAPTER_BINDING_FILE`) and `preparation.json`, and added the new run
  binding; `mcp.json` and `CLAUDE.md` unchanged. Home unchanged.
- No setup dialogs. The screen replayed the earlier prompt and answer at
  startup. `sessions/455.json` carried the same `sessionId`. SessionStart
  (`source: resume`) fired at startup (16:02:46.741). MCP reconnected at startup
  (a new MCP session id).
- File: same path, same inode 20753389, same size and hash at startup
  (mtime touched without content change). After the prompt it grew 152,079→156,883
  bytes by appending six records (`file-history-snapshot`, `user`, `attachment`,
  `assistant`, two `system`); at stop a further 1,184 bytes (two `cost-state`,
  `last-prompt`) → 158,067 bytes, sha256
  `a9753c89ce0eef8d7fe1305bc20a6ca8090cf8a84abd20f5c015c70ee455c1c6`. The
  152,079-byte prefix hash was unchanged throughout: append only, no rewrite or
  move. `discover` with `resumed_from_run_id` set returned the same source
  (offset 0 proof) and 13 attachment gaps.
- The recorder's new epoch/run captured 3,131 bytes before stop (3,502 after) of
  its own terminal output, separate from the first run's.

Codex resume (`codex resume 01a11c47-… …`, new run
`6db5866b-3338-4439-abde-5b6034f121b1`, generation 2, native pid 1001, same home):

- Unmodified adapter refresh fails: `AdapterError: Refusing to replace authored
  or changed generated file: .codex/config.toml` (native had rewritten it; text in
  `evidence/codex-resume/prepare-asis-refusal.txt`). Nothing was written by the
  failed call (state, `config.toml` and `hooks.json` hashes unchanged).
  FIXTURE: I restored the adapter's original `config.toml` bytes, ran `prepare`
  (which then changed only `hooks.json` and `preparation.json` and added the run
  binding), and re-applied the native-modified bytes
  (`fixture-actions.jsonl`, hashes recorded). The manifest therefore records the
  adapter's hash while the file on disk is the native's.
- The same hooks dialog ("8 hooks are new or changed") appeared even though the
  prior trust entries were present: the per-run binding path changes each hook
  command and its hash. After Trust all, Codex rewrote `config.toml` with new
  `trusted_hash` values for all 8 (same size).
- Startup replay: the screen replayed the earlier prompt and answer. MCP
  reconnected at startup. The rollout appended 2,112 bytes at offset 44,580
  (`event_msg` `thread_settings_applied`) right after the hooks confirmation,
  before any input. A new thread-writer lock was taken by pid 1001.
- On the prompt: a second `thread_settings_applied` plus the turn, 9 more
  records → 55,505 bytes, sha256
  `e500cc96aaf3247b4dd91a23e7aff1df75e06231e27b22d52c136265c61882d9`. Same
  file name (original timestamp), same inode 20753889, no new `session_meta`,
  both prior prefix hashes unchanged: append only. SessionStart (`source:
  resume`) fired with the prompt (16:15:46.567), not at startup. The turn request
  carried the earlier exchange. `discover` with `resumed_from_run_id` set
  returned the same source and gaps. At stop the rollout did not change.

## Accepted-API results

- `discover` returned `child_association_contract_unverified` and
  `attachment_coverage_partial` in every run; Claude's adds
  `unsupported_attachment_reference` per attachment record. I did not extend
  or work around any gap.
- `translate_hook` accepted every real Claude and Codex payload I had
  (`SessionStart`, `UserPromptSubmit`, `Stop`, Claude `SessionEnd` → activity
  `unknown`). It does not check that a payload's run matches the `NativeBinding`
  passed in (the resume script fed it every logged hook).

## Failed attempts and diagnostics

- Claude attempts 1–3 (archived in `$RUN/attempts/`): the interactive start
  exited code 1 at onboarding with a failed name resolution (`EAI_AGAIN`) for
  `api.anthropic.com`; the namespace has no resolver or route, so nothing left the
  host. `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and a
  200 on `/api/hello` did not avoid it; seeding `hasCompletedOnboarding: true`
  did. Whether the preflight would have been satisfied some other way was not
  explored. Other outbound destinations Claude or Codex may try were not logged.
- Codex: both installed versions failed the accepted profile (see Versions), so
  no Codex interactive step ran until Manager answered question 144.
- Fixture classifier: Claude's footer is SGR-split, so my first classifier
  (plain-substring check) was rewritten to strip SGR before any prompt was sent.
  For Codex it was extended to use only the last `› ` row, because a resumed
  screen replays earlier `› ` prompt rows.

## Cleanup

After the last stop: servers (`:18101`, `:18102`, `:18111`, `:18112`) and the file
watcher were terminated by pid after confirming their command lines contained the
run directory; the owned `unshare` launcher was SIGKILLed, which removed the
namespace. Verified afterward: no host process whose command line references the
run directory; no listener on 18101/18102/18111/18112 on the host; no tmux server
using a `qnr-*` socket; the four `/tmp/qnr-*` socket directories (each containing
only `t.sock`) removed; one stale `/tmp/cc-socks/115.sock` removed because it
matched the Claude pid recorded in attempt 1. Two other zero-byte sockets,
`/tmp/cc-socks/16.sock` and `45.sock`, were created within a second of my
attempts 2 and 3 but I could not tie their pids to those attempts, so they were
left in place. No `auth.json`, `.credentials.json` or similar credential store
exists in the disposable homes. The synthetic bearer/target/key values remain only
in private mode-600 fixture files, copies of generated configs and hashed log
lines under `$RUN`, valid only against the now-stopped loopback fakes.

## Private evidence (not in Git)

`$RUN/evidence/{claude-fresh,claude-resume,codex-fresh,codex-resume,codex-pin,version-probe-*}/`
(launch/prepared/launchspec JSON, tree before/after/diffs, per-step screens
plain and ANSI, `drive.json` callback timelines, `links.json`, `stop.json`,
`terminal-spool.raw`); `$RUN/native/{claude,codex}/{home,workspace,local.sqlite3,state.json}`
(raw synthetic stores); `$RUN/logs/` (provider requests and bodies, MCP logs,
hook-sink records, file-watch timelines); `$RUN/attempts/`; `$RUN/codex-pkg/`;
`$RUN/fixtures/` (scripts); `$RUN/bin/`.
