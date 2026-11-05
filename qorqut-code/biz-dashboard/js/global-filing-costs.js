// Costs page: Context usage. One compact row per recorded model call (`qorqut-context-usage-v1`: date, observed model, cost)
// and a known-cost subtotal from the backend aggregates (never a sum of the loaded pages). Nothing here prices, estimates or
// infers: a missing charge stays "Unavailable" and is never shown as free. Identifiers, token counters, attempts, scan
// coverage and raw errors are validated or paged but not displayed.
let globalFilingCostsGeneration = 0;

const FILING_COST_FORMAT = 'qorqut-context-usage-v1';
const FILING_COST_PAGE_LIMIT = 10;
const FILING_COST_UNAVAILABLE = 'Unavailable';
const filingCostCall = async (args, signal) => coreData(await post('/root/operations/context_filing_costs', args, {signal}));

// Decimal text from the backend is shown as text: no binary floating-point round trip, a recorded zero stays a zero and a
// sub-cent charge keeps every digit. Anything that is not plain decimal text (null, missing, invalid) is Unavailable.
function filingCostMoney(value) {
  if (typeof value !== 'string' || !/^\d+(?:\.\d+)?$/.test(value)) return FILING_COST_UNAVAILABLE;
  const [whole, fraction = ''] = value.split('.');
  return '$' + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + fraction.padEnd(2, '0');
}

// Compact San Juan timestamp for a call; unparsable values pass through.
function filingCostWhen(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return value || FILING_COST_UNAVAILABLE;
  return sjDateTime(date, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}) + ' San Juan';
}

function filingCostRange(period) {
  if (period === 'all') return {};
  const now = new Date(), year = now.getUTCFullYear(), month = now.getUTCMonth();
  const day = date => `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')}`;
  return {start_date:day(new Date(Date.UTC(year, month, 1))), end_date:day(new Date(Date.UTC(year, month + 1, 0)))};
}

function filingCostIsPage(result) {
  return result?.format === FILING_COST_FORMAT && Array.isArray(result.calls) && result.aggregates && typeof result.aggregates === 'object'
    && result.coverage && typeof result.coverage === 'object' && result.calls.every(call => typeof call?.call_id === 'string' && call.usage && call.observed && call.configured);
}

// The model the call was observed to use; the provider only when no model was observed.
const filingCostModel = observed => observed.model || observed.provider || FILING_COST_UNAVAILABLE;
// A recorded amount for a call that did not finish or whose usage is not proven final is only part of what it may cost.
const filingCostPartial = call => call.usage.cost_usd !== null && (call.state !== 'completed' || (call.evidence?.limitations || []).length > 0);

function filingCostRow(call) {
  const unknown = call.usage.cost_usd === null;
  return `<div class="filing-cost-call" role="rowgroup" data-filing-call="${esc(call.call_id)}">
    <div class="filing-cost-row" role="row">
      <span role="cell" data-label="Date"><strong>${esc(filingCostWhen(call.started_at))}</strong></span>
      <span role="cell" data-label="Model" class="filing-cost-model">${esc(filingCostModel(call.observed))}</span>
      <span role="cell" data-label="Recorded cost" class="filing-cost-amount${unknown ? ' is-unknown' : ''}">${esc(filingCostMoney(call.usage.cost_usd))}${filingCostPartial(call) ? '<small>Partial</small>' : ''}</span>
    </div>
  </div>`;
}

// Missing or malformed required evidence is never proof of completeness: anything that is not a plain zero counts as a gap.
function filingCostIncomplete(aggregates, coverage) {
  const gap = (source, key) => !source || source[key] !== 0;
  return ['unknown_cost_calls', 'unfinished_calls', 'events_not_ready'].some(key => gap(aggregates, key))
    || ['partial_events', 'unavailable_events', 'unscanned_events', 'ready_events_without_calls'].some(key => gap(coverage, key));
}

function filingCostAggregates(aggregates, coverage, period) {
  const label = period === 'all' ? 'All time' : 'This month';
  return `<div class="filing-cost-subtotal"><span class="filing-cost-label">Recorded cost</span>
      <strong>${esc(filingCostMoney(aggregates.known_cost_usd))}</strong><small class="filing-cost-period">${label}</small></div>
    ${filingCostIncomplete(aggregates, coverage) ? '<p class="filing-cost-caveat">Some costs unavailable</p>' : ''}`;
}

// The Costs page's Context usage section.
async function filingCostsMount(c, life) {
  const generation = ++globalFilingCostsGeneration;
  let cursor = null, selection = 0, requestSequence = 0, loading = false, range = {};
  let calls = new Map(), aggregates = null, coverage = null;
  const current = () => generation === globalFilingCostsGeneration && c.isConnected && costsPageActive(life);
  c.innerHTML = `<header class="costs-subhead filing-costs-heading"><div><h2>Context usage</h2></div>
      <label>Period<select id="filing-cost-period" class="inp"><option value="month">This month</option><option value="all">All time</option></select></label>
    </header>
    <div class="filing-cost-total" id="filing-cost-total" aria-live="polite"><span class="muted">Loading…</span></div>
    <div id="filing-cost-status" role="status" class="muted"></div>
    <div id="filing-cost-table"></div>
    <div class="filing-cost-more"><button type="button" class="ghost" id="filing-cost-more" hidden>Load more</button></div>`;
  const period = $('#filing-cost-period', c), status = $('#filing-cost-status', c), body = $('#filing-cost-table', c);
  const total = $('#filing-cost-total', c), more = $('#filing-cost-more', c);

  const paintFacts = () => {
    total.innerHTML = aggregates ? filingCostAggregates(aggregates, coverage, period.value) : '<span class="muted">Loading…</span>';
  };
  const paintRows = () => {
    if (!aggregates) { body.innerHTML = ''; return; }
    if (!calls.size) {
      body.innerHTML = '<p class="empty">No recorded costs for this period</p>';
      return;
    }
    body.innerHTML = `<div class="filing-cost-table" role="table" aria-label="Context usage">
      <div class="filing-cost-row filing-cost-head" role="row"><span role="columnheader">Date</span><span role="columnheader">Model</span><span role="columnheader">Cost</span></div>
      ${[...calls.values()].map(filingCostRow).join('')}</div>`;
  };

  // The selected dates are frozen when a selection begins: every continuation and retry of it sends the same dates (the backend
  // binds a cursor to its exact filters), even if the UTC month turns meanwhile. Only a deliberate new selection recomputes them.
  const load = async (reset = false, fresh = reset) => {
    if (!current() || (loading && !reset)) return;
    if (fresh) range = filingCostRange(period.value);
    const mine = ++requestSequence, mySelection = reset ? ++selection : selection;
    loading = true; status.textContent = '';
    if (reset) {
      cursor = null; calls = new Map(); aggregates = null; coverage = null;
      more.hidden = true; paintFacts(); body.innerHTML = '<p class="muted" role="status">Loading…</p>';
    }
    more.disabled = true;
    try {
      const result = await filingCostCall({...range, limit:FILING_COST_PAGE_LIMIT, ...(cursor ? {cursor} : {})}, life?.signal);
      // A response for an earlier period, an earlier request or a Costs view that is gone never paints.
      if (!current() || mySelection !== selection || mine !== requestSequence) return;
      if (!filingCostIsPage(result)) throw new Error('The Context usage response is not in the expected format');
      // A later page can repeat a call with a newer cumulative snapshot: it replaces the earlier row, never adds to it.
      for (const call of result.calls) calls.set(call.call_id, call);
      cursor = result.next_cursor || null; aggregates = result.aggregates; coverage = result.coverage;
      paintFacts(); paintRows();
      more.hidden = !cursor;
    } catch (error) {
      if (!current() || mySelection !== selection || mine !== requestSequence) return;
      status.innerHTML = `<span role="alert">Couldn't load. Retry</span> <button type="button" class="ghost" id="filing-cost-retry">Retry</button>`;
      $('#filing-cost-retry', c).onclick = () => load(!aggregates, false);
      if (!aggregates) { total.innerHTML = '<span class="muted">Unavailable</span>'; body.innerHTML = ''; }
      more.hidden = !cursor;
    } finally {
      if (mine === requestSequence) { loading = false; if (current()) more.disabled = false; }
    }
  };
  period.onchange = () => load(true);
  more.onclick = () => load(false);
  await load(true);
}
