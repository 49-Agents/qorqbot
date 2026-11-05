# qorqut-agentic

A standalone Python library and local JSON command for authored skills,
instructions, preferences, templates, custom tools and workflows. Content lives
in a dedicated Git working tree; runtime SQLite, journals, receipts and scratch
files live at a separate explicit path. Importing the library creates nothing.
Python 3.12+, PyYAML, jsonschema and Git for history operations are required.

This is the standalone foundation. Qorqut operation registration, permission
adapters, native skill projection, execution bridging and Overseer inbox/nudge
delivery require the following host integration. The combined foundation exposes
the persistent workflow runner through `layer.workflows` and the JSON CLI.

Startup and hook wording, reminder policy, instruction ordering and completion
guidance are owned here too. See the [runtime instruction index and ownership guide](docs/RUNTIME-INSTRUCTIONS.md).

```sh
python -m pip install .
printf '%s\n' '{"operation":"lifecycle.initialize","arguments":{"git_user":{"name":"Example","email":"example@example.invalid"}}}' |
  qorqut-agentic --root /absolute/scratch/authored --state /absolute/scratch/runtime
```

Initialization creates an `agentic` Git branch. `open` fails for missing stores
and does not repair state. History follows ordinary Git branch and detached-HEAD
behavior without imposing a branch naming policy. Operations preserve existing commits and remain local.

Shipped registered-agent instructions, workspace composition and native tool guidance
are owned here. See [instruction ownership](docs/RUNTIME-INSTRUCTIONS.md) for the
source indexes, managed delivery, preserved instance records and upgrade behavior.

The distribution contains a five-area authored tree at
`qorqut_agentic/resources/defaults/`. A host's trusted fresh installer copies
these defaults into an empty authored tree. Protected role, operations and
Overseer packages are read-only through public authoring APIs; plugins and the
singleton `user-preferences/preferences` entry are editable. Package manifests are folders in one authored repository, not plugin repositories.
Discovery grants no tool, credential or plugin authority, and the edit restriction
is not an OS sandbox. Installation and existing-instance migration are host
responsibilities; legacy layout apply is still in progress.

The `agentic-management/bootstrap` instruction and management skill provide
shared guidance for explicitly authorized agents. Put universal guidance in `user-preferences/SKILL.md` and scoped guidance in an
editable plugin or skill. Every role receives complete preferences; the next normal prompt or resume refreshes
the managed projection and relevant catalogue. Agents locate and read relevant
procedural entries; discovery does not inject every plugin body. Delayed refresh
remains pending.
Clear owner instructions can be recorded within existing authority, while inferred
changes need owner-led review and application. Role briefs and other unreviewed
procedures remain preserved. See `docs/defaults-provenance.json` for
source paths, hashes and rewritten native manifest fields.

Every CLI call reads one `{ "operation": "...", "arguments": {...} }` value and
writes one `{ "ok": true, "result": ... }` or `{ "ok": false, "error": ... }`
envelope. Unknown fields and malformed input fail. Both paths are required.
`python -m qorqut_agentic` is equivalent to the installed command.

Use explicit package selections from the host's authorization context. Scope
labels help routing; they grant no access. Standalone attribution is caller
supplied. The module does not authenticate human confirmation or caller identity.

Custom code runs only through an explicitly registered runtime. The standalone
`LocalProcessRuntime` has bounded timeout/output and a caller-supplied environment;
it is **not an OS security sandbox**. A privileged host must supply its authorized
execution adapter and must not use local subprocess execution as a fallback.

See [interfaces](docs/INTERFACES.md), [file formats](docs/FILE-FORMATS.md),
[workflows](docs/WORKFLOWS.md), and [synthetic examples](examples/README.md). Tests use disposable data and do not
contact models or providers. Run focused checks with an explicit temporary root:

```sh
TMPDIR=/absolute/scratch/tmp python -m pytest tests/test_catalog_and_import.py tests/test_authoring.py tests/test_changes_feed.py tests/test_history.py tests/test_cli.py tests/test_core_regressions.py --basetemp=/absolute/scratch/pytest
```
