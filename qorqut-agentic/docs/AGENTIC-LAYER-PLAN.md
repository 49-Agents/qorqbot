# Agentic layer: design and implementation plan

Manager-authored, 2026-10-26. Status: foundation implementation authorized;
integration and migration follow foundation review. This file is a design, not a
claim that any feature is already implemented.

## 1. Base and owner decisions

Build on Qorqut **v1.3/final-assembly**, host commit
`beb336ab6b44034328649b9c1caed0b6c3546937` (Pin Signoff label cleanup).
Do not use v1.2 or v2.2. Dedicated worktrees are under
`/home/alp/qorqbot-v1.3/worktrees/v1.3-agentic-layer/`, on
`v1.3/agentic-layer`. The original final-assembly checkout stays unchanged.
The captured source commits are in
`/home/alp/qorqbot-v1.3/execution/agentic-layer/source-manifest.json`.

Settled requirements:

- A standalone, reusable module owns the agentic layer: authored plugins,
  skills, procedures, instructions, work-specific preferences, workflow
  definitions, custom tool definitions/code, and their management toolkit.
- Bring **all surviving v1.3** plugins and skills into the layer in the first
  integrated release, including extension contributions. Optional installation
  remains optional. Do not resurrect products or roles removed in v1.3.
- Include runnable custom tools, tool tests, and the basic stateful workflow
  runner in this release.
- Use ordinary Git history and reasonably focused thematic commits. A theme
  may span several files and packages. No semantic commit gate, one-file rule,
  special branching scheme, or copy of Context's versioning machinery.
- Ordubashy and Overseer use the same editing toolkit. Explicit owner
  instructions can be applied directly, within the host's authority. Lessons
  inferred from conversation or edits must be proposed and reviewed by the owner
  before affecting future work.
- Every managed change has a durable event, independently of whether or when
  someone commits. Ordubashy changes notify Overseer through the existing inbox
  and nudge mechanism. A delivery failure must not lose the change.
- Overseer will later own experience extraction, proposal review/application,
  and verification that explicit instructions were actually applied. Its
  workflows belong in this module, with host adapters for evidence and delivery.
- Message routing and checking that fifty task instructions were all executed
  are a separate, deferred problem.

The owner examples are acceptance scenarios, not instructions to insert those
preferences into every installation during migration.

## 2. Module boundary

Distribution: `qorqut-agentic`; Python package: `qorqut_agentic`.
Python >=3.12, with PyYAML and jsonschema for file/schema validation. SQLite,
subprocess, and filesystem operations use the standard library. Git is an
explicit executable dependency of history operations. No dependency on Qorqut
host, Operations, Context, the old learning library, an LLM, or a provider SDK.

```text
Ordubashy / Overseer / owner
          |
Qorqut adapter: identity, authority, enabled extensions, native capabilities,
               agent skill projection, inbox delivery, evidence access
          |
qorqut-agentic: catalog + authoring + Git + events + tools + workflow runs
          |
authored files in Git              separate runtime SQLite and scratch files
```

The base module accepts explicit filesystem paths, caller attribution, catalog
selection, and runtime adapters. Importing it does nothing. Initialization,
recovery, scanning, execution, and delivery are explicit calls. Opening a missing
store fails; it must not initialize a guessed production location.

Operations remains the only host permission/transaction/inbox engine. Context
remains the knowledge store. Existing native plugin manifests, immutable
publication records, and permission checks remain the authority for native
capabilities; an authored file cannot grant a capability. The host does not load
agent-written Python into its own process.

No framework for arbitrary extension discovery is needed. Selected extensions
provide explicit asset descriptors; the host constructs the adapter directly.

## 3. Authored files and discovery

An authoring root is a normal, dedicated Git working tree:

```text
packages/
  email-composition/
    agentic.yaml
    skills/compose/SKILL.md
    preferences/style.md
  example-project-workflow/
    agentic.yaml
    workflows/landing-page.yaml
    tools/normalize-results.yaml
    tools/normalize_results.py
  imported-role-ordubashy/
    agentic.yaml
    native/...                     # original package-relative layout
```

Each `agentic.yaml` has `schema_version: 1`, a stable `id`, a human name and
description, optional source provenance, optional `native` metadata, and
`entries`. Entries have stable IDs within the package, kind, description,
`when_to_use`, relative path, optional `requires` references, and optional
applicability (`work_types`, `project_ids`). Supported kinds are `skill`,
`instruction`, `preference`, `workflow`, `tool`, and `template`.
An entry reference is `package-id/entry-id`. Dependencies are explicit entry
references, not heuristics based on text. Reject duplicate IDs, broken local
references, and dependency cycles. Cross-package references must resolve in the
installed catalog; a disabled dependency makes its dependent entry unavailable
with an explanatory diagnostic.

Skills remain ordinary `SKILL.md` files with their supporting resources.
Preserve existing frontmatter during import. Catalog metadata describes routing;
it is not permission policy. Shared preferences are canonical files referenced
by applicable skills, not duplicated rule text in a second preference database.
Descriptions and `when_to_use` are authored text, not embeddings or classifiers.

Catalog reads return small metadata entries, with IDs, triggers, applicability,
dependencies, source provenance, and a content revision. A separate read loads
the selected body and identifies its dependencies. Query matching is simple text
matching and explicit filters. Always permit listing the eligible catalog so
correctness does not depend on a keyword search succeeding.

The host supplies the caller's allowed/enabled package IDs and selected project
and work type. Scope labels do not grant access. Local standalone callers may
select the entire installed catalog; Qorqut must always provide its authoritative
selection. Globally applicable entries and matching scoped entries are both
discoverable. Do not silently resolve contradictory prose using a made-up
priority rule: expose the overlapping entries for the agent to reconcile.

Keep a short always-loaded management instruction and catalog pointer in agent
bootstrap. Work-specific bodies load when relevant. A saved change returns its
revision; host projection separately reports whether it is available to the
next read and whether a running native session needs a refresh.

## 4. Authoring toolkit and history

Provide one Python facade and a small JSON command interface, accepting explicit
`--root` and `--state` paths. The command receives JSON on stdin and returns a
bounded JSON result on stdout. It is a local tool, not a service. No default model,
scheduler, network endpoint, or hidden home-directory search.

Public operations, also used as the eventual host tool vocabulary:

| Area | Operations |
| --- | --- |
| Discovery | catalog list/filter; read entry/resource; validate |
| Authoring | apply file changes; import a supplied package snapshot; remove/update package entries |
| History | status; diff; commit; restore selected files from a revision |
| Changes | list after a durable sequence; scan external edits; read/advance consumer cursor |
| Tools | invoke tool; run explicit tool test cases; read invocation receipt |
| Workflows | start/get/list; supply arguments; start/complete step; pause/resume/cancel |

An apply request contains an attribution record and a list of creates, edits, or
deletes. Each existing file mutation names its expected SHA-256; creation expects
absence. Validate the projected catalog before making a batch visible. Return
conflicts with the current revision; do not overwrite a concurrent author's
edit. Attribution records actor ID, actor kind, reason, and optional source
references. Qorqut derives these from its invocation context, not from an agent's
untrusted request fields. Standalone attribution is caller supplied and described
as such.

All paths stay within the explicit authoring root. Reject absolute paths, `..`,
symlink escapes, `.git` edits, and mutations of runtime state through authoring
APIs. Copy supplied asset bytes; do not create links to their source repository.

Use a local exclusive lock and a small durable pending-change journal in runtime
SQLite to bridge files and events. Validate first; record before/after contents
and a pending event; atomically replace each file; finalize the event and observed
file inventory. An explicit recovery call completes an interrupted batch only
when each affected file matches its recorded before or after hash. Unexpected
external edits produce a recovery conflict, never an overwrite. Consumers see
only finalized changes. Normal opening reports required recovery before allowing
mutations; do not hide an incomplete mutation as a successful write.

This journal is crash recovery, not a new semantic version-control system.
Authored files and ordinary Git remain inspectable and editable with normal
tools. Edits outside the toolkit are reconciled by an explicit scan: one event
per observed batch, attribution `external/unknown` unless supplied by a trusted
adapter. Scanning can identify the observed net change; it cannot reconstruct
every intermediate filesystem write or guess who made it.

History operates only on the dedicated authoring repository. Commits take an
ordinary message and optional selected paths, preserve unrelated staged work,
and do not execute repository hooks. Never push, reset a branch, clean files, or
rewrite history. `restore` reads selected files from a requested revision and
applies them as a new ordinary change with expected hashes and a change event;
the caller can then commit. Grouping changes thematically is guidance, not
enforcement. The runtime state and tool/run output are outside the Git root.

## 5. Durable change feed and Overseer notifications

Each finalized change has a stable ID, increasing sequence, timestamp, actor,
reason/source references, affected paths/entry IDs, before/after hashes, and an
optional commit reference. A multi-file apply request can be one event. A later
commit does not invent duplicate content changes. Restores, imports, deletes,
and externally observed edits are visible too.

Named consumers hold durable cursors. Delivery is at least once: advance a cursor
only after the receiver acknowledges, use the change ID as the idempotency key,
and make replay safe. The foundation exposes feed/cursor primitives; it does not
start a background service or contact an agent.

The Qorqut adapter drains this feed into existing Operations inbox events for the
registered Learning Applier/Overseer identity. It can coalesce a pending nudge,
but must preserve each underlying event. Missing/offline recipient or failed
delivery leaves events pending. Filter self-generated changes from automatic
self-nudges without deleting their history. Retain the registered UUID and
`learning-applier` role compatibility; do not rename workspace paths or create a
second agent merely to show the name Overseer.

## 6. Custom tools

A tool entry points to a YAML definition containing its input/output JSON
schemas, runtime alias, package-relative code path, and bounded timeout/output
limits. Validate arguments before dispatch and outputs before declaring success.
Definitions select a runtime from the trusted caller's registry; they cannot
name a Python import to execute inside the module or an arbitrary shell command
to execute inside the host server.

Include an opt-in `LocalProcessRuntime` for standalone use and synthetic tests.
The caller registers an absolute interpreter executable for a runtime alias.
Execute `[interpreter, code_path]` without a shell, send one JSON value on stdin,
require one JSON value on stdout, and capture bounded stderr separately. Use a
fresh explicit invocation scratch directory and a caller-supplied minimal
environment, never inherit the full service environment. Bound both streams
while reading (not only after process exit), enforce a host maximum timeout, and
terminate the process group on timeout/output overflow. Resolve code/resources
within the package, reject symlinks, and record the actual code revision used.
Document that this is a process adapter, **not an OS security sandbox**.

Qorqut integration must run custom code through its authorized agent execution
environment, with an explicit runtime adapter. Do not enable LocalProcessRuntime
as a fallback inside the privileged host. Existing terminal/browser/native
tools remain host capabilities and use their existing permission checks.

Persist invocation ID, actor, definition/code revision, start/end/status,
validated output, and bounded diagnostics outside Git. Caller retry with an
existing invocation ID returns its known receipt, never repeats an external
effect silently. An interrupted in-flight call is `indeterminate` until the
caller reconciles it; no claim of exactly-once external execution. Tests are
explicit fixture invocations with expected results, not automatic execution
whenever a package is imported or a catalog is read.

## 7. Basic workflow runner

A workflow YAML has schema version, ID, description, arguments, and ordered step
definitions. Each step has a stable ID, instructions or an instruction entry
reference, `depends_on`, and an optional verifier tool reference. Validate missing
references and cycles before a run starts. Independent ready steps may proceed
in parallel. Dependencies become satisfied only when the predecessor completes.

Persist the definition and referenced instruction bodies at run start, their
revisions, arguments, step states, completion attempts/evidence, and timestamps.
Editing the catalog changes future runs; existing runs retain their snapshot.
Tool references record the expected definition/code revision too: if a required
verifier changed, return a revision conflict, not a different verification.

Arguments have a JSON schema (including enum/free text as appropriate), optional
default, prompt, optional owning step, and one acquisition mode:

| Mode | Runner behavior |
| --- | --- |
| `default` | Use the validated default unless a value is supplied. |
| `ask_before_start` | Request missing values before any steps become ready. |
| `ask_at_step` | Request when the named step's dependencies are satisfied. |
| `confirm_default` | Return a proposed default and require explicit confirmation before use. |
| `derive` | Ask the executing agent for a derived value and its source/note; the runner does no inference. |

The argument submission API records the supplied value, source/note, and explicit
confirmation markers. It does not pretend to authenticate a human response;
the host adapter supplies that authority. Return `argument_requests` and ready
step instructions with the current argument values. Do not invent defaults or
ask a model inside the runner. Step-local missing arguments block that step,
not independent steps.

Run states are `waiting_arguments`, `running`, `paused`, `completed`, and
`cancelled`. Step states include pending/active/completed, with separate
verification outcome and stored failed attempts. Computed readiness is returned
by get/start/supply/complete operations. Starting and completing a step require
ready dependencies and resolved arguments; pause/cancel reject new progress.
Completing all steps completes the run. Repeated completion with the same
operation ID returns the existing result; conflicting repeats fail.

The executor supplies completion note and JSON output/evidence. With no
verifier, accept completion as **declared**, not verified. A configured verifier
receives `{run_id, step_id, arguments, output, completion_note}` via the same tool
runtime and returns `{passed: bool, feedback: string}`. Only `passed: true`
satisfies verification. Failure stores actionable feedback and leaves the step
incomplete. Missing runtime/tool, revision conflict, invalid output, crash, or
timeout leaves verification blocked/incomplete; never silently fall back to
declared completion. No whole-workflow scheduler, script routines, automatic
provider sends, or replacement of existing Operations routines is included.

## 8. Qorqut v1.3 adapter and complete migration

This follows the foundation implementation and review. It is required before
calling the first integrated release complete.

1. Compose the module explicitly in the host; add offline initialization and
   installation/export dependencies using local source/wheel contracts. Runtime
   state uses an explicit instance path separate from authored Git. No import or
   runtime startup migration and no live data changes during this assignment.
2. Register authoring, catalog, history, workflow, and tool operations through
   existing Operations descriptors and permission handling. Provide the same
   management skill to Ordubashy and Learning Applier/Overseer. Ordinary workers
   may read/use only their allowed catalog and capabilities. Do not use package
   scope metadata as authorization.
3. Preserve native publication/grants for capability definitions, and consume
   current managed procedure text separately from those grants. This allows a
   directly instructed email-style correction to take effect without inventing
   a new permission grant or falsely claiming that an immutable native pin was
   mutated. Changing native capabilities still uses the established publication
   and permission path. Detect and clearly report a required publication or
   session refresh rather than reporting everything as already active.
4. Extend native skill projection and bootstrap catalog pointers. Preserve
   supporting resources and exact source provenance; identify new/deleted skills
   as well as edits. A subsequent catalog read sees the saved revision. Refresh
   running session projections at their existing safe boundary; do not stop or
   restart native sessions as part of a content edit.
5. Provide a source-managed adapter in each selected extension for its catalog
   assets, rather than special-casing marketing or provider code in the base
   library. Extension upgrades are explicit imports with a diff and conflict
   detection against local edits; never overwrite customized instructions.
6. Connect durable changes to existing inbox/nudge delivery and recovery.
   Treat notification delivery as retryable even after the authoring call has
   returned successfully. A projection/delivery failure must remain visible.
7. Connect custom execution to the existing authorized native execution boundary.
   The exact host launch/receipt mapping is a Manager-owned integration decision
   to settle from current terminal/native code before the integration handoff.
   It is not permission to use server subprocess execution as a shortcut.

The migration inventory must account for each surviving source item:

| Source | Coverage |
| --- | --- |
| `qorqut-roles/agent-roles` | Ordubashy, global-worker, learning-applier briefs/manifests and all retained skill/resource directories; shared skills have one canonical managed copy. |
| Host `app/bazarlyk/agents/skills`, `bootstrap/instructions`, and authored guidance | Built-in role/cleanup, shared/base instructions, inbox/email guidance where currently projected into agent context. Runtime identifiers and grants stay host owned. |
| Host `app/plugins/agent-terminal-control` | Native manifest, schemas/tool references/rules, skill and resources. |
| `dependencies/email-management` | Generic email plugin, its skill/resources, instance-template skill, and the existing exact-link/attachment guidance. |
| `qorqut-content` and `dependencies/content-library` | Retained content-creation plugin assets, six production/method skills and their reference files, case-study index, creative-channel templates/critic scaffold actually retained by v1.3. Reconcile duplicate source assets explicitly; preserve the selected v1.3 version. |
| `qorqut-paper` | Selected Paper guidance/native connection/tool metadata and supporting authored resources; Paper service code and provider credentials stay outside the layer. |
| `qorqut-fastmail` | Selected provider capability metadata/guidance; no fabricated skill if the extension supplies no skill. Credentials and provider implementation stay outside the layer. |
| Existing instance-installed custom plugins/skills | Enumerate from an explicitly supplied offline source during installation/migration; import their asset bytes/provenance and preserve visibility/grants. No live fixture capture. |

Generate a migration report listing source path/ID, original digest, destination,
resource coverage, selected/disabled state, and any conflicts or missing sources.
No silent skips. Templates are templates, not automatically enabled workflows.
Unselected optional assets may be managed and inventoried but remain unavailable
to agents until the host enables their extension and grants their capabilities.
All shipping and supplied instance material must be accounted for before the
integrated acceptance check passes.

## 9. Management instructions and later Overseer workflows

Ship a management skill that teaches either agent to:

1. Distinguish a direct owner instruction from an inferred preference.
2. Determine the instruction's scope from the actual request. Find the relevant
   existing skill/catalog entry; create one with an explicit trigger if absent.
3. Apply a direct instruction to its canonical file, preserving unrelated
   instructions. Include source reference and reason. Resolve genuine conflicts
   with the owner rather than silently broadening scope.
4. Validate, inspect the diff and catalog reachability, and test executable
   behavior when changed. Commit reasonably related work together when useful.
5. Report what changed and any publication/projection state accurately. The
   durable change event handles notification; do not rely on remembering to send
   an informal message to Overseer.
6. Keep inferred changes out of active files until owner review. Present the
   evidence, proposed diff, applicability, and uncertainty first.

Later experience extraction is a separate workflow with stages: select authorized
episodes and edits; collect stable evidence references; distinguish explicit
instructions from candidate inferences; inspect current layer/history; verify
direct-instruction application; group candidate changes by theme; present review
items with evidence/counterevidence and high/medium/low confidence plus rationale;
apply owner-approved items through this same toolkit; verify resulting behavior.
Review supports accept, edit, reject, and defer, without a forced numerical
confidence score. Routine and on-demand runs share this workflow.

The old learning library is source material, not the new algorithm. Its existing
three-pass extraction, verification and clustering have no privileged status.
Its README reports synthetic coverage and no quality calibration; reusing a
component must be justified by the new workflow. Do not change existing live
learning jobs, Context management, or review screens during foundation work.

## 10. Ordered implementation and acceptance

### A. Standalone foundation (first worker)

Implement sections 2–7 and the management instructions in section 9. Deliver a
working installable package, documented Python/JSON interfaces, synthetic example
packages, and focused tests. Import accepts explicit snapshots; the foundation
does not crawl Qorqut or implement the host adapter. No production rules seeded
from hypothetical examples. Keep host checkout unchanged.

Required checks:

- Import is inert; missing paths do not create state; all data uses explicit
  disposable roots, with no host or Context dependency.
- Catalog routing loads the relevant skill and referenced preferences; all
  resource bytes survive import; broken references and unsafe paths fail.
- A direct synthetic email instruction is saved, discovered on the next catalog
  read, and produces an event before any Git commit. A plugin-specific no-price
  rule is absent from unrelated email workflows.
- Concurrent edit conflicts are detectable; failed validation writes nothing;
  interrupted edits recover without losing events or overwriting external edits.
- Thematic changes spanning packages commit together; selected-path commits do
  not consume unrelated staged work; restoring files creates a new event.
- Change cursors survive reopen; failed consumer delivery can replay; scanning
  external edits reports their actual net change and unknown attribution.
- A synthetic custom tool runs, validates schemas, preserves helper resources,
  records receipts, and handles invalid output, nonzero exit, timeout, output
  overflow, missing runtime, and repeated/indeterminate invocation IDs honestly.
- Workflow dependencies and all argument modes work; independent steps become
  ready; progress survives reopen and concurrent completion; pause/cancel are
  respected; catalog edits do not alter an existing run's instructions.
- Missing verifier never passes; failed verification carries feedback; successful
  verification and unverified declared completion remain distinguishable.
- No tests invoke real services or models. Build/import an offline wheel with
  synthetic package data to verify that resources are actually distributed.

### B. Host integration and all-existing migration

After reviewing A, Manager supplies the concrete host operation schemas,
execution bridge, projection mapping, and migration handoff. Implement section 8
on dedicated v1.3 work branches and add focused host tests with existing synthetic
fixtures. Do not treat A alone as satisfying the all-existing migration promise.

Integrated checks: Ordubashy and Overseer can use the same toolkit; host authority
cannot be escalated by manifest edits; all inventory rows are resolved; disabled
extensions stay disabled; exact instruction/resource revisions reach the native
catalog; Ordubashy changes leave replayable Overseer notices even with delivery
offline. Reconcile pending native publication or refresh explicitly. Run the
custom-tool and workflow scenarios through the real host adapter with a mocked
execution backend, not only directly against the library.

### C. Review and source completion

Manager reviews completed diffs, focused test results, built package contents,
and migration coverage. Worker fixes confirmed defects; no self-review subagents.
Record each affected repository/branch/commit and any remaining limitations.
No merge into final-assembly, push, deployment, native session activation, or
live migration is included in this source implementation assignment.

### D. Experience extraction and review UI (later design/implementation)

Build the extraction/review/application workflow after the layer and toolkit are
usable. Its input includes transcripts, task comments, Signoff comments, and
authorized inline-edit before/after evidence. Design this separately with the
owner, retaining the settled rule that all inferred changes require review.
Message routing/completeness remains outside this workstream.
