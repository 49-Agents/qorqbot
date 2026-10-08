# Native collection continuity: Manager acceptance

2026-10-09 UTC. Manager accepts implementation
`6341f74d4fa373d2fb9412fe7b7712be597d0f7f` against
[D48](../NATIVE-COLLECTION-CONTINUITY.md). The candidate is clean and pushed and
changes only native_collector.py, its owning tests and the worker evidence.
Manager read the full production change, related ownership/archive/service paths
and changed tests. The original audit documents remain unchanged.

Manager independently ran all twelve collector checks: 12 passed in 40.348 s.
They cover periodic late-source discovery, restart, closed-owner scheduling,
held-descriptor draining, recovery holds, per-source fairness/freshness, exact
run/archive binding and raw-health refusal independent of parser coverage gaps.
The immutable memory accessor matches the merged D46 consumer; it does no I/O or
input. Schema 9's earlier inventory change merged mechanically into the existing
collector test. Three focused composition checks on the combined branch also
verify that inventory, normal application dispatch and raw/semantic gap separation;
their command/results are saved beside the first run. The first composition
command passed the collector case but two input fixtures rejected the Manager's
overlong TMPDIR before setup (the socket path exceeds 100 bytes). Only those two
checks were retried with a short temporary root; both original results remain.

Private Manager commands/results are under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manager-review304/`.
This accepts the bounded correction, not real native Chat dispatch. The
[parallel connected checks](../CONNECTED-CHAT-ACCEPTANCE.md) use the composed code.
Mandatory final drain, child/attachment capture and complete source coverage
remain open. No live owner fixture, VM or deployment was used.
