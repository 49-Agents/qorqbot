let globalFilingCostsGeneration = 0;

const filingCostCall = async args => coreData(await post('/root/operations/context_filing_costs', args));

function filingCostNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function filingCostMoney(value) {
  if (typeof value !== 'string' || !/^-?\d+(?:\.\d+)?$/.test(value)) return 'Unknown';
  const negative = value.startsWith('-'), unsigned = negative ? value.slice(1) : value;
  const [whole, fraction = ''] = unsigned.split('.');
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return '$' + (negative ? '-' : '') + grouped + '.' + fraction.padEnd(8, '0');
}

function filingCostBasis(component) {
  const bases = Array.isArray(component?.bases) ? component.bases : (component?.basis ? [component.basis] : []);
  const parts = [];
  if (bases.includes('estimate')) parts.push(bases.length > 1 ? 'Includes estimates' : 'Estimated');
  else if (bases.includes('provider')) parts.push('Provider-reported');
  else if (bases.includes('mock')) parts.push('Mock');
  else if (bases.length > 1) parts.push('Mixed basis');
  if (Number(component?.unknown_calls || 0)) parts.push(`${component.unknown_calls} unknown`);
  return parts.length ? parts.join(' · ') : '—';
}

function filingCostComponent(component) {
  if (!component) return '<span class="filing-cost-unknown">Unknown</span>';
  const amount = filingCostMoney(component.known_usd);
  const unknown = Number(component.unknown_calls || 0) > 0;
  const basis = filingCostBasis(component);
  return `<span class="filing-cost-amount ${unknown ? 'filing-cost-partial' : ''}">${esc(amount)}</span>
    ${basis !== '—' ? `<small>${esc(basis)}</small>` : ''}`;
}

function filingCostBasisLabel(value, bases) {
  const values = Array.isArray(bases) ? bases : (value ? [value] : []);
  if (values.includes('estimate')) return values.length > 1 ? 'Includes estimates' : 'Estimated';
  if (values.length === 1 && values[0] === 'provider') return 'Provider-reported';
  if (values.length === 1 && values[0] === 'mock') return 'Mock';
  if (values.length === 1 && values[0] === 'unknown') return 'Unknown basis';
  if (values.length > 1) return 'Mixed basis';
  return value ? String(value) : '';
}

// Compact UTC timestamp for a filing row; unparsable values pass through.
function filingCostWhen(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return value || 'Date unavailable';
  return date.toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23', timeZone:'UTC'}) + ' UTC';
}

function filingCostRowBases(item) {
  return [...new Set([...(item.luna?.bases || []), ...(item.jev?.bases || [])])].sort();
}

function filingCostRange(period) {
  if (period === 'all') return {};
  const now = new Date(), year = now.getUTCFullYear(), month = now.getUTCMonth();
  const day = date => `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')}`;
  return {start_date:day(new Date(Date.UTC(year, month, 1))), end_date:day(new Date(Date.UTC(year, month + 1, 0)))};
}

// The Costs page's Context filing sub-table.
async function filingCostsMount(c) {
  const generation = ++globalFilingCostsGeneration;
  let cursor = null, loading = false;
  let rows = [];
  const current = () => generation === globalFilingCostsGeneration && c.isConnected
    && state.scope === 'global' && state.globalTab === 'costs';
  c.innerHTML = `<header class="costs-subhead filing-costs-heading"><div><h2>Context filing</h2>
      <p>API usage incurred by Luna and Jev while filing context.</p></div>
      <label>Period<select id="filing-cost-period" class="inp"><option value="month">This month</option><option value="all">All time</option></select></label>
    </header>
    <div class="filing-cost-total" id="filing-cost-total" aria-live="polite"><span class="muted">Loading costs…</span></div>
    <div id="filing-cost-status" role="status" class="muted"></div>
    <div id="filing-cost-table"></div>
    <div class="filing-cost-more"><button type="button" class="ghost" id="filing-cost-more" hidden>Load more</button></div>`;
  const period = $('#filing-cost-period', c), status = $('#filing-cost-status', c), body = $('#filing-cost-table', c);
  const total = $('#filing-cost-total', c), more = $('#filing-cost-more', c);

  const paint = data => {
    const unknown = Number(data.unknown_call_count || 0);
    total.innerHTML = `<strong>${esc(filingCostMoney(data.known_total_usd))}</strong><span>Known cost</span>
      <small>${Number(data.event_count || 0)} model call${Number(data.event_count || 0) === 1 ? '' : 's'} in this period${unknown ? `; ${unknown} with unknown cost` : ''}</small>`;
    status.textContent = unknown ? 'Known costs only; calls with missing provider charges are not counted as zero.'
      : Number(data.event_count || 0) === 0 && data.status === 'untracked'
        ? 'No cost events are recorded in this period; earlier costs are untracked.' : '';
    if (!rows.length) {
      body.innerHTML = '<p class="empty">No filing costs in this period.</p>';
      return;
    }
    body.innerHTML = `<div class="filing-cost-table" role="table" aria-label="Global filing costs">
      <div class="filing-cost-row filing-cost-head" role="row"><span>Filing / date</span><span>Source</span><span>Status</span><span>Luna</span><span>Jev</span><span>Total</span><span>Completeness</span></div>
      ${rows.map(item => {
        const partial = item.status === 'partial' || Number(item.unknown_call_count || 0) > 0;
        const source = [item.business_name || (item.business_id ? 'Business' : 'Global'), item.source || 'Context upload'].filter(Boolean).join(' · ');
        const amount = filingCostMoney(item.known_total_usd);
        const totalLabel = partial ? `Known ${amount}` : amount;
        return `<div class="filing-cost-row" role="row" data-filing-cost="${esc(item.upload_id)}">
          <span><strong title="${esc(item.date || '')}">${esc(filingCostWhen(item.date))}</strong><small>${esc(item.upload_id)}</small></span>
          <span>${esc(source)}</span>
          <span>${esc(item.filing_status || 'Unknown')}</span>
          <span class="filing-cost-component">${filingCostComponent(item.luna)}</span>
          <span class="filing-cost-component">${filingCostComponent(item.jev)}</span>
          <span class="filing-cost-total-cell">${esc(totalLabel)}${partial ? '<small>Unknown calls excluded</small>' : ''}</span>
          <span><span class="filing-cost-state ${partial ? 'is-partial' : 'is-complete'}">${partial ? 'Partial' : 'Complete'}</span>${item.basis ? `<small>${esc(filingCostBasisLabel(item.basis, filingCostRowBases(item)))}</small>` : ''}</span>
        </div>`;
      }).join('')}</div>`;
  };

  const load = async (reset = false) => {
    if (loading || !current()) return;
    loading = true;
    if (reset) { cursor = null; rows = []; more.hidden = true; body.innerHTML = '<p class="muted" role="status">Loading costs…</p>'; }
    more.disabled = true; status.textContent = '';
    try {
      const result = await filingCostCall({...filingCostRange(period.value), limit:30, ...(cursor ? {cursor} : {})});
      if (!current()) return;
      if (!Array.isArray(result.items) || typeof result.known_total_usd !== 'string') throw new Error('Filing costs response is incomplete');
      rows = reset ? result.items : rows.concat(result.items);
      cursor = result.next_cursor || null;
      paint(result);
      more.hidden = !cursor;
    } catch (error) {
      if (!current()) return;
      status.innerHTML = `<span role="alert">${esc(error.message)}</span> <button type="button" class="ghost" id="filing-cost-retry">Retry</button>`;
      $('#filing-cost-retry', c).onclick = () => load(true);
    } finally {
      loading = false;
      more.disabled = false;
    }
  };
  period.onchange = () => load(true);
  more.onclick = () => load(false);
  await load(true);
}
