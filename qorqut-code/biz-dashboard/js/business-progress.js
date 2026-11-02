// Read-only evidence-based progress of a project's Tasks and Actions; no background polling.
const PROGRESS_METRICS = [['tasks_done','Tasks done'],['actions','Actions'],['signoffs_approved','Signoffs approved']];
function progressChart(series, key) {
  const maximum = Math.max(1,...series.map(d => d[key]));
  const width = 520, height = 116, step = width / Math.max(1,series.length);
  return `<svg class="business-progress-chart" viewBox="0 0 560 150" role="img" aria-label="Daily counts, maximum ${maximum}"><text x="0" y="12">${maximum}</text><text x="0" y="127">0</text><path d="M30 8V128H558" fill="none" stroke="var(--line)"/>${series.map((d,i) => `<rect x="${30+i*step}" y="${128-d[key]/maximum*height}" width="${Math.max(1,step-2)}" height="${d[key]/maximum*height}" rx="1"><title>${esc(d.date)}: ${d[key]}</title></rect>`).join('')}</svg>`;
}
async function renderBusinessProgress(container, days=30) {
  const biz = state.biz, token = {};
  container._businessPage = token;
  const active = () => container.isConnected && container._businessPage === token && state.biz === biz && state.tab === 'progress' && state.scope !== 'global';
  container.innerHTML = `<section class="business-progress-page"><header><h1>Progress</h1><div class="business-progress-period" role="group" aria-label="Chart period">${[30,90,365].map(n=>`<button data-days="${n}" aria-pressed="${n===days}">${n} days</button>`).join('')}</div></header><div class="business-progress-results"><p role="status">Loading progress…</p></div></section>`;
  container.querySelectorAll('[data-days]').forEach(b => b.onclick = () => renderBusinessProgress(container,Number(b.dataset.days)));
  try {
    const data = coreData(await post(`/biz/${encodeURIComponent(biz)}/operations/business_progress_get`, {days}));
    if (!active()) return;
    sjConfigure(data);
    container.querySelector('.business-progress-results').innerHTML = `<div class="business-progress-grid">${PROGRESS_METRICS.map(([key,label]) => `<article class="business-progress-metric"><h2>${label}</h2><strong>${Number(data.totals[key]).toLocaleString()}</strong><span class="muted"> recorded total</span>${progressChart(data.series,key)}<div class="business-progress-axis"><span>${esc(data.series[0].date)}</span><span>${esc(data.series.at(-1).date)}</span></div>${data.undated[key] ? `<p class="muted">${data.undated[key]} without a recorded date, included in the total.</p>` : ''}</article>`).join('')}</div><p class="business-progress-coverage">Daily counts · ${esc(SJ_LABEL)}. ${esc(data.coverage)}</p>`;
  } catch (error) {
    if (!active()) return;
    container.querySelector('[role=status]').textContent = error.message || 'Progress unavailable.';
    const retry = document.createElement('button'); retry.textContent = 'Retry'; retry.onclick = () => renderBusinessProgress(container, days); container.querySelector('.business-progress-results').appendChild(retry);
  }
}
