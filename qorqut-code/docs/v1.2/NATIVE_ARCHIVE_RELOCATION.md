# Relocating a copied native transcript

Native Chat archives bind to a transcript's absolute path, device and inode.
Copying a running installation can therefore leave Chat at its last archived
message while the terminal continues: capture reports `source_path_changed`
or `source_replaced`. Ordinary capture must not silently accept a replacement.

An operator performing an authorized instance migration can call
`TranscriptArchive.relocate_source(binding, expected=receipt)`. Supply the
inspected latest receipt and the later, verified native session binding for
the same agent, provider, conversation and workspace. This is a maintenance
library API, not an agent tool or an automatic retry policy.

Under the archive lock, the operation verifies the complete archived prefix
against both its stored digest and the copied transcript, verifies the index,
and checks the new file identity. It rejects changed history, a stale expected
receipt, a non-advancing generation and any unresolved capture tail. It writes
an immutable `relocation-<previous sequence>.json` binding the previous receipt
digest and prefix to the new source path/device/inode/generation. Normal capture
then verifies again and appends the next receipt. Existing receipts, offsets,
message IDs and payload bytes remain unchanged.

Capture and crash recovery accept that source transition only with its matching
relocation record. Unapproved replacement files still fail. Retain the relocation
records with the archive; archive writers predating relocation support cannot
recover a chain across this transition.

The observer subsequently indexes the missing suffix and clears the capture
error through its normal database APIs. Do not reset captures, rewrite archived
receipts, resend owner inputs or restart native agents to repair this condition.
Chat uses the original capture path and the current receipt's verified path when
resolving system-input markers, preserving attribution on both sides of the move.
