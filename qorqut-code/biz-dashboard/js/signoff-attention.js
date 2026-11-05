const sgAttention = { businesses: [], urgent: [], deliveries: [], loading: false, error: null };
const SG_BELL = '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4"/></svg>';
window.sgAttentionBadge = slug => {
  const row = sgAttention.businesses.find(b => b.slug === slug);
  return row?.total ? `<span class="sg-attention-badge${row.urgent ? ' urgent' : ''}" aria-label="${row.total} waiting items${row.urgent ? ', urgent or blocking signoffs' : ''}">${row.total}</span>` : '';
};
// Mount the existing signoff renderer in the shared Focus surface.
window.sgFocusOpen = (title, options, afterClose) => window.focusExpansion.open({title, mount(host, session) {
  let view, confirmation = null;
  const notice = document.createElement('p'); notice.className='sg-notice'; notice.setAttribute('role','status'); notice.hidden=true;
  const prompt = document.createElement('div'); prompt.className='sg-focus-confirm'; prompt.hidden=true;
  const body = document.createElement('div');
  host.append(notice, prompt, body);
  const finish = answer => { prompt.hidden=true; prompt.replaceChildren(); const resolve=confirmation; confirmation=null; resolve?.(answer); };
  const confirmExecutionCancel = () => {
    if (confirmation) return Promise.resolve(false);
    prompt.hidden=false;
    const text=document.createElement('p'); text.textContent='Request cancellation? Work that has already started may still need reconciliation.';
    const cancel=document.createElement('button'); cancel.type='button'; cancel.className='quiet'; cancel.textContent='Keep execution'; cancel.onclick=()=>finish(false);
    const confirm=document.createElement('button'); confirm.type='button'; confirm.className='danger'; confirm.textContent='Confirm cancellation'; confirm.onclick=()=>finish(true);
    prompt.append(text,cancel,confirm); cancel.focus();
    return new Promise(resolve=>{confirmation=resolve;});
  };
  view=sgCreateView({...options, active:()=>session.isCurrent() && body.isConnected, body:()=>body, confirmExecutionCancel});
  session.setBeforeLeave(async()=>{
    if (confirmation) { notice.textContent='Finish or cancel the confirmation first.'; notice.hidden=false; return false; }
    if (view.hasPendingWrite()) { notice.textContent='Finish or check the pending signoff operation before leaving.'; notice.hidden=false; return false; }
    if (view.hasUnsaved()) { notice.textContent='Save or discard the message edit before leaving.'; notice.hidden=false; return false; }
    return true;
  });
  view.paint();
  return ()=>{finish(false);view.dispose();afterClose?.();};
}});
window.sgAttentionNavigate = async (slug, id) => {
  if (typeof embeddedAgentPane !== 'undefined' && embeddedAgentPane) {
    try {
      if (parent === window || typeof parent.sgAttentionNavigate !== 'function') throw new Error('The Focus pane is unavailable.');
      return await parent.sgAttentionNavigate(slug || null, id || null);
    } catch (error) { throw new Error(error?.message || 'The Focus pane is unavailable.'); }
  }
  await Promise.resolve();
  return sgFocusOpen('Signoffs', {workspace:()=>slug || 'scope:global', global:!slug, execution:true,
    ...(id?{item:()=>({id,businessId:slug ? (state.bizs.find(b=>b.slug===slug)?.id || state.bizs.find(b=>b.slug===slug)?.business_id) : null})}:{})});
};
function sgAttentionPaint() {
  const urgent = sgAttention.urgent.length > 0;
  const b = sgAttention.businesses.find(b => b.slug === (state.scope === 'global' ? null : state.biz));
  const count = state.scope === 'global' ? sgAttention.businesses.reduce((sum, row) => sum + (row.signoffs || 0), 0) : b?.signoffs ?? 0;
  const scopeUrgent = state.scope === 'global' ? urgent : !!b?.urgent;
  const tab = document.querySelector('#taskspanel .ttab[data-v="signoffs"]');
  if (tab) {
    tab.querySelector('.sg-signoff-count')?.remove();
    if (count) tab.insertAdjacentHTML('beforeend', `<span class="sg-signoff-count${scopeUrgent ? ' urgent' : ''}" aria-label="${count} waiting signoffs${scopeUrgent ? ', urgent or blocking signoffs' : ''}">${count}</span>`);
  }
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
  if(typeof desktopNotify!=='undefined')desktopNotify.send({kind:'signoffs',title:delivery.kind==='individual'?row.title:'Waiting on you',
    body:delivery.kind==='individual'?(row.attention?.reason||row.business_name||''):`${delivery.items.length} signoffs need your response`,tag:'signoff:'+delivery.id});
  const groups=new Map();
  delivery.items.forEach(item=>{ const key=item.business_slug || ''; const old=groups.get(key); groups.set(key,{name:item.business_name,count:(old?.count||0)+1}); });
  host.innerHTML=`<section class="sg-toast"><header>${SG_BELL}<strong>${delivery.kind==='individual' ? esc(row.business_name) : 'Waiting on you'}</strong><button type="button" data-action="dismiss" aria-label="Dismiss notification">×</button></header>
    ${delivery.kind==='individual' ? `<button type="button" class="sg-toast-open" data-action="open" data-id="${esc(row.id)}">${esc(row.title)}</button><p>${esc(row.attention.reason || '')}</p>${row.attention.deadline ? `<p>Deadline: ${esc(sjDateTime(row.attention.deadline))}</p>` : ''}<button type="button" class="sg-toast-snooze" data-action="snooze">Snooze 4 hours</button>` :
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
// Stage 2 PR6: push replaces the 15s poll; the focus handler stays (a
// backgrounded tab can miss its own change-feed connection's relevance while
// unfocused), and the 60s safety poll bounds staleness if an event is missed.
if (typeof embeddedAgentPane === 'undefined' || !embeddedAgentPane) {
  onChange(['signoffs'], null, sgAttentionRefresh);
  setInterval(sgAttentionRefresh,60000);
  window.addEventListener('focus',sgAttentionRefresh);
  window.addEventListener('hashchange',sgAttentionPaint);
  sgAttentionRefresh();
}
