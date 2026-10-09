# Native cold-start facts before the first prompt

2026-10-09 UTC. Both pinned CLIs select a native session before a prompt, but
neither produced an admissible primary transcript during these fresh startups
and bounded idle observations. **Claude supplied an accepted SessionStart and
idle owner; Codex exposed its session UUID through a held native writer lock,
without a SessionStart or core owner.** Neither case satisfies current Chat
input prerequisites. This is factual exploration, not a startup design or
connected-flow/history acceptance. No prompt, seed, Chat message or native
`tools/call` was sent in either fresh run.

Current source citations below are fixed at
`b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71`, the clean starting HEAD of
`codex/explore-native-cold-start-r1`. Only this report changes the repository.
Private evidence root, abbreviated `$E` below:

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/explore-native-cold-start-r1
```

The executed core packages were the completed connected-run artifacts, **not a
new build of the citation base**:

| Fixture | Executed core pin | Native executable SHA-256 |
| --- | --- | --- |
| Codex 0.160.0 | `852301fd244bcc83bb86c167ab7d0d8fb6157045` | `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad` |
| Claude 2.1.287 | `99a63b6aac134b087b59ef3d2b924f69cf25e40a` | `3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0` |

`pins-startup-source-comparison.json` verifies the relevant setup, hooks,
ownership, input, discovery and collector files against those Git pins,
installed files and the citation base: no differences. `executed-core-imports.json`
records twelve relevant loaded modules per server and their hashes. Copied console
entrypoints retain their completed artifact's interpreter paths; hook/recorder
interpreters use private copies of the same installed environment. No dependency
or native version changed. The completed UI artifact associated with
`30bf7181127a5bac4ff7c5fa6297112b63a84ea1` supplied manual Terminal control.

The original evidence was inspected first and remains byte-identical. Its roots
are siblings of `$E`: `connected-chat-codex-r2` and
`connected-chat-claude-r1/attempt-2`. Their reports and
[Manager acceptance](connected-chat-manager-acceptance.md) qualify the seed and
Claude's unresolved native MCP classifier refusal.

| Original pre-input evidence | What was actually saved |
| --- | --- |
| Codex `evidence/06-pre-seed-snapshot.json`, SHA-256 `c42602fcc59decbc1d73f76c2a5159d49b0cbff8338ad7b54196c45221d6759f` | PID 1023/start 209357989; no hook/owner/rollout. **Already before the seed**, fd 43 named `thread-writer-locks/01a11eae-fba5-7da0-8e4e-dc5b66795c46.lock`. No fdinfo/held-lock proof or pre-seed SQLite image was saved there. |
| Claude `evidence/pre-message-idle-snapshot.json`, SHA-256 `1c44e9fc2e1fcfc6e85b6ba89c2a8bc284edaddd53bbfc2ce176bd4da4acc251` | Accepted SessionStart event 11 at `02:58:18.485045Z`, exact native PID 432/start 209208992 and session/path; `waiting_source`, zero native sources. The only saved JSONL was an MCP diagnostic log. No native fd inventory was saved there. |

Later original Codex events place SessionStart at `03:22:55.492510Z`,
UserPromptSubmit at `03:22:56.175763Z`, and Stop at `03:22:56.877049Z`, in the
seeded run (`09-idle-snapshot.json`). These are **post-submission evidence**,
not startup proof. The rollout filename's earlier timestamp does not establish
when the file materialized. Original log row 64 also names that UUID in
`session_init/shell_snapshot`; the extraction was made from the completed log
database using the original pre-seed snapshot timestamp, and is explicitly
labelled as such in `original-inspection/codex-original-pre-seed-log-rows.json`.

One fresh interactive startup per provider filled those missing observations.
Each used fresh HOME, native stores, core data and browser profile in an owned
user/network/PID/mount namespace, private `/tmp`, and loopback-only networking.
Core/provider ports were 18641/18642 in separate namespaces. The normal installed
server, worker, monitor, recorder, managed hooks and core MCP configuration ran.
No fabricated hook, owner, native UUID/file, direct input dispatcher, collector
pump, tool invocation or permission-mode change was used. Claude's sole authored
native preference was the previously accepted fixture-only
`hasCompletedOnboarding: true`; normal workspace, this-server-only MCP and
synthetic-key dialogs were answered visibly. Codex's eight hooks were reviewed
and trusted through the native dialog; its bundled-bubblewrap warning was viewed
and dismissed. Native defaults remained displayed as Codex GPT-6.1-Sol/default
and Claude Opus 5.5/medium/auto. These were no-inference fixtures, not actor-model
changes. Codex made zero canned-provider requests; Claude made only a local
`HEAD /api/hello` returning 404. The existing classifier block was never exercised.

| Fresh pre-prompt fact | Codex | Claude |
| --- | --- | --- |
| Native root PID / Linux start ticks | 1624 / 209632817 | 1808 / 209657145 |
| PID namespace | 4026532978 | 4026533053 |
| Verified ancestry back to recorded Bash | Codex 1624 → npm Node 1615/start 209632811 → Bash 55/start 209626940 | Claude 1808 → Bash 41/start 209649859 |
| Recorded terminal UUID | `a1faa099-0e36-4e29-a529-76fd7b8fdf6d` | `719c6d72-a571-41de-a71b-17e63d0ea667` |
| Native-selected session UUID | `01a11ed8-eed6-7ad0-af7d-de4d9d222741` | `b7e5143e-6b21-4994-b1e3-7696cc9a7afb` |
| Core owner/run UUID | Absent | `fe214a3c-cdba-491e-b9ee-1f6150218c6f`, generation 1 |
| Post-trust startup snapshot UTC | `04:09:30.583907Z` | `04:13:03.226643Z` |
| Final pre-input snapshot UTC | `04:10:19.285356Z` | `04:13:44.253868Z` |
| Explicit idle wait in the latter observation command | 30.256 seconds including snapshot | 30.255 seconds including snapshot |
| Core observation at both snapshots | disconnected; `waiting_session` | connected; `waiting_source` |

The shared boot ID was `c4bbfea9-4f61-4f2d-bb51-011e55b47392`, UID 1000.
Both snapshot pairs preserve the same native, Bash, recorder and tmux
fingerprints, executable paths, parent chains, native fd targets/fdinfo and file
inventories. Both had available healthy recorded terminals, unclaimed control
at epoch 2, empty visible composers, zero messages/attempts/conversation entries,
zero primary native sources, and no native archive-run link. The existing
terminal-source link is a separate recorded-shell lineage.

Codex details are in `$E/codex/evidence/{10-startup,11-idle30}-snapshot.json`
and their `-original/` directories. The exact native process held fd 45 on the
UUID-named writer lock: `FLOCK ADVISORY WRITE 1624`, inode 19180538, device 09:01,
range 0–EOF. The path inventory and fdinfo inode agree. The lock and
`.coordination.lock` each contained zero bytes: the UUID is in the filename,
not a fabricated lock payload. A native `.codex/shell_snapshots/<UUID>.*.sh`
and log row 66 independently name that same session; the row records the
in-process `thread/start → thread_spawn → session_init` context and PID 1624.
Native SQLite descriptors were open, but `state_5.sqlite.threads` had zero rows
at both snapshots. `queued_items`, goals and memory outputs were also empty.
There was no `.codex/sessions` rollout, archived rollout, session index entry,
primary fd or externally observed selected rollout path. Zero official hook
events and zero owners persisted through the idle wait despite trusted hooks.
Thus there is positive pre-input native UUID/process evidence; it is not a
primary-source observation or an accepted idle hook.

Claude details are in `$E/claude/evidence/{08-startup,09-idle30}-snapshot.json`
and their `-original/` directories. Accepted event 11 at
`04:12:46.973231Z` is `SessionStart/idle/connected`, binding hook PID 2758/start
209660927 to root PID 1808 and the recorded terminal. It names the exact absolute
`.claude/projects/<escaped-workspace>/<UUID>.jsonl` path. That path and even its
projects directory were absent in both inventories; no primary fd existed.
`.claude/sessions/1808.json` independently supplies the same `sessionId`, PID,
`procStart="209657145"`, version, interactive/cli kind, PID domain, recorded
tmux session/pane, and `status="idle"`. Its Unix socket `/tmp/cc-socks/1808.sock`
matches native fd 12 via inode 446626809 in `/proc/net/unix`. A same-session
`session-env` directory and peer key file existed; no native file lock was
observed. None of these is the primary transcript. The only JSONL was the
`.cache/claude-cli-nodejs/.../mcp-logs-qorqut-core/...jsonl` diagnostic. An empty
core native spool existed, with no source registration or raw health proof.

Configuration evidence is preserved before launch and at every snapshot.
[NativeSetup](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/native_setup.py#L153-L204)
uses the recorded shell binding and installs the core connection and hook sink.
[manual_hooks.py](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/adapters/manual_hooks.py#L151-L176)
writes the seven shared events plus Codex Interrupt or Claude PostToolUseFailure,
each with a three-second command timeout. Codex reads managed HOME
`.codex/hooks.json`; Claude reads workspace `.claude/settings.local.json`.
The command is the configured Python `-I -m qorqut_core.runtime.hook_client
--binding <workspace>/.qorqut/manual-hooks/<provider>.json --provider <provider>`.
[manual_core.py](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/adapters/manual_core.py#L155-L174)
installs only the managed core MCP entry and inbox instructions; it does not
select native model/effort or create a session. Exact expanded commands and
config bytes are private, including synthetic credentials.

The inspected installed package READMEs do not document pre-input hook/file
timing. No complete pinned Rust startup source was present in the inspected
Codex package. Its pinned binary contains explicit strings saying a thread is
not materialized yet and history APIs are unavailable before the first user
message (byte offsets 233707632 and 233708274), plus writer-lock source references
at 239838563. Claude's embedded JavaScript constructs SessionStart from the
session object (208338741) and supplies `session_id` plus a computed transcript
path (function offset 208353394), through the path accessor at 210312665.
The payload constructor contains no file-existence check.
These bounded excerpts corroborate shapes/materialization limits, not a complete
source-level timing proof. See `source/native-binary-targeted-excerpts.json` and
`source/claude-embedded-hook-excerpts.json`; their binaries are pinned above.

Current failing prerequisites follow directly from these measured absences and
the accepted source. **No fresh Chat dispatch was attempted**, so the refusal
names here are source-derived consequences, not newly observed delivery rows:

1. **Owner admission:** [native_hooks.py 87–136](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/native_hooks.py#L87-L136)
   and [242–267](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/native_hooks.py#L242-L267)
   requires a real hook's native UUID/path and a twice-verified bounded ancestry
   with exactly one configured native executable. Only SessionStart connects;
   other events need a matching incumbent. A missing fresh file is allowed at
   admission. [owners.py 87–146](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/owners.py#L87-L146)
   preserves live/unknown incumbents. Core does not admit owners from the native
   lock or Claude peer metadata. Codex therefore fails
   [RuntimeInput._owner](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/input_dispatch.py#L158-L169)
   with `session_unavailable` before its later checks.
2. **Accepted idle:** [input_dispatch.py 217–241](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/input_dispatch.py#L217-L241)
   requires the latest accepted, agent-attributed, exact-owner event to match
   process/session/provider/version/terminal/generations and be idle
   SessionStart, Stop or Interrupt. Codex lacks this; Claude has it. A blank
   composer or peer `status=idle` does not replace this event.
3. **Primary-source admission and healthy capture:**
   [discovery.py 129–223](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/adapters/discovery.py#L129-L223)
   needs an eligible exact-session JSONL filename **and matching native identity
   in complete file records**, with stable file/proof spans. Neither absent nor
   empty files satisfy that. [NativeCollector](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/native_collector.py#L281-L336)
   discovers from saved owners, retrying never-discovered owners at minimum
   one-second intervals. [Raw health](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/native_collector.py#L412-L445)
   requires at least one admitted mapped primary epoch, a successful service
   observation no older than five seconds and matching archive lineage without
   raw failure/recovery holds. Source count zero cannot be healthy.
   [RuntimeInput._capture](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/input_dispatch.py#L243-L279)
   requires that exact full RunBinding health, observation at/after connection,
   and an unconflicted primary native archive link. Both lack this; Claude's
   initial blocker is consequently `capture_unavailable`.
4. **Other guards remain independent:** managed config equality, current
   installation/recovery/environment/terminal identities, live executable and
   ancestry, fresh recorder/no terminal gap, unclaimed human control and the
   pinned empty-composer classifier still apply, with final rechecks under the
   input gate. See [input_dispatch.py 172–215](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/input_dispatch.py#L172-L215)
   and [281–365](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/input_dispatch.py#L281-L365).
   The normal dispatcher [defers prepare failures before creating an attempt](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/messaging/dispatch.py#L161-L211).
   Public `input_ready` is a [literal false](https://github.com/qorqutbot/qorqut-core/blob/b3c9f20d533b26e1a30ff39cf0aa2cb905df1e71/src/qorqut_core/runtime/models.py#L74),
   not a reusable readiness grant or the diagnosis of this failure.

Only immutable main-Qorqut source from PORTING's
`e71fd1e527368bfd41b9815126f4bf33452aeebb` was read with `git show` from the local
`biz-core` object store. No live configuration, database or sessions were read.
[claude_session.handle_hook](https://github.com/Qorqut/biz-core/blob/e71fd1e527368bfd41b9815126f4bf33452aeebb/bazarlyk/bootstrap/claude_session.py#L65-L125)
handles both providers' SessionStart/UserPromptSubmit and already requires the
hook's session ID. [native_binding.attest and PROBE](https://github.com/Qorqut/biz-core/blob/e71fd1e527368bfd41b9815126f4bf33452aeebb/bazarlyk/bootstrap/native_binding.py#L15-L84)
bind it to an interactive ancestor, record the trusted observation, then check
PID/start ticks and registered pane ancestry/foreground group; the shared Codex
app-server case uses the held session writer-lock descriptor instead.
[codex_session.py 27–109](https://github.com/Qorqut/biz-core/blob/e71fd1e527368bfd41b9815126f4bf33452aeebb/bazarlyk/bootstrap/codex_session.py#L27-L109)
checks fd/device/inode/FLOCK for that shared-server case; it takes an already
known session ID, rather than discovering an unknown startup session.
[terminal_chat.publish](https://github.com/Qorqut/biz-core/blob/e71fd1e527368bfd41b9815126f4bf33452aeebb/bazarlyk/bootstrap/terminal_chat.py#L37-L66)
records the exact hook path/session/process marker without requiring a transcript
read. [Inbox start_session](https://github.com/Qorqut/biz-core/blob/e71fd1e527368bfd41b9815126f4bf33452aeebb/bazarlyk/inbox/service.py#L198-L264)
uses verified binding and generations; unlike current core's incumbent-wins
rule, it permits a different verified startup/resume/clear/fork selection under
its stated conditions without first proving the old process dead. These are
source facts, not permission to import that ownership policy or a port proposal.

Principal executed commands were `/usr/bin/python3 "$E/<provider>/launch.py"`
and its copied `action.py` with JSON requests. The launch expands to
`unshare --user --map-current-user --keep-caps --net --pid --fork --mount
--mount-proc`, private `/tmp`, `ip link set lo up`, and the private controller.
`namespace-launch.json`, `evidence/*-process.json`, `requests/`, `responses/`,
`run/control/`, native screens, original configs and `frames.jsonl` retain exact
arguments, native trust keys, manual launch/exit inputs and results. Native launch
retained accepted Codex `--no-daemon` with loopback `qfake` CLI overrides and
Claude's loopback environment command; neither supplied a prompt/session UUID.
The `idle30` requests slept 30 seconds then performed read-only observation.

Both `/exit` commands returned to the original recorded Bash. Later snapshots
prove the native process absent; Claude's owner closed as exited. Exit hooks are
separate later evidence: they do not retroactively provide startup acceptance.
Both consoles logged normal shutdown. Fingerprint-checked cleanup stopped only
owned identities, using the accepted ShellManager/pidfd procedure, and killed
the remaining owned recorder after native exit. Each final cleanup records zero
live saved identities, ECONNREFUSED on both ports, no remaining namespace
processes and an absent outer launcher. The redundant browser stop refused its
already-nonlive fingerprint; it sent no unverified signal. Its diagnostic remains.

Private `audit.py` passed 54 evidence assertions, including original-byte
preservation, exact ancestry/session evidence, zero input work and cleanup;
`audited-facts.json` records them. These are evidence checks, not product tests.
`harness-failures.txt` preserves two Codex fixture-preparation failures before
any interactive native launch (artifact symlink roots, then redacted server
startup failure), both empty-namespace cleanup results, an observer's absent
receipt-table assumption and an audit's absent-native-store assumption. The
corrections changed only private fixture/observation helpers. Codex also logged
unavailable filesystem watchers and failed update-network requests in its
isolated namespace; these were not hidden or worked around.

Remaining unknowns: no first-Chat success, resume/daemon/child-session matrix,
longer-idle guarantee, complete pinned Rust hook-timing trace, or complete native
history is established. Codex's future rollout path was not observed before input.
The metadata/lock observations do not independently validate a new admission or
capture rule. No product, test, dependency, schema or shared planning edit,
delegation, merge, PR, deployment, VM or browser installation occurred.
