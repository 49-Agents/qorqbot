# File references — Manager acceptance

Accepted candidate `7f681b79a16d932b0d395f6c5729c0d10f20cb3b` against
[D45](../FILE-REFERENCES.md) and the default-label decision, directly reviewed
on 2026-10-09. The exact clean candidate and remote branch matched. Manager
reviewed the full production change, shared prepare/replay path, no-follow
metadata observation, schema-8 preservation/immutability, typed ledger entries,
publication, owning tests and original builder evidence.

Manager independently ran 12 file-reference, populated-v7 migration and shared
HTTP/official-MCP checks: all passed in 30.638 s, with no skips. They include
metadata-only observation of a 1 TiB sparse file, negative mtime, Unicode/default
labels, no source reads, immutable attribution, replay after deletion, rollback,
source changes, headless dated publication and restart.

Integration with D43 kept both the control-containing message body and the file
metadata assertions in their shared transport test. The historical pre-D43
receipt fixture now originates a separate genuine v7 store, lets the pinned old
executable write its failed receipt, then explicitly migrates before replay.
No old receipt/assertion was removed or changed. Both integration checks passed
in 17.361 s, preserving failed legacy delivery after recovery while new bodies
with controls remain queued. No production behavior was changed for the fixture.

Private original output/provenance is under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manager-review292/`
and `manager-integration288-292/` beside it.

This accepts persisted past metadata and publication, not current availability,
file contents/preview, guest absolute-path mapping, UI cards or full-system
acceptance. No live user data, native inference, deployment or VM setup was used.
