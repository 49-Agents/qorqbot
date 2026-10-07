# Recorded Terminal server: Manager acceptance

Accepted source: `003c5badc3aa7e6d3f958b461e8e7a98dda49758`, based on
`c3d5b633aedcc1fdd056a02136259eb26f56adb1`, with revision parent
`5d56a45feda60cdcec7bea10457a4e1223f92eb8`. Acceptance is scoped to
[D38](../TERMINAL-SERVER.md), including the four corrections from
[the first review](terminal-server-review-r1.md).

The normal installed console now exposes the persistent recorded onbash Bash
shell over an authenticated Terminal stream. Creation/viewing starts no native
CLI. Human input requires explicit control; disconnect and server restart retain
the recorded shell and durable hold. Ordinary agents remain unsupported by this
local target while their VM substrate is undecided.

## Independent review and verification

A fresh ACLA Codex `gpt-6-astra` reviewer at `max` effort, with self-review
disabled, reported no actionable defects in message 209. It built a fresh wheel
from an exact Git export and ran six owning Terminal groups against its installed
package (55.016 seconds), plus 20 directly affected regressions (20.975 seconds).
All passed without skips. Real checks used the normal console entrypoint and
isolated tmux/Bash/recorder processes; network viewers used actual WebSockets.

The Manager read the service, protocol/admission and amended source, checked
regression assertions and execution evidence, and independently compared all
66 Python/NOTICE files across Git, the review export, wheel and installation.
Seven test/helper copies and 62 loaded module paths matched the candidate.
Only `websockets==17.2` was added; the previous 31 dependency pins are unchanged.
Frozen runner, recorder, adapter, ownership, messaging, storage and shared
lifetime code remain unchanged. The builder and reviewer worktrees were clean;
the pushed builder branch identified the exact candidate.

The four verified corrections are:

- Endpoint shutdown settles a delayed real stale-control error and closes without
  a second cancellation or waiting for a stopped reply writer.
- Loss of the exact tmux target closes its viewer before later altered-pane output
  is forwarded after that unavailable observation, preserving recording and hold.
- A resize blocked on the runner lock leaves one-second state updates and readiness
  responsive; output reads skip a busy stream lock.
- Initialization checks the entire saved UTF-8 settings document before setup.
  ASCII and non-ASCII 16,385-byte inputs are refused without partial setup;
  16,384-byte inputs save and reopen with identical bytes.

The Manager did not run a third copy of the full suites. The independent source,
installed behavior and provenance checks resolve the first review's concerns;
the following combined UI/core acceptance is a separate required check.

## Evidence and limits

The independent wheel SHA-256 is
`26f9b28649f20818e5b876cae948521b96c4e511bdb180486f16fe26327be9a0`.
Private records are under the existing build run's
`review-terminal-server-r2-private/` and `manager-review209/` directories.
Syscall/process evidence identifies two normal console starts and four recorder
processes from the installation. All four fixture roots were removed and all
14 recorded fixture process identities were gone. Captured bytes include output
produced while the console was stopped.

Cancellation timing, some reply-loss/slow-send barriers and capacity/observer
failure injection are synthetic and are labeled in the builder/reviewer reports.
These checks do not prove real-browser behavior, power-loss/reboot behavior,
native startup/inbox connection, Chat delivery, complete native transcripts,
browser MCP/takeover, VM operation or deployment. Local terminal byte capture
is distinct from host archival and native assistant conversation history.
