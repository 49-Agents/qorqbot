# Isolated native CLI interaction rehearsal

Manager scope, 2026-10-08. The first connected UI/core workflow is accepted.
The next runtime design needs actual CLI observations beyond the existing help
and synthetic-format evidence. This assignment is exploration and reproduction,
not a new runtime architecture or implementation of public operations.

## Fixed boundary

Use the accepted core package in a dedicated worktree. Own only
`docs/evidence/native-rehearsal-r1.md`; private fixture scripts, raw synthetic
logs, environments and disposable native homes remain outside Git. Do not edit
production code, existing tests, dependency pins, capability flags or global
plans. Do not inspect the unaccepted browser MCP candidate or resume its blocked
review. The browser endpoint below is an explicit harmless test service, not
browser implementation or acceptance.

Use only new synthetic HOME/workspace/XDG/native-config directories and a fully
constructed environment without inherited authentication, provider configuration
or owner sessions. Locate the actual installed native executables without reading
private configuration. Record their paths, version output and executable hashes.
Compare with the accepted Claude `2.1.287` and Codex `0.160.0` profiles. A mismatch
is evidence for Manager, not permission to change or pretend a supported profile.

Manager amendment after question 144: the installed Codex executables now report
`0.161.0` and `0.155.1`; neither satisfies the accepted profile. Preserve those
observations. Rehearse Codex `0.160.0` using a separately acquired, pinned private
copy from the official `@openai/codex` npm package. Manager verified that exact
version's registry metadata. Acquire dependencies outside the loopback-only
namespace into a new task-owned directory with clean npm HOME/config/cache,
explicit registry and install scripts disabled. Do not modify global packages,
PATH defaults, either installed executable or accepted capability profiles.
Verify npm integrity, package version, launcher/native binary hashes and actual
`--version` inside the namespace before using the private absolute executable
with the matching adapter. Stop and ask if these checks fail; never substitute
a version string or fall back to `0.161.0`. Label every resulting observation as
the pinned private `0.160.0` rehearsal, not compatibility of the current host CLI.

All native interaction and fake HTTP/MCP endpoints must run together inside an
owned disposable network namespace with only loopback. Test user/network namespace
availability first. Do not install system packages, edit host networking, use elevated
host changes, or fall back to external provider access. If this containment cannot
be established, report the exact failure through a Manager question before any
interactive native launch; independent read-only version/source checks may proceed.
No owner credentials, live stores, paid calls or external model requests are used.

## Observations to obtain

1. Use the existing adapter preparation with unique run/agent/conversation IDs,
   real authored synthetic persona and two separate loopback MCP URLs with
   distinct synthetic credentials. Run two harmless official-SDK MCP servers,
   each advertising one distinguishable read-only probe tool. Record whether
   each native CLI loads the generated configuration, discovers both endpoints
   and presents the correct tools. Save only synthetic request/identity evidence.
   A browser probe is not proof of Chrome control or its independent MCP product.
2. Point native inference exclusively at a loopback fake provider. Test-only
   native configuration may supply its documented endpoint/model/key settings
   in the disposable home; preserve before/after generated-file diffs and
   distinguish these fixture additions from the adapter's actual output. Serve
   one canned benign text response, `qorqut-native-probe-complete`, in the pinned
   CLI's documented protocol. No actual model runs. Use local installed code/help
   and primary documentation to determine the observed protocol, identifying
   versions and uncertainty. If an additional product contract/design choice is
   necessary, ask Manager rather than changing accepted preparation or capabilities.
3. Launch each native CLI under the existing recorder inside a new private tmux
   socket/session. All process/session IDs, files and cleanup must be positively
   owned. Use accepted preparation/launch and recorder APIs; test-only fixture
   orchestration is permitted. Observe the actual pane/recorder/native process
   relationship, successful-exec evidence, fresh recorder probe and raw spool.
   Do not promote supplied identities or historical health into live readiness.
4. Drive only one benign prompt through the owned test terminal after observing
   its actual composer. Record screenshots/text and input timing, including any
   trust/authentication/setup prompts. Fixture driving is not authorization to
   override production composer gating. Report which readiness/paste facts the
   installed CLI actually proves and which remain unknown.
5. Inspect only the resulting synthetic native stores. For fresh Claude and
   fresh Codex, identify concrete evidence linking the exact owned native process,
   run and session UUID; note when that evidence appears relative to first input.
   Run accepted discovery on an identity only when the prior association is
   independently established, never by newest filename/cwd alone. Record format,
   offsets and exact raw source bytes versus terminal capture. Do not create a
   capture mapping by assumption or claim attachments/children are covered.
6. If the fresh session succeeded, stop only its owned processes and perform one
   isolated native resume of that exact session using a distinct run binding and
   the same disposable home. Observe whether startup input is replayed, where
   subsequent bytes appear, and whether the native file appends, rewrites or
   moves. Use a second canned response and preserve exact synthetic before/after
   file identities/hashes. No general stability loop or full compatibility matrix.

## Report and stopping rule

Report facts, precise invocation/environment variable names (no credential
values), versions, source references/hashes, observed process and session links,
generated-config deviations, commands/results and failed attempts. Distinguish
actual native CLI behavior with a fake provider from real inference, semantic
tool use, complete transcript coverage and product readiness. No design choices,
new public APIs, unsupported success claims or usage examples belong in this
report. Findings guide Manager's managed launch/native-reader decisions.

Keep the rehearsal compact: one fresh and one resume flow per supported installed
CLI, plus only failure-specific diagnostics. Confirm no owned descendants,
listeners, tmux server or credentials remain after cleanup. Retain useful private
synthetic evidence and record its location; do not commit raw native stores.
Commit and push the evidence report on the assigned branch. A fresh independent
review follows before any evidence advances the runtime implementation plan.
