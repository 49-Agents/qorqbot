# Shared terminal input — Manager acceptance

Accepted candidate `e3748b1fd73ec6f2570d05539d1d93118707898d` against
[D44](../SHARED-TERMINAL-INPUT.md), directly reviewed on 2026-10-09.
The exact clean candidate and its remote branch matched. Manager reviewed the
production diff, mapped registration/races, control and callback boundaries,
paste/Enter receipts, new tests and original builder evidence.

Manager independently ran the 48 owning runner tests: all passed in 27.663 s,
with no skips. This includes a real disposable recorded Bash/tmux flow proving
human takeover and restarted inspections serialize with automated input on the
same terminal gate, plus bounded repaint/final-proof loss and receipt replay.
The candidate's private editable environment imported the exact worktree.
An initial Manager import probe misspelled `transcripts.probe`; the corrected
probe-first import succeeded before the test run. It was not a product failure.

Private commands, provenance and original output are retained under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manager-review288/`.

This accepts the local mapping/control/receipt contract. The Bash receiver used
synthetic readiness; it does not establish native Chat dispatch. Host composition
must still supply current native ownership, hook, capture and composer evidence.
No provider calls, owner sessions, browser/VM work or deployment were performed.
