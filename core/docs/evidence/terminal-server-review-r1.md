# Terminal server r1 review: corrections required

2026-10-08. Candidate `5d56a45feda60cdcec7bea10457a4e1223f92eb8`
is not accepted or merged. Its fresh independent Astra MAX review reported four
defects; the Manager confirmed all four from source and reproduced them before
sending revisions. No production code was changed during this review.

- A cancelled control operation can settle with `revision_conflict`, then wait
  forever to send an error through its already cancelled writer. The Manager
  reproduced pending shutdown/endpoint tasks and an open stream 5.3 seconds
  after the real effect finished; targeted diagnostic cleanup released them.
- Adding/selecting a second window in an owned disposable tmux session leaves
  the old stream open after `binding_unconfirmed`. The Manager received that
  replacement window's output after core's unavailable observation, while those
  bytes were absent from the original recording.
- A resize waiting on the real runner input lock also blocks output/state
  delivery. The Manager measured a 3.426-second state gap during a 3.289-second
  hold while the normal console remained responsive.
- An accepted PATH can serialize beyond the existing installation-file read
  limit. The Manager initialized an 18,676-byte installation file that the same
  application then refused to reopen; this passive probe created no shell.

The Manager checked all 66 package Python/NOTICE files against candidate Git,
the reviewer's source export, wheel payload and installed bytes, and checked
the four copied test helpers before rerunning the three reviewed diagnostic
scripts in separate Manager-owned temporary fixtures. The stream probes use
the actual installed console/WebSocket and real Bash/tmux/recorder; cancellation
uses a synthetic ASGI socket around real runner locking. No failure outcome was
fabricated. Fixture processes/directories were cleaned; human hold remained
until explicit fixture shutdown. No owner data or native/provider calls were
used. Private evidence is in the build run's `manager-review202/` directory.

The independent review also passed the six owning installed test groups.
Those checks omit these four conditions and do not supersede the defects.
The builder must make the numbered corrections and add concise regressions at
the owning boundary. Its revised immutable commit needs a fresh Astra MAX review
and Manager acceptance before paired compiled-UI/core integration.
