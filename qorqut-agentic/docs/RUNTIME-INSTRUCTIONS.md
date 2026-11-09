# Shipped instruction ownership

Agentic owns Qorqbot's shipped instruction prose and pure behavior composition.
The host supplies authorized records and handles delivery. This consolidation
preserves existing wording, section order, triggers, timing, permissions and
role exclusions. It does not revise Ordubashy's responsibilities.

## Where the instructions live

All supplier text lives under `qorqut_agentic/resources/defaults/`:

| Content | Supplier package or library | Active delivery |
| --- | --- | --- |
| Role responsibilities | `roles/imported-roles` | Mandatory configuration |
| Role skills | `operations/imported-role-skills` | Selected skills; nine configured skills load at startup |
| Owner preferences | `user-preferences` | Mandatory configuration |
| Startup, workspace, inbox, Attention, delegation and recovery messages | `operations/runtime-instructions` | Current catalog or prepared native-hook snapshot |
| Native tool descriptions, schema help and response guidance | `operations/tool-instructions` | Tool discovery, configuration inventory and exact schema reads |
| Shared fragments, workspace skills, terminal and email packages, connector guidance, and initial document seeds | `operations/source-instructions` | Existing native catalog destinations and bindings |
| Agentic management and Overseer | `operations/agentic-management`, `overseer` | Authorized management and selected procedures |

The [runtime index](../src/qorqut_agentic/resources/defaults/operations/runtime-instructions/README.md)
lists individual messages. The tool and source libraries have machine-readable
indexes mapping entries to their native delivery sites or stable source aliases.
Historical Telegram role snippets and basic plugin templates remain inactive.
Provider implementations, independent Context-filer prompts and external coding
session instructions are outside this registered-agent instruction library.

## Native aliases and existing edits

The source library replaces host- and connector-owned source files. Native calls
keep their stable source aliases and installed catalog destinations. The importer
verifies those aliases, records Agentic as the actual supplier, and preserves the
native package identity separately. Moving a supplier does not replace an existing
local catalog edit. Connector prose ships as data; selecting a connector and
registering its code and tools remain separate host operations.
Connector index version 2 explicitly selects Agentic-supplied prose. Bundles carry
the index and renderer contract; the installer verifies the declared resources
against the current core before installing wheels. Older installers refuse these
bundles instead of installing a connector with missing instructions.

Three shared documents (`foundation`, `ecosystem-agent`, `ecosystem`) remain
registered Operations records after their initial seed. Saved agent assignment
briefs and owner-written email instructions are also instance records. Their
**shipped defaults** are centralized here; their current authored content and
history are not migrated or overwritten by this source consolidation. Moving those
live records would require an explicit data migration that preserves local edits.

Tool libraries construct ordinary string metadata from Agentic's packaged seeds.
Before delivering that trusted metadata, the host substitutes the current catalog
clauses, once, in descriptions only. Machine schema constraints, enum values,
permission decisions and handlers stay native. The same projection is used for
MCP discovery, `operations_describe` and `agent_config_get`; it never runs on user
records or conversation text. The Operations library depends on Agentic only for
these instruction seeds, not for authorization or execution.

## Behavior code

The `qorqut_agentic.behavior` modules own:

- `composition`: mandatory section order and capability-based guidance selection.
- `workspace`: prepared Claude and Codex instruction composition and delegation branches.
- `instructions`: template rendering, recovery selection, skill selection and budgets,
  and the activity-reminder condition.
- `attention` and `attention_clock`: reminder wording, presentation, timing and deferral.
- `completion`: review eligibility, finding closure and correction formatting.
- `sources`, `seeds` and `tool_help`: supplier aliases, initial seeds and tool wording.

Authentication, operational reads and writes, hook registration, native process
observation, durable claims, clock access and message transport stay with the host.
Authored catalog text is never executed as Python. Template parameters are data
and are substituted once. Operational records, status labels and ordinary errors
are not a second set of behavioral rules.

## Installation and refresh

Fresh imports require the protected runtime and tool instruction packages.
Active delivery reads their current managed content without a packaged fallback.
Trusted preparation also writes `.qorqut/runtime-instructions.json` and copies
its standard-library renderer beside isolated hooks. These are generated copies
for startup and service outages, not separately authored sources.

Instruction-only changes use the existing locked projection refresh: verify the
prepared files, refuse local-edit conflicts, and replace the snapshot atomically.
Changed executable behavior still requires normal trusted release/preparation;
editing catalog text cannot load Python into a running client.

Existing installations need a reviewed catalog update followed by agent
preparation. Opening the service does not overwrite the catalog. Older inventory
bindings remain verifiable for update history, including bindings that predate
these packages. Retired and legacy entries retain their existing activation state.
