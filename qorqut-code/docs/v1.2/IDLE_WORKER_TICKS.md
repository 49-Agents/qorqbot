# Idle worker ticks: terminal control reads first

Package P06 of the v1.2 responsiveness work. Two background workers opened a write unit on every cycle although nothing was due,
holding the single SQLite writer lock for no effect. The host's terminal controller is fixed here; the Signoff owner-attention
tick is the Operations half (Operations `docs/SIGNOFF_IDLE_TICK.md`).

## Terminal control (`app/qorqut/agents/terminal_control.py`)

`TerminalControl._loop` calls `tick()` once a second. It used to enter `database.transaction()` each time just to find the queue
empty. Now `tick()` keeps its admission and authority boundary (`writer_admission.writer()`, then `runtime.require_authority()`),
and inside it first runs `_has_queued()`: one `database.read()` unit with `SELECT 1 FROM terminal_actions WHERE state='queued'
LIMIT 1` (it uses the existing `terminal_actions_queue` index). No queued action returns `False` without a write unit.

When one exists, `_tick()` runs unchanged. It opens the write transaction, repeats the maintenance check and the selection of the
oldest queued action, and repeats every step that guards an effect: the whole-batch authorization of every saved target against
the proposer's current permission and the current target (a revoked grant or an unauthorized target fails the batch without any
effect), the exact terminal binding check (a changed session fails only its own action), the durable `uncertain` state written in
that transaction before the effect, execution outside every transaction, and the final receipt update with the saved revision.
The preflight only decides whether to look; it carries no selection, authorization or binding result into the transaction.
A queued action that appears after an empty read is handled by the next one-second tick, as before; one that is claimed by a
concurrent tick between the read and the transaction is simply not found there and the tick returns `False`.

One visible difference: an empty queue no longer evaluates the maintenance check, so a service in maintenance with nothing
queued stops logging a failed tick every second. Queued work still meets the check inside the transaction, as before.

## Checks

`app/tests/test_host_terminal_idle_tick.py` builds a synthetic host instance (`build_instance`) with a registered Ordubashy that
holds the terminal-control plugin and one business agent, and a recording terminal stand-in; no tmux, native session or provider
is involved. It covers: an empty queue takes no write unit and reports no work; an action queued after an idle tick runs on the
next one with exactly the claim and receipt units, is durably `uncertain` when the effect begins and no unit is held during it; a
revoked permission blocks the effect; a changed binding blocks it; one unauthorized target fails its whole batch before any
effect; an unconfirmed execution stays `uncertain` and is never replayed; a definitely undelivered one is `failed`; and four
concurrent ticks run a queued action once.

```
QORQUT_TEST_WORKSPACE_ROOT=<workspace> QORQUT_TEST_ROLE_ROOT=<role tree> python -m pytest -q tests/test_host_terminal_idle_tick.py
```

The test needs the pinned `qorqut-business-docs-versioning` and a role source whose default tool policies match this
host's operations.

## Effect

On an empty queue a tick performs no write unit, so it takes no writer-lock acquisition; the previous tick body performed one
write unit per tick. Splitting the real due-work transaction is not part of this change.
