# Directory-tree file effect

`create_directory_tree` is an action of the existing prepared file-effect family (`PreparedChange` of kind `file`). It
adds no executor, store or marker file: the same `FileApplier`, apply receipt, resource claims, writer admission, policy
revalidation and final record transaction carry it. Ordinary `write`/`delete` effects are unchanged.

## Frozen values

`directory_values(layout)` builds the complete values: the relative layout (`directories`, every parent listed, safe
segments, at most 64 paths and 8 levels), the before state (absence: `before_hash` is `None`) and the deterministic typed
layout hash as `after_hash`. The hash describes a layout of empty directories only; an identical empty tree made by
someone else has the same hash, so **the hash is never ownership proof**.

## Target

The trusted host resolves the effect's resource to a `RegisteredDirectoryTarget(root, relative_path, staging_root)`.
`staging_root` is private runtime state: absolute, outside the target and on the destination's filesystem (a different
filesystem is refused before anything is published). Every path component is walked with `O_NOFOLLOW`; a symlinked parent
or final path is refused. Hosts also register a target resolver that reports `observe_directory(target)` (the typed hash of
what exists, or nothing) so ordinary preparation and recovery checks can compare existence and layout.

## Phases and proof

Recorded in the receipt progress for the resource, each only after the step it names (SQLite runs with
`synchronous = FULL`, so the commit is the durable record):

| Phase | Meaning |
| --- | --- |
| `intent` | The receipt-bound staging name is recorded before any filesystem effect. |
| `staged` | The empty tree was built under `staging_root` and fsynced; its exact identity (device, inode, mode, owner, link count of every directory) is recorded. |
| `published` | `renameat2(RENAME_NOREPLACE)` moved it to the final path, both parents were fsynced, and the destination matched the recorded identity and layout. Only now is `verified` set. |
| `abandoned` | Publication lost a race; exactly the proven empty directories were removed. |

A preexisting final path, including an identical empty tree, is never replaced or adopted. A retry after publication
recognizes only the exact recorded identity and layout. `Application.check_targets` re-proves a published directory each
time a receipt is relied on, including immediately before the domain row and idempotency result commit.
`ApplyReceipts.finish` refuses a directory without a recorded `published` identity.

## Failure and recovery

* Before anything exists (no progress, or `intent` with no staging directory) the application is released normally.
* A proven, unpublished staging tree is removed on release or when publication loses a race: only the recorded empty
  directories are removed, never files and never recursively.
* If staging was interrupted before its identity was recorded (`built` boundary), the application **fails closed** with
  `apply_incomplete` and `details.disposition = "unproven_staging"`: the directory is not adopted, published or deleted,
  and the claims remain.
* Other dispositions (all `details.disposition`, with a `recovery` hint, claims retained): `staging_changed`,
  `staging_missing`, `target_replaced`, `unexpected_contents`. `destination_occupied` is a `revision_conflict` raised
  before any effect of the receipt exists, so its claims are released.

Fault checks set `FileApplier.after_phase` (called with the resource and `intent`, `built`, `staged`, `renamed`,
`published` or `abandoned`) and raise to stand for a crash at that boundary.
