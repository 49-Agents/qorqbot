# Paper workspace entry

The Outreach workspace always opens `/integrations/outreach/ui/paper`. There is
one visible Outreach entry and no profile switch or old navigation handler.
Historical workspace selections open the fresh Paper campaign list; no campaign
ID is converted and no historical draft is loaded. The stable internal
`campaign_review` key preserves links without exposing a second interface.

The guarded handshake retains exact origin/window checks, signed owner session
exchange, business isolation, serialized cookie changes, unsaved-edit approval
and stale-response protection. Focus and other agent panes remain available.

The entry pairs with the host's registered-agent connector and the Paper service's registry and worker fencing.
Focused checks: `tests/test_paper_host.js`, `tests/test_campaign_review.js` and
`tests/test_workspace_dispatch.js`.
