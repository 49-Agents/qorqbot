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
const SG_CLIPBOARD = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#eee" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="m9 14 2 2 4-4"/></svg>';
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
    (document.querySelector('#toasts') || document.body).append(host);
    const current = () => sgAttention.deliveries.find(d=>d.id===host.dataset.delivery);
    // Delivery dismissal and snooze go to the server only from the context menu; opening never approves or dismisses.
    const update = async action => {
      const delivery=current(); if (!delivery) return;
      try {
        if (action==='dismiss') coreData(await post(`/me/attention/deliveries/${encodeURIComponent(delivery.id)}/dismiss`,{}));
        else coreData(await post(`/me/attention/signoffs/${encodeURIComponent(delivery.items[0].id)}/snooze`, {until:new Date(Date.now()+4*3600000).toISOString()}));
        await sgAttentionRefresh();
      } catch (error) { toast(error.message || 'Could not update notification.'); }
    };
    const open = async (slug, id) => {
      try { await sgAttentionNavigate(slug || null, id || null); }
      catch (error) { toast(error.message || 'Could not open the signoff.'); }
    };
    const activate = async (pill, event) => {
      const delivery=current(); if (!delivery) return;
      if (delivery.kind==='individual') {
        const item=delivery.items[0];
        return open(item.business_slug, item.id);
      }
      // A grouped delivery opens its one project's pending list; with several projects the owner picks which.
      const slugs=[...new Set(delivery.items.map(item=>item.business_slug || ''))];
      if (slugs.length===1) return open(slugs[0], null);
      const rect=pill.getBoundingClientRect();
      const x=event?.clientX || rect.left, y=event?.clientY || rect.bottom;
      ctxMenu(x, y, slugs.map(slug=>{
        const group=delivery.items.filter(item=>(item.business_slug || '')===slug);
        return {icon:SG_CLIPBOARD, label:`${group[0].business_name || 'Global'} (${group.length})`, onClick:()=>open(slug, null)};
      }));
    };
    host.addEventListener('click', event => { const pill=event.target.closest('.sg-toast'); if (pill) void activate(pill, event); });
    host.addEventListener('keydown', event => {
      const pill=event.target.closest('.sg-toast');
      if (pill && event.target===pill && ['Enter',' '].includes(event.key)) { event.preventDefault(); void activate(pill); }
    });
    host.addEventListener('contextmenu', event => {
      const pill=event.target.closest('.sg-toast'); if (!pill) return;
      event.preventDefault();
      const rect=pill.getBoundingClientRect(), keyboard=!event.clientX && !event.clientY;
      const items=[{icon:SG_BELL, label:'Dismiss notification', onClick:()=>void update('dismiss')}];
      if (current()?.kind==='individual') items.push({icon:SG_BELL, label:'Snooze 4 hours', onClick:()=>void update('snooze')});
      ctxMenu(keyboard ? rect.left : event.clientX, keyboard ? rect.bottom : event.clientY, items);
    });
  }
  const delivery = sgAttention.deliveries.find(d=>d.kind==='individual') || sgAttention.deliveries[0];
  if (!delivery) { host.replaceChildren(); delete host.dataset.delivery; return; }
  const identity=JSON.stringify(delivery);
  if (host._identity===identity && host.children.length) return;
  host._identity=identity; host.dataset.delivery=delivery.id;
  const row=delivery.items[0], individual=delivery.kind==='individual';
  const title=individual ? row.title : `${delivery.items.length} signoffs need your response`;
  if(typeof desktopNotify!=='undefined')desktopNotify.send({kind:'signoffs',title:individual?row.title:'Waiting on you',
    body:individual?(row.attention?.reason||row.business_name||''):`${delivery.items.length} signoffs need your response`,tag:'signoff:'+delivery.id});
  // Same capsule as a reminder; it stays until the owner acts and expiry never changes a record.
  host.innerHTML=`<div class="toast reminder-toast sg-toast" role="button" tabindex="0" aria-keyshortcuts="Shift+F10" aria-label="Approval needed: ${esc(title)}. Press Shift+F10 for dismiss and snooze options." title="${esc(title)}"><span class="reminder-icon" aria-hidden="true">${SG_CLIPBOARD}</span><span class="reminder-text">${esc(title)}</span></div>`;
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
