# Runtime composition r1: accepted-code evidence

Exploration only, 2026-10-07 America/New_York. Clean starting branch:
`codex/runtime-composition-r1`; inspected base:
`242a7a9e5c2f6f30e3d5a8f4c12deba9c14ddd9d`.
No runtime integration, new contract or implementation decision is supplied here.
Source citations below use paths relative to `src/qorqut_core/`; `tests/` and
`docs/` paths are repository-relative. All line numbers refer to this base.

Read the applicable ancestor/workspace AGENTS.md (no applicable CLAUDE.md found),
IMPLEMENTATION-PLAN, CONTRACTS, BUILD-EXECUTION, CORE-INTEGRATION, AGENT-REGISTRY,
M03/M04/M07/M11 and the four accepted evidence reports linked below. No other
worktree, unfinished M01 work or browser candidate was inspected.

**1. Preparation returns files and an argv recipe, not a recorder launch.**

The concrete seams are `ClaudeAdapter(version)` / `CodexAdapter(version)`,
`prepare(provider: Provider, version: str, data: PreparationInput) -> PreparationResult`,
and `adapter.launch(executable: Path, prepared: PreparationResult, *, native:
NativeBinding | None = None, model: str | None = None) -> LaunchSpec`
(`adapters/clients.py:55–88`, `adapters/preparation.py:76–154`). Exact preparation
profiles are Claude `2.1.287` and Codex `0.160.0`; other versions fail dependent
operations (`adapters/capabilities.py:14–40`).

Required preparation inputs (`adapters/types.py:23–100`):

- Explicit distinct absolute `home` and `workspace`, neither `/` nor containing
  `..`; every ancestor/root must already be a real directory. No-follow dirfd
  traversal opens them; preparation additionally rejects equal device/inode
  roots. It creates generated subdirectories, not either root
  (`adapters/managed_io.py:23–37,50–67`; `adapters/preparation.py:110–118`).
- Agent, conversation, recovery, boot and run UUIDs; positive integer environment
  and run generations. These are supplied identities, not OS observations.
- Separate core/browser HTTP(S) MCP URLs with nonempty, unique explicit headers.
  Validation checks shape, not authentication or caller/target semantics; no
  endpoint, header or credential is discovered.
- Optional nonempty hook sink argv with an absolute executable. The sink's
  existence/durability is not verified. Omission generates no hooks and returns
  `durable_hook_sink_not_configured`. Configured hooks receive the immutable run
  file via `QORQUT_ADAPTER_BINDING_FILE` (`adapters/preparation.py:49–57,147–154`).

Let `H=home`, `W=workspace`, `P=W/.qorqut/adapters/<provider>` and `N` be the
supplied native UUID. Generated paths and structured argv are:

| Provider | Generated provider files | Child argv (optional `--model <model>` appended) |
| --- | --- | --- |
| Claude | `W/CLAUDE.md`, `P/runs/<run-id>.json`, `P/mcp.json`, `P/settings.json` | `<executable> --settings P/settings.json --mcp-config P/mcp.json --strict-mcp-config --permission-mode bypassPermissions --session-id N`; resume substitutes `--resume N` |
| Codex | `W/AGENTS.md`, `P/runs/<run-id>.json`, `H/.codex/config.toml`, `H/.codex/hooks.json` | `<executable> [resume N] --no-daemon --cd W --sandbox danger-full-access --ask-for-approval never` |

Both also return `H/.qorqut/adapters/identity.json`,
`W/.qorqut/adapters/identity.json`, and `P/preparation.json` in `generated_paths`.
Preparation creates `.qorqut/adapters/prepare.lock` under both roots; these locks
are not in that tuple. Generated files are mode 0600, with per-file
fsync/replace/directory-fsync and the manifest installed last. Existing authored
or changed generated files conflict; the selected run file is immutable.
The manifest contains current generated-file hashes, but neither result type
returns a standalone generated-config hash. Authored persona/memory are only
referenced, never seeded/read here (`adapters/preparation.py:28–46,76–154`;
`adapters/managed_io.py:95–124`).

`PreparationResult` contains provider/run/roots, paths, binding path, gaps and
`authentication="required_setup"`; it has no argv. `LaunchSpec` contains
provider/run, argv, `cwd=W`, environment pairs, native/prior-run UUIDs,
setup requirements (`native_authentication`, `workspace_and_hook_trust`,
`recorder_integration`, `native_interaction_acceptance`) and `executed=False`
(`adapters/types.py:119–160`). Its exact
environment is `HOME=H`, `CODEX_HOME=H/.codex`,
`CLAUDE_CONFIG_DIR=H/.claude`, `XDG_CONFIG_HOME=H/.config`,
`XDG_CACHE_HOME=H/.cache`, `XDG_DATA_HOME=H/.local/share`, plus the binding-file
variable. It includes no PATH/TERM/authentication variables or environment merge
policy. Naming these directories does not create them: Claude preparation does
not create `.claude`, and neither preparation creates the XDG directories.
The executable must be absolute; this function does not verify its existence.
Claude requires a reserved exact native UUID even for a fresh recipe; fresh
Codex rejects a supplied non-resume identity and returns no native UUID. Resume
requires the same current run binding and a distinct supplied prior run UUID
(`adapters/clients.py:13–52`; `adapters/types.py:129–144`).

Recorder signatures are `Recorder(spool, source_id: str, source_epoch: str,
run_id: str)` and `run(argv: Sequence[str], *, input_fd=0, output_fd=1,
display_write=os.write) -> int` (`transcripts/recorder.py:74–93,247–258`). The
module CLI additionally requires `--spool`, `--installation-id`, `--agent-id`,
`--recovery-epoch`, `--boot-id`, `--run-id`, `--source-id`, `--source-epoch`,
`--environment-generation`, `--run-generation`, then `-- <child argv>`;
`--busy-timeout-ms` defaults to 2000 (`transcripts/recorder.py:444–470`).

Concrete mismatches: adapter recipes invoke the provider directly; no accepted
wrapper composes them with the recorder. Recorder has no cwd/environment
parameters or CLI switches and uses `os.execvp`, inheriting its own cwd/env
(`transcripts/recorder.py:284–299`). Neither adapter result supplies installation,
durable source/epoch IDs or the spool path. Recorder's CLI creates only a
`terminal` source with no native session and capability text explicitly denying
native semantic capture. The spool's parent directory must already exist
(`transcripts/spool.py:223–229`). No preparation path creates runtime/spool
directories or applies the recipe's setup requirements.

**2. Identity and PTY topology have separate observed and supplied parts.**

The runner addresses a supplied tmux pane (outer terminal). Recorder reads its
outer input and writes its outer output; it creates a second PTY, forks, and the
child calls `login_tty(slave)` (new session/controlling tty/stdin-out-err) then
`execvp`. The parent owns the inner master, commits child output before display,
and forwards input/resize/signals (`transcripts/recorder.py:247–389`). There is
no accepted production tmux launcher joining these pieces. The real-tmux tests
launch a Python receiver directly; the recorder tests create an outer PTY with
`Popen`, without tmux (`tests/runner/support.py:23–48`;
`tests/transcripts/test_recorder.py:23–38`).

| Identity | Accepted observation/check | Supplied or absent part |
| --- | --- | --- |
| Terminal input target | `Tmux.inspect(binding) -> TerminalSnapshot` checks socket device/inode/type/UID, server fingerprint, tmux server PID/session/window/pane IDs, pane PID/dead flag and all supplied process fingerprints, before and after screen capture (`runner/tmux.py:47–106`). Effects also recheck IDs/input flags inside tmux's queue (`:122–158`). | Caller supplies the complete binding/socket/IDs. No name search, `new-session`, process adoption or launch receipt factory (`:1–5`). |
| Recorder | Runner `RunBinding` requires the recorder fingerprint to equal `tmux.pane` and all process boot IDs to match (`runner/models.py:55–73`). | This structural rule plus live checks does not identify a recorder executable or prove its capture loop is active. A separate shell pane PID would not satisfy the equality unless it became the recorder PID. |
| Native/direct child | Recorder locally observes child PID and start ticks after fork; private `_Process` also observes SID/PGID/state (`transcripts/recorder.py:44–71,284–313`). | These are local variables, not an exported/durable runner fingerprint or successful-exec receipt. Runner's `native` fingerprint is independently supplied; no runner check establishes parentage, PTY membership or provider executable identity. |
| Live Linux process | `fingerprint(pid: int, *, proc_root=Path("/proc")) -> ProcessFingerprint` and `verify_process(expected, *, proc_root=...) -> None` observe boot/PID/start ticks/UID/PID namespace, reread start/state and reject absent/zombie/dead/reused processes (`runner/process.py:9–36`). | They do not attest ancestry, argv, HOME, native session, cgroup or responsiveness; a stopped process is not rejected merely for being stopped. |
| Native source owner | `discover(...)` corroborates the supplied native UUID with filename and record byte proofs; hook translation matches that bound source/session/path (`adapters/discovery.py:129–224`; `adapters/hooks.py:15–42`). | `NativeBinding(run, native_session_id, resumed_from_run_id=None)` explicitly presupposes caller-verified process ownership, carries no process fingerprint, and does not attest it (`adapters/types.py:129–144`). No accepted primitive establishes the process-to-native-session association. |
| Logical run/environment | Runner validates supplied boot equality against observed fingerprints; spool persists immutable supplied coordinates (`runner/models.py:55–73`; `transcripts/spool.py:68–82,243–259`). | Agent/installation/conversation/recovery/run IDs and environment/run generations are caller inputs. Recorder/spool do not verify supplied boot or VM/environment identity against the OS. |

`TerminalRunner.register_binding(binding) -> RunBinding` checks a new binding via
tmux and persists it; repeated registration checks stored equality, not current
liveness. `inspect_binding(run_id)` returns saved ownership; `inspect(binding)`
performs live verification (`runner/terminal.py:70–107`). The receiver fixture
puts the same observed Python process in pane/recorder/native slots; synthetic
unit bindings also reuse a fabricated fingerprint. Neither is evidence of a
real recorder/native parent-child binding (`tests/runner/support.py:39–48,161–171`).

Failure cleanup is narrower than supervision. Recorder retains the unreaped
child via `waitid(WNOWAIT)`, verifies its start/SID, includes live background
groups in that original SID, and revalidates PID/start/SID through pidfds before
TERM/CONT then KILL. Each phase has a one-second deadline; direct-child reaping
has 250 ms. Before child session creation only the exact child is eligible.
Normal signal forwarding uses the actual PTY foreground group, with additional
direct/original-group TERM/HUP handling (`transcripts/recorder.py:108–245`).
Cleanup runs on failures; normal return proves direct-child exit plus PTY drain,
not arbitrary descendants' termination (`:319–389,398–441`). Detached sessions,
atomic quiescence against concurrent forks, cgroup producer pause and service
restart survival are not provided. M03/M11 explicitly require independent runtime
cgroups; those planning requirements are not fulfilled by these process tests
(`docs/modules/03-runner.md:19–27`; `docs/modules/11-operations.md:15–27`).

**3. Automated readiness is injected; neither saved health nor liveness certifies it.**

`TerminalRunner(database, *, tmux=None, capture_ready=None,
classify_composer=None, verify_paste=None, machine_hold=None)` accepts:
`capture_ready(binding) -> bool`, `machine_hold(binding) -> bool`,
`classify_composer(binding, snapshot) -> Literal["empty","busy","blocked","unknown"]`,
and `verify_paste(binding, snapshot, body_sha256) -> bool`
(`runner/terminal.py:29–68`). All are absent by default. Literal `True` capture,
literal `False` hold and an exclusively editable, classified `"empty"` composer
are required (`:121–139`).

`attempt(binding, attempt_id, body, *, expected_epoch)` serializes under the run
flock, retains exact-body receipts and checks readiness before load, after load
and after durable paste intent. After possible paste it checks capture/hold and
paste proof, commits Enter intent, checks capture/hold again, obtains a fresh
tmux-bound snapshot/proof, then checks capture/hold once more immediately before
Enter. Failure after possible paste is uncertain; retry returns the saved
receipt without repeating input (`runner/terminal.py:162–260`;
`runner/tmux.py:122–158`). Callbacks run outside SQL transactions under the same
input lock, with no callback into the runner or input effects; readiness calls
are prompt observations, not settling loops (`runner/terminal.py:29–51`).
These ordered checks do not make asynchronous process death atomic.

There is **no `CaptureSpool.health()` method/property** at this base. Health is
`state(source_id, source_epoch)["health"]`, and
`require_healthy(source_id, source_epoch) -> None` checks that stored value plus
the current spool object's `last_error` (`transcripts/spool.py:281–283,386–400`).

| Evidence | What it actually establishes |
| --- | --- |
| `starting` / `recorder_started_at` | Default epoch health; `claim_recording` reserves a terminal epoch and run before fork, preventing another launch on that epoch. It does not record a PID or successful exec (`transcripts/spool.py:137–159,531–545`). |
| `healthy` | Activation changes the row before any native exec confirmation; any successful new append also sets healthy. Clean recorder exit leaves healthy with finished/finalized/drain fields. Thus healthy can mean retained bytes or a completed recording with no live child (`transcripts/spool.py:402–447,547–565`; `transcripts/recorder.py:284–313`). |
| `blocked` / `incomplete` | `block` latches the local spool object first, but its SQLite update may fail. Nonzero/signal exit marks incomplete. Another connection cannot infer a failed-to-persist local latch from the old row (`transcripts/spool.py:386–400,557–565`). |
| `Recorder.admission_ready` | Process-local `_active` plus `require_healthy`; a new object starts false. `_active` becomes true after parent setup/activation, false on observed child exit, PTY EOF, exception or return. It does not establish native authentication, composer or native-file capture, and is not exported over IPC (`transcripts/recorder.py:74–93,312–329,355–399`). |
| Verified recorder/native fingerprints | Exact supplied processes still exist at observation. No proof of active recorder state, successful native initialization, current storage progress or source coverage. SIGKILL cannot update persisted health. |

Epoch `observed_at` is set at registration; started/finished timestamps and chunk
`captured_at` record specific past events. None is a live recorder heartbeat
(`transcripts/spool.py:137–168,346–355,437–445,531–565`).

The adapter composer is also an incompatible direct callback: `composer(raw,
cursor=None)` returns a `ComposerVerdict` object, not the runner's string literal,
and accepts raw screen text/cursor rather than binding/snapshot. Its
`native_verified=False` and `permits_paste=False` even for synthetic `empty`;
discarding that evidence and passing `.state` would not establish the runner's
required native eligibility. No accepted provider `verify_paste` implementation
exists (`adapters/clients.py:79–80`; `adapters/composer.py:20–70`;
`adapters/types.py:218–228`; `runner/terminal.py:35–46,129–139`).

Missing observations for an actual managed native ready claim are the joined
launch/process/native-session ownership, successful provider/config/MCP/auth/trust
initialization, externally observable active recorder state, verified native
source ingestion/coverage, native empty-composer/exact-paste proof and a real
external hold observation. No production caller supplies those callbacks.
Current exact-version capabilities still mark native capture/resume/MCP/auth and
child coverage unsupported (`adapters/capabilities.py:17–35`). No freshness
threshold or ready default is specified by this evidence.

**4. Source descriptors and spool records are not interchangeable.**

`discover(provider, version, roots: ManagedRoots, binding: NativeBinding)
-> DiscoveryResult` scans only `H/.claude/projects` or
`H/.codex/{sessions,archived_sessions}`. Claude requires the exact UUID filename
and independent primary `sessionId`; Codex requires the matching rollout UUID
(optional `_UUID` suffix) and `session_meta.payload.id`. Cwd/newest selection is
absent. Returned `NativeSource` has provider, binding, Path/relative path,
device/inode, observed size, ownership proof tuples, format hint and
`archived=False` (`adapters/discovery.py:23–55,129–224`;
`adapters/types.py:163–201`). Multiple Codex paths for one native UUID can both
be returned (`tests/adapters/test_adapters.py:583–594`).

Proof offsets/lengths count raw bytes, including the JSONL newline. The metadata
scan bounds individual records at 1 MiB, preserving the descriptor with explicit
large/partial-record gaps when independent ownership exists. Final validation
rereads emitted ownership/attachment proof spans on the original fd in 64 KiB
blocks, reopens the managed path only to compare inode/device, and brackets the
check with size/mtime/ctime observations. A completed append preserving proofs
can pass; this is neither a snapshot nor proof of every byte/newly appended
record. The fd closes before return (`adapters/discovery.py:32–101,159–218`).

The three `RunBinding` types carry different information:

| Type | Supplied fields beyond shared agent/recovery/environment/boot/run coordinates |
| --- | --- |
| `adapters.types.RunBinding` (`adapters/types.py:23–30`) | Conversation UUID; UUID objects and positive generations. |
| `runner.models.RunBinding` (`runner/models.py:55–73`) | Tmux, recorder and native fingerprints; no conversation/installation/native-session field. |
| `transcripts.spool.RunBinding` (`transcripts/spool.py:48–82`) | Installation ID; canonical UUID **strings**, strict generations limited to `2**63-1`; no conversation or processes. Direct UUID objects fail `_uuid`. |

The accepted shared-file test explicitly converts runner UUIDs to strings and
adds a synthetic installation ID; it is the only examined cross-module binding
composition, and supplies a fixture terminal source, not adapter ingestion
(`tests/integration/test_shared_local_db.py:29–57`).

The spool seams are `register_run(binding)`,
`register_source(source: Source, source_epoch: str, run_id: str, *,
capability_evidence: str, locator: str | None = None, file_identity: str | None = None,
native_created_at: str | None = None)`, and
`append(source_id: str, source_epoch: str, offset: int, payload: bytes) -> int`
(`transcripts/spool.py:243–259,302–344,402–463`). Required caller contributions
and unimplemented conversions:

- `Source(source_id, installation_id, agent_id, source_type,
  native_session_id=None, parent_source_id=None)` needs durable canonical source
  and installation UUID strings plus explicit type (`terminal|native|attachment`).
  Adapter discovery allocates no durable source/epoch IDs. Native UUIDs are
  objects in the adapter but optional bounded text in `Source`; terminal sources
  forbid native-session metadata (`transcripts/spool.py:85–103`).
- Adapter `Path`, `(device,inode)`, proof tuples, format hint and gap objects are
  not directly accepted metadata arguments: spool locator/file identity/
  capability evidence are nonempty strings of at most 4096 characters when
  present; capability evidence is required. Native time is optional UTC text
  (at most 64 characters), which discovery does not return. There is no accepted
  serializer or dedicated structured proof/gap field. The spool records supplied
  text; it does not reopen files or check inode/proof hashes
  (`adapters/types.py:170–201`; `transcripts/spool.py:302–344`).
- Proof offsets and observed size are observations, not captured cursors or
  byte payloads. `append` requires contiguous bytes from offset zero for a new
  epoch and atomically stores at most 256 KiB per chunk with its cursor. Retries
  require the original chunk coordinates/hash; conflict evidence is retained.
  No accepted native-file tailer or descriptor-to-append path performs the
  required ingestion revalidation (`transcripts/spool.py:402–463`;
  `adapters/discovery.py:1–6`).
- `link_run(source_id, source_epoch, run_id)` preserves coordinates; it checks
  installation/agent lineage, not current recovery/boot/generation equality,
  allowing historical lineage. `begin_epoch(source_id, source_epoch, *,
  previous_epoch, reason, run_id, capability_evidence, locator=None,
  file_identity=None, native_created_at=None)` records explicit divergence and
  retains prior epochs. The caller supplies prior source/epoch identity and
  reason; neither path resolves `resumed_from_run_id`, native UUIDs or filesystem
  changes into those identities (`transcripts/spool.py:285–300,357–384`).

Spool accepts a supplied parent source after installation/agent checks; that is
not native child discovery (`transcripts/spool.py:316–344`). Adapters skip
child-looking files; Claude child-marked records cannot prove primary ownership
but do not discard independently owned primary bytes. Codex child/fork metadata
prevents returning that candidate as a primary source. Both report unverified
child/partial attachment coverage (`adapters/discovery.py:129–189`). Codex image references
retain raw record coordinates; external bytes are not read and embedded bytes
remain in the raw source. Claude queued-command data stays inline, other
attachment layouts are gaps (`:104–126,196–206`). Hook observations similarly
require caller-supplied sequence/time and verified binding, remain nondurable,
and require a separate real sink (`adapters/hooks.py:15–42`;
`adapters/types.py:204–215`).

**5. Existing callable seams, reusable fixtures and unanswered decisions.**

In addition to the signatures above, accepted local seams include
`runner.storage.input_lock(database, run_id, *, timeout=10)`, independent
`runner.storage.ensure_schema(connection)` and
`transcripts.spool.ensure_schema(connection, *, busy_timeout_ms=2000)`;
both schema owners preserve global `user_version` and use WAL/FULL/foreign keys
(`runner/storage.py:15–85`; `transcripts/spool.py:194–213`). The shared canonical
DB path and private adjacent `.runner-locks` matter to serialization
(`runner/storage.py:85–114`). Spool `pending(..., after=None, limit=4)`,
`acknowledge(..., offset)` and `reclaim(...)` provide local bytes/receipt seams;
ACK means the caller asserts host durability, not that the spool proves host
fsync (`transcripts/spool.py:1–6,465–529`). These remain module-local interfaces.

| Accepted fixture / check | Reusable evidence boundary and injected facts |
| --- | --- |
| `tests/adapters/test_adapters.py:29–57,180–222,298–331,334–354,564–703`; `tests/adapters/fixtures/` | Private managed roots, exact synthetic identities/JSONL, preparation/argv/source mutation checks, hook/composer classification. Native boot/session/headers are supplied fixtures; no provider execution. Composer test explicitly requires paste permission false (`:672–690`). |
| `tests/runner/support.py:23–111`; `tests/runner/test_tmux.py:199–205,230–310` | Positively owned real tmux receiver; `capture_ready=lambda _: True`, hold false, composer/proof from receiver state (`support.py:82–94`). Missing callbacks defer; final-verifier tests inject changed callbacks and assert no Enter/inert retry. No recorder or native client. |
| `tests/runner/test_receipts.py:15–43`; `tests/runner/effect_worker.py:61–70`; `tests/runner/test_crash_and_lock.py:40–120` | FakeTmux/unit readiness and disposable real-tmux dispatchers inject true/empty/proof and test receipts, crash recovery and locking. They do not measure capture readiness. |
| `tests/transcripts/test_recorder.py:23–101,200–292,294–368`; `tests/transcripts/pty_failures.py:194–295` | Real nested PTYs, durable-before-display, partial writes, killed-recorder/epoch reuse rejection; owned pidfd/subreaper failure fixtures. `admission_ready` is checked locally, not through TerminalRunner. Spool run/boot IDs are synthetic (`tests/transcripts/support.py:17–29`). |
| `tests/transcripts/test_spool.py:79–119,121–179,210–277` | Explicit synthetic resume/child/epoch metadata and local append/ACK/reclaim fault boundaries. No native ownership attestation or host archive. |
| `tests/integration/test_shared_local_db.py:24–136` | Both initialization orders, exact shared-file receipts/control/ACK/reclaim/reopen. Uses Receiver's injected readiness; fixture source bytes and simulated host ACK are explicit (`:3–4,54–57,72–74`). No joined recorder/adapters runtime. |

Questions left for Manager; no choices are made here:

- What is the bounded managed-runtime scope/acceptance while VM selection,
  native compatibility and independent service/cgroup supervision remain open?
- What launch ownership evidence will supply the separate recorder/native
  fingerprints and native-session association, including fresh Codex identity,
  and how will uncertain launch/stop/replacement be reconciled? Current
  registration and recorder reservation do not enforce one live run per agent
  (`runner/storage.py:30–43`; `transcripts/spool.py:531–545`).
- What caller owns application of cwd/environment, required root/runtime
  directories, generated-config evidence and real hook persistence? The current
  recipes and recorder arguments do not settle these connections.
- What actual observations will feed capture, hold, composer and paste callbacks?
  Saved healthy rows, generic live fingerprints and synthetic `empty` verdicts
  do not supply the missing observations in section 3.
- What durable source/epoch/resume mapping and provenance/gap representation will
  connect discovery to ingestion, given multiple source paths, incompatible
  metadata types and unsupported child/attachment coverage? Which parts belong
  in the next increment versus later archive work?
- Which owned integration boundaries can be assigned without overlapping M01's
  exclusive migrations/registry/assembly ownership? AGENT-REGISTRY's runtime
  dependency hold is a plan at this base, not inspected unfinished code.

**Checks performed and limits.**

Primary evidence was static inspection (`rg --files`, targeted `rg -n`, `cat`,
`nl -ba`/`sed` on the named checked-out documents, source and tests).
`git status --short --branch` initially showed only the branch line;
`git rev-parse HEAD` returned the base above. The following read-only comparisons
all exited 0 with empty diffs, establishing that the inspected code/evidence
matches the accepted components and that fixtures match combined integration:

```sh
git diff --exit-code 2db2be6d051e83b383e240aed41424a212153107 242a7a9e5c2f6f30e3d5a8f4c12deba9c14ddd9d -- src/qorqut_core/adapters docs/evidence/adapters-r1.md
git diff --exit-code 170412438596a98845e2fee8ed3f48ce984b899b 242a7a9e5c2f6f30e3d5a8f4c12deba9c14ddd9d -- src/qorqut_core/runner docs/evidence/terminal-r1.md
git diff --exit-code 08a0273cccf172d8f5a3c0d98fdff93a36531521 242a7a9e5c2f6f30e3d5a8f4c12deba9c14ddd9d -- src/qorqut_core/transcripts docs/evidence/recording-r1.md
git diff --exit-code 9701c661104f7042d0c001796d517957697b5599 242a7a9e5c2f6f30e3d5a8f4c12deba9c14ddd9d -- tests docs/evidence/core-integration-r1.md
```

A static method-inventory check also exited 0: 18 modules parsed; Recorder has
the local `admission_ready` property and CaptureSpool has `state`/`require_healthy`
but no `health` method. The exact command used no product imports or execution:

```sh
python3 - <<'PY'
import ast
from pathlib import Path
roots = tuple(Path('src/qorqut_core') / name for name in ('adapters', 'runner', 'transcripts'))
modules = {str(p): ast.parse(p.read_text()) for root in roots for p in sorted(root.glob('*.py'))}
for path, tree in modules.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in ('CaptureSpool', 'Recorder'):
            names = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            print(f'{path}: {node.name} methods: {", ".join(names)}')
print(f'Statically parsed {len(modules)} source modules; no product imports or runtime execution.')
PY
```

Historical accepted commands/results and their limits remain in
[adapters-r1.md](adapters-r1.md), [terminal-r1.md](terminal-r1.md),
[recording-r1.md](recording-r1.md), and
[core-integration-r1.md](core-integration-r1.md). They were not rerun. Static
evidence sufficed: **no new runtime validation or synthetic reproduction was
performed**. No provider CLI/model, native store/credential, fixture process,
service, install, VM/network change or global configuration was used; there are
no runtime fixtures to clean up. No parser, archive or readiness claim was added.
Only this evidence file is changed; final commit, whitespace/status checks and
normal assigned-branch push are reported through ACLA. No merge/deployment.
