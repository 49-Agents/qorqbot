# Context usage in the Costs page

Consumer notes for `js/global-filing-costs.js` and `js/global-costs.js`. The backend contract is the host's
`context_filing_costs` / `costs_overview`; this UI changes no backend.

- **Request:** `/root/operations/context_filing_costs` with inclusive UTC `start_date`/`end_date` ("This month"; "All time" sends no
  dates), a fixed `limit` of 10 and the returned opaque `next_cursor` with unchanged filters. The selected dates are frozen when a
  selection begins: continuations, retries and a failed first page's retry reuse them even if the UTC month turns; only a new
  selection or remount recomputes them. Anything other than
  `format: qorqut-context-usage-v1` is an error, never reinterpreted. No input picker.
- **Rows:** one per stable `call_id`; a later page repeating a call replaces the earlier row (snapshots are never added). Primary
  columns: start, scope, event/input, stage and library attempt, state, observed provider/model, tokens, recorded cost. Details:
  IDs, configured model/effort/tier (distinct from the observed provider/model), native thread/turn, finish time, bounded error,
  bootstrap call, usage evidence and limitations. A started or failed call keeps its recorded evidence; no row is badged Final.
- **Money:** decimal text is formatted without a float round trip; `"0"` is a recorded zero, null/missing/invalid is Unknown.
  No price lookup or estimate. The subtotal and counts are the response `aggregates` (not a sum of loaded pages).
- **Coverage:** a separate "All Context events" inventory from `coverage` (not date filtered). Fully scanned means traversal
  reached its end, not zero usage or complete cost; `ready_events_without_calls` stays visible beside known charges.
- **Async:** period change resets rows, cursor and facts; a response for an earlier selection/request, after leaving Costs, or
  after a remount never paints. A failed continuation keeps rows and retries the same cursor. An unavailable endpoint shows an
  error and Retry without touching the other Costs sections. Reading never mutates or starts anything.
- **Summary / P&L:** the `filing` source's null amount is Unknown (with "no charge recorded this month" / "Unavailable"), the
  overall spend is labelled a known subtotal, a completeness note lists the backend's gaps, and while any source is incomplete the
  chart's net reads "Net before unreported costs" (spend is "Recorded spend").
- **Fixtures:** `tests/fixtures/context-usage/*.json` are the exact answers of the real host/Operations/Context code over a
  disposable ledger (`generate.py`, `provenance.json`); synthetic only.
