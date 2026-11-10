# Contributing

Use a focused change with a concise explanation and the checks that demonstrate
its behavior. Include documentation when a user-visible operation changes.
Keep credentials, personal records, local configuration and machine paths out of
code, examples and screenshots. Use synthetic examples.

Current backend and dashboard tests are in this repository. Ordinary PR checks
run application tests; installer and connector qualification runs for relevant
changes or an explicit download release. See [CI and release preparation](qorqut-code/docs/CI-AND-RELEASES.md)
for selection rules, local commands and retained evidence.

## Rights and license

Only submit work you have the right to contribute. Preserve existing third-party
copyrights, licenses and provenance. Identify copied or adapted material and its
source in the pull request; do not relabel it as first-party code.

Use the supported versions and standard tools documented by the project. Changes
to optional integrations must keep core usable when the integration is absent.
