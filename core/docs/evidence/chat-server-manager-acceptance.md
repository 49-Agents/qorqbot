# Running-server chat publication acceptance

Manager acceptance, 2026-10-08, for candidate
`4e969f944cd5736de7a42ead0ebc60592804b5c3`, based on
`3e9ac2b1613550d81d5e24a554ad7858ebb5dad6`. This combines the previously
accepted chat domain and console hosting without changing their production
code. The six changed paths contain fixture corrections, two integration flows
and [implementation evidence](chat-server-integration-r1.md).

Independent ACLA review 137 used Claude Code Sonnet 5.5 at xhigh effort under
the owner's replacement launch policy, with internal self-review disabled. It
found no blocking in-scope defects. Thirty source transport tests, one source
messaging test, five installed checks and two installed child-provenance checks
passed. The reviewer verified all 56 installed product files against candidate
Git bytes and confirmed the four child servers imported the installed product.

Manager inspected all six changes and the relevant worker, publication and
fixture boundaries; verified the exact clean, pushed candidate and unchanged
production tree `57c8ef83e6db94c12fe750db419a03c954d07277`; matched all 158 tracked
files, 56 installed product files and 31 dependency pins; and verified the
reviewer's wheel SHA-256
`537572b5269f9a8b9ee783a80eb3f93f5356c7025b3dad0d24403c0ca67e4899`.
Manager independently ran both integration flows from outside the checkout
against that installed package: two passed in 42.093 seconds. Temporary
installations were cleaned. The verification script, result and log are retained
privately in the run's `manager-integration-r1/` directory.

The accepted behavior is automatic publication of recorded platform messages
to each agent's dated JSONL files, including onbash and exact Unicode content.
Cookie and MCP callers preserve their actual identities. Equal text with distinct
keys remains distinct; exact replay is unchanged. Publication continues with
clients disconnected. Normal restart preserves entries and bytes; restart after
durable receipt attachment finalizes the original receipt and job without
another send or manual reconciliation.

The crash helper's one-second initial claim lease also covers provisioning.
An unusually slow provision could cause an explicit test failure before the
requested crash; this was not observed. It cannot falsely satisfy the checked
fault point, process identity and exit status. Keep this timing limitation
visible without changing production behavior or expanding the fault matrix.
Publication progress still renews the production 30-second lease.

This acceptance does not cover native assistant ingestion, terminal delivery,
real VM/browser execution, the UI, deployment or full G0–G6 gates. Agents remain
in provisioning and deliveries remain pending with runtime unavailable. Actual
browser testing against the compiled UI follows its independent acceptance.
Reviewer and builder were approved separately in messages 138 and 139.
