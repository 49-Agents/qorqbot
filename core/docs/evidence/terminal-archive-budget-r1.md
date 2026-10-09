# Small-chunk Terminal archival budget r1

Implements only the Small-chunk Terminal archival section of
[D49](../RESPONSIVENESS-CORRECTIONS.md) on
`codex/fix-terminal-archive-budget-r1`, based on
`ac607b80a40cc90ab57c6f9636bfd9e9e579accd`. Manager review is pending.

Each existing transfer observation now completes at most 128 original chunks
and 1 MiB. A monotonic deadline starts at transfer entry; the first pending
chunk may finish even if preparation consumes 200 ms. Later chunks begin only
before the deadline, and a chunk that would exceed the byte budget remains
pending. Receipt persistence, ACK, reclamation and intent retirement complete
together before the next scheduling check. Blocking I/O can exceed 200 ms;
this is a scheduling budget, not a hard latency guarantee.

Original chunk coordinates/bytes, producer bindings, receipt authority,
recovery/conflict holds and cleanup guards are retained. The observer cadence,
recording format, schemas, native capture and shutdown lifecycle are unchanged.

Focused check, using Python 3.12.3 and unchanged locked dependencies:

```text
PYTHONPATH=src:tests <private>/venv/bin/python -m unittest runtime.test_terminal_archive -v
git diff --check
```

All 10 tests passed in 32.081 seconds. Three added groups verify 128 small
original chunks followed by exact two-chunk continuation after reopening the
host, an uneven byte boundary without splitting/skipping the next chunk, and
deterministic time limits (125 ms steps, exactly 200 ms, one-second I/O and
preparation already beyond the budget). The existing binary case verifies the
exact 1 MiB boundary with a frozen clock. Existing interrupted-transfer,
receipt-mismatch, binding/recovery, retention and lifetime-guard checks pass
unchanged. The existing observation test uses disposable recorded Bash and
verifies host-failure isolation/counter recovery; it passed with owned cleanup.

Private command/results, dependency versions and discovery failures are under
`~/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/fix-terminal-archive-budget-r1/`
(`focused.log`, `focused-command.json`, `environment-setup.json`,
`preflight-failures.txt`). A lane-owned interpreter reads the completed archive
lane's matching dependencies without modifying that environment or fixtures.
Initial discovery commands encountered an absent optional `.github` directory,
no nested instruction-file matches and absent guessed `.venv` locations;
direct reads and the private interpreter resolved these setup issues. No test
command failed.

These are owning component checks with synthetic bytes and the existing local
Bash observation fixture, not a native CLI or combined core/UI acceptance run.
No installed native workflow was repeated. Cold-start, echo association,
final-drain, browser/VM and full-system limitations remain open. Manager owns
combined acceptance and integration; no merge, PR or deployment was performed.
