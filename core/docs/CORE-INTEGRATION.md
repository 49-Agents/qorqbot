# Core package integration: first increment

Manager scope, 2026-10-07. Combine the independently accepted primitives into
one installable package. This prepares the base for domain and runtime work;
it does not implement the headless agent workflow or satisfy a system gate.

## Exact inputs

Start from the Manager coordination commit containing this plan. Integrate
these immutable accepted revisions, in the order listed, on a new dedicated
`codex/core-integration-r1` branch:

| Revision | Accepted input |
| --- | --- |
| `d2e251c4cd02be91b95c392d225cf52d25c2958a` | HTTP/MCP/operator, including foundation ancestor `ef18e59e2df372963f5fba222156fe6b6e50195f` |
| `2db2be6d051e83b383e240aed41424a212153107` | Provider preparation/discovery adapters |
| `170412438596a98845e2fee8ed3f48ce984b899b` | Local terminal control/input primitives |
| `08a0273cccf172d8f5a3c0d98fdff93a36531521` | Local transcript spool and nested-PTY recorder |
| `44328807c84db01c3351bef74ada285171117d15` | Environment prerequisite evidence only |

Use ordinary non-fast-forward Git merges in this integration branch, preserving
input commits and notices. Never import an unaccepted browser branch, sibling
runtime, operational data or another actor's uncommitted work. Retain the newer
Manager planning documents. A conflicting behavior/schema/pin requires a Manager
decision; do not silently resolve it by deleting evidence or selecting a new API.
No rebase, squash or force push is needed.

## Package and test boundaries

Keep Python 3.12 and the reviewed transport lock, including Pydantic 2.12.3.
Earlier adapter/runner checks used 2.13.5, so validate their actual code against
the retained core version. Do not upgrade dependencies simply to avoid a failure.
The existing setuptools package discovery should include storage, transports,
adapters, runner and transcripts. Preserve the console `serve`/`call` entry point,
recorder module entry point, passive imports and packaged storage NOTICE.

No production behavior change is planned. The integration actor owns necessary
package metadata corrections, a small integration test, and test subprocess
import-path corrections only. For a demonstrated production compatibility defect,
report the exact failure and request direction before changing domain/storage or
runtime code. Shared schemas, model families, registry and application assembly
are not redesigned in this increment. Public operations remain the existing four.

Run existing owning suites as separate Python processes so the two test-only
`support` modules cannot collide. Root discovery alone currently omits the
runner/adapter/transcript directories without package initializers. Explicitly
run root foundation/transports plus each of those three directories and the new
integration test; neither skip omitted tests nor duplicate their implementations.

Four existing child harnesses derive PYTHONPATH from the checkout: foundation's
subprocess check, adapter's aliased-root child, runner's crash/lock worker and
transcript `child_env`. For these paths, derive the import root from the loaded
`qorqut_core.__file__`, as the accepted transport harness already does. Source
runs then select source and wheel runs select the installed package. Preserve all
assertions, timeouts, ownership and private-child environment rules. Do not add a
product flag, second package, global environment change or test dependency.

## One shared local database check

Add one focused parameterized scenario under `tests/integration/`. Reuse the
existing positively owned real-tmux Receiver fixture and public TerminalRunner
and CaptureSpool APIs. It tests the shared file, not a managed native agent:

1. Exercise both schema-initialization orders on a fresh private local DB.
   Use the same real receiver binding's agent/recovery/boot/run generations for
   the capture binding; supply a separate synthetic installation ID/source epoch.
2. Register one terminal binding and one fixture-only `terminal` capture source.
   Append two small chunks in separate appends, then submit one safe fixture input
   through TerminalRunner with a
   stable attempt ID, and keep their receipts in that exact same DB file.
3. Commit a valid ACK for the first chunk and reclaim that chunk while retaining
   receipt/hash evidence and an unacknowledged suffix. Reopen both components;
   confirm source/offset/ACK and terminal attempt/control state remain intact.
   An identical terminal retry must not produce a second input effect.
4. Repeat schema initialization without resetting either subsystem. Check
   integrity/foreign keys, WAL/FULL settings on the owning connections and no
   competing global user_version. The separate host foundation database remains
   separate. Clean up only the fixture's owned tmux session/processes/data.

Use existing synthetic readiness/composer callbacks only within that receiver
fixture. This is no claim of real recorder readiness, provider compatibility,
native source ingestion or runtime launch wiring. Do not add a simulated product
mode or implement those later dependencies to make this integration test pass.
Keep one scenario with two initialization orders; no copied fault-test matrix.

## Verification and delivery

Use one disposable source-check environment and a separate wheel-install
environment built from the same lock, with no sibling environment imports.
Run all existing owning suites once from source and once against the installed
combined wheel, plus the one new shared-file scenario. Use isolated invocation
and retain evidence that parent and child imports resolve to the intended
package. A temporary test-only staging directory without source is also useful
for checking that no installed test silently falls back to the checkout.

Check dependency consistency, all locked distribution versions, installed
module/source inventory, entry-point help and retained NOTICE. The product
imports must neither open data nor launch processes/listeners. Existing import
checks may be extended to the newly combined module inventory in their owning
test; no additional import framework is needed. Record real tmux/OS versions.

Report every failed check and its practical meaning. Preserve the known
foundation busy-timeout observation; do not relax its timeout/assertions, omit
it, retry until green without disclosure, or treat a higher test count as a goal.
No new native/model turns, host installs, VM/service changes or live owner data.

Commit only task-owned integration metadata/test changes and concise evidence at
`docs/evidence/core-integration-r1.md`. Preserve merge history and record exact
input SHAs, ancestry, source/installed commands and results, locks/package hashes,
cleanup and remaining limits. Push the dedicated branch normally, then stop edits
for a fresh independent Astra MAX reviewer. Manager owns the eventual `main`
merge under the owner's standing authorization. The builder is authorized to
merge the exact inputs into its integration branch, not to land an unreviewed
candidate on `main`, create a release or deploy.

Acceptance establishes compatible packaging and local table coexistence only.
Agents/personas, messaging, managed runtime, host archive, browser integration,
UI, recovery and real VM/provider/system acceptance remain separate assignments.
