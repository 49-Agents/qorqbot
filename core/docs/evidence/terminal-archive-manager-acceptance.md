# Recorded Terminal archival — Manager acceptance

2026-10-09. Accepted candidate `121d1b55125914329f779156ca76de52c5611840`
against D41. The installed-flow implementation pin is
`3994fae36ba2411aba19330853d99873e0766ffc`; only the evidence document changed
between those commits. Manager reviewed all five changed files and reran the
seven owning archive checks plus the affected Terminal transport flow:
**8/8 passed in 33.354 seconds**.

The transfer validates the original saved terminal producer, bounds each
observation to four chunks, persists the exact intent and host receipt before
local ACK/reclaim, and keeps host lifetime/effects guards through cleanup.
Interrupted operations retain retry evidence. Recovery/conflict admission is
checked before cleanup, including an already-ACKed intent; no empty-page or
historical-watermark cleanup shortcut exists. Host storage errors remain
separate from shell availability and recording health.

Manager independently checked the saved normal installed-server evidence:
local/host counters progressed 18/18, 116/116, 211/116 during core downtime,
211/211 after restart, then 297/297 after normal Bash exit. All seven chunk
coordinates and hashes agree with the saved bytes and host ledger; the final
stream SHA-256 is
`9ca40f35f193fb127239647a616df26a7396ed5fe85db24abf866fc18249ca61`.
Original run/source/epoch metadata, shell identities and control survive restart.
The three intended output markers occur once each. No native owner, Chat entry
or completed transfer intent was introduced. Both saved databases passed
integrity checks. Normal-server harness inspection confirms archival is driven
by the observer rather than a manual transfer/job call.

The reported fixture cleanup left no known process, listener or matching socket
and removed its disposable data. The earlier fixture setup failures remain
recorded. Existing Archive filesystem durability tests are retained, not
reimplemented here. This accepts automatic host archival for the recorded
onbash Terminal, including outage catch-up and stopped-shell drain; it is not
full G3, native Chat delivery, reboot, backup, VM/browser or deployment acceptance.

Private direct checks are under `manager-review275` in the existing run's
private evidence root. See [the builder report](terminal-archive-r1.md) for
installed provenance and bounded-flow limitations.
