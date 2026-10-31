// A small owner-facing projection; all object mutations remain in their own views.
const AGFOCUS_POLL_MS = 60000;
function agfocusPath(p) { return `/me/agents/${encodeURIComponent(p.agentId)}/focus`; }
function agfocusText(el, value) { if (el && el.textContent !== value) el.textContent = value; }

function agfocusMount(p) {
  const body = $('#agpad-body', p.el);
  body.innerHTML = `<div class="agfocus"><p class="agfocus-error" role="alert" hidden></p>
    <header><div class="global-eyebrow">Focus</div><h2 class="agfocus-status">Loading Focus…</h2><p class="agfocus-updated muted"></p></header>
    <p class="agfocus-activity muted" hidden></p><div class="agfocus-browser"></div><div class="agfocus-controls"><button class="quiet" data-focus-refresh>Refresh</button><label><input type="checkbox" data-focus-archived> Archived</label><label><input type="checkbox" data-focus-deferred> Postponed</label></div><div class="agfocus-items"></div><button class="quiet" data-focus-more hidden>More items</button></div>`;
  p.focusDrafts ||= new Map();
  $('[data-focus-refresh]',p.el).onclick=()=>agfocusRefresh(p);
  for(const kind of ['archived','deferred']) $('[data-focus-'+kind+']',p.el).onchange=e=>{p['focusInclude'+kind]=e.target.checked;p.focusOffset=0;agfocusRefresh(p);};
  $('[data-focus-more]',p.el).onclick=()=>{p.focusOffset=p.focus.next_offset;agfocusRefresh(p);};
  agfocusPaint(p);
}

function agfocusTick(p) {
  if (Date.now() >= (p.nextFocus || 0)) agfocusRefresh(p);
  if (p.view === 'focus') agfocusPaint(p);
}

async function agfocusRefresh(p) {
  if (p.focusPending || !agpadActive(p)) return;
  p.focusPending = true;
  try {
    const data = coreData(await post("/root/operations/focus_get",{scope:"global",include_archived:!!p.focusIncludearchived,include_deferred:!!p.focusIncludedeferred,offset:p.focusOffset||0}));
    if (!agpadActive(p)) return;
    if (!Array.isArray(data.items) || !Number.isInteger(data.revision)) throw new Error('Invalid Focus response');
    p.focus = data; p.focusError = '';
    p.focusRefreshThreads=true;
  } catch (error) {
    if (agpadActive(p)) p.focusError = 'Could not refresh Focus. ' + error.message;
  } finally {
    p.focusPending = false; p.nextFocus = Date.now() + AGFOCUS_POLL_MS;
    if (agpadActive(p)) agfocusPaint(p);
  }
}

function agfocusPaint(p) {
  const data = p.focus;
  const attention = data?.browser?.state === 'pending' || data?.items.some(item => item.needs_input);
  const tab = $('[data-v="focus"]', p.el);
  tab?.classList.toggle('agfocus-attention', !!attention);
  if (tab) tab.title = attention ? 'Focus — your input is needed' : 'Focus';
  if (p.view !== 'focus') return;
  const host = $('.agfocus', p.el);
  if (!host) return;
  const error = $('.agfocus-error', host);
  agfocusText(error, p.focusError || ''); error.hidden = !p.focusError;
  if (!data) return;
  agfocusText($('.agfocus-status', host), data.status || (data.items.length?'Owner attention':'Nothing on Focus yet'));
  agfocusText($('.agfocus-updated', host), data.updated_at
    ? `${data.stale ? 'Earlier session · ' : ''}Updated ${agpadInboxAge(data.updated_at)}` : `${data.items.length} item${data.items.length===1?'':'s'} on this page`);
  const activity = $('.agfocus-activity', host), a = data.activity;
  activity.hidden = !a;
  if (a) {
    const running = a.phase === 'running' && Date.parse(a.until) > Date.now();
    agfocusText(activity, running ? `Browser · ${a.command} running` :
      `Browser · ${a.command} ${a.phase === 'running' ? 'outcome unknown' : a.phase} · ${agpadInboxAge(a.at)}`);
  }
  const list = $('.agfocus-items', host);
  const focused=document.activeElement,selection=focused?.selectionStart!=null?[focused.selectionStart,focused.selectionEnd]:null;
  const ids=new Set(data.items.map(item=>item.id));
  for(const node of [...list.children]) if(!ids.has(node.dataset.cardId)) {
    const conversation=node.querySelector('.agfocus-conversation')?._conversation;
    if(conversation?.state.text || conversation?.isExpanded()) {
      node.querySelector('.agfocus-meta').textContent='This card left the current board. Your conversation draft is retained.';
    } else node.remove();
  }
  for(const item of data.items) {
    let node=[...list.children].find(n=>n.dataset.cardId===item.id);
    if(!node) {
      node=document.createElement('article');node.className='agfocus-card';node.dataset.cardId=item.id;
      node.innerHTML='<div class="agfocus-meta"></div><h3></h3><p class="agfocus-reason"></p><p class="agfocus-recommendation"></p><p class="agfocus-request"></p><p class="agfocus-context muted"></p><div class="agfocus-actions"></div><div class="agfocus-related"></div><div class="agfocus-conversation"></div>';
      list.appendChild(node);
    }
    list.appendChild(node);
    if(item.unavailable) {
      node.querySelector('h3').textContent='This item is no longer available.';
      for(const el of node.querySelectorAll('p,.agfocus-related,.agfocus-conversation,.agfocus-actions')) {el.replaceChildren();delete el.dataset.conversation;delete el._conversation;}
      continue;
    }
    node.querySelector('h3').textContent=item.label;
    node.querySelector('.agfocus-meta').textContent=[item.resolved_source?.business_name,item.source?.type || 'Reference',item.state,item.archived?'Archived':''].filter(Boolean).join(' · ');
    node.querySelector('.agfocus-reason').textContent=item.reason;
    node.querySelector('.agfocus-recommendation').textContent=item.recommendation;
    node.querySelector('.agfocus-request').textContent=item.requested_response;
    node.querySelector('.agfocus-context').textContent=item.context_label?'Conversation: '+item.context_label:'';
    const actions=node.querySelector('.agfocus-actions');actions.innerHTML='<button class="quiet" data-open>Open</button><button class="quiet" data-up>Move to top</button><button class="quiet" data-archive></button>';
    actions.querySelector('[data-open]').onclick=()=>{
      if(item.opening_target?.url){const url=agfocusURL(item.opening_target.url);if(url)window.open(url,'_blank','noopener,noreferrer');}
      else agfocusOpenItem(p,item.resolved_source || item.opening_target).catch(e=>toast(e.message));
    };
    const mutate=async(op,args)=>{try{coreData(await post('/root/operations/'+op,{scope:'global',item_id:item.id,expected_card_revision:item.revision,idempotency_key:newRequestId(),...args}));await agfocusRefresh(p);}catch(e){toast(e.message);}};
    actions.querySelector('[data-up]').onclick=()=>mutate('focus_move',{position:'top'});
    actions.querySelector('[data-archive]').textContent=item.archived?'Restore':'Archive';
    actions.querySelector('[data-archive]').onclick=()=>mutate('focus_set_archived',{archived:!item.archived});
    const related=node.querySelector('.agfocus-related');related.replaceChildren();
    for(const ref of item.references||[]) {
      const button=document.createElement('button');button.className='quiet';button.textContent=ref.unavailable?'Related item unavailable':ref.label;button.disabled=!!ref.unavailable;
      button.onclick=()=>agfocusOpenItem(p,ref).catch(e=>toast(e.message));related.appendChild(button);
    }
    const binding=item.conversation;
    conversationMount(node.querySelector('.agfocus-conversation'),{type:binding.type,id:binding.id,biz:item.resolved_source?.business_slug || item.context_business_slug || null,revision:item.source_revision},{
      read:async pagination=>coreData(await post('/root/operations/focus_get',{scope:'global',item_id:item.id,conversation:true,...pagination})).item.thread,
      send:async request=>coreData(await post('/root/operations/focus_reply',{scope:'global',item_id:item.id,text:request.text,idempotency_key:request.idempotency_key,...(request.revision?{expected_source_revision:request.revision}:{})}))
    });
  }
  if(focused?.isConnected && list.contains(focused)){focused.focus({preventScroll:true});if(selection)focused.setSelectionRange(...selection);}
  if(p.focusRefreshThreads){p.focusRefreshThreads=false;for(const node of list.querySelectorAll('.agfocus-conversation'))node._conversation?.refresh();}
  $('[data-focus-more]',host).hidden=data.next_offset==null;
  agfocusBrowser(p, $('.agfocus-browser', host));
}

function agfocusBrowser(p, host) {
  const b = p.focus.browser, pending = b?.state === 'pending';
  // Do not replace a textarea as status, source revisions or activity refresh.
  const key = b ? `${b.request_id}:${b.state}` : '';
  if (host.dataset.request !== key) {
    host.dataset.request = key;
    host.innerHTML = !b ? '' : `<section class="agfocus-card agfocus-handoff">
      <div class="agfocus-meta">${pending ? 'Browser input needed' : 'Input sent to Ordubashy'}</div>
      <h3>${esc(b.prompt)}</h3><p>${esc(b.app)} · ${esc(b.tab)}</p>
      ${pending ? `<p class="muted">Automation is paused. Complete the input, then tell Ordubashy you’re done.</p>
        <button class="btn agfocus-desktop" type="button">Show desktop</button><p class="agfocus-desktop-note muted"></p>
        <label>Note for Ordubashy <span class="muted">(optional)</span><textarea class="inp agfocus-note" rows="2" maxlength="4000"></textarea></label>
        <button class="btn agfocus-done" type="button">I’m done</button><p class="agfocus-done-error" role="alert" hidden></p>
        <p class="muted">Input completion only. This does not approve a signoff.</p>` : `<p>${esc(b.note || 'The owner completed this input.')}</p>`}</section>`;
    if (pending) {
      let draft = p.focusDrafts.get(b.request_id);
      if (!draft) { draft = {note: ''}; p.focusDrafts.set(b.request_id, draft); }
      const input = $('.agfocus-note', host);
      input.value = draft.note;
      input.disabled = !!draft.submission;
      input.oninput = () => { draft.note = input.value; };
      $('.agfocus-done', host).onclick = () => agfocusDone(p, b.request_id, host, draft);
      $('.agfocus-desktop', host).onclick = () => agfocusDesktop(p, b.request_id).catch(error => toast(error.message));
    }
  }
  if (pending) {
    const configured = !!agfocusURL(p.focus.desktop_control_url);
    $('.agfocus-desktop', host).disabled = !configured;
    agfocusText($('.agfocus-desktop-note', host), configured ? 'Desktop opens here only when you click.' : 'Set DESKTOP_CONTROL_URL in Global Settings to show desktop control.');
  }
}

async function agfocusDone(p, requestId, host, draft) {
  if (draft.sending) return;
  draft.sending = true;
  // Freeze the original note on ambiguous failure. Retrying cannot become a new response.
  draft.submission ||= {request_id: requestId, note: draft.note};
  const button = $('.agfocus-done', host), input = $('.agfocus-note', host), error = $('.agfocus-done-error', host);
  button.disabled = true; input.disabled = true; error.hidden = true;
  try {
    coreData(await post(agfocusPath(p) + '/done', draft.submission));
    p.nextFocus = 0;
    await agfocusRefresh(p);
  } catch (e) {
    if (host.isConnected) { error.hidden = false; error.textContent = e.message; button.textContent = 'Retry same response'; }
    p.nextFocus = 0;
  } finally {
    draft.sending = false;
    if (button.isConnected) button.disabled = false;
  }
}

function agfocusURL(value) {
  try {
    if (!value || /\s/.test(value)) return null;
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch (_) { return null; }
}

async function agfocusDesktop(p, requestId) {
  const data = coreData(await api(agfocusPath(p)));
  if (!agpadActive(p)) return;
  if (data.browser?.request_id !== requestId || data.browser.state !== 'pending') throw new Error('This input request changed. Refresh Focus.');
  const url = agfocusURL(data.desktop_control_url);
  if (!url) throw new Error('Set DESKTOP_CONTROL_URL in Global Settings.');
  const restore = document.activeElement;
  modal({title: 'Desktop control', width: 1400, stack: true,
    body: `<div class="agfocus-desktop-help"><strong>${esc(data.browser.prompt)}</strong><p>${esc(data.browser.app)} · ${esc(data.browser.tab)}</p>
      <p>Close this window when finished, then click “I’m done” in Focus. Closing alone does not notify Ordubashy.</p></div>
      <iframe class="agfocus-frame" title="Desktop control" src="${esc(url)}" referrerpolicy="no-referrer" allow="clipboard-read; clipboard-write; fullscreen"></iframe>`,
    onOpen: el => { el.classList.add('agfocus-desktop-modal'); el.closest('.overlay').classList.add('agfocus-desktop-overlay'); $('.mx', el).focus(); },
    onClose: () => { if (restore?.isConnected) restore.focus(); }});
}

async function agfocusOpenItem(p, item) {
  if (item.type === 'task') {
    if(!item.business_id)return taskProgressOpenReference(null,{type:'task',id:item.id});
    const task = await tFetchTask(item.business_slug, item.id);
    if (agpadActive(p)) tDetail(task);
    return;
  }
  if (item.type === 'signoff') {
    let view;
    modal({title: esc(item.label), width: 850, stack: true, body: '<div class="agfocus-signoff"></div>',
      onOpen: el => {
        view = sgCreateView({active: () => el.isConnected, body: () => $('.agfocus-signoff', el),
          workspace: () => item.business_slug || 'scope:global', global: !item.business_id, category: item.category,
          item: () => ({id: item.id, businessId: item.business_id}), waitingOnly: true, execution: true});
        view.paint();
      }, onClose: () => view?.dispose()});
    return;
  }
  if (item.resource_type === 'documents') {
    const {document: doc} = coreData(await api(agfocusPath(p) + '/documents/' + encodeURIComponent(item.id)));
    if (agpadActive(p)) modal({title: esc(item.label), stack: true,
      body: `<div class="story-source">${esc(doc.body || 'No text preview. Open this document in its workspace for attachments.')}</div>`, actions: []});
    return;
  }
  // Explicit owner navigation for other domain objects, never on an agent update.
  if (item.business_slug) return contextOpenItem(item.business_slug, item);
  modal({title: esc(item.label), stack: true, body: `<p>${esc(item.summary || item.reason)}</p>`, actions: []});
}
