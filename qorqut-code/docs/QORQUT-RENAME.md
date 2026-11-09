# Qorqut application identifiers

The host's Python import package is `qorqut`, its distribution is `qorqut-core`,
and the email engine distribution is `qorqut-engine-email`. All in-tree
connectors, resource indexes, launchers, examples and build tooling use these
names. Start the service with `python -m qorqut.service`.

New agent preparation uses `.qorqut`, the `qorqut` MCP server,
`mcp__qorqut__` tool permissions and `qorqut-*` skill names. New owner preparation
uses `.qorqut-owner`. The default tmux socket is `qorqut`; an installation's
explicit `BIZ_TMUX_SOCKET` still takes precedence. New history snapshots use
`qorqut-history`, continuing an existing `bazarlyk-history` without deleting or
rewriting it. Use `QORQUT_MCP_URL` and `QORQUT_CAPTURE_TRANSCRIPTS`; their legacy
environment names remain read-only fallbacks, with the new names taking precedence.

## Existing installations

This is a coordinated package and managed-configuration change, not a live
find-and-replace of an installation. The ordinary native update admission check
continues to reject changes to managed instruction content. Do not bypass it or
replace an environment while native clients still reference it.

Build a new environment and rebuild every selected connector against
`qorqut-core`, including private connectors outside this repository. Update the
application command and any helper module allowlist to `qorqut.*`. Apply the
managed instruction update through its existing plan/conflict workflow, preserving
owner-authored changes, then regenerate registered agent and owner preparation
through their supported preparation paths. Their generated paths, skills,
permissions and MCP configuration must agree before launching the new clients.
Existing saved `.bazarlyk` projections and old helper artifacts remain retained
until their consumers have exited; a directory rename or symlink does not update
their recorded hashes, commands or registrations. The rename itself does not
modify the live database, profiles, package pins or running sessions.

The compatibility reads are deliberate: old environment settings remain usable,
old chat tool receipts still render, old private directories remain excluded from
authored exports, preparation retires the old managed MCP entry, and update
preflight checks both old and new tmux sockets. Historical secret-scanner
fingerprints retain their exact original commit/path identities.
