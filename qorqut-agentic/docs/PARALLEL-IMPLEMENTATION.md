# Parallel implementation amendment

Manager-authored, 2026-10-26. Read with `AGENTIC-LAYER-PLAN.md`.

The owner now requests up to three parallel **GPT-6.1 Sol** ACLA workers in
**ultrafast** mode, **without self-review**. This supersedes the Sonnet/normal
speed defaults for these assignments. Keep xhigh reasoning. Manager reviews
completed work. No worker may spawn agents or redesign the system.

The former Sonnet worker has been stopped with its session retained. Its two
local implementation commits (`c8cc044`, `e89d7da`) are preserved. They contain
the initial catalog, authoring journal/change feed, history and tool runtime.
They are an unfinished baseline, not a reviewed release. All new module
worktrees start from the same preserved baseline plus this amendment. No one
edits the former worker's checkout while the replacements work.

All product requirements in the original plan remain. This parallel round
finishes the independent foundation and prepares the offline v1.3 import
adapter. The live host operation/projection/execution/nudge wiring remains a
following integration assignment; the import adapter alone is not the finished
host integration. No source changes are merged into final-assembly or deployed.

## Shared contracts

Keep the baseline Python API names and call signatures for authoring, catalog,
history, change-feed and tools unless Manager approves a compatibility change.
The baseline `AgenticLayer`, `LayerContext`, `Selection`, error classes, JSON
schemas and SQLite DDL are the shared contract. Ordinary bug fixes are allowed
within file ownership. Any required cross-owned change goes to Manager.

The JSON command envelope is `{ "operation": "catalog.list", "arguments": {} }`.
Use explicit `--root` and `--state`. Success is
`{"ok": true, "result": ...}`; failure is
`{"ok": false, "error": {"code": ..., "message": ..., "details": ...}}`.
Reject unknown operations/arguments and malformed input. Commands cover the
existing public component methods, lifecycle/recovery, and the workflow API
below. Tool runtimes are registered only from explicit trusted CLI configuration
or injected Python objects, never from an arbitrary interpreter in request JSON.

### Workflow boundary between workers A and B

Worker B supplies `qorqut_agentic.workflows.Workflows(ctx, tools)`. Worker A adds a
lazy, cached `AgenticLayer.workflows` property constructing that class. Lazy
import lets A test its portion before the workflow module is integrated.
Neither worker edits the other's files to make its branch look complete.

Public workflow methods:

```python
start(ref, actor, arguments=None, selection=None, operation_id=None)
get(run_id)
list(state=None, limit=100, offset=0)
supply_arguments(run_id, arguments, actor, operation_id)
start_step(run_id, step_id, actor, operation_id)
complete_step(run_id, step_id, actor, operation_id,
              completion_note="", output=None)
pause(run_id, actor, operation_id)
resume(run_id, actor, operation_id)
cancel(run_id, actor, operation_id)
```

`actor` has the same shape as the existing Tools API. Supplied `arguments` is a
mapping of names to records `{value, source, note?, confirmed?}`. `source` is an
attribution string, not an authorization grant. `derive` requires a nonempty
note; `confirm_default` requires `confirmed: true`. Validate each value against
the existing argument schema. Defaults are filled by the runner, without
pretending a user confirmed them. Omitted start operation ID is generated and
returned; callers needing start retry protection must reuse an explicit ID.

Mutators and get return `{run: ...}`. The run view contains `run_id`,
`workflow_ref`, `workflow_revision`, `state`, `arguments`, `steps`, `ready_steps`,
`argument_requests`, and `events`. Each ready step includes its frozen
instructions and current argument values. Steps expose their state, verification
outcome, completion note/output, and attempts. List returns `{runs, total}`.
Mutators return their operation ID too. Reusing an operation ID with the same
request returns the stored result; different content is an operation conflict.

Preserve the original plan's states, dependency readiness, argument acquisition,
definition snapshots, failed-verifier semantics, pauses/cancellation and receipt
reconciliation. A verifier failure is not a completed step. Re-check state after
external execution so a concurrent pause/cancel cannot be overwritten by a late
verifier result. Reusing the existing DDL is expected; ask Manager if a schema
change is necessary.

## Worker A: core, CLI, management guidance and packaging

Repository/branch: `qorqut-agentic`, `v1.3/agentic-core`.

Own all existing module files except Worker B's explicit list below. In
particular A owns `layer.py`, `core.py`, `store.py`, `catalog*.py`, `authoring.py`,
`files.py`, `history.py`, `changes.py`, `schemas.py`, `errors.py`, `util.py`, public
exports, package metadata, CLI and entrypoint, common test fixtures, core tests,
README, `docs/INTERFACES.md`, `docs/FILE-FORMATS.md`, and packaged management
resources. Do not edit either Manager-authored plan file.

Finish sections 2–5 of the original plan, the facade/CLI, management skill,
documentation and distribution. Verify the preserved implementation rather than
assuming committed means correct. Address core defects within the design.
Preserve workflow/tool schema compatibility for B. The migration worker uses
the existing `authoring.apply` and `import_package` interfaces unchanged.

Specific points to check: missing-store opens and normal reads must not silently
create or repair state; failed validation must not partially write content;
pending changes must remain recoverable; observed external edits need honest
attribution; selected-path commits must preserve unrelated staged work; invalid
catalog entries must have diagnostics rather than disappear silently. Initialize
synthetic authoring repositories on an `agentic` branch, not main/master; do not
commit to protected branch names even in fixtures.

Wire the workflow facade and CLI to the agreed interface; do not implement the
runner yourself. An absent B module on A's isolated branch is an integration
dependency, not permission to add a second runner or stub. Test A's actual
operations now and identify the remaining workflow integration checks in the
report. Build/import the wheel and verify packaged management resources. Final
end-to-end wheel checks will run after the reviewed branches are combined.

## Worker B: tools and persistent workflow execution

Repository/branch: `qorqut-agentic`, `v1.3/agentic-execution`.

Own `src/qorqut_agentic/tools.py`, `runtime.py`, new `workflows.py` and any new
`workflow_*.py` helper modules; `tests/test_tools.py`, new
`tests/test_workflows*.py`, `docs/TOOLS.md`, `docs/WORKFLOWS.md`, and
`examples/packages/example-project-workflow/**`. Do not edit `layer.py`, shared
fixtures, schemas, DDL, package metadata or other files. If one is needed, send
the exact required change to Manager. Test Workflows directly with
`Workflows(layer._ctx, layer.tools)` while A owns the facade wiring.

Finish original-plan sections 6–7 and the shared API above. Review and fix the
preserved tools/runtime implementation, then implement the runner and examples.
Keep runtime execution opt-in. Bound both output streams, enforce timeouts,
terminate child process groups, validate JSON schemas, and never silently repeat
an external effect after an indeterminate receipt. Check actor/selection on
receipt access and retries as appropriate; caller-selected IDs must not become
an authority bypass in a later adapter.

Cover all five argument modes, independent ready steps, dependency errors,
definition snapshots, reopen/resume, completion idempotency, competing updates,
pause/cancel during verification, verifier failure/missing/runtime error/revision
change, and the distinction between declared and verified completion. Do not
claim the runner verifies human confirmation; that is a host responsibility.

## Worker C: offline v1.3 assets and import adapter

Repository/branch: `qorqut-code`, `v1.3/agentic-import`.

Own only new `app/bazarlyk/agentic/` modules,
`app/tests/agentic_import/`, and `docs/v1.3/AGENTIC-IMPORT.md` in the host repo.
The copied source and module repositories in the same worktree group are
read-only references. No edit to host lifecycle, registry, authorization,
bootstrap, prompts, existing package source map or installation scripts yet.

Implement a pure offline adapter and a local JSON command, not a live migration:

```python
inventory_sources(source_roots, selected_extensions, custom_sources=())
build_import_plan(inventory, existing_files=None, previous_import=None)
apply_import_plan(layer, plan, attribution)
```

`source_roots` is an explicit mapping of source IDs to absolute copied roots.
Require host, roles and email sources. Optional content/Paper/Fastmail sources
are explicit and retain selected/unselected status. `custom_sources` explicitly
names supplied plugin/skill roots and ownership; never scan home directories or
read live configuration. Use relative paths inside the inventory and keep
original commit/digest provenance. Never import or execute a supplied plugin's
Python in order to inspect it.

The inventory lists every retained skill/manifest/brief/instruction and its
resource files, with source ID/path, digest, destination, native owner bindings,
template status, enabled status, and diagnostics. Preserve native YAML and
source bytes. Package manifests for the agentic layer are additional wrappers.
Native manifests and operation declarations do not become executable custom
tools. Content/role labels do not confer permissions.

Build a canonical package per source asset group: roles (all three manifests,
briefs, and shared skills in one preserved tree), host builtins, terminal-control,
email-management, content, Paper metadata, Fastmail metadata, and each explicitly
supplied custom package. Prefix imported asset paths with `native/`, preserving
their within-source layout. Emit a `bindings` index mapping native role/plugin/
extension IDs to the relevant entry refs; later host projection consumes this
index. This round does not expose those entries to a running agent.

Choose canonical retained content guidance from `qorqut-content`, whose
`PROVENANCE.md` explains the v1.3 adaptations. Use content-library's native
plugin manifest, rules, tools and config schema; map its duplicated skills to
the canonical content entries, recording both source digests and the deliberate
choice. Do not overwrite the adapted instructions with older library copies.
Keep the source-native skill copies only in a clearly labelled provenance
snapshot if necessary for exact native package reconstruction; they must not be
advertised as a second competing active skill. The binding index must state the
canonical entry and any source adaptation, without silently mutating a native
published artifact.

Coverage:

- All retained role skill trees, role manifests and referenced briefs.
- Host `agents/skills`, `bootstrap/instructions`, terminal-control and basic
  plugin template. Also inventory authored string instructions embedded in
  `agents/role_workflow.py`, `agents/context.py`, `inbox/defaults.py`, and
  `bootstrap/email_guidance.py`. Extract static literal assignments through AST
  rather than executing modules; report dynamic expressions requiring later
  host composition as such. Preserve the source reference and do not pretend
  a dynamically composed prompt was fully extracted.
- Email-management's plugin closure and instance-template skill.
- Content's six skill trees, resources, case-study index and creative-channel
  template including its retained critic scaffold. No deleted specialist,
  finance, SEO, publishing or private critic products are reintroduced.
- Paper/Fastmail supplied native/tool metadata and any authored skill/guidance
  that actually exists. Report a source as metadata-only when it has no skill.
  Do not fabricate a skill or copy executable provider/service implementation
  into the authoring layer. Keep provider dependencies and credentials out.
- Supplied custom plugin/skill snapshots with explicit source selection.

Use native manifests plus explicit retained asset roots to identify resources;
do not copy entire repositories, virtual environments, runtime trees, .git or
credentials. Missing resources, path escapes, duplicate incompatible IDs and
unclassified candidate assets are diagnostics that prevent a claim of complete
coverage. Templates remain templates and disabled extensions remain disabled.

The plan contains `packages`, `bindings`, `coverage`, `changes`, `conflicts`,
`source_digest` and `plan_digest`. `changes` uses the baseline authoring API's
create/edit/delete format and expected hashes. With no existing_files, plan an
initial import into an empty authoring root. For updates, require an explicit
previous import manifest: apply an upstream change only when the destination
matches the previous imported bytes (or already equals the desired bytes).
Retain local-only files. A locally modified affected file becomes a conflict;
never overwrite customization. An unchanged rerun is a no-op.

`apply_import_plan` rejects conflicted/incomplete plans, checks the plan digest,
and makes one `layer.authoring.apply` batch so catalog validation and durable
events cover the import. Return a new import manifest for the caller to retain
outside authored content. Do not independently write files, commit, publish
native packages, enable extensions, alter grants, or run a tool. Update receipts
must be independently replayable from their input digests.

Test with synthetic source layouts, including a custom package, modified local
instructions, missing resources, disabled extensions, and shared role/content
assets. Also run inventory/plan validation against the copied clean v1.3 roots;
keep that output in the group's scratch directory, not committed source fixtures.
Use the copied agentic module for a disposable apply/read/event smoke test.
Tests may use `pytest --confcutdir=app/tests/agentic_import` to avoid unrelated
host fixtures. No full host suite or live service is needed.

## Review and follow-through

Each worker commits only its assigned files locally and reports exact commits,
checks, gaps and remaining cross-worker integration checks once finished. No
interim reports. Design questions go to Manager via `ask-question`; no worker
messages another worker or changes a peer's checkout. Manager owns shared API
decisions and supplies amendments to affected workers.

After review, combine the branches on a dedicated task integration branch and
verify the full package/CLI/workflow/import scenario. That later assignment must
be explicit; no worker merges peers or final-assembly on its own. Then continue
the host operations/projection/execution/nudge wiring from the original plan.
Nothing is pushed or deployed.
