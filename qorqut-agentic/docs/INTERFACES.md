# Python and JSON interfaces

`layer.instance_id` is the read-only `inst_<32 lowercase hex digits>` identity
created by `initialize`. It survives reopening and repository relocation. Reading
it does not create, repair or update state; missing/invalid metadata raises
`state_mismatch`. Receivers can bind this identity to their own transactional
delivery cursor without accessing the layer's private context.

`AgenticLayer.initialize(root, state, *, git_executable=None, git_user=None,
runtimes=None, clock=None, attribution_source="caller_supplied")` explicitly
creates state and a dedicated Git repository when needed. New repositories use
branch `agentic`. An existing repository must be its own top-level worktree.
Runtime state and authored content cannot contain one another.

`AgenticLayer.open(root, state, *, git_executable=None, runtimes=None, clock=None,
attribution_source="caller_supplied", rebind_root=False)` opens existing state.
Missing paths fail. Opening does not initialize or recover content, or recreate
missing scratch/lock files. `rebind_root=True` explicitly updates the stored root
binding when moving a repository; ordinary opens do not change it.

Components are `catalog`, `authoring`, `history`, `changes`, `tools` and lazy,
cached `workflows`. Importing the package performs no scans or initialization.
`recovery_required` reports pending changes. `recovery_status()` inspects them;
`recover()` explicitly finishes only files matching recorded before/after hashes.
Recovery conflicts never overwrite unexpected files. Readers can report pending
recovery; authoring/commit mutations require recovery first. Ordinary filesystem
readers can see an interrupted partial batch until recovery; the journal does
not make multiple filesystem renames a single OS transaction.

## Command envelope and configuration

```sh
printf '%s\n' '{"operation":"catalog.list","arguments":{"selection":{"package_ids":["email-composition"],"work_type":"email"}}}' |
  qorqut-agentic --root /absolute/scratch/authored --state /absolute/scratch/runtime
```

`python -m qorqut_agentic` uses the same entrypoint. JSON input must contain
exactly `operation` and `arguments`. Arguments use the Python parameter names
below. Unknown operations/arguments, wrong types, duplicate JSON keys, multiple
JSON values, invalid UTF-8 and non-finite numbers are rejected. Input is bounded
at 96 MiB; output at 8 MiB. Success exits 0 and emits
`{"ok":true,"result":...}`. Errors exit 1 and emit
`{"ok":false,"error":{"code":"...","message":"...","details":{...}}}`.
The output limit may be reached after an operation succeeds: its replacement
`limit_exceeded` error carries `operation_completed:true`. Oversized failures
retain their original error code and a bounded message, with `details.truncated`
set to true. Only already-present explicit `operation_completed` and
`attempt_recorded` flags are preserved from the original details; completion is
never inferred from an error. Inspect state/receipts before retrying a mutation.

Both paths are required command-line flags; no home-directory lookup is used.
Optional `--git-executable` names an absolute executable. Runtime objects can be
injected into Python. CLI subprocess execution is opt-in using an absolute
`--runtime-config` JSON file owned by the trusted launcher:

```json
{
  "local_process": {
    "interpreters": {"python": "/absolute/trusted/python"},
    "env": {"LANG": "C.UTF-8"},
    "max_timeout_seconds": 30,
    "max_output_bytes": 1048576,
    "max_stderr_bytes": 16384
  }
}
```

The interpreter must be an executable absolute path. The file is bounded at
64 KiB and accepts only the `LocalProcessRuntime` constructor arguments.
Request JSON cannot supply interpreters, runtime objects or runtime config.
The configured environment is the complete child environment, plus explicit
package/scratch paths; service environment variables are not inherited.
`LocalProcessRuntime` is a process adapter, **not an OS security sandbox**.
Host integration must inject its own authorized execution adapter.

## Operations

Each row names the JSON operation and its corresponding Python component method.
The listed parameters are the method arguments; omitted optional parameters use
the defaults shown. Values are JSON-compatible. An attribution is
`{actor_id, actor_kind, reason, source_refs?: [string]}`; tool/workflow actors are
`{actor_id, actor_kind}`. Hosts derive them from trusted invocation context.
Standalone values and workflow confirmation/source strings are caller supplied,
not proof of identity, permission or a human response.

| Operation | Arguments |
| --- | --- |
| `lifecycle.initialize` | `git_user=None` (`{name,email}`); root/state come from flags |
| `lifecycle.open` | `rebind_root=False`; root/state come from flags |
| `lifecycle.recovery_status` | none |
| `lifecycle.recover` | none |
| `catalog.list` | `selection=None, query=None, kinds=None, package_ids=None, include_unavailable=False, include_inapplicable=False, limit=200, offset=0` |
| `catalog.read_entry` | `ref, selection=None, include_dependency_bodies=False` |
| `catalog.read_resource` | `package_id, path, selection=None, max_bytes=1048576` |
| `catalog.validate` | `package_ids=None` (narrows diagnostics, not validation) |
| `authoring.apply` | `attribution, changes, dry_run=False` |
| `authoring.import_package` | `attribution, package_id, snapshot, manifest=None, provenance=None, expected_package_revision=None, dry_run=False` |
| `authoring.update_entry` | `attribution, package_id, entry, expected_manifest_sha256, content=None, content_base64=None, expected_sha256=None, dry_run=False` |
| `authoring.remove_entry` | `attribution, package_id, entry_id, expected_manifest_sha256, delete_files=False, expected_bundle_revision=None, dry_run=False` |
| `authoring.remove_package` | `attribution, package_id, expected_package_revision, dry_run=False` |
| `history.status` | none |
| `history.diff` | `from_revision=None, to_revision=None, staged=False, paths=None, include_untracked=True, max_bytes=262144` |
| `history.log` | `limit=20, paths=None, revision=None, include_files=False` |
| `history.commit` | `message, paths=None, author=None` (`{name,email}`) |
| `history.restore` | `attribution, revision, paths, expected_sha256=None, dry_run=False` |
| `changes.list` | `after=None, limit=100, consumer=None` |
| `changes.get` | `change_id` |
| `changes.cursor` | `consumer` |
| `changes.advance_cursor` | `consumer, sequence, expected_cursor=None` |
| `changes.scan` | `attribution=None` |
| `changes.cursor_head` | none |
| `tools.invoke` | `ref, arguments, invocation_id, actor, selection=None, expected_definition_revision=None, expected_code_revision=None` |
| `tools.test` | `ref, actor, cases=None, selection=None` |
| `tools.receipt` | `invocation_id, *, actor=None, selection=None` |
| `tools.reconcile` | `invocation_id, resolution, note, actor, *, selection=None` |
| `workflows.start` | `ref, actor, arguments=None, selection=None, operation_id=None` |
| `workflows.get` | `run_id` (the run view includes a defensive copy of its original `selection`) |
| `workflows.list` | `state=None, limit=100, offset=0, *, actor=None` (optional creator filter before pagination/counting) |
| `workflows.supply_arguments` | `run_id, arguments, actor, operation_id` |
| `workflows.start_step` | `run_id, step_id, actor, operation_id` |
| `workflows.complete_step` | `run_id, step_id, actor, operation_id, completion_note="", output=None` |
| `workflows.pause` / `resume` / `cancel` | `run_id, actor, operation_id` |

The facade exposes the persistent runner as `layer.workflows`. The CLI calls the
same public component methods and forwards receipt/reconciliation actor and
selection fields. See [workflows](WORKFLOWS.md) for a complete synthetic sequence.

## Discovery and revisions

`selection` is `{package_ids?: [string], project_id?: string, work_type?: string}`.
Omitted `package_ids` means the whole installed catalog for standalone callers;
hosts always supply authoritative enabled/allowed packages. Scope is routing
metadata. Global and matching scoped entries appear together. Listing without
query returns every eligible entry and does not resolve conflicting prose.

Entry descriptions include physical `area` and `editable` fields; these do not
grant host authority. Public authoring, history commit/restore and application
publication may write only `plugins/<valid-id>/...` and `user-preferences/...`.
Normal imports target plugins, except `user-preferences`, which targets the
singleton. Existing protected IDs cannot be shadowed by import. Other paths
return `protected_agentic_area`. A nonempty legacy `packages/` reports
`layout_migration_required`; migration and protected bundle installation are
separate operations.

List results include small `entries`, `total`, `catalog_revision`, `issues`,
truncation flags and `recovery_required`. Invalid entries have diagnostics and
are unavailable. Read results contain `entry`, `body` or `body_base64`,
`frontmatter`, `resources`, `dependencies` and, for tools/workflows, `definition`.
Resources include package-relative paths, SHA-256 and byte sizes. Resource reads
return `content`, `encoding` (`utf-8` or `base64`), `sha256` and `size`.

`revision` hashes the entry body. `bundle_revision` also covers its resources;
`catalog_revision` hashes installed catalog files. Metadata-only edits still
report the affected entry even when its body revision stays the same.

The Python facade provides `catalog.revision()` for the same catalogue revision
without parsing package manifests. `with catalog.read_snapshot():` holds a
shared authoring lock and reuses one catalogue for nested reads on that layer
and thread. On successful exit it checks a fresh no-follow file inventory,
special paths, legacy-layout state and recovery state; a change raises
`revision_conflict`. It is a read-only scope, is released on exceptions, and
must not wrap authoring writes. Individual reads outside a scope retain their
normal behavior.

## Authoring and imports

An apply batch is a nonempty list of operations:

```json
[
  {"op":"create","path":"notes/example.md","content":"New text\n"},
  {"op":"edit","path":"plugins/email-composition/preferences/style.md","expected_sha256":"<64 lowercase hex characters>","content":"Revised text\n"},
  {"op":"delete","path":"notes/old.md","expected_sha256":"<64 lowercase hex characters>"}
]
```

Creates expect absence. Edits/deletes require the current hash. Supply exactly
one of `content`/`content_base64` for create/edit. Optional boolean `executable`
sets a regular file's mode. Paths are root-relative POSIX paths; absolute paths,
`..`, `.git`, reserved temporary names, symlinks and nonregular targets fail.
Limits are 1,000 files, 16 MiB per file and 64 MiB total content per batch.

The projected catalog is validated before content is written. Pre-existing
unrelated errors can be repaired incrementally; a batch cannot introduce a new
catalog error. A durable pending journal records before/after contents before
atomic file replacement. Events become visible only after finalization. Hash
checks detect competing managed authors; external filesystem editors should
coordinate with the authoring lock because the OS has no atomic compare-hash
and replace primitive.

Results include `changed`, `dry_run`, `files`, `unchanged`, affected `entries`,
`catalog_revision`, `warnings`, and `change` (or null). Conflicts return current
hashes. Dry runs never write authored files or create change events. A normal
apply can first reconcile unobserved external changes to affected paths under
honest `external/unknown` attribution; validation failure never partially writes
the requested content.

Snapshots are `{"directory":"/absolute/snapshot"}` or `{"files":[{"path":
"skills/example/SKILL.md","content":"..."}]}`. File-list items may use base64 and
`executable`. Supply `agentic.yaml` inside the snapshot or as `manifest`, never
both. Source bytes are copied, not linked; executable code is never imported or
run during inspection. Optional `provenance` is recorded under manifest source
metadata with snapshot digest. Updates require `expected_package_revision` and
make that package match the supplied snapshot, including deleting removed files.
Use preview and inspect the diff before updating locally customized packages.

## History, feed and delivery

History uses ordinary local Git with global/system configuration disabled and
hooks, executable filters, fsmonitor, signing and external diffs suppressed.
Selected paths commit together while unrelated staged work remains staged.
Commits require a supplied author or repository-local identity. Restore writes
selected historical file contents as a new ordinary change; it does not move
HEAD or automatically commit. Do not use the history API for pushes or resets.

Each finalized event has a stable `change_id`, increasing `sequence`, timestamp,
kind, attribution, affected paths/entries, before/after hashes and optional commit
references. Later commits attach references without duplicate content events.
External scans report the observed net change and cannot reconstruct intermediate
writes or infer who made them. Without trusted attribution their actor is
`external/unknown` and trust is `external_unknown`.

`changes.list` returns events after an explicit sequence or consumer cursor,
`next_after`, `has_more` and current `head`. Consumers acknowledge using the
change ID as an idempotency key, then call `advance_cursor`. Cursors persist over
reopen, advance monotonically and optionally compare `expected_cursor`. Failed
receiver delivery leaves the cursor unchanged so the event can replay. The
library has no background delivery process; the host owns inbox/nudge delivery.

## Tool and workflow outcomes

Tool invocation and test results carry durable receipts outside authored Git.
Reuse an invocation ID to inspect its known receipt, never to repeat an external
effect. The invocation fingerprint includes ref, arguments, normalized actor and both
expected revision pins; reusing an ID with changed inputs is an operation conflict. Interrupted
in-flight effects can be indeterminate and require explicit reconciliation;
exactly-once external execution is not claimed. Inputs and outputs are validated.
Tool tests execute only on an explicit call.

Workflow mutators/get return `{run: ...}`; mutators also return `operation_id`.
The run view includes run/workflow IDs and revision, state, arguments, steps,
ready steps, argument requests and events. `list` returns `{runs,total}`.
Arguments map names to `{value,source,note?,confirmed?}`. `derive` needs a note;
`confirm_default` requires explicit `confirmed:true`. Definitions/instructions
are frozen for each run. Same operation ID and request replay the stored result;
a different request conflicts. Missing/changed/failed verifiers leave a step
incomplete. Declared completion without a verifier is distinct from verification.

Blocked verification (missing tool/runtime, changed revision, malformed verdict,
execution failure or intervening control change) first durably saves the
incomplete step and attempt, then raises `VerificationBlockedError`. Its details
are `{run, operation_id, reason, attempt_recorded: true}`; `reason` identifies
the underlying cause. The ordinary CLI response is `ok:false` with
`error.code:verification_blocked`. Receipt references and underlying causes are
retained in the persisted attempt. Reusing the same operation ID replays the
saved error and details without invoking the verifier again; use `get` for the
current run after later actions. A well-formed false verdict instead returns the
normal `{run, operation_id}` result with an incomplete step and feedback.

## Error codes

All Python errors derive from `AgenticError`; `to_dict()` yields the CLI error
object. The following codes are exported as `ERROR_CODES`:

| Code | Meaning |
| --- | --- |
| `invalid_request` | The request is malformed: unknown or missing parameter, wrong type, bad identifier. |
| `validation_failed` | Content failed validation (catalog, schema, argument); nothing was written or started. |
| `protected_agentic_area` | A requested write targets a protected agentic area. |
| `unsafe_path` | A path is absolute, escapes the root, names .git, or traverses a symlink. |
| `not_found` | A package, entry, resource, run, step, receipt, revision or consumer does not exist. |
| `conflict` | An expected SHA-256 did not match the current file; the current revision is returned. |
| `unavailable` | The entry exists but is not available to this selection (its package or a dependency is not enabled). |
| `revision_conflict` | A tool definition or code revision differs from the one a run recorded. |
| `operation_conflict` | An operation or invocation ID was reused with different content. |
| `state_conflict` | The requested transition is not allowed in the current run/step/receipt state. |
| `recovery_required` | An interrupted mutation must be recovered before further authoring changes. |
| `recovery_conflict` | Recovery found an affected file matching neither its recorded before nor after hash. |
| `runtime_unavailable` | No runtime is registered for the tool's runtime alias. |
| `verification_blocked` | A configured verifier could not produce a verdict; the step is not verified. |
| `store_not_initialized` | No initialized state exists at the given path; open never creates one. |
| `already_initialized` | The state path already holds an initialized store. |
| `invalid_root` | The authoring root is missing, not a dedicated Git working tree, or overlaps the state path. |
| `invalid_state_path` | The state path is unusable (inside the root, or a non-empty foreign directory). |
| `state_mismatch` | The state store is bound to a different authoring root or has an unsupported version. |
| `git_unavailable` | The Git executable is missing or not runnable. |
| `git_failed` | A Git command failed; bounded stderr is included. |
| `git_identity_required` | A commit needs an author identity, which was neither supplied nor configured locally. |
| `nothing_to_commit` | The selected paths have no changes to commit. |
| `lock_timeout` | The authoring lock could not be acquired in time. |
| `limit_exceeded` | A size, count or output limit was exceeded. |
| `internal_error` | An unexpected failure; the exception type and message are included. |

Receipt reads with a supplied actor, reconciliation and invocation retries check
that normalized actor against the recorded owner. A supplied selection must
allow the package in the recorded tool ref. These checks precede replay and
conflict details; another owner's receipt is `unavailable`. Omitted receipt
actor/selection is trusted standalone mode. Hosts must derive and supply them,
not expose that mode as an unauthenticated endpoint. Historical receipt access
does not depend on a current tool definition: deleting or editing a tool does
not erase or strand its execution evidence. New invocations still validate the
current catalog and runtime. The library does not authenticate supplied actors.

Starting a workflow step or reserving its first completion attempt atomically
locks its resolved local arguments and all resolved global arguments before
verification. Other independent steps' local arguments remain editable until
consumed. A locked value cannot change, including after failed verification.
Resubmitting an identical canonical JSON value preserves original provenance;
JSON booleans and numbers remain distinct. Changed consumed inputs require a
new run. A verifier's actual verdict and receipt are retained if a pause or
cancellation intervenes, but the step remains incomplete with a control-related
blocked reason. Even a pause followed by resume prevents a late completion
from silently advancing the run. After resume, use a new completion operation
ID; retrying the old ID returns its stored outcome without executing again.

### Outside observations

`overseer.outside.record(record, operation_id)`, `overseer.outside.get(id)`, and
`overseer.outside.list(limit=50, cursor=None)` store and retrieve immutable
observations separately from proposals and applications. A version 1 record
contains exactly `schema_version`, canonical UUID `id`, `summary`, `sources`,
`suggested_improvement`, `created_at`, and `created_by`. The summary and optional
improvement are bounded to 8000 characters. Sources contain 1–100 proposal-style
source links with at least one supporting link; Overseer-origin sources are excluded.
A repeated operation with identical input returns the original result; reusing an
operation or record ID for different input fails. CLI operation names match these
facade names. These records have no review, approval or application lifecycle.

Fresh runtime stores use schema 3. Explicit offline `lifecycle.migrate_state`
upgrades known schema 1 or 2 stores atomically, preserving state and recording each
step. Opening an older store does not migrate it. The migration checks known layout
and refuses unfinished authoring or publication work; it never moves authored files.
