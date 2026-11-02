# Context source status and upload consumers

Dashboard side of the host's `docs/v1.2/CONTEXT_SOURCE_STATUS_HOST.md`. `js/context-source.js` owns the shared
filing status, the source reader, exact commands and the short `context_queue` state. The page shows
no ids, revisions, fact grids, attempt/filing history or input controls; those stay in the backend.

* **Routes.** Business `_source/{document-id}`; Global `#/global/context/library/{document-id}`. Both read the document with
  scoped `context_get` (bounded 6000-character pages; the server validates its opaque cursor against the document, scope and body, and the client requires the exact document identity and next character offset). A file `content_hash` change alone does not invalidate body paging: filing completion can update frontmatter without changing the body. Old `_dumps/`/`_inbox/` paths are ordinary authored paths; nothing is guessed from titles.
* **Filing.** One status chip from `filing.state` + `completion_state` + `operation` (Not added, Waiting, Processing, Couldn't process,
  Cancelling, Cancelled, Finishing, Needs attention, Added to Context, Removed, Unavailable). A blank title reads "Untitled source".
  No diagnostic reads (`context_upload_map`, `context_filings`, `context_queue_get`) are made.
* **Commands.** `context_retry` / `context_retract` `{upload_id}` + `expected_revision` + `idempotency_key`; the command binds the revision displayed when the owner made the decision. A read immediately before submission only verifies that same revision; if it differs, nothing is sent and the refreshed view requires a new decision. Uncertain outcomes repeat the stored request unchanged; a conflict rereads and needs a
  new decision (new key). Actions are "Add to Context", "Retry" and "Remove from Context" (with a short confirmation). Per-input
  retry/discard controls are not in the dashboard. No `force` field is sent anywhere.
* **Queue.** Footer and process tray read `context_queue` from `/biz/<slug>/context?view=processes` and
  `/root/context?view=processes`; it shows only "Updating Context" or "Context needs attention"; a missing queue is an error, and a vanished row is never reported as finished.
* **Fixtures.** Host evidence is an isolated synthetic
  composition (real HTTPAPI dispatcher, composed host, injected model runner and clock, deterministic entropy), not an installed
  product, running server or live data.
  * `tests/fixtures/context-source/captured.json` - a bounded VERBATIM subset of the host's committed
    `app/tests/fixtures/context_source_status/captures.json`, HTTP-dispatcher captures only (the ten direct agent
    `Application.invoke` captures and the one route-handler capture are not HTTP evidence and are not shipped).
  * `tests/fixtures/context-source/exchanges.json` - exchanges GENERATED through the same fixed host handlers by
    `generate_exchanges.py` for the exact requests this page sends (6000-character pages, 100-character preflights, limit 30/25,
    one-key control bodies), which the host capture does not contain (it uses other page sizes). Each exchange carries the
    named stage (host state) it was recorded in; responses are never edited. The generator imports the pinned sources from
    read-only `git archive` extracts and runs in a temporary directory; its usage is in the script header.
  * Disclosed normalization (from the host fixture): hashes, commits, ETags, five-character Context tokens and cursors are
    replaced by stable synthetic values of the same shape; wall-clock instants are remapped to an ordered synthetic timeline while
    the injected queue clock keeps its own domain (the two are not comparable). Normalized cursors are valid only against the
    fixture, with request/response cursor relationships intact.
  * The browser checks answer only from a recorded request inside the selected stage; an unrecorded request fails visibly. Cases
    the host cannot produce for the recorded owner session (a dropped connection, a malformed copy of a response, a denied or
    unavailable diagnostics answer, an input that already started) are injected in the test and labelled as such.
* **Test.** `PLAYWRIGHT_MODULE=… node tests/test_context_source_browser.js` (loopback, synthetic).
