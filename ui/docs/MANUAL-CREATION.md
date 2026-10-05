# Creation without starting a CLI

Manager implementation plan, 2026-10-08, D36. Read the exact core D36 plan SHA
provided in the handoff, AGENTS.md and SHELL-IMPLEMENTATION.md. This assignment
corrects the existing UI; it does not yet implement the terminal viewer or inbox.

Creation asks only display name and the existing twenty-icon picker. Remove
provider/model/start controls and model validation. Store.create accepts only
name/icon and sends only those plus the existing idempotency key. Do not send
hidden provider/model/start defaults. New core accepts this identity-only input;
legacy optional core fields are not UI configuration.

Keep the existing pending-mutation, ambiguous-response, retained-key, retry,
draft and generation guards unchanged. Pending requests are immutable snapshots;
never modify a previously sent request's args/key to make it match a new form.
In-page requests cannot survive asset reload; no new persisted draft/request
store is needed. Preserve metadata rename/icon updates and creation error UX.

Explain briefly that the user starts/configures the CLI in Terminal. Do not
claim Terminal is currently available, create a fake tab/button, or make opening
the dialog/chat start anything. Existing unavailable viewer explanations stay.
Remove configured provider/model/start-request fields from the inspector; current
run may show the actual existing observation or Unknown. No model/effort/provider
selector anywhere. Keep unrelated work/status/publication/identity details.

Update the existing grouped creation browser test and synthetic wire fixture:
assert controls absent and actual request lacks cli/start, accepted creation
still shows truthful provisioning and retains twenty icons/rename behavior.
Preserve the other five grouped browser tests and their ambiguity/recovery cases;
no additional component matrix or usage examples. Run npm build and the six
Chrome checks once on final changes using installed /usr/bin/google-chrome,
fresh disposable contexts and temporary ports; visually inspect desktop/mobile.
No external QA service or owner sessions. Keep private artifacts outside Git.

Core's shape otherwise remains unchanged (14 operations, schema 5, legacy
AgentView fields retained). Core and UI corrections are built in parallel;
Manager later runs compiled UI against installed revised core. Synthetic UI
checks alone do not establish that integration or native/browser/VM readiness.

Own UI src, relevant existing browser tests/fixture and concise evidence at
docs/evidence/manual-creation-ui-r1.md. No dependency changes are needed. Do not
edit core/browser or global plans. Commit/push assigned branch for a fresh
independent review; Manager owns merge and combined acceptance.
