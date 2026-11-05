# Scripting and automation — reviewed source branch

This branch contains the original reviewed scripting-and-automation implementation from `v1.3/scripting-integration-20261030`, collected from its independent component repositories. `SOURCE-MANIFEST.json` records every original commit. Source file contents are preserved; obsolete submodule wiring is omitted because the component directories are included directly.

This is a source snapshot, not a rebase onto current public `main`, a merged release or a deployed feature. It deliberately has its own snapshot commit rather than importing the private development history. Integration with current public code remains for later. Tests, builds and migrations were not run for this publication.

The new extension is in `qorqut-automation/`. Its README describes Python scripts, schedules, output subscriptions, input waits and action Signoffs. Host integration and offline upgrade instructions are in `qorqut-code/docs/AUTOMATION-UPGRADE.md`. Shared engine changes are in `qorqut-code/qorqut-operations/`; bundled role changes are in `qorqut-agentic/`, with the matching independent role source under `qorqut-roles/agent-roles/`.
