# Desktop QA repairs — Manager acceptance

2026-10-13. The five earlier QA workers each covered only 20 of 50 criteria.
Their delivery was not a whole-product pass. This repair round used Sonnet 5.5
ACLA workers, at most three in parallel, with self-review disabled.

Accepted components, integrated in one tree:

- UI `9499c3c`: scrollbar, whitespace/send, history errors, copy feedback,
  name validation, correction/retry and rename focus fixes. Manager ran the nine
  added UI regressions and the affected existing scenarios in disposable Chrome.
- Terminal `756f9a7`: human Ctrl+C/Ctrl+Z input boundaries and bounded admission
  while superseded viewers close. Manager passed 14 backend checks, inspected the
  recorded rapid-switch Chrome evidence and ran the combined UI scenarios.
- Local agents `0ec8541`: schema 11, local workspaces/private native homes,
  recorded shells, activation and per-agent runtime. Manager passed 19 focused
  checks. The actual console/API/tmux flow created two ordinary agents with
  distinct shells and no automatic provider launch. Owner and native transcript
  fixtures were synthetic. The combined tree passed eight admission/local-agent
  checks after preserving both test helper arguments at merge.
- Local-agent UI `9e1640f`: neutral creation notice, local type labels and
  removal of the unused Machine/Desktop card. Manager inspected screenshots,
  rebuilt the UI and passed the three affected existing Chrome scenarios.
- Local capabilities `6b47b48`: ordinary-agent absolute file references and
  explicit own-browser attachments. Manager reviewed the two production gates
  and passed four focused tests in the combined tree: path attribution, replay
  and dated publication, invalid paths, and separate browser control/card/message
  routing. Browser peers were simulated HTTP services, not Chrome/VNC.

All Manager checks used disposable test roots and isolated runtime resources.
Existing owner data, native logins, conversations and browser profiles were not
test fixtures. No new native provider reply is claimed by this repair round.

One pre-existing file-reference assertion still expects mutable conversation-view
fields in an immutable transcript line. The worker's file suite was 11/12 and
browser suite 13/13; that failure was not represented as a pass. Source inspection
confirms that the obsolete expectation includes `inbox_reference` and
`browser_request`, while the durable format contains `Entry` fields only.
Other earlier reported baseline/load-dependent failures remain in the worker
evidence. This report does not claim a clean full suite or all 50 QA criteria.

Creation still starts no CLI or browser service. An ordinary agent needs its own
explicit browser attachment. Sequential observation under heavy host load remains
a documented limit in [D62](../LOCAL-AGENTS.md). Raw archives and visible Chat are
still separate. Deployment and push status are recorded separately.

Private commands, test logs and screenshots are under
`/home/alp/qorqbot-launch/qa/2026-10-13/acla-fixes-9552beea/`, including
`local-agents-manager-checks.log`, `local-agents-integration-checks.log`,
`local-ui-manager-checks.log` and `local-capabilities-manager-checks.log`.
