# Manual startup → inbox ownership: source evidence

2026-10-08. Exploration only; no runtime execution or acceptance claim. `C:` means
`src/qorqut_core/` at the base below; `Q:` means `bazarlyk/` in the immutable
PORTING source. Line citations refer to those bytes.
Unprefixed filenames continue the preceding directory. `report:` refers to the
accepted native rehearsal below; `R` denotes its original private evidence root.

| Source | Revision / location |
| --- | --- |
| Core base | `010b551722b300ed617dbe9d4e21267a8f35f221` |
| Accepted native rehearsal | `13227a82e0797b081d704998e0dc568c7c60cc12`; [report](native-rehearsal-r1.md), [acceptance](native-rehearsal-manager-acceptance.md) |
| Accepted local ownership / shell / adapter corrections | `1af5f852ccbc83cc898df11099beb4031ca0b2ca` / `395d8315399bd9e10bba7220425ef9b46f16d05c` / `9737e2a7096f4e471e224dcaa7cd90c4d4bc9f3c` |
| Main Qorqut | `e71fd1e527368bfd41b9815126f4bf33452aeebb`; `/home/alp/qorqut/components/core/<revision>/source` from [PORTING](../PORTING.md) and accepted environment evidence |

## 1. Observed native facts, supplied metadata, and process evidence

Original sink records show **both providers**, fresh/resumed, supplied
`session_id`, `cwd`, `transcript_path`, `hook_event_name=SessionStart`, and
`source=startup|resume`. Neither supplies a process fingerprint, platform identity,
unique event ID, sequence, or observation timestamp. Model/cache/permission fields
are metadata. Sink `ts`, ancestry and binding-file environment are fixture output.
Observed cwd was `R/native/<provider>/workspace`; transcript paths were under
`R/native/<provider>/home/`: Claude `.claude/projects/<encoded-cwd>/<UUID>.jsonl`,
Codex `.codex/sessions/2026/10/08/rollout-2026-10-08T12-09-54-<UUID>.jsonl`.
Each provider's resume kept its original UUID/path.
The UUIDs were Claude `d97e2375-5404-4630-a515-67f23c1871ce` and Codex
`01a11c47-4367-7b23-93da-768092953c1f`.

Claude **2.1.287** fired before first input: fresh 15:51:49.278 UTC, resume
16:02:46.741. The caller reserved the fresh UUID and supplied resume UUID; this
does not prove discovery of a freely chosen manual session. The fresh transcript
path did not yet exist. Native `.claude/sessions/<pid>.json` supplied session,
`procStart`, `pidDomain`, version and tmux metadata; comparisons with `/proc`
were collected after input. Fresh pre-input evidence establishes file existence,
not a live read of its contents. The `pidDomain` boot-hash derivation remains
unproven (accepted report:282–311,442–461).

Codex **0.160.0** fired only with first input: fresh 16:10:41.628, resume
16:15:46.567. Fresh UUID was native-selected. Lock/snapshot filenames existed
before input; the rollout did not. PID-tagged logs were read afterward. Open
rollout/lock descriptors and exclusive FLOCK holders were observed **after input**
for both runs; no pre-input lock-holder proof exists. Resume retained the native
UUID but changed process lifetime (report:329–387,463–498).

Recorded chains were hook→sh→Claude→recorder→tmux and
hook→Codex→Node→recorder→tmux. Native PID/start-ticks changed from
Claude `67/205208350` to `455/205280913`, Codex `681/205320326` to
`1001/205354432`. Recorder probes supplied complete child fingerprints
(boot/PID/start/UID/namespace), but Codex's child/fixture runner-native binding was
**Node**, not Codex. Hook children are not independently connected roots. No model tool
calls or native child-session association were established. SIGTERM-to-recorder
observations showed exit in all four runs; Claude emitted SessionEnd, Codex
neither SessionEnd nor Interrupt. These are bounded fixture observations, not
manual graceful-exit/crash coverage (report:194–214,430–440,500–521).
Claude discovery used its supplied UUID unconditionally; only Codex's fixture
checked hook/descriptor UUID agreement. Neither ran production hook ancestry
admission; Claude's diagnostic translation even included earlier-run hooks.

## 2. Current preparation and shell inputs

**Verified code:** `PreparationInput` requires distinct absolute HOME/workspace,
complete separate core/browser endpoint headers, and `RunBinding` containing
agent/conversation/recovery/boot UUIDs, environment generation, **preallocated
run UUID/generation** (`C:adapters/types.py:23–100`). Existing roots open through
no-symlink dirfds; preparation locks both roots, checks identity markers and
generated hashes, and preserves authored files (`managed_io.py:23–121`,
`preparation.py:140–223`).

`prepare` writes immutable
`workspace/.qorqut/adapters/<provider>/runs/<run_id>.json`, containing
provider/version/run/roots. Hook commands explicitly set
`QORQUT_ADAPTER_BINDING_FILE` and invoke caller-supplied absolute sink argv
(three-second timeout); absent argv means no hooks and an explicit gap
(`preparation.py:52–60,103–135,194–199,224–231`). Claude settings/MCP files live
under that adapter directory; `clients._launch` supplies their CLI flags.
Codex config/hooks live under managed `.codex`. Native Codex additions are
preserved only for a previously claimed compatible owned projection
(`preparation.py:115–123,173–223`; `clients.py:13–52`). No production sink exists.

`ShellManager` requires explicit database/runtime/socket/Python/tmux/Bash paths;
`ShellSpec` supplies installation/agent/recovery/environment, existing distinct
HOME/workspace and PATH. It creates native/XDG directories, records terminal
UUID/source/epoch, and launches only Bash `--noprofile --norc -i` through the
recorder. Environment supplies HOME, CODEX_HOME, CLAUDE_CONFIG_DIR, XDG paths,
PATH, TERM, LC_ALL, QORQUT_TERMINAL_ID and QORQUT_SHELL_LAUNCH; no adapter
binding/configuration is installed. Paths require owned directories, private
runtime/socket parents, no symlink crossings, executable files and a socket path
at most 100 bytes. Capture uses terminal UUID as producer run ID, generation 1
(`C:runner/shell.py:92–108,114–162,193–266`; `shell_models.py:26–51`).

**Static inference:** human input can start a CLI in this shell without a native
run allocation, but current adapter preparation cannot. Nothing connects those
paths. `translate_hook` already requires trusted `NativeBinding`, discovered
`NativeSource`, runner sequence/time and prior ancestry validation; it only
corroborates session/path and returns a non-durable observation. Children/platform
identity in payload are rejected. Fresh Claude's missing source cannot satisfy
that initial translation input (`C:adapters/hooks.py:15–42`, `types.py:129–144,204–215`).
Stop projects idle; SessionEnd projects unknown, not release (`hooks.py:10–12`).

## 3. Ownership admission and visibility

`SessionOwners.connect(OwnerCandidate)` requires agent, terminal, recovery,
environment generation, provider/version, native UUID and `ProcessFingerprint`
(boot, PID, start ticks, UID, PID-namespace inode). Shell records supply terminal
coordinates and shell/recorder fingerprints; hook metadata supplies native UUID;
neither establishes their trusted association to the actual root CLI/version.
No candidate derivation/caller is wired. `connect` validates local reserved
onbash, epoch/profile and process liveness; it does **not** verify terminal
ancestry, native-session correspondence or ShellSpec/environment association
(`C:runtime/models.py:20–30`; `owners.py:45–64,87–147`; `runner/models.py:29–34`).

Exact complete binding reconnects. A different live candidate conflicts even
when resuming the same native UUID; unknown retains ownership/unavailable;
verified exit permits replacement. Admission allocates owner UUID = run UUID
and next generation. `release(owner_id, process)` and `reconcile()` reobserve and
fence the exact saved binding. `observe_process(expected)` classifies missing,
dead, boot/start-changed processes as exited and inspection failures as unknown;
it does no discovery. Monitor reconciles every second. `runtime_get` uses a
five-second observation window, never expires ownership, and always reports
`input_ready=false` (`owners.py:102–202`; `process.py:13–63`; `monitor.py:31–47`).
Server lifespan starts/joins that monitor and includes its health in readiness
(`C:transports/server.py:111–133,194–199`); `AgentView.current_run` remains null
and activity unknown (`C:agents/models.py:127–131`).

`SessionConflict` has Python `reason=session_conflict`, code `invalid_transition`
and “Another session is connected…” text. Public `Error` contains only
code/message/retryable. No connect transport, conflict event, hook response or
Terminal presentation is wired; `runtime_get` exposes the incumbent
(`C:runtime/models.py:12–17`; `contracts.py:75–84`; `transports/server.py:221–231`).

**Main Qorqut, static source:** registration-scoped generated hooks call
`claude_session.handle_hook`→`native_binding.attest`→bootstrap HTTP. Attestation
rejects child markers/diagnostic argv and walks native ancestry; target-local
`NativeBindingAdapter.verify` corroborates pane ancestry/foreground group or
Codex app-server writer lock (`Q:bootstrap/preparation.py:374,470–471,614–630`;
`claude_session.py:65–108`; `native_binding.py:15–83,370–388`). HTTP checks
credential registration, verifies native binding, then calls
`inbox.start_session`; binding failures become `inbox_binding_error` and paused
hook context (`Q:transport/http.py:1263–1329`; `claude_session.py:140–164,227–237`).
Crucially, `start_session` permits a different verified startup/resume/clear/fork
selection to replace a live/unknown incumbent, advances generation and overwrites
the watcher. This contradicts D36's incumbent-wins rule
(`Q:inbox/service.py:198–264`). It fences superseded consumers/generations on
subsequent access (`169–185`); this is not current core behavior.

## 4. Identity after admission and remaining blockers

HTTP/MCP credentials resolve only `Invocation(actor_id)`; MCP is stateless and
shares application dispatch. No native owner travels in that principal
(`C:transports/configuration.py:121–147`, `boundary.py:66–99`, `mcp.py:47–69`;
`contracts.py:103–106`). `messages_send` saves actor/message/thread/recipient,
delivery and conversation entries; list/acknowledge use logical actor/message
IDs, without an ownership gate. `_delivery_resolution` always waits for runtime
integration; `deliveries_get` returns no attempts (`C:messaging/service.py:34–162`).

`TerminalRunner.register_binding` needs a separately supplied exact runner
RunBinding: agent/epoch/environment/boot/run/generation plus tmux, recorder and
native fingerprints, but no native UUID/terminal UUID. `Tmux.inspect` checks
saved fingerprints/pane, not ancestry/foreground ownership. `attempt` consumes
attempt ID, exact body/hash and control epoch with capture/hold/composer/paste
callbacks (`C:runner/models.py:55–73`; `terminal.py:70–88,121–139,187–260`;
`tmux.py:73–104`). Composer snapshot recognition still never permits paste
(`C:adapters/types.py:218–228`).

Shell human controls instead use `runner_shell_control`, terminal UUID,
viewer/epoch/sequence and a terminal-keyed lock; runner automation uses
`runner_control` and a run-keyed lock. No shared hold or owner linkage exists
(`C:runner/shell.py:418–469,494–521`; `terminal.py:195–225,262–328`). Application
assembles owners but no ShellManager/TerminalRunner; workspace provisioning stays
dependency-held, and no terminal network route exists (`C:app.py:125–159`;
`agents/workspace.py:287–291`; `transports/server.py:221–231`).

Dated publication consumes saved message entries; native assistant insertion and
owner/source linkage are absent (`C:conversation/ledger.py:32–74`). Shell raw
capture remains terminal-scoped independently of ownership; it cannot stand for
semantic transcript capture.

**Unanswered Manager decisions:** What establishes root CLI/session/terminal
association and preparation without a preallocated native run? What handles the
observed pre-input/source gaps and surfaces conflict versus unavailable? What
binds the admitted owner to inbox consumption, dispatch, terminal hold and native
source lineage? These are blocking seams, not proposed interfaces.

## Provenance and inspection limits

Commands: `git status --short --branch`, `git rev-parse HEAD`, targeted
`rg -n`/`nl -ba`/`sed`/`cat`; Python JSON metadata extraction and SHA-256.
An immutable `git diff --name-only` against `5a50666c981c727c32dad239886ba5fcd7c9f1ac`
was also inspected; behavior citations use the base, not that candidate.
No suites/rehearsals were rerun. No provider, browser, tmux, service or VM was
started; no operational stores, moving lanes or suppressed material were read.
No code was imported from Qorqut. Credentials and transcript bodies are omitted.
Private hashes/metadata: this run's `explore-manual-hook-seams-r1-validation/provenance.json`.
The saved `verify_metadata.py` was run with `/usr/bin/python3`; it rechecks those
finite source/artifact hashes and prints only startup metadata/ancestry.

`R` below is the accepted report's original private
`native-rehearsal-r1-validation` directory. Hashes recomputed from original files.

| Original artifact under R | SHA-256 |
| --- | --- |
| `logs/hooks-claude.jsonl` | `43678ee3a7403a0f79028c01aac0125bfcf93d774a4a35f39fea079195e6b9c4` |
| `logs/hooks-codex.jsonl` | `20869dc08da3986c3aca93a032241bddf12a0b0a9ff33071e8bef61154ba20f7` |
| `fixtures/hook_sink.py` | `a7226edd3968dbca465657b6a9ba10ebcef0e8b954fd245edd420a26a0fce4f7` |

| R/evidence flow | `links.json` SHA-256 | `launch.json` SHA-256 | `stop.json` SHA-256 |
| --- | --- | --- | --- |
| `claude-fresh` | `b1e2aef8b525c5ed8e166fb9b07c1c6bca40ea403a453b716bd962b38909433e` | `5a93188dca5082711292965e17038b8530df287dbda74a107259964ef735f71f` | `74d56661b87d344230c580ae8b6422549935bee9828f3d0d6ebf9d9eca99b287` |
| `claude-resume` | `e0cd6b3bcf639430c8f7efcbc18010c073796fbde3f75a6d7e8e8851170fdb11` | `20c639aad3790f644ef7d0bd65f126440211ca7f592ce8d4c8cd4ef0ce6aff74` | `00e8e9d6944b5eccd0bb0a66e0d4c2580a8f66f17f64f35956746780a7f96338` |
| `codex-fresh` | `06ccad8dc1122d6b41604b199351eaad7ef1716c84acf48fec1abe7d15e12a04` | `cc883af6fd9ee889285d39006cdbddae20cf2b169112ef450e2ed7c599244ef1` | `447aaf2693a5c46732870fe7597fc476243f699feee6a5dd65a4326acd4891dd` |
| `codex-resume` | `4b6db17a5b3aaa5e70422279e3742e3ba8dd3ce23f76776d872895e8aa081c02` | `56009f3f5f82de4d725229f4075f9228a580542b8f335a1c27a96236c61807f9` | `06c52cd6f2d3e6ac27a9185a1b04f21e59d4c05658fce316c844baa1509facc0` |

| Pinned Q source | SHA-256 |
| --- | --- |
| `bootstrap/preparation.py` | `6056413202ca7f17ae644d954bdb6db2972daa90ea2d44a9db9d60c1ed2c9f6f` |
| `bootstrap/claude_session.py` | `2db180df4393f6ccca21ad9c22320df8364ccb4bbda9ea6222798db123d3242d` |
| `bootstrap/native_binding.py` | `f8e757d32fa7d37b70ec395693ddf2e1c4c6fce8817f11b62913ddf7f556fe66` |
| `inbox/service.py` | `280df01bfe3fa24a3115ecaa956e11d21fafecbef11aec452c544039b2cf7eed` |
| `transport/http.py` | `fdd3aa00a66ddc8fbfe91fd8b5371abf9e596ba791a530aa4d8e7e32102a5852` |

The pinned `inbox/messages.py` hash also matches PORTING's
`041cba06ba21328e1c75cb80f21267096a4ccede1f989fcd19bb13888bcec65f`.
