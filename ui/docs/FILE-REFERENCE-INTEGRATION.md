# Actual core/MCP/file-card acceptance

Manager verification assignment, 2026-10-09. Pin accepted core
`6e7b67b53c826dd5e995c522d2e3fa12de019fb6` (implementation c13eb8b) and UI
`3d5e5e7483d1e5a1eb9ca1814b2fe1faf85ee26e` (implementation 52c21d0).
No unfinished dispatch/collector code is a dependency. Execute this fixed flow;
report defects and evidence, without product fixes, redesign or new test suites.

Build the pinned UI with its lockfile. Build/install the pinned core wheel in a
private environment outside the source tree, recording hashes and installed
module provenance. Existing compatible dependency environments/artifacts may be
reused, but the core import must be the exact pinned package, not PYTHONPATH.
Use a fresh temporary data root, loopback port and owner/agent credentials. Run
the normal console server and background worker; no manual job pump, direct
domain publication call or SQL mutation. No local-terminal/native-program
configuration, CLI/model, browser profile reuse, VM or global service is needed.
Use only this run's synthetic files and generated credentials. Keep tokens out
of URLs, logs, screenshots, traces and committed evidence.

1. Initialize a fresh installation/onbash and serve the actual compiled UI.
   Wait for normal workspace initialization through the public API. Sign in via
   the real session form using an isolated Chrome profile. This is a real core
   HTTP session, not the UI synthetic API fixture.
2. Write small unique disposable files: one Unicode relative workspace path and
   one absolute host path outside that workspace. Include significant spaces
   and an HTML-shaped note/label in a reference without using source contents
   as the reference. Invoke files_reference through the official MCP client:
   one owner-attributed call selecting onbash, one authenticated onbash call
   omitting agent_id. Bootstrap only the matching two registered credential
   bindings needed for this fixture; never use personal credentials.
3. Observe exactly one typed Chat card per reference ID, with matching sender,
   location, full saved path, label, escaped note, observation/recording times and
   exact size. Verify normal HTTP conversation_list and the automatically written
   UTC transcripts/YYYY-MM-DD.jsonl agree with the MCP result and reference
   identities. Wait through normal polling; never manufacture publication state.
   Record IDs/hashes and sanitized evidence, not source contents or tokens.
4. Copy a path in Chrome, checking exact bytes. Prefer real isolated clipboard
   permission; if unavailable, report the limitation and use the already-scoped
   stub only for a presentation observation. Capture desktop and 390px phone
   screenshots; inspect wrapping, focus, page errors and horizontal overflow.
   Observe that showing/copying cards makes no file-content fetch/open request.
5. Remove only these disposable source files. Repeat the same MCP calls with
   their original idempotency keys/arguments: saved results must replay, cards
   and dated entries must not duplicate, and saved metadata remains readable.
   Stop/restart the same temporary core normally. Reauthenticate when required
   and verify both cards and dated records persist unchanged with the same IDs.
6. Close the owned browser/context and server and verify owned processes exit.
   Preserve the private evidence/data for Manager review. Do not change or stop
   any owner's installation, tmux/native session or service.

Only tracked output is docs/evidence/file-reference-integration-r1.md, recording
exact source/installed/build provenance, commands, results, original private
scripts/log/screenshot paths and limits. Keep the harness private rather than
adding an example app or duplicating suites. A failed check is a finding, not
permission to alter the product. Commit/push this report on the assigned branch;
Manager reviews and decides acceptance/merging. No deployment or claim of native
Chat delivery, browser takeover or complete transcript coverage follows.
