# Runtime instruction ownership

Agentic owns the wording, selection and behavioral policy for Qorqbot's startup,
native hooks, Attention reminders and completion guidance. The host supplies
authorized records and performs authenticated delivery. This consolidation keeps
the existing wording, prompt order, timing, role exclusions and correction limits.

## Where to read the instructions

| Content | Canonical location in the instance | Delivery |
| --- | --- | --- |
| Role responsibilities | `roles/imported-roles` | Mandatory startup configuration |
| Shared foundation, work loop and instruction fragments | `operations/imported-host-builtins` and its registered documents | Mandatory configuration, selected by available capabilities |
| Role skills | `operations/imported-role-skills` | Selected skill projection; nine configured skills are loaded at startup |
| Owner preferences | `user-preferences` | Mandatory configuration |
| Hook, recovery and reminder messages | `operations/runtime-instructions` | Conditional native hook or background delivery |
| Agentic management | `operations/agentic-management` | Authorized management orientation and procedures |

The [runtime message index](../src/qorqut_agentic/resources/defaults/operations/runtime-instructions/README.md)
lists each migrated message, its delivery path and active/legacy status. Its
machine-readable index declares substitutions and previous source locations.
Template parameters contain data and are substituted once, never interpreted as
additional templates. Dynamic source records, status labels and ordinary errors
remain data; they are not independently authored behavioral rules.

## Behavior code

The `qorqut_agentic.behavior` modules own:

- `composition`: existing mandatory section order and capability-based guidance selection.
- `instructions`: template rendering, recovery-message selection, always-loaded
  skill selection and budgets, and the existing activity-reminder condition.
- `attention`: bounded reminder presentation and choice of focus, board or
  workflow-detach guidance.
- `attention_clock`: focus/board reminder timing, owner-message deferral and
  workflow-detach eligibility.
- `completion`: review eligibility, finding closure and correction formatting.

The host retains authentication, permission checks, operational reads and writes,
native hook registration, process observation, durable claims, clocks and message
transport. Executable behavior code is shipped with the Agentic library; authored
catalog content is not executed as Python.

## Prepared copies and refresh

The fresh importer registers `runtime-instructions` as a protected operations
package, using the same inventory, provenance and catalog validation as existing
native instructions. Server-side messages read the current managed package.
There is no runtime fallback to the repository's default wording.

Trusted agent preparation writes `.bazarlyk/runtime-instructions.json` from that
catalog and copies the standard-library Agentic renderer beside native hooks.
These files are generated projections, not a second authored source. They allow
startup and recovery hooks to work while the service is unavailable. Missing or
invalid snapshots are reported; they never select another set of instructions.

Instruction-only changes reuse the existing locked skill refresh and projection
receipt. Refresh first verifies the prepared files, preserves local edits by
refusing conflicts, and replaces the snapshot atomically. Changed behavior code
requires normal trusted preparation/release adoption; it is not loaded from the
authored catalog or silently rewritten into a running client.

Existing installations need an explicit reviewed catalog update containing the
new protected package, followed by agent preparation. Opening the service does
not seed, migrate or overwrite an existing instruction catalog. Package updates
that change managed instructions retain the existing release qualification rules.

Retired Focus completion wording and legacy environment/briefing fallbacks remain
labelled for inspection and historical compatibility. Consolidation does not
activate them, change the Ordubashy role, add checks, or introduce a new supervisor.
