# Synthetic examples

Everything under `examples/` is **synthetic example material**. These packages
exist to document the file model and to serve as test fixtures. They are *not*
owner preferences, are not installed anywhere, and must not be copied into a
real catalog as if they were rules. Each package manifest says so in its
`source` and description.

| Package | Shows |
| --- | --- |
| `packages/email-composition` | A general skill, a shared preference file it requires (an example rule: no em dash), and a binary skill resource. |
| `packages/meeting-requests` | A *separately scoped* package (`work_types: [meeting-requests]`) whose timing rule must not appear for unrelated email work. |
| `packages/example-project-workflow` | A custom JSON-transform tool with helper resources and tests, a verifier tool, and a landing-page preparation workflow with arguments, parallel steps and a verifier. |

Import one with an explicit snapshot, for example:

```sh
qorqut-agentic --root /path/to/authoring-root --state /path/to/state <<'JSON'
{"operation": "authoring.import_package",
 "params": {"attribution": {"actor_id": "me", "actor_kind": "owner", "reason": "try the example"},
            "package_id": "email-composition",
            "snapshot": {"directory": "/abs/path/to/examples/packages/email-composition"},
            "provenance": {"kind": "example", "path": "examples/packages/email-composition"}}}
JSON
```

The tool examples need a runtime. Nothing is enabled implicitly: pass
`--local-runtime python=/absolute/path/to/python3` to opt in to the local
process adapter (see `docs/TOOL-RUNTIMES.md` for its trust boundary).
