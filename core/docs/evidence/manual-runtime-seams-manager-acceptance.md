# Manual runtime evidence acceptance

Manager accepted two factual explorations on 2026-10-08:

- [Native conversation seams](native-conversation-seams-p0.md),
  `73bd45c391d484252b68be1339845d89cedb1944`: fresh Astra MAX review 192 was
  clean; reviewer and explorer approved separately in 194/195.
- [Manual startup and inbox seams](manual-hook-seams-p0.md),
  `7b6c4fea770d6ee8c2acbfa6158b15b1c83a3f4a`: fresh Astra MAX review 193 was
  clean; reviewer and explorer approved separately in 196/197.

Both use exact core base `010b551722b300ed617dbe9d4e21267a8f35f221`, the accepted
pinned native rehearsal and explicitly named original synthetic metadata.
Each changes only its evidence report. No production code or design was
delegated through these explorations.

For native conversation evidence, Manager independently matched 21 source and
fixture inventory files to Git, both native final/prefix hashes, both hook-file
hashes and three retained review artifact hashes. Key record checks confirmed
the 35/26-record files, Claude human/assistant identifiers and hook correlation,
Codex contextual-user distinction, different user representation IDs, matching
assistant item/response IDs and preserved resume metadata. Source tracing and
model/SQL inspection confirmed the finite discovery proof, separate raw archive,
message-only canonical writer and absent native reader/collector connections.

For manual startup evidence, Manager independently matched 24 core files, 25
original metadata hashes and six pinned Qorqut source hashes. Checks over the
original hook/drive/link artifacts confirmed four startup/first-input orderings,
root ancestry, missing process identity in native hook payloads, distinct resume
process lifetimes and the Codex Node-parent/native-root distinction. Manager
inspected per-run preparation, shell environment, actor-only credentials,
dependency-held delivery and Qorqut's incumbent-replacement conditional.

The independent reviewers checked the full assigned reports and their material
source/metadata claims; Manager's diagnostics focused on the key claims above.
Private checks and approval receipts live in the build run directory under
`manager-native192/`, `manager-hook193/`, `native192-*` and `hook193-*`.

These are saved-fixture observations and static source evidence. No unit suite,
package build, provider/native/browser/VM execution or live owner data was needed.
The reports preserve timing, supplied-association, child/tool/attachment and
streaming limits. They do not establish native history ingestion, a manual CLI
connection or a complete system gate. Manager owns the implementation contracts
that follow from these facts.
