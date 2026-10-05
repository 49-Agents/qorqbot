# Manual creation UI acceptance

Manager acceptance, 2026-10-08. Accepted UI:
`6a4841d5830ae3970b492ebf87528de4f94f787c`; paired core:
`5a50666c981c727c32dad239886ba5fcd7c9f1ac`.

Creation now requests only display name and one of twenty icons. It sends no
provider/model/start configuration and explains that the user starts the CLI in
Terminal. The inspector reports observed runtime information with honest
unknown/unavailable states. Existing durable mutation, draft and session behavior
remains. No fake Terminal connection or automatic native launch is introduced.

The [source implementation evidence](manual-creation-ui-r1.md) retains the
original checks and their actual provenance. A fresh independent Astra MAX ACLA
review of the paired core/UI increment subsequently passed both actual Chrome
flows against the normal installed console server. Those checks exercised
creation, rename/icons, distinct equal messages, onbash, exact automatic UTC-dated
files while the browser was closed, server restart, sessions and desktop/mobile
layouts. They used synthetic data and no mocked API responses or manual job
pumping. Reviewer and builder were approved separately.

Manager independently verified all 43 UI source files and the retained artifact
checksums, inspected the browser harness and three desktop/mobile screenshots,
and passed the historical and installed core creation checks. Manager did not
rerun the entire Playwright workflow. The host's missing emoji glyph does not
represent content loss; exact Unicode survived DOM/API/SQL/file comparisons.

The authoritative combined pins, checks, provenance and limitations are in
[core's Manager acceptance](https://github.com/qorqutbot/qorqut-core/blob/main/docs/evidence/manual-creation-integration-manager-acceptance.md).
Native startup/inbox association, live Chat delivery, assistant ingestion,
Terminal transport/UI, browser cards and VM integration remain pending. This
merge is not deployment or whole-system acceptance.
