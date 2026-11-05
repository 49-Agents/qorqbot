# Reference review UI preview

Run `node tests/reference-review-preview.js` and open its printed loopback URL.

This preview uses the real dashboard assets, core icons, shared chat renderer, attachments and composer. `js/chat-review-reference.js` is a new reusable presentation component mounted via an optional chat renderer callback. `css/chat-review-reference.css` uses existing product tokens. Production callers do not provide this callback and index.html does not load the new component yet.

The fixture adapter is intentionally disposable. Editing, comments and decisions update a shared in-memory record, so the inline view and side pane stay synchronized. Reload resets the examples. No live API, Signoff, delta, native conversation or provider is touched. Decision buttons simulate state changes only. No production write adapter or new backend endpoint is claimed.

Review content and comments are visible directly in chat on first render, matching existing inline Signoffs; no expansion or side-pane navigation is required to edit. Review interactions: title optionally opens details; chevron collapses/expands inline; comment icon opens discussion; clicking proposal text enables Save/Discard; Save never approves. Approval/acceptance is disabled during an edit. A stale revision retains the local draft and asks the user to discard/reload. Proposed changes use the existing brain icon; Signoffs and discussion reuse existing app icons. Agent references are omitted from this preview (the existing transport does support typed agent references).

Before activating against live records: bind Signoff amendments to the existing amendment/new-proposal contract, retain exact review bindings, reuse the private conversation controller, and design the owner delta revision write contract (current Overseer UI is read-only). Do not overwrite frozen accepted/rejected material or convert this fixture into a live adapter. The owner requested a design preview before deciding on merge. Tab pane/Journal work is deferred.

Focused test: `PLAYWRIGHT_MODULE=/tmp/node_modules/playwright REFERENCE_PREVIEW_URL=<printed-url> node tests/test_chat_review_reference_browser.js`. It covers Save/Discard, synced comments, save/decision separation, stale drafts and narrow layout. Page screenshots are under `/home/alp/qorqbot-v1.3/execution/chat-reference-review-20261029/`.

Owner follow-up: decisions now use a filled primary button and outlined Reject button. Message/change bodies have a subtle inset background. Signoffs show only the comment composer, with a short submission acknowledgment; comment history, count/toggle and revision-history display are omitted in both chat and the preview detail pane. Stored sample comments/history are retained. Delta discussion/history remains as before.

Latest owner simplification: all review editable fields use only a left border; the save/discard reminder sentence and all comment UI are removed. Existing stored sample discussion is retained but not rendered.

Latest action layout: one right-aligned filled button pair changes from Approve/Reject (Accept change/Reject for deltas) to Save/Discard during editing. No separate edit toolbar or click-to-edit tooltip. Email and message Signoffs use the existing mail and record-chat glyphs; generic Signoffs keep the existing Signoff glyph.
