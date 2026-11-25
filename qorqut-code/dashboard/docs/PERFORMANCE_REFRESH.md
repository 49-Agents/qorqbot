# Dashboard refresh coalescing

`js/reply-notifications.js` and `js/attention-queue.js` share one rule: one request in flight per resource, with at most one trailing request.

## Reply notifications

- Polling intervals are unchanged (10 s visible, 30 s hidden).
- `reply-notifications-changed` is dispatched on the first successful load and when the normalized result changes. The comparison is key-order independent and covers every field, not only IDs. `paint` stays available for newly mounted navigation.
- Toast deduplication, the first-load baseline and retention of the last known state after a failed poll are unchanged.
- `refresh()` during a read records one trailing read and resolves after it, so the caller sees the later state. Timer and `start()` calls never record a trailing read.

## Attention

- Mount, focus, reply-change and retry triggers that arrive during a read record one trailing read and resolve after it. The 10 s timer never does.
- A trigger during an acknowledgement save still defers to one refresh after the saves finish. That caller's promise is the write chain: it resolves when the saves settle, not after the deferred read has loaded data. Generation-based stale-response protection is unchanged.
- The poll timer is cleared when the last mount goes away, including when a read finishes after that.
- Pending Signoff reads follow the same rule over their whole paginated cycle (60 s timer and pagination unchanged; the timer and any follow-up cycle are dropped when the last mount goes away).

Because unchanged replies no longer dispatch, repeated identical reply polls do not cause Attention requests.

## Focused checks

- `tests/test_dashboard_refresh_coalescing_browser.js` uses a synthetic fetch and a paused fake clock. It covers a settled two-page Signoff cycle whose 60 s timer comes due while an explicitly started cycle is held on page one: no extra request, one two-page cycle, maximum concurrency one and no trailing cycle. Set `PLAYWRIGHT_MODULE` to a Playwright install.
- `tests/test_attention_queue_browser.js` covers held-save serialization and stale-read suppression.
- `tests/test_reply_notifications_browser.js` covers toast deduplication.
