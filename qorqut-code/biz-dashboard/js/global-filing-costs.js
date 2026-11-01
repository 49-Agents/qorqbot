// Costs page: Context usage. One row per recorded model call (`qorqut-context-usage-v1`), a known-cost subtotal from the
// backend aggregates (never a sum of the loaded pages) and a separate coverage inventory of every reserved Context event.
// Nothing here prices, estimates or infers: a missing charge, token counter or provider stays "Unknown".
let globalFilingCostsGeneration = 0;

const FILING_COST_FORMAT = 'qorqut-context-usage-v1';
const FILING_COST_PAGE_LIMIT = 10;
const FILING_COST_STATES = {started:'Started, not finished', completed:'Completed', failed:'Failed'};
const FILING_COST_STAGES = {bootstrap:'Bootstrap', propagation:'Propagation'};
const FILING_COST_LIMITATIONS = {unfinished_call:'The call has not finished', usage_unreported:'No token usage was reported',
  partial_counters:'Some token counters were not reported', final_totals_unproven:'Final totals are not proven',
  cost_unreported:'No charge was reported', ignored_updates:'Some usage updates were ignored'};
const FILING_COST_SCANS = [['reserved_events', 'Reserved'], ['ready_events', 'Fully scanned'], ['partial_events', 'Partly scanned'],
  ['unavailable_events', 'Unavailable'], ['unscanned_events', 'Not scanned yet'], ['ready_events_without_calls', 'Fully scanned, no call evidence'],
  ['unattempted_inputs', 'Inputs not yet attempted']];

const filingCostCall = async args => coreData(await post('/root/operations/context_filing_costs', args));

// Decimal text from the backend is shown as text: no binary floating-point round trip, a recorded zero stays a zero and a
// sub-cent charge keeps every digit. Anything that is not plain decimal text (null, missing, invalid) is Unknown.
function filingCostMoney(value) {
  if (typeof value !== 'string' || !/^\d+(?:\.\d+)?$/.test(value)) return 'Unknown';
  const [whole, fraction = ''] = value.split('.');
  return '$' + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + fraction.padEnd(2, '0');
}

function filingCostCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('en-US') : 'Unknown';
}

// Compact San Juan timestamp for a call; unparsable values pass through.
function filingCostWhen(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return value || 'Unknown';
  return sjDateTime(date, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}) + ' San Juan';
}

function filingCostRange(period) {
  if (period === 'all') return {};
  const now = new Date(), year = now.getUTCFullYear(), month = now.getUTCMonth();
  const day = date => `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')}`;
  return {start_date:day(new Date(Date.UTC(year, month, 1))), end_date:day(new Date(Date.UTC(year, month + 1, 0)))};
}

function filingCostScope(call) {
  if (call.business_id === null) return 'Global';
  return (state.bizs || []).find(biz => biz.id === call.business_id)?.name || call.business_id;
}

function filingCostIsPage(result) {
  return result?.format === FILING_COST_FORMAT && Array.isArray(result.calls) && result.aggregates && typeof result.aggregates === 'object'
    && result.coverage && typeof result.coverage === 'object' && result.calls.every(call => typeof call?.call_id === 'string' && call.usage && call.observed && call.configured);
}

function filingCostText(value) {
  return value === null || value === undefined || value === '' ? '<span class="filing-cost-unknown">Unknown</span>' : esc(value);
}

function filingCostTokens(usage) {
  const part = (label, value) => `<span>${label} ${Number.isSafeInteger(value) ? esc(value.toLocaleString('en-US')) : '<span class="filing-cost-unknown">unknown</span>'}</span>`;
  return [part('In', usage.input_tokens), part('Out', usage.output_tokens), part('Total', usage.total_tokens)].join('');
}

function filingCostObserved(observed) {
  if (!observed.provider && !observed.model) return '<span class="filing-cost-unknown">Unknown</span><small>No observed provider or model</small>';
  return `<span>${filingCostText(observed.provider)}</span><small>${filingCostText(observed.model)}</small>`;
}

function filingCostDetail(call) {
  const evidence = call.evidence || {}, fact = (label, value) => `<div><dt>${esc(label)}</dt><dd>${filingCostText(value)}</dd></div>`;
  const counters = [['Input tokens', call.usage.input_tokens], ['Cached input tokens', call.usage.cached_input_tokens], ['Output tokens', call.usage.output_tokens],
    ['Reasoning output tokens', call.usage.reasoning_output_tokens], ['Total tokens', call.usage.total_tokens]]
    .map(([label, value]) => fact(label, Number.isSafeInteger(value) ? value.toLocaleString('en-US') : null)).join('');
  const limits = (evidence.limitations || []).map(name => FILING_COST_LIMITATIONS[name] || name);
  return `<dl class="filing-cost-facts">
    ${fact('Call', call.call_id)}${fact('Input', call.input_id)}${fact('Context event', call.context_event_id)}${fact('Library attempt', `${call.library_attempt_sequence} (${call.attempt_id})`)}
    ${fact('Started', call.started_at)}${fact('Finished', call.finished_at)}
    ${fact('Configured model', call.configured.model)}${fact('Configured effort', call.configured.effort)}${fact('Configured service tier', call.configured.service_tier)}
    ${fact('Observed provider', call.observed.provider)}${fact('Observed model', call.observed.model)}
    ${fact('Native thread', call.observed.thread_id)}${fact('Native turn', call.observed.turn_id)}
    ${counters}${fact('Recorded charge (USD)', call.usage.cost_usd === null ? null : filingCostMoney(call.usage.cost_usd))}
    ${fact('Usage source', evidence.usage_source ? `${evidence.usage_source} (${evidence.usage_scope}, ${evidence.usage_updates} updates, ${evidence.ignored_updates} ignored)` : null)}
    ${fact('Bootstrap call', call.bootstrap_call_id)}
    ${call.error ? fact('Error', `${call.error.type}: ${call.error.message}`) : ''}
    ${fact('Scan of this event', call.sync ? `${call.sync.status}${call.sync.error ? ` (${call.sync.error.message})` : ''}` : null)}
  </dl>
  <p class="filing-cost-limits">${limits.length ? esc(limits.join(' · ')) : ''}</p>`;
}

function filingCostRow(call, open) {
  const unknown = call.usage.cost_usd === null;
  const stateLabel = FILING_COST_STATES[call.state] || call.state, id = `filing-cost-detail-${call.call_id.replace(/[^a-z0-9]/gi, '')}`;
  return `<div class="filing-cost-call" role="rowgroup" data-filing-call="${esc(call.call_id)}">
    <div class="filing-cost-row" role="row">
      <span role="cell" data-label="Started"><strong title="${esc(call.started_at)}">${esc(filingCostWhen(call.started_at))}</strong></span>
      <span role="cell" data-label="Scope" class="filing-cost-scope" title="${esc(filingCostScope(call))}">${esc(filingCostScope(call))}</span>
      <span role="cell" data-label="Context" class="filing-cost-ids"><span title="${esc(call.context_event_id)}">Event ${esc(call.context_event_id)}</span><small title="${esc(call.input_id)}">Input ${esc(call.input_id)}</small></span>
      <span role="cell" data-label="Stage">${esc(FILING_COST_STAGES[call.stage] || call.stage)}<small>Attempt ${esc(call.library_attempt_sequence)}</small></span>
      <span role="cell" data-label="State"><span class="filing-cost-state is-${esc(call.state)}">${esc(stateLabel)}</span></span>
      <span role="cell" data-label="Observed">${filingCostObserved(call.observed)}</span>
      <span role="cell" data-label="Tokens" class="filing-cost-tokens">${filingCostTokens(call.usage)}</span>
      <span role="cell" data-label="Recorded cost" class="filing-cost-amount${unknown ? ' is-unknown' : ''}">${esc(filingCostMoney(call.usage.cost_usd))}${unknown ? '<small>No charge recorded</small>' : ''}</span>
      <span role="cell" class="filing-cost-toggle-cell"><button type="button" class="ghost filing-cost-toggle" aria-expanded="${open}" aria-controls="${id}" aria-label="Details for call ${esc(call.call_id)}">Details</button></span>
    </div>
    <div class="filing-cost-detail" role="row" id="${id}" ${open ? '' : 'hidden'}><div role="cell">${open ? filingCostDetail(call) : ''}</div></div>
  </div>`;
}

function filingCostAggregates(aggregates, period) {
  const label = period === 'all' ? 'in all time' : 'this month';
  const count = key => filingCostCount(aggregates[key]);
  return `<div class="filing-cost-subtotal"><span class="filing-cost-label">Known cost subtotal</span>
      <strong>${esc(filingCostMoney(aggregates.known_cost_usd))}</strong></div>
    <ul class="filing-cost-counts">
      <li>${count('calls')} recorded call${aggregates.calls === 1 ? '' : 's'} started ${label}</li>
      <li>${count('unknown_cost_calls')} with no recorded charge</li>
      <li>${count('unfinished_calls')} not finished</li>
      <li>${count('calls_without_token_usage')} without token usage</li>
    </ul>
    <p class="filing-cost-caveat">Only charges recorded for these calls are added. A missing charge is unknown, not free, and recorded usage is not proven final.</p>`;
}

function filingCostCoverage(coverage) {
  const scoped = coverage.input_id ? 'Selected input' : 'All Context events';
  const when = coverage.last_checked_at ? `Latest scan ${filingCostWhen(coverage.last_checked_at)}` : 'No scan has been recorded yet';
  return `<h3>${scoped}</h3>
    <dl class="filing-cost-inventory">${FILING_COST_SCANS.map(([key, label]) => `<div class="${key === 'ready_events_without_calls' && coverage[key] > 0 ? 'is-attention' : ''}">
      <dt>${esc(label)}</dt><dd>${filingCostCount(coverage[key])}</dd></div>`).join('')}</dl>
    <p>${esc(when)}. Coverage counts every reserved Context event, not only this period. Fully scanned means its receipts were read to the end; it does not prove that no usage happened or that the cost is complete.</p>`;
}

// The Costs page's Context usage section.
async function filingCostsMount(c) {
  const generation = ++globalFilingCostsGeneration;
  let cursor = null, selection = 0, requestSequence = 0, loading = false, range = {};
  let calls = new Map(), aggregates = null, coverage = null;
  const expanded = new Set();
  const current = () => generation === globalFilingCostsGeneration && c.isConnected
    && state.scope === 'global' && state.globalTab === 'costs';
  c.innerHTML = `<header class="costs-subhead filing-costs-heading"><div><h2>Context usage</h2>
      <p>Model calls recorded while Context processed its inputs.</p></div>
      <label>Period<select id="filing-cost-period" class="inp"><option value="month">This month</option><option value="all">All time</option></select></label>
    </header>
    <div class="filing-cost-total" id="filing-cost-total" aria-live="polite"><span class="muted">Loading Context usage…</span></div>
    <section class="filing-cost-coverage" id="filing-cost-coverage" aria-label="Context event coverage" hidden></section>
    <div id="filing-cost-status" role="status" class="muted"></div>
    <div id="filing-cost-table"></div>
    <div class="filing-cost-more"><button type="button" class="ghost" id="filing-cost-more" hidden>Load more</button></div>`;
  const period = $('#filing-cost-period', c), status = $('#filing-cost-status', c), body = $('#filing-cost-table', c);
  const total = $('#filing-cost-total', c), coverageHost = $('#filing-cost-coverage', c), more = $('#filing-cost-more', c);

  const paintFacts = () => {
    total.innerHTML = aggregates ? filingCostAggregates(aggregates, period.value) : '<span class="muted">Loading Context usage…</span>';
    coverageHost.hidden = !coverage;
    if (coverage) coverageHost.innerHTML = filingCostCoverage(coverage);
  };
  const paintRows = () => {
    if (!aggregates) { body.innerHTML = ''; return; }
    if (!calls.size) {
      body.innerHTML = '<p class="empty">No Context model calls are recorded in this period. That does not mean the period was free: see the coverage above.</p>';
      return;
    }
    body.innerHTML = `<div class="filing-cost-table" role="table" aria-label="Context model calls">
      <div class="filing-cost-row filing-cost-head" role="row"><span role="columnheader">Started</span><span role="columnheader">Scope</span><span role="columnheader">Context</span><span role="columnheader">Stage</span><span role="columnheader">State</span><span role="columnheader">Observed</span><span role="columnheader">Tokens</span><span role="columnheader">Recorded cost</span><span role="columnheader"><span class="filing-cost-sr">Details</span></span></div>
      ${[...calls.values()].map(call => filingCostRow(call, expanded.has(call.call_id))).join('')}</div>`;
    body.querySelectorAll('.filing-cost-toggle').forEach(button => {
      const id = button.closest('[data-filing-call]').dataset.filingCall;
      button.onclick = () => {
        const detail = $('#' + button.getAttribute('aria-controls'), body), open = detail.hidden;
        open ? expanded.add(id) : expanded.delete(id);
        detail.hidden = !open; button.setAttribute('aria-expanded', String(open));
        detail.firstElementChild.innerHTML = open ? filingCostDetail(calls.get(id)) : '';
      };
    });
  };

  // The selected dates are frozen when a selection begins: every continuation and retry of it sends the same dates (the backend
  // binds a cursor to its exact filters), even if the UTC month turns meanwhile. Only a deliberate new selection recomputes them.
  const load = async (reset = false, fresh = reset) => {
    if (!current() || (loading && !reset)) return;
    if (fresh) range = filingCostRange(period.value);
    const mine = ++requestSequence, mySelection = reset ? ++selection : selection;
    loading = true; status.textContent = '';
    if (reset) {
      cursor = null; calls = new Map(); aggregates = null; coverage = null; expanded.clear();
      more.hidden = true; paintFacts(); body.innerHTML = '<p class="muted" role="status">Loading Context usage…</p>';
    }
    more.disabled = true;
    try {
      const result = await filingCostCall({...range, limit:FILING_COST_PAGE_LIMIT, ...(cursor ? {cursor} : {})});
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
      status.innerHTML = `<span role="alert">${esc(error.message)}</span> <button type="button" class="ghost" id="filing-cost-retry">Retry</button>`;
      $('#filing-cost-retry', c).onclick = () => load(!aggregates, false);
      if (!aggregates) { total.innerHTML = '<span class="muted">Context usage is unavailable.</span>'; body.innerHTML = ''; }
      more.hidden = !cursor;
    } finally {
      if (mine === requestSequence) { loading = false; if (current()) more.disabled = false; }
    }
  };
  period.onchange = () => load(true);
  more.onclick = () => load(false);
  await load(true);
}
