# D40 manual native setup

Implementation evidence, 2026-10-08. Branch `codex/build-native-setup-r1`, based on
`a475fa5f87cb05b756c870d761c7ccc5b1bd11f2`. Manager review and acceptance remain separate.

## Implemented boundary

`NativeSetup` is passive until the existing session monitor ticks it. It checks
the reserved agent, provisioning recovery hold, owned workspace and recorded
shell through `Terminals.native_context`, with the existing lifetime/effect and
workspace guards. A successful binding is cached; restart validates preparation
again. It neither allocates a session owner nor invokes a native executable.

The agent receives a stable, private `runtime/core-credential.json`, bound to the
installation and agent and containing a bearer generated from 32 random bytes.
Malformed, differently bound, permissive or linked credentials are refused.
The transport retains only digests in a thread-safe local overlay. Each
`create_app` clones configured bindings into its own `Credentials`; shutdown
clears only the local overlay. Existing owner credentials and readiness remain.

`prepare_manual_core` installs the core HTTP MCP entry and a delimited instruction
block for each configured provider. It reuses the manual-hook identity and
locks. Claude's other JSON settings survive; Codex receives an appended core
table and subsequent retries preserve its entire document, including native
trust additions. Changed owned entries, invalid configuration and unsupported
TOML replacement fail with fixed diagnostics. No model, effort, native
authentication or permission preference is added. Existing manual hook
preparation receives the installed Python `-I -m qorqut_core.runtime.hook_client`
command and the exact loopback core URL with the agent bearer.

`runtime_get` includes cached nullable setup/capture observations on connected
and disconnected paths. Setup observations expire after five seconds. The
collector reservation remains `None`; input readiness remains false. The monitor
ticks setup/collection after ownership reconciliation and closes both on its
own thread. Existing server startup/stop and viewer shutdown ordering remains.
Host schema **7**, public operations **16**, job handlers **5** are unchanged.
`config.py`, native readers/capture/parsers and existing preparation helpers were
not modified. The only historical test change omits `local_terminal` from input
to the pinned pre-D36 `InstallationSettings`.

## Checks

An isolated editable installation used the pinned dependencies and a private
test environment. Five grouped owning checks passed in **22.505 seconds**:

```text
python -m unittest -v runtime.test_native_setup
```

They establish:

- Normal console startup and its normal workers create the recorded Bash shell
  and install both providers' hooks, MCP entries and instructions. Explicit
  sentinel executables are never invoked; no native owner is allocated.
- Actual loopback HTTP and official MCP SDK status mutations use the registered
  agent actor. The same bearer passes authentication at the hook POST boundary;
  the deliberately invalid hook body is rejected. No native admission is claimed.
- Cached success causes no per-tick rewrites. Server restart preserves the
  terminal, credential bytes/mtime, config bytes and native-added Codex trust.
  No configured owner bearer is copied to generated agent files or responses.
- Authored instructions, persona/memory/data, unrelated MCP entries, hook groups,
  model/effort and trust settings survive preparation. Owned edits, malformed
  documents, identity conflicts and unsupported table edits are refused.
- Invalid/badly bound/insecure/symlinked credentials and malformed configuration
  report `blocked/setup_unavailable`. Owner calls continue and a real Terminal
  viewer can still execute a harmless Bash command. A recovery activation reports
  `blocked/recovery_review`, preserves tracked bytes/mtimes and leaves Bash alive.
- Two applications created from the same configured credentials do not share
  local registrations. Shutdown clears the local bearer and retains configured
  credentials. Concurrent registrations and conflicting digest attribution are
  checked. Disabled setup, unsupported targets, stale and connected/disconnected
  views, monitor tick ordering and same-thread cleanup are checked.

Four existing focused checks passed in **5.251 seconds**:

```text
python -m unittest -v runtime.test_monitor \
  agents.test_registry.RegistryTests.test_old_code_start_true_receipts_replay_unchanged_after_reopen_and_never_launch
```

These retain sequential ownership observation, fatal readiness behavior,
cancelled startup joining the monitor before storage closes, and all historical
start=true receipt replay/no-launch assertions. `pip check` and `git diff --check`
also passed. No broad suite or additional reviewer actor was run.

Private logs and temporary fixture data were kept under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-setup-r1`.
Short owned Unix socket directories used `/tmp` for the socket-length bound.
The existing fixture cleanup stopped and reaped its own Bash/recorder/tmux/server
processes. No existing service, personal CLI home or agent session was a fixture.

## Manager review correction

The known non-onbash target check now precedes cache expiry. Ordinary agents
remain `disabled/unsupported_target` after five seconds; onbash still becomes
`unknown/observation_stale`. The view remains passive and memory-only.
The existing grouped observation check includes the stale non-host assertion.
Only that affected check was rerun; it passed in **0.376 seconds**:

```text
python -m unittest -v runtime.test_native_setup.Observations.test_passive_disabled_cached_views_and_monitor_thread_lifetime
```

`git diff --check` passed. The private log is `native-setup-r1/review-r2-check.log`
under the run state directory above. No server-flow or broad-suite rerun occurred.

## Limits

Ready means configuration installed. These checks did not launch Claude or
Codex, verify native automatic loading/login/trust, run a model turn, or test
native collection. The separate explorer and Manager's later combined flow own
that evidence. No UI/browser-MCP work, VM selection/install, global installation,
deployment, main merge or PR was performed.

## Manager acceptance

Manager accepts `16c4a58bb1452da86b27b0869bbabd322aca4304`. I reviewed
managed credentials, owned configuration/instructions, monitor/server lifetime,
passive observations and the unchanged no-launch boundary. Nine focused checks
passed independently on the initial candidate in 25.693 seconds. My stale
non-host observation reproduction prompted the small correction above; I reviewed
its exact diff and independently reran the affected grouped check (pass, 0.505
seconds). The revised branch and remote match and are clean. Private evidence
is in `manager-review249/` and `manager-review253/` under the build state directory.
This accepts managed setup only; native loading has separately qualified evidence,
and the combined normal-server/native workflow remains open.
