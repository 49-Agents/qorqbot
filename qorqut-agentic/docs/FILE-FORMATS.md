# Authored file formats

A dedicated repository contains packages under `roles/<id>/`, `operations/<id>/`,
`overseer/<id>/`, and `plugins/<id>/`. The singleton `user-preferences/` has ID
`user-preferences` and its canonical `preferences` skill is `SKILL.md`. Each
package has an `agentic.yaml` wrapper and ordinary authored files. IDs are
globally unique. A nonempty legacy `packages/` requires explicit migration. All paths in
an entry or tool definition are relative to its package. Git metadata and
runtime databases, receipts, workflow state and scratch output stay outside the
asset tree. The schema constants in `qorqut_agentic.schemas` are authoritative.
YAML must contain JSON-compatible values, string mapping keys, no duplicate
keys and no non-finite numbers. Timestamps remain strings.

## Package manifest

```yaml
schema_version: 1
id: email-composition
name: Email composition
description: Synthetic guidance for a disposable installation.
source:
  repository: supplied-offline-snapshot
  commit: example-commit
native:
  owner: example-plugin
entries:
  - id: compose
    kind: skill
    description: Compose an email using shared style guidance.
    when_to_use: When composing an outgoing email.
    path: skills/compose/SKILL.md
    requires: [email-composition/style]
  - id: style
    kind: preference
    description: Shared email style.
    when_to_use: When writing an email.
    path: preferences/style.md
    applicability:
      work_types: [email]
      project_ids: [example-project]
```

Required root fields: `schema_version: 1`, `id`, `name`, `description`, `entries`.
Optional `source` and `native` are provenance/host metadata objects, not authority.
Package ID must equal the containing directory name. IDs have 1–64 lowercase
letters, digits, dots, underscores or hyphens and begin with a letter/digit.
Unknown manifest/entry fields are rejected.

Every entry requires `id`, `kind`, `description`, `when_to_use`, `path`. Supported
kinds: `skill`, `instruction`, `preference`, `workflow`, `tool`, `template`.
`requires` is a list of explicit `package-id/entry-id` references; duplicates,
missing targets and cycles fail validation. Disabled dependencies make their
users unavailable with diagnostics. Applicability may specify `work_types` and
`project_ids`; it helps discovery and does not grant permission. An entry may
also include a `native` object.

Skills keep their original `SKILL.md` frontmatter and body. Supporting files
under that skill's directory are resources and contribute to its bundle digest.
Text instructions/preferences are UTF-8. Templates may contain binary data.
Resource files are copied byte-for-byte. Native YAML stays native; its additional
agentic wrapper is not a new permission or executable declaration.

## Tool definition

```yaml
schema_version: 1
description: Normalize a supplied list in a synthetic fixture.
runtime: python
code: tools/normalize_results.py
resources: [tools/lib, tools/data/aliases.json]
input_schema:
  type: object
  required: [names]
  properties:
    names: {type: array, items: {type: string}}
  additionalProperties: false
output_schema:
  type: object
  required: [names]
  properties:
    names: {type: array, items: {type: string}}
  additionalProperties: false
limits:
  timeout_seconds: 5
  max_output_bytes: 65536
  max_stderr_bytes: 4096
tests:
  - name: empty
    input: {names: []}
    expect:
      status: succeeded
      output: {names: []}
```

Required: `schema_version`, `runtime`, `code`, `input_schema`, `output_schema`.
Optional: `description`, `resources`, `limits`, `tests`. Unknown fields fail.
`runtime` is a trusted caller's alias, never a shell command or host import.
Code/resources must be confined regular files without symlinks. Resource
folders include their files. The actual staged code/resource bytes determine
`code_revision`; the definition file determines its own `revision`.

Schemas use JSON Schema draft 2020-12. Only in-document references are allowed;
validation must not fetch remote schemas. Limits default to 30 seconds, 1 MiB
stdout and 16 KiB stderr. Definitions allow at most 3,600 seconds, 64 MiB stdout
and 1 MiB stderr; a runtime may impose lower maxima. A local adapter runs
`[interpreter, code_path]` without a shell, reads one JSON value from stdin and
writes one JSON value to stdout. Stderr is separate and bounded. The runtime
adapter is explicitly supplied and is not an OS sandbox.

Each optional test requires `name`, `input`, `expect`. Expected `status` is
`succeeded`, `failed` or `rejected`; optional expectation keys are `output`,
`failure_code`, `error_code`. Tests run only via an explicit tools test call.
Execution receipts and logs belong to runtime state, not this definition file.

## Workflow definition

```yaml
schema_version: 1
id: example-workflow
description: Prepare and check a synthetic result.
arguments:
  - name: format
    schema: {type: string, enum: [short, detailed]}
    default: short
    prompt: Which format should this use?
    acquisition: confirm_default
  - name: audience
    schema: {type: string, minLength: 1}
    prompt: Who is the audience?
    acquisition: ask_at_step
    step: draft
steps:
  - id: collect
    instructions: Collect authorized synthetic evidence.
  - id: draft
    instruction_ref: email-composition/compose
    depends_on: [collect]
    verifier: example-tools/check-result
```

Required root fields: `schema_version`, `id`, `description`, nonempty `steps`.
The workflow ID must equal the manifest entry ID. Optional `arguments` is a
list. Unknown fields fail. Each step has an ID and exactly one of `instructions`
or `instruction_ref`. Optional `depends_on` lists predecessor step IDs;
`verifier` references a tool entry. Missing references, duplicate IDs and cycles
are rejected. Instruction references may point to instruction, skill, preference
or template entries. Dependencies must complete before a step becomes ready.

Each argument requires `name`, `schema`, `prompt`, `acquisition`. Optional
`step` scopes acquisition; `default` is validated against its schema.

| Acquisition | Required behavior |
| --- | --- |
| `default` | Validated default is used unless a value is supplied. Default is required. |
| `ask_before_start` | Missing values block all steps until supplied. |
| `ask_at_step` | Requires `step`; requests the value when its dependencies are satisfied. |
| `confirm_default` | Requires a default and explicit confirmation before use. |
| `derive` | Executing agent supplies a value plus its nonempty source/note; runner does no inference. |

Supplied values use `{value, source, note?, confirmed?}` records. Caller/source
strings and confirmation are evidence recorded by the runner; human authority
is checked by the host. Step-local arguments do not block independent steps.

The run snapshots its definition, referenced instructions and revisions at start.
Future edits affect future runs. Verifier definition/code revision changes are
conflicts for existing runs. Run states are `waiting_arguments`, `running`,
`paused`, `completed`, `cancelled`. Step states include pending, active and
completed, with a separate verification outcome and stored attempts. Paused or
cancelled runs reject progress. A verifier receives `{run_id, step_id, arguments,
output, completion_note}` and must return `{passed: bool, feedback: string}`.
Only passed verification satisfies it; no-verifier completion is declared.
Starting or attempting completion of a step locks the global arguments and
that step's local arguments, including when verification fails. Identical
resubmission preserves provenance; changed locked values require a new run.
A pause/cancel during verification preserves its verdict/receipt but blocks
step completion, including when a pause was followed by resume before return.
The full runner and execution semantics are verified in the execution workstream.
