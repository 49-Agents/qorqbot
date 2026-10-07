# Combined chat and server verification

Manager plan, 2026-10-08. Combine the reviewed chat domain and console hosting
without changing their production contracts. This closes the gap between a
manually pumped domain test and core's automatically maintained dated files.
The independent UI revision continues separately; actual frontend/backend
browser checks follow its acceptance.

## Ownership

Use the assigned isolated integration branch based on the exact Manager merge.
Production code, migrations, package versions and the two reviewed inputs remain
unchanged. Own only `tests/transports/`, a narrowly necessary adjustment to
`tests/messaging/test_transports.py`, and integration evidence. If a production
defect or another design choice is found, report it with a reproducer and ask
Manager before making that change. Do not modify other repositories or workers.

## Existing fixture corrections

`WorkerRecovery.fixture_jobs` currently appends synthetic `conversation_publish`
and `message_delivery` handlers to the full registry. Actual handlers now exist.
For this deliberately synthetic eligibility test, replace just those two kinds
in its local test registry; preserve all other handlers and existing exclusion,
finite-delay, recovery-hold and no-execution assertions. Do not relax the
production duplicate-kind check or mistake this fixture for real publication.

Concurrent real publication may change the conversation's publication observation
between HTTP and MCP reads. In the existing cross-transport test, wait with a
bounded deadline for the real publication to settle before comparing complete
views. Preserve exact immutable entry, authorship, handling and delivery checks.
Do not ignore arbitrary mismatches or assert that all in-flight observations are
identical. No additional mutation may be used merely to trigger publication.

## Two focused integration flows

1. Start the actual core server on an isolated loopback port with synthetic
   installation/credentials and no UI root. Use existing installation helpers.
   Register two VM-kind agents through real application operations during setup,
   bind test credentials to their actual registered actors, and retain onbash.
   Do not prepare their workspaces by directly running jobs. Let server startup
   and its worker provision the folders while lifecycle truthfully remains
   provisioning/runtime unavailable. Sign in using the real cookie route; send
   two equal-text owner messages with different keys and replay one exact key.
   Send an agent-to-agent message through the real MCP SDK and an owner message
   to onbash. Check exact recorded identities/content and expected entry counts.
   Close all clients, then observe the dated files appear using bounded read-only
   filesystem polling. Compare parsed JSONL fields to the saved immutable entries,
   including exact Unicode/newlines, source/message IDs and recording-date
   filenames. Reconnect and confirm current/error-null publication with no lag.
   Restart normally and verify original entries/files and same-key replay remain
   unchanged. No UI connection, export/save tool or direct queue pumping is used.

2. Verify restart after an actual publisher receipt boundary. During synthetic
   setup, save a message using the real application operation. Start an owned
   test child server whose fault hook exits its own process at
   `conversation_after_attach_commit`, after durable pending-receipt attachment.
   Keep this fault injection entirely in the test helper. For this child only,
   wrap `Jobs.run_next` to use a one-second claim lease; do not change production
   lease defaults. Await its recorded fault/exit and confirm that it exited at
   the intended boundary, rather than interpreting any process failure as the
   expected crash. Restart using the unmodified normal server entry point and
   real clock. Its background worker must expire/reconcile the original-epoch
   intent, install the file and finalize publication without another message,
   manual run_next/reconcile calls, or a UI. Check exact one-entry content,
   original message/receipt binding, current publication and healthy server.
   Use bounded deadlines; retain existing recovery-held-work refusal tests.

Extend `owned_server.py` and its fixture only with the optional test crash point
and short-lease controls needed for the second flow. Existing test behavior is
unchanged when those controls are absent. A child may exit only itself; cleanup
may stop only positively owned test processes and remove newly owned temp data.
Never use live Qorqut installations, owner credentials, profiles or native stores.

## Checks and evidence

Use a private environment with unchanged dependency pins. Run the transport suite
including the two new flows, plus the existing messaging HTTP/MCP test once on
final source. Build the combined wheel, install it into a separate environment,
and run the two new flows, both synthetic worker eligibility/delay tests and the
messaging HTTP/MCP test from outside the checkout. Prove child servers import the
installed product, and compare installed files with candidate Git bytes. Do not
repeat unrelated foundation/archive/native matrices or create usage examples.

Commit the focused test corrections, two grouped flows and concise evidence with
exact commands, SHAs, provenance, results and limitations. Push only the assigned
branch. A fresh independent review precedes Manager acceptance and main landing.
This proves automatic publication of recorded platform messages, not native
assistant ingestion, terminal submission, browser/VM readiness or all release
gates. Those requirements remain in the global implementation plan.
