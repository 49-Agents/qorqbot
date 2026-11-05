# Published Context graph readers

Consumer notes for `js/context-published.js`, `js/global-context-graph.js`, `js/global-context-base.js` and the business
Context routes in `js/context.js`. The backend contract is the host's reader/operations
(`context_home`, `context_find`, `context_object`, `context_expand`, `context_changes`; see the host's
`docs/v1.2/CONTEXT_COMPOSITION.md`). This UI changes no backend and only reads.

- **One transport.** Global `/root/operations/<op>`, business `/biz/<slug>/operations/<op>`; both read the same published graph.
  `state.biz` is never replaced by Global and no protected source is proxied. Every response is checked against the closed schema
  (`pubCheck`); an unknown or retired field (`brief`, timeline/attribute-history cursors, `item.kind=person`, XML refs) is an error.
- **Fixed bounds.** Home 30, search 50, expansion depth 1 / `both` / 50, history 10, source text 16000, changes 20. Only returned
  opaque cursors are followed; there is no `limit:500` and no whole-graph loop. A search that reports `window_exhausted` asks to
  narrow the query.
- **Refs.** `node:xxxxx`, `edge:xxxxx`, `source:xxxxx` only. Global URLs encode the exact ref; business routes use `_obj/<ref>`. A
  malformed or old `<object>…</object>` ref is rejected visibly and makes no read. `origin_ref` is shown as inert text and never
  fetched; evidence links go to the graph reader only.
- **Integrity (`pubCollection`).** A collection binds the revision of its first page. A page with another revision, a repeated
  or non-advancing cursor, repeated records, a size or continuation inconsistency, or a different request echo keeps every
  earlier page and shows a recoverable error ("Restart from the first page"). A transient failure keeps its pages and "Retry this
  page" resends the exact cursor. History, expansion, text and owner changes are separate collections with independent cursors.
  If the object and its expansion answer at different revisions the view says the Context was published again and offers a
  deliberate reload. Text continuations send only `text_cursor`, must start exactly where the loaded text ends (code points) and
  keep the same revision and total.
- **Late responses.** Every mount takes a generation; a response only paints while its element is attached, its generation and
  the caller's route/scope are current and the read was not aborted. Navigating, changing scope or remounting drops older results.
- **Graph.** `contextPublishedGraphModel` projects Home nodes and their relationships straight into the existing relationship
  explorer (`global-context-graph-renderer.js`): node identity is its ref, edge identity its edge ref, parallel and self edges are
  kept, and an endpoint that is not among the loaded nodes is a dashed "Endpoint (not loaded)" placeholder labelled only with the
  endpoint DTO's own summary/ref. The status line says "Bounded view: N of M nodes and … relationships loaded" and never claims
  the complete graph; `relationships_truncated` / `truncated` markers stay visible. "Load more nodes" appends the next
  same-revision page. The renderer's search box searches the loaded records only (the business sidebar search uses `context_find`).
- **States.** `ready` with `empty` (explicit empty), `not_initialized` and `unavailable` (with Retry) are different views; no fake
  nodes. Owner change diagnostics have their own forbidden and unavailable states and never block the record.
- **Fixtures.** `tests/fixtures/context-published/*.json` are exact answers of the real reader/operation handlers over disposable
  synthetic repositories, validated against the closed input and output schemas; `provenance.json` records the pins and
  `generate.py` regenerates them. Isolated-composition limitation: handlers are called directly with a synthetic caller (no installed
  package, HTTP app or dashboard session) and the search window is lowered to 60 only for `window.json`. The sidebar
  `context_navigation_get` answer in the browser test is a stand-in of the documented shape, not a recorded answer.
- **Checks.** `node tests/test_context_published_browser.js` (set `EVIDENCE_DIR` for screenshots) and `node tests/test_ctxgraph.js` (stored-relationship graph geometry, layout and scope) and `node tests/test_people_published_contract.js`.

Read ownership: `ctxReadFindAll` (`js/context.js`) loads the full People list for its two consumers: the business People list
(`js/context.js`) and the Global People list (`js/global-context-base.js`, an empty-query load that is then filtered locally). Context
search is separate: `pubMountSearch` (`js/context-published.js`) reads through `pubRead`/`pubCollection` itself. `ctxReadFindAll` reads
through the same pair with `PUB_LIMITS.find` (50) per page, follows only returned opaque cursors and rejects a changed revision,
repeated cursor or record, or a missing business scope; an aborted signal cancels the read.
`tests/test_people_published_contract.js` covers root and business paths, query/cursor continuity, the page limit and cancellation.

Not changed (out of scope, still on their own contracts): upload/raw-document filing and `baseObjectForLegacyPath`, Learning, the
library and the sidebar navigation counts.
