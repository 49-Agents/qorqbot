// Global Costs: spend by source, then one small table per source.
let globalCostsGeneration = 0;
const COST_SECTIONS = {filing:'costs-filing', background_ai:'costs-background'};
const COST_BASIS = {estimate:'Estimated', provider:'Provider-reported', recorded:'Recorded', mixed:'Mixed basis', none:'No data',
  observed:'Observed charges (known subtotal)', unavailable:'Unavailable'};
const COST_PROGRAMS = {'learnings.extract':'Learning extraction', 'context.file':'Context filing (legacy)', 'context.summarize':'Context summaries',
  'context.map':'Context maps', 'agent.compact':'Agent compaction'};

function costsMoney(value) {
  // A missing amount is unknown: Number(null) would otherwise read as a free $0.
  if (value === null || value === undefined || value === '') return 'Unavailable';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  const abs = Math.abs(number);
  return (number < 0 ? '−$' : '$') + abs.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:abs > 0 && abs < 1 ? 4 : 2});
}
const costsDay = (date, options = {year:'numeric'}) => new Date(date + 'T00:00:00Z').toLocaleDateString(undefined, {month:'short', day:'numeric', timeZone:'UTC', ...options});

// `life` is the shared drawer's mount lifecycle ({signal, active}); the embedded page passes none and follows the global route.
const costsPageActive = life => life ? life.active() : state.scope === 'global' && state.globalTab === 'costs';
async function renderGlobalCosts(c, life) {
  const generation = ++globalCostsGeneration;
  const current = () => generation === globalCostsGeneration && c.isConnected && costsPageActive(life);
  c.innerHTML = `<section class="pane global-content global-costs" aria-label="Costs">
    <div class="global-eyebrow">Global</div>
    <header class="global-heading"><h1>Costs</h1></header>
    <div id="costs-summary"><p class="muted" role="status">Loading costs…</p></div>
    <div class="costs-subtables">
      <section class="costs-sub" id="costs-filing"></section>
      <section class="costs-sub" id="costs-background"></section>
    </div></section>`;
  const load = async () => {
    try {
      const data = coreData(await post('/root/operations/costs_overview', {}, {signal:life?.signal}));
      if (!current()) return;
      if (!Array.isArray(data?.sources) || !data.total) throw new Error('Costs are unavailable');
      costsPaintSummary($('#costs-summary', c), data);
      costsPaintBackground($('#costs-background', c), data.background_ai || []);
    } catch (error) {
      if (!current()) return;
      $('#costs-summary', c).innerHTML = `<p role="alert">Couldn't load. Retry</p><button type="button" id="costs-retry">Retry</button>`;
      $('#costs-retry', c).onclick = load;
    }
  };
  filingCostsMount($('#costs-filing', c), life);
  await load();
}

function costsTable(label, className, head, rows) {
  return `<div class="costs-table ${className}" role="table" aria-label="${esc(label)}">
    <div class="costs-row costs-head" role="row">${head.map(h => `<span role="columnheader">${esc(h)}</span>`).join('')}</div>
    ${rows.join('')}</div>`;
}
const costsCells = (cells, className = '') => `<div class="costs-row ${className}" role="row">${cells.map(([html, cls = '']) => `<span role="cell" class="${cls}">${html}</span>`).join('')}</div>`;

// The Context source's period cells: a recorded amount, or Unavailable (never a free $0).
function costsFilingCell(source, period) {
  const amount = period === 'month' ? source.month_usd : source.total_usd;
  if (amount !== null && amount !== undefined) return [filingCostMoney(amount), 'costs-num'];
  return ['Unavailable', 'costs-num'];
}

function costsPaintSummary(host, data) {
  const basis = source => COST_BASIS[source.basis] || source.basis || '';
  const subtotal = data.total.known_subtotal === true, incomplete = data.total.incomplete_sources || [];
  host.innerHTML = costsTable('Costs by source', 'costs-summary', ['Source', 'This month', 'All time', 'Basis'], [
    ...data.sources.map(source => costsCells([
      [COST_SECTIONS[source.key] ? `<button type="button" class="costs-jump" data-costs-jump="${esc(COST_SECTIONS[source.key])}">${esc(source.label)}</button>` : esc(source.label)],
      source.key === 'filing' ? costsFilingCell(source, 'month') : [costsMoney(source.month_usd), 'costs-num'],
      source.key === 'filing' ? costsFilingCell(source, 'total') : [costsMoney(source.total_usd), 'costs-num'], [esc(basis(source)), 'costs-basis']])),
    costsCells([[subtotal ? 'Total spend (known subtotal)' : 'Total spend'], [subtotal ? filingCostMoney(data.total.month_usd) : costsMoney(data.total.month_usd), 'costs-num'],
      [subtotal ? filingCostMoney(data.total.total_usd) : costsMoney(data.total.total_usd), 'costs-num'], ['']], 'costs-total')]) +
    (incomplete.length || data.context_usage_error ? `<p class="costs-note costs-incomplete" role="${data.context_usage_error ? 'alert' : 'note'}">Some costs unavailable</p>` : '');
  host.querySelectorAll('[data-costs-jump]').forEach(button => button.onclick = () =>
    document.getElementById(button.dataset.costsJump)?.scrollIntoView({behavior:'smooth', block:'start'}));
}

function costsPaintBackground(host, rows) {
  host.innerHTML = `<header class="costs-subhead"><div><h2>Background AI</h2><p>Estimated model usage by background programs.</p></div></header>
    ${rows.length ? costsTable('Background AI costs', 'costs-programs', ['Program', 'Calls', 'This month', 'All time'], rows.map(row => costsCells([
      [esc(COST_PROGRAMS[row.program] || row.program)], [Number(row.calls || 0).toLocaleString(), 'costs-num'],
      [costsMoney(row.month_usd), 'costs-num'], [costsMoney(row.total_usd), 'costs-num']]))) : '<p class="empty">No background AI costs recorded.</p>'}`;
}
