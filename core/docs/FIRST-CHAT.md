# First Chat without a manual seed prompt

Manager decision D52, 2026-10-09. D51 connects pinned Codex before its delayed
SessionStart hook. Both supported CLIs may create their primary transcript only
after input. Break that initial dependency without calling missing capture
healthy, changing the CLI launcher or creating another service.

## Initial input evidence

RuntimeInput retains its existing exact owner/process/configuration checks,
healthy recorded Terminal, human-control exclusion and fresh empty-composer
classification. Trust/setup dialogs, drafts, busy or unknown composers still
refuse input. No permission mode or classifier rule changes.

Prefer the latest accepted native hook exactly as today. Only when this owner
has no accepted hook may pinned Codex use its exact D51
`native.startup_observed` event as the initial session observation. Validate
actor, installation, owner/run, process, native UUID, terminal and recovery/
environment bindings. A mismatched or non-idle accepted hook must never fall
back to startup. Revalidate the same event immediately before submission.
The composer proves it is empty now; startup itself makes no idle claim.

Keep InputTarget and saved attempt JSON unchanged. Its historical field
`idle_event_seq` may identify this startup event for the initial attempt.
The existing one-notification-per-event barrier consumes it after any
dispatching/submitted/uncertain attempt. No new attempt after uncertainty,
no duplicate paste and no fabricated hook. Later native hooks use the normal
path. Historical receipts and hashes remain readable unchanged.

## Collector waiting for the initial primary

Extend private NativeInputObservation with `awaiting_primary: bool = False`.
This means a fresh successful discovery of the exact bound native UUID found
no primary yet and collection is ready to discover it. It does not mean bytes
were captured. The existing healthy/source observation retains its semantics.

Only the collector thread can publish this evidence, after its owned store,
directory and recovery guards and a successful discovery attempt. Require an
active current owner, no prior admitted local or host primary for this native
UUID (including earlier runs), no mapped source, and no operational failure.
Allowed discovery gaps are only the two existing blanket coverage limitations,
`bound_native_source_not_found`, and missing not-yet-created provider roots
(`managed_source_unavailable:FileNotFoundError`). Any malformed/partial/conflicting
matching file, other discovery error or failed guard denies bootstrap.
For a missing-root gap, verify the named provider root is actually absent;
an existing or uninspectable root cannot turn a failed file read into readiness.
Base freshness on that discovery's monotonic time, not a later cache refresh;
expire after the existing five seconds and clear on close/failure/restart.
Keep public collection state waiting until actual source admission.

RuntimeInput may substitute this exact fresh waiting evidence for the native
healthy/source-link requirements only for an initial SessionStart or D51 startup
event. Preserve all Terminal capture requirements. All other events require the
normal healthy primary. Existing delivery persistence prevents more than one
possibly-performed notification for the initial event. Once the primary appears,
ordinary discovery/capture/archive/projection proceeds without any manual pump.
No zero-byte source, guessed filename or manufactured receipt is permitted.

## Implementation and focused verification

One builder owns native_collector.py, input_dispatch.py and their owning tests.
Keep public schemas, migrations, UI, dispatcher/runner state machines, native
configuration and parser behavior unchanged. Reuse fixtures and add a few
grouped checks: cold discovery versus failures/staleness; startup fallback versus
real/busy hooks; first input versus human hold/nonempty composer; one-event
consumption and unchanged historical attempt decoding. Ask Manager if another
module or policy change is needed.

Then run one fresh isolated actual Codex flow using completed startup/connected
Chat helpers and the installed candidate. Manually launch once, complete ordinary
visible setup, release Terminal control and send the first message from real
Chat without a seed prompt. Use only the existing local canned provider. Check
one notification/receipt, native source discovery and automatic raw/daily
transcript publication; then a second distinct Chat message through normal hooks.
Do not repeat the broad responsiveness or restart matrix. Preserve refusals and
unknown input without replay. Do not retry the blocked Claude MCP workflow.
Report actual processing/handling separately from submission, and clean up only
owned fixture resources. Manager reviews the source and focused evidence.
