# D45 file references — builder evidence

Implemented on `codex/build-file-references-r1` from exact base
`0fc5ffbc71cc0036f81967e34e9b670846e1fc6e`, following the fixed
[D45 contract](../FILE-REFERENCES.md). This is implementation evidence awaiting
Manager review, not integration or deployment acceptance.

`files_reference` saves one immutable metadata observation, attributed event,
conversation entry and operation receipt in the shared mutation transaction.
Preparation checks replay before opening a source, observes outside the write
transaction under the existing installation guards, closes descriptors, and
rechecks actor/workspace/installation bindings before committing. Request text
remains unchanged for hashing. Only the saved location is normalized.

Relative paths use either kind of agent's initialized authored workspace.
Absolute paths are accepted only for reserved onbash; those within its workspace
save a relative workspace location, and other absolute paths save a host
location. No-follow directory descriptors, regular-file checks and before/after
descriptor/path metadata checks reject links, special files and observed
substitutions. Observation does not read source bytes or compute a source hash.
Saved nanosecond modification times are decimal strings, and `sha256` is null.

Manager's ACLA decision 291, answering question 289, resolves derived labels:
all saved labels use the same nonblank 1..200-character bound. A whitespace-only
basename defaults to `File`; an overlong basename defaults to its first 199
Python Unicode characters plus U+2026. Other basenames and valid supplied labels
retain their exact spacing. Both path fields remain complete, explicit labels
override the fallback, and default derivation never changes the input hash.

Migration 8 adds immutable `file_references` and the unique nullable entry FK.
It rebuilds the ledger and its native child while foreign keys remain enabled,
preserving prior rows, cursors, provenance and immutable triggers. The exact
statements and checksums of migrations 1–7 match the base. Existing message and
native wire records and daily JSONL bytes remain unchanged. File entries use the
same format-version-1 publisher, with full saved reference metadata and null
delivery/handling. Inventory is 17 public operations and the existing 5 handlers.

## Checks

All checks used disposable installation roots, synthetic data and private child
servers. No provider inference, native CLI session, UI, VM, browser profile or
operational Qorqut data was used. There were no reviewer agents.

49 distinct targeted tests passed across these groups:

- 10 file-reference checks: Unicode and a 1 TiB sparse file with source reads
  blocked; negative nanosecond mtime; strict controls/UTF-8/bounds; literal spaces,
  tilde, variable and URL-shaped paths; Manager's derived-label cases; owner,
  agent and cross-target attribution; onbash locations and ordinary absolute
  refusal; missing/link/directory/FIFO/socket refusals; missing ownership marker;
  parallel exact replay and conflicting hashes after deletion; both replay
  checks; precommit rollback and postcommit response loss; observed content,
  inode and ancestor changes; binding/epoch changes; immutable source linkage;
  headless restart publication and later reads without the source.
- 1 populated schema-7 upgrade check: immutable message and synthetic native
  user/agent rows, complete prior table values and operation JSON, cursor and
  transcript byte preservation; migration rollback/retry; FK/integrity checks;
  retained update/delete/replace protections; appending and publishing a new
  file entry after upgrade.
- 20 existing conversation ledger, native projection and publication checks.
- 1 extended shared HTTP/official-MCP test: matching schemas, description,
  attribution and exact mutation results, cross-transport replay after deletion,
  conflict parity, headless publication and server restart.
- 17 selected operation, inventory and migration regressions: normal-operation
  validation/hash/replay/rollback, passive assembly, old-schema upgrades and
  immutable legacy receipts, plus the existing assertions affected by the new
  operation/schema counts. No broad runtime workflow suite was run.

The archived test commands, results and SHA-256 log manifest are in
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-file-references-r1-validation/`:

| Original artifact | Result |
| --- | --- |
| `commands.json` | Exact test selections and log manifest |
| `domain-conversation-transports.log` | 31 passed; transport child failed to start in the older environment missing the declared `websockets` dependency |
| `inventories-migrations-operation-regressions.log` | 13 passed; two mistyped test-class selectors and two imports blocked by that missing dependency |
| `environment-setup.log` | Private venv installed the existing project requirements; no dependency changes |
| `inventories-migrations-operation-regressions-rerun.log` | All 17 passed with corrected selectors and dependencies |
| `shared-http-mcp-rerun.log` | Shared transport check passed |
| `files-label-decision-final.log` | All 12 owning file/migration/transport checks passed after decision 291 |
| `migration-checksums.log` | Migrations 1–7 unchanged; migration 8 checksum recorded |

Tests ran with `PYTHONPATH=src:tests`; the final targeted checks used the private
`build-file-references-r1-validation/venv/bin/python`. `git diff --check` passed.
The earlier logs are retained rather than overwritten by successful reruns.

## Limits

A reference is a past metadata observation. An external writer may change or
remove the source after observation, including before SQL commits; this is
tested and does not become a current-availability claim. Host paths retain their
original installation provenance without promising relocation after import.
Preview, download, content snapshots, general file APIs, guest mapping, input
delivery and UI rendering remain outside this change.

The current agent schema persists only `provisioning`; archived/archiving
admission refusal is exercised with synthetic registry rows, without adding
lifecycle operations or extending that schema. Existing initialized workspaces
need no native session. No merge, PR, deployment or complete-system acceptance
is claimed here.
