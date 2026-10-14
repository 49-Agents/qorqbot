# Terminal observer: durable directory setup once per binding

2026-10-14, base `4309418e8f36d0a56a49c1cc323441d9fbfb9d22`. A diagnosis of a
six-agent disposable fixture found one real Terminal socket closing when its
cached view passed the unchanged five-second limit. A sequential observer pass
took about 3.6 s idle and 4–5.6 s while input frames were processed; each
agent's visit (about 0.6 s) created and fsynced its directory chain again, 19
directory fsyncs per visit, while holding the effect gate that input, control and
native hooks share. This change removes only that repeated durable setup. It is
not shown to fix every closure, echo delay or hook error, and the pasted
bracketed-paste marker leak is a separate, unresolved question.

## Change

`runtime/terminals.py` keeps a process-local `_prepared` map: agent to
(target binding, saved terminal id). A visit marks it only after the whole
existing creation/fsync sequence and both ownership guards succeeded, whether or
not the later archive transfer succeeds. A visit uses it only when a manager
exists, the saved attempt still matches the current spec and launch
configuration (the unchanged check) and the saved terminal id and target binding
equal the marked ones. Such a visit still opens and validates the workspace,
owner marker, private runtime/home chain and native directories with the same
guards, and still runs `ShellManager.ensure` (which inspects the shell and
recorder), the archive transfer and the saved counters. It does not create a
directory and does not fsync one. Initial and recovery visits are unchanged,
including the parent sync when a directory already exists. A failed observation
(any exception) or a changed target, lifecycle, recovery epoch or terminal drops
that agent's mark only; a restarted core starts empty. Freshness, cadence,
streams, input proof and `ShellManager` are untouched.

## Checks

`TerminalPreparation` in `tests/transports/test_terminal.py` (real tmux/Bash in
a temporary installation; no owner data, provider or login):

- An injected EIO on the first fsync of the runtime root's parent, after its
  mkdir succeeded, leaves the visit unavailable and unmarked; the retry finds
  every directory present and still syncs the complete expected set (all
  ancestors of the runtime root and agent root, the agent runtime and `home`
  directories, and the six native/XDG directories).
- The next visits of the unchanged binding sync none of those directories, still
  call `ensure` and the archive transfer once each, and report capture offsets
  that grow after new Terminal output, within the runner's saved counters.
- An observation failure unmarks; the retry repeats the whole set. A reopened
  core instance starts unmarked and repeats it too. A prepared visit reports a
  shut-down shell as `stopped`: liveness is not cached.
- A different target (the provisioning→active activation) and a different saved
  terminal repeat the setup; a recovery-epoch change refuses and unmarks.
- Prepared visits refuse, create nothing and unmark when the owner marker is
  substituted, `home` is missing or replaced by a symlink or is not private, the
  runtime root is not private, or a native directory is missing; restoring the
  state repeats the full setup.
- With two agents, one agent's refusal leaves the other's mark identical and its
  next visit sync-free, and the retry syncs only the first agent's own paths.

Existing directly affected checks and the new class passed from source: 44 tests
(`transports.test_terminal`, `test_terminal_admission`, `test_local_agents`,
`agents.test_activation`, `runtime.test_terminal_archive`). The new class and the
existing recovery-fence test (`test_workspace_and_recovery_configuration_fences_never_replace_shell`)
passed again, 5 tests, from the installed wheel in a private virtual environment
(`qorqut_core` imported from its `site-packages`). No full suite was run.

Eight deliberately broken copies of `terminals.py` (always durable, no unmark on
failure, no ownership check or native-directory open when prepared, one failure
clearing every agent, a mark that ignores the terminal or the target, a prepared
visit that recreates `home`) each fail at least one of these tests.

## Observe timing (bounded; not a pass threshold)

One in-process fixture per run, six agents (onbash plus five created through
`agents_create`), real tmux/Bash, 12 rounds of one visit per agent and one
`observe_all` pass, wall time and Python `os.fsync` calls per visit. Same host,
load average about 22 before and 20 after, 8 CPUs; single runs, so differences
within a run are not controlled. Latency is not attributed to fsync alone: a
visit also runs `ShellManager.ensure`, tmux and recorder probes, the archive
transfer and SQLite commits.

| | Before (base) | After |
|---|---|---|
| directory fsyncs per steady visit | 19 | 0 |
| steady visit, median (min–max) | 0.50 s (0.32–1.75) | 0.10 s (0.07–0.24) |
| six-agent pass, median (min–max) | 3.10 s (2.60–11.49) | 0.63 s (0.53–1.03) |

The input/echo/hook contention of a real socket, and the first visit of each
binding (unchanged), are not measured here.

## Artifacts and limits

Private artifacts are in
`/home/alp/qorqbot-launch/qa/2026-10-14/acla-browser-a147d77a/fix-terminal-observer/evidence/`:
`timing-before.json`, `timing-after.json`, `observe_timing.py`, the source and
installed-wheel test logs, `mutate.py` with `mutations/results.txt`, the wheel
and `wheel.sha256`, and the private source and installed virtual environments.
The wheel is `qorqut_core-0.1.0-py3-none-any.whl`, SHA-256
`25993a91ffefe6c8d3fdd1adc4612e6b2e9c1071be05f5ad3d5607e5a867b3e3`.
SHA-256 of the changed source: `terminals.py`
`ed0587be2adad9b147eeba54855de0d8f6aa089791454adf2965bfa4293d44d0`.

Limits: the mark is memory in one core process and certifies directory setup only;
a directory removed behind a prepared agent is unavailable for one visit and the
next visit recreates what the durable path creates, as before for an unprepared
agent. Nothing was installed into the shared six-agent or real-provider
fixtures and no live socket, typing or hook probe was run; the effect on the
observed closure and on echo and hook latency still needs the Manager's rerun
against an installed build.
