// Read-only business milestones and evidence-based progress; no background polling.
const PROGRESS_METRICS = [['emails_sent','Emails sent'],['tasks_done','Tasks done'],['actions','Actions'],['signoffs_approved','Signoffs approved']];
function milestoneTime(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : 'Date not recorded';
}
async function renderMilestones(container) {
  const biz = state.biz, token = {};
  container._businessPage = token;
  const active = () => container.isConnected && container._businessPage === token && state.biz === biz && state.tab === 'milestones' && state.scope !== 'global';
  container.innerHTML = '<section class="business-progress-page"><header><h1>Milestones</h1></header><p role="status">Loading milestones…</p></section>';
  try {
    const data = coreData(await post(`/biz/${encodeURIComponent(biz)}/operations/milestones_list`, {}));
    if (!active()) return;
    const items = data.items || [], done = items.filter(m => m.status === 'done').length;
    container.innerHTML = `<section class="business-progress-page"><header><h1>Milestones</h1><span class="muted">${done} / ${items.length} done</span></header>
      ${items.length ? `<ol class="milestone-list">${items.map((m,i) => `<li class="milestone-card ${m.status === 'done' ? 'is-done' : ''}"><span class="milestone-marker" aria-label="${m.status === 'done' ? 'Done' : 'Open'}">${m.status === 'done' ? '✓' : i+1}</span><div class="milestone-body"><h2>${esc(m.title)}</h2>${m.description ? `<p>${esc(m.description)}</p>` : ''}
      ${m.status === 'done' ? `<div class="milestone-completion"><span>Done · ${esc(milestoneTime(m.completed_at))}</span>${m.completion_comment ? `<p>${esc(m.completion_comment)}</p>` : ''}</div>` : ''}
      ${m.history?.length > 1 ? `<details class="milestone-history"><summary>History</summary>${m.history.map(e => `<p>${e.status === 'done' ? 'Done' : 'Reopened'} · ${esc(milestoneTime(e.created_at))}${e.comment ? ` — ${esc(e.comment)}` : ''}</p>`).join('')}</details>` : ''}</div></li>`).join('')}</ol>` : '<p class="muted">No milestones yet.</p>'}</section>`;
  } catch (error) {
    if (!active()) return;
    container.querySelector('[role=status]').textContent = error.message || 'Milestones unavailable.';
    const retry = document.createElement('button'); retry.textContent = 'Retry'; retry.onclick = () => renderMilestones(container); container.firstElementChild.appendChild(retry);
  }
}
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
    container.querySelector('.business-progress-results').innerHTML = `<div class="business-progress-grid">${PROGRESS_METRICS.map(([key,label]) => `<article class="business-progress-metric"><h2>${label}</h2><strong>${Number(data.totals[key]).toLocaleString()}</strong><span class="muted"> recorded total</span>${progressChart(data.series,key)}<div class="business-progress-axis"><span>${esc(data.series[0].date)}</span><span>${esc(data.series.at(-1).date)}</span></div>${data.undated[key] ? `<p class="muted">${data.undated[key]} without a recorded date, included in the total.</p>` : ''}</article>`).join('')}</div><p class="business-progress-coverage">Daily counts · UTC. ${esc(data.coverage)}</p>`;
  } catch (error) {
    if (!active()) return;
    container.querySelector('[role=status]').textContent = error.message || 'Progress unavailable.';
    const retry = document.createElement('button'); retry.textContent = 'Retry'; retry.onclick = () => renderBusinessProgress(container, days); container.querySelector('.business-progress-results').appendChild(retry);
  }
}
