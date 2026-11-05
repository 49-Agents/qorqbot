# CRM paired navigation implementation report

## Review corrections

- Kept the canonical scope on CRM rows independently from the CRM table scope. Explicit `business_id: null` remains Global, an explicit project scope resolves through loaded business metadata, and an unresolved or contradictory slug fails closed.
- Allowed a CRM parent to differ in scope from its row while preserving the parent breadcrumb and Back path. The bridge validates both identities independently; standalone chat references do not inherit an unrelated selected CRM parent.
- Resolved the project slug before mounting the paired private conversation so same-ID Global and project drafts remain separate. Chat message task/reference callbacks now route through the actual current agent pane, select and mount Focus when needed, and dispatch through its live Focus frame or workspace. An undefined/stale callback cannot report success. The detached native chat cannot mark messages read.
- Kept the paired record in the existing right Focus pane, with the selected record filling that pane while the original left transcript stays interactive. Removed the overlay behavior for the paired path.
- Fixed paired host disposal, removed the CRM child Pin control, forwarded CRM navigation options through the record workspace, and used the actual CRM title in breadcrumbs. The CRM mount owns its title and description without extra copies.
- Reworked the Focus conversation browser test and added main-transcript table/task reference navigation from the Business pane, plus a composed synthetic Focus → CRM → Global row → Back scenario using the real Focus/record workspace mounts and a stubbed CRM mount seam.

## Verification

- Passed: `tests/test_focus_conversations_browser.js` with the configured Playwright module and `/usr/bin/google-chrome`. Covers main-transcript references, automatic Focus selection, visible right-pane content, no modal/overlay, parent context, same-ID scope-separated drafts and a canonical private send.
- Passed: `tests/test_agent_workspace_navigation_browser.js` with the configured Playwright module and `/usr/bin/google-chrome`. Covers the same-origin frame bridge, independent parent/row scope validation, canonical Global row and project CRM identities, Back restoration and stale generations.
- Passed: `node --check` on changed JavaScript and browser test files; `git diff --check`.
- `tests/test_agent_focus_browser.js` still times out at its unrelated Browser attention marker assertion (line 359). I reproduced the same timeout with the review edits stashed, at the original implementation commit `7148d46`, confirming it predates this review round.

## Integration seam

The table workstream's optional `crmTableMount(..., {onOpenRecord})` callback is still needed at integration. This branch tests the navigation callback with a synthetic CRM mount and does not include or merge sibling table code.
