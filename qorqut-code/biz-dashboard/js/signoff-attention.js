function sgAgentAttentionPaint() {
  const pane = typeof _agpad !== 'undefined' ? _agpad : null;
  const items = pane?.agent ? sgAttention.urgent.filter(item => item.business_slug === pane.biz) : [];
  const blocking = items.filter(item => item.attention?.blocking);
  const business = pane && sgAttention.businesses.find(item => item.slug === pane.biz);
  const count = business?.signoffs ?? items.length;
  for (const [selector, amount, red] of [['#agpad .ttab[data-v="signoffs"]', count, items.length > 0], ['#agpad-toggle', blocking.length, true]]) {
    const target = document.querySelector(selector);
    if (!target) continue;
    target.querySelector('.sg-signoff-count')?.remove();
    if (amount) target.insertAdjacentHTML('beforeend', `<span class="sg-signoff-count${red ? ' urgent' : ''}" aria-label="${amount} ${selector === '#agpad-toggle' ? 'blocking' : 'waiting'} signoffs">${amount}</span>`);
  }
}
// One owner attention projection, shared by all workspaces. No automatic navigation.
const sgAttention = { businesses: [], urgent: [], deliveries: [], loading: false, error: null };
const SG_BELL = '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4"/></svg>';
window.sgAttentionBadge = slug => {
  const row = sgAttention.businesses.find(b => b.slug === slug);
  return row?.total ? `<span class="sg-attention-badge${row.urgent ? ' urgent' : ''}" aria-label="${row.total} waiting items${row.urgent ? ', urgent or blocking signoffs' : ''}">${row.total}</span>` : '';
};
window.sgAttentionNavigate = async (slug, id) => {
  await Promise.resolve(); // Finish the originating click before mounting a new pane.
  // Keep the current workspace. Attention links must never switch businesses
  // or leave the page the owner is working in.
  const matchesCurrentScope = slug ? state.scope !== 'global' && state.biz === slug : state.scope === 'global';
  openTasks(true); tSetView('signoffs');
  if (id && matchesCurrentScope) sgOpenItem(id);
};
function sgAttentionPaint() {
  const urgent = sgAttention.urgent.length > 0;
  const handle = document.querySelector('#thandle');
  if (handle) {
    handle.classList.toggle('sg-attention-urgent', urgent);
    handle.title = urgent ? `${sgAttention.urgent.length} urgent or blocking signoffs` : 'Open side pane';
  }
  document.querySelectorAll('.workspace-menu .mi[data-business-id]').forEach(row => {
    const business = sgAttention.businesses.find(b => b.id === row.dataset.businessId);
    row.querySelector('.sg-attention-badge')?.remove();
    if (business) row.querySelector('.workspace-name')?.insertAdjacentHTML('afterend', sgAttentionBadge(business.slug));
  });
  const tab = document.querySelector('#taskspanel .ttab[data-v="signoffs"]');
  if (tab) {
    tab.querySelector('.sg-signoff-count')?.remove();
    const b = sgAttention.businesses.find(b => b.slug === (state.scope === 'global' ? null : state.biz));
    const count = state.scope === 'global' ? sgAttention.businesses.reduce((sum, row) => sum + (row.signoffs || 0), 0) : b?.signoffs ?? 0;
    const scopeUrgent = state.scope === 'global' ? urgent : !!b?.urgent;
    if (count) tab.insertAdjacentHTML('beforeend', `<span class="sg-signoff-count${scopeUrgent ? ' urgent' : ''}" aria-label="${count} waiting signoffs${scopeUrgent ? ', urgent or blocking signoffs' : ''}">${count}</span>`);
  }
  sgAgentAttentionPaint();
  let host = document.querySelector('#sg-toast-host');
  if (!host) {
    host = document.createElement('div'); host.id='sg-toast-host'; host.setAttribute('aria-live','polite');
    document.body.append(host);
    host.addEventListener('click', async event => {
      const button=event.target.closest('button'); if (!button || button.disabled) return;
      const delivery=sgAttention.deliveries.find(d=>d.id===host.dataset.delivery);
      if (!delivery) return;
      try {
        button.disabled=true;
        if (button.dataset.action==='dismiss') coreData(await post(`/me/attention/deliveries/${encodeURIComponent(delivery.id)}/dismiss`,{}));
        else if (button.dataset.action==='snooze') {
          coreData(await post(`/me/attention/signoffs/${encodeURIComponent(delivery.items[0].id)}/snooze`, {until:new Date(Date.now()+4*3600000).toISOString()}));
        } else if (button.dataset.action==='open') {
          const item=delivery.items.find(i=>i.id===button.dataset.id) || delivery.items.find(i=>(i.business_slug || '')===button.dataset.slug);
          if (item) await sgAttentionNavigate(item.business_slug, button.dataset.id ? item.id : null);
          return;
        }
        await sgAttentionRefresh();
      } catch (error) { host.querySelector('.sg-toast-error').textContent=error.message || 'Could not update notification.'; }
      finally { button.disabled=false; }
    });
  }
  const delivery = sgAttention.deliveries.find(d=>d.kind==='individual') || sgAttention.deliveries[0];
  if (!delivery) { host.replaceChildren(); delete host.dataset.delivery; return; }
  const identity=JSON.stringify(delivery);
  if (host._identity===identity && host.children.length) return;
  host._identity=identity; host.dataset.delivery=delivery.id;
  const row=delivery.items[0];
  const groups=new Map();
  delivery.items.forEach(item=>{ const key=item.business_slug || ''; const old=groups.get(key); groups.set(key,{name:item.business_name,count:(old?.count||0)+1}); });
  host.innerHTML=`<section class="sg-toast"><header>${SG_BELL}<strong>${delivery.kind==='individual' ? esc(row.business_name) : 'Waiting on you'}</strong><button type="button" data-action="dismiss" aria-label="Dismiss notification">×</button></header>
    ${delivery.kind==='individual' ? `<button type="button" class="sg-toast-open" data-action="open" data-id="${esc(row.id)}">${esc(row.title)}</button><p>${esc(row.attention.reason || '')}</p>${row.attention.deadline ? `<p>Deadline: ${esc(new Date(row.attention.deadline).toLocaleString())}</p>` : ''}<button type="button" class="sg-toast-snooze" data-action="snooze">Snooze 4 hours</button>` :
      `<p>${delivery.items.length} signoffs need your response</p>${[...groups].map(([slug,g])=>`<button type="button" class="sg-toast-business" data-action="open" data-slug="${esc(slug)}">${esc(g.name)} <span>${g.count}</span></button>`).join('')}`}
    <p class="sg-toast-error" role="alert"></p></section>`;
}
window.sgAttentionRefresh = async () => {
  if (sgAttention.loading) return;
  sgAttention.loading=true;
  try {
    const [overview, deliveries] = await Promise.all([api('/me/attention'), api('/me/attention/deliveries')]);
    Object.assign(sgAttention,coreData(overview),{deliveries:coreData(deliveries).items,error:null});
    sgAttentionPaint();
    window.sgAttentionViewsPaint?.();
  } catch (error) { sgAttention.error=error; }
  finally { sgAttention.loading=false; }
};
setInterval(sgAttentionRefresh,15000);
window.addEventListener('focus',sgAttentionRefresh);
window.addEventListener('hashchange',sgAttentionPaint);
sgAttentionRefresh();
