# D45 — Durable metadata-only file references

[D63](LOCAL-AGENT-CAPABILITIES.md) extends the absolute host-path contract below
from reserved onbash to every prepared local host agent. Workspace normalization
uses the target agent's own workspace. Other validation and persistence rules
stay unchanged; no legacy guest-path mapping is added.

Manager decision, 2026-10-09. Implement the existing intent tool and its durable
conversation record independently of input delivery. No file preview, download,
content snapshot, general file API, VM mapping or UI is part of this core change.

## Operation and path contract

One operation: `files_reference`, mutation with the shared idempotency key.
Arguments: path (1..4096 characters), optional agent_id UUID, label (1..200 when
supplied), note (0..4000 when supplied). Missing agent_id resolves from the
registered calling agent; an owner must supply it. No actor or conversation ID
comes from the caller. All registered actors may select any registered target.
Reject archived/archiving or uninitialized workspaces before a new reference.

Keep validated request text unchanged for shared input hashing: omitted versus
explicit target/label and differently spelled paths are different arguments for
the same key, following the existing operation contract. Path must be valid UTF-8,
contain no C0/DEL/C1 and no `..` component; do not strip spaces or expand ~, shell
variables or URLs. Normalize repeated separators and `.` with POSIX lexical
rules for the saved location; reject root/directory/empty targets. Label must
contain non-whitespace text and no controls; preserve its supplied spacing.
Note preserves supplied text, allowing LF/TAB but no other C0/DEL/C1. Null note
stays null. Default label is the normalized basename. If that basename is
whitespace-only use `File`; if longer than 200 Python Unicode characters use its
first 199 characters plus U+2026. Other basenames retain their exact spacing.
All saved labels share the same nonblank 1..200-character bound. This affects
only the derived display label: full paths and original idempotency arguments
remain unchanged, and valid supplied labels override the default exactly.

Relative paths identify a file in the target's persistent authored workspace,
which is already host-owned for both agent kinds. They need no native session.
A native absolute path is supported only for reserved host onbash. Normalize an
absolute path contained in onbash's workspace to location=workspace and a relative
path; other onbash absolute paths use location=host and absolute path. Refuse all
ordinary-agent absolute paths (including /workspace) with unsupported_capability
until the guest mapping/runner exists. Never guess a guest path on this host.

Reject symlinks in every component and non-regular final files; reuse existing
no-follow descriptor primitives. Open read-only/nonblocking and fstat without
reading source contents. This deliberately small policy is target validation,
not a defense against the owner using their own unrestricted shell.

## Immutable public record

FileReference fields are: reference_id, agent_id, conversation_id, actor_id,
installation_id, recovery_epoch (UUIDs); state literal `referenced`; location
`workspace|host`; path and requested_path (normalized location and unchanged
validated request respectively); label; note|null; size_bytes (nonnegative signed
64-bit range); mtime_ns (decimal string, allowing negative values); observed_at
and recorded_at (UTC); sha256 literal null. The decimal string avoids JavaScript
nanosecond precision loss. There is no invented VM environment generation.
Workspace provenance is stable agent/installation identity; host location is a
path observation in that installation, not a promise it maps after import.

Return `{reference: FileReference, entry: EntryReference}` as completed only once
reference, event, entry and operation result commit. Event `file.referenced`
contains reference_id, agent_id, conversation_id, location and path, with normal
authenticated actor/operation attribution. It never says the user saw/opened it.

Add FileReferenceSource `{type: 'file_reference', reference: FileReference}` to
Entry.source and kind `file_reference`. Preserve all existing message/native
wire bytes and source semantics. New entry text equals the saved label, role is
user for an owner caller else agent, direction is outgoing, sender_actor_id is
the authenticated actor; occurred_at=observed_at, recorded_at is commit time.
Delivery/handling are null. Entry fields and full reference metadata appear in
the existing format_version=1 daily JSONL; use the same publication mechanism.
There is one entry per reference ID. Text/paths/tool transcripts never infer a
reference or card. Later reads return the saved observation even if the source
changed/disappeared; no current-availability assertion or source reopen occurs.

## Transaction and filesystem boundary

Metadata observation is external I/O, not work inside a write transaction.
Extend the fixed Operation descriptor only with optional `prepare` and optional
`description`. prepare(validated_args, Invocation) returns a per-call bound
handler with the normal four-argument handler signature. It may observe files
outside SQL; no mutable shared invocation state or runtime registration system.
Use this only for files_reference. Description defaults to the existing tool
name text; MCP uses it when supplied. File tool description says when to call it
and that path/metadata is saved, with no UI mechanics.

Application retains one shared validator/hash/result-commit path. For a prepared
mutation, first check actor and existing operation/key/hash in a read transaction;
return an exact compatible saved result before filesystem access. On a new key,
under existing installation use/effects guards, prepare the observation outside
SQL, then run the normal transaction with its bound handler, checking replay
again in that transaction. Reuse a small prior-result helper for both checks.
Normal operations without prepare keep their current path. Do not create a
second transport-specific implementation of validation, hashing or persistence.

Preparation resolves target/installation/epoch in a read transaction; checks the
workspace ownership marker; opens the target and checks metadata plus descriptor/
path identity before ending observation; closes descriptors before commit. The
bound handler rechecks registered actor, target identity, lifecycle, initialized
workspace and installation/epoch against the observed binding, then commits.
A source may change after observation: record the time and metadata honestly,
without claiming filesystem/SQL atomicity against external writers. Filesystem
errors before observation completion produce typed errors and no reference/entry.
Use fixed public messages, not arbitrary OS diagnostics or file contents.

## Storage and validation

This builder alone owns host migration 8, Operation/Application assembly and
conversation union changes in this wave. Add immutable file_references and a
nullable file_reference_id FK/unique entry linkage; rebuild conversation_entries
constraints as required, preserving original message/native rows, cursors,
foreign keys and immutable triggers. File rows require non-null reference/actor
and no message/native source coordinates; message/native rows require null file
linkage and retain all prior rules. Preserve historical operation JSON unchanged.
No other new operation, job kind, worker, broker, package or dependency. Inventory
expectations become 17 public operations, existing 5 handlers, schema 8.

Own files/reference domain, Application/Operation, storage migration, conversation
models/read/append/publication support, minimal MCP description seam, focused
owning tests and evidence. Leave runtime, input adapter/runner and UI alone.

Checks: regular Unicode path metadata without reading a large file; missing,
symlink, traversal/non-regular refusal; owner/agent target attribution and ordinary
absolute-path refusal; exact same-key replay even after source deletion and
conflicting-key refusal; one event/entry/job with rollback on commit fault;
schema-7 populated message/native upgrade unchanged; headless publication and
restart of the file entry. Extend one shared HTTP/MCP test to prove the same tool
contract, not duplicate domain suites. Run only affected inventories/migration/
conversation checks. No native models, UI fixtures or broad workflow reruns.
