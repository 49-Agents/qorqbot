// A small owner-facing projection; all object mutations remain in their own views.
const AGFOCUS_POLL_MS = 60000;
function agfocusPath(p) { return `/me/agents/${encodeURIComponent(p.agentId)}/focus`; }
function agfocusText(el, value) { if (el && el.textContent !== value) el.textContent = value; }
function agfocusPageKey(p) { return JSON.stringify([p.focusOffset||0,!!p.focusIncludearchived,!!p.focusIncludedeferred]); }

function agfocusMount(p) {
  const body = $('#agpad-body', p.el);
  body.innerHTML = `<div class="agfocus"><p class="agfocus-error" role="alert" hidden></p>
    <header><div class="agfocus-heading"><div class="global-eyebrow">Focus</div><button class="quiet agfocus-icon" data-focus-refresh aria-label="Refresh Focus" title="Refresh Focus"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.6-1L20 9M4 15l2.3 3A7 7 0 0 0 18 17"/></svg></button></div><div class="agfocus-controls"><button class="quiet" data-focus-archived aria-pressed="false">Archived</button><button class="quiet" data-focus-deferred aria-pressed="false">Postponed</button></div><h2 class="agfocus-status">Loading Focus…</h2><p class="agfocus-updated muted" hidden></p></header>
    <p class="agfocus-activity muted" hidden></p><div class="agfocus-browser"></div><div class="agfocus-items"></div><nav class="agfocus-pages" aria-label="Focus pages"><button class="quiet" data-focus-previous hidden>Previous</button><span class="muted" data-focus-page></span><button class="quiet" data-focus-more hidden>Next</button></nav></div>`;
  p.focusDrafts ||= new Map();
  p.focusPages ||= [];
  $('[data-focus-refresh]',p.el).onclick=()=>agfocusRefresh(p);
  for(const kind of ['archived','deferred']) {
    const toggle=$('[data-focus-'+kind+']',p.el);
    toggle.setAttribute('aria-pressed',String(!!p['focusInclude'+kind]));
    toggle.onclick=()=>{p['focusInclude'+kind]=!p['focusInclude'+kind];toggle.setAttribute('aria-pressed',String(p['focusInclude'+kind]));p.focusPages=[];p.focusOffset=0;agfocusRefresh(p);};
  }
  $('[data-focus-more]',p.el).onclick=()=>{if(p.focusPending)return;p.focusPages.push(p.focusOffset||0);p.focusOffset=p.focus.next_offset;agfocusRefresh(p);};
  $('[data-focus-previous]',p.el).onclick=()=>{if(p.focusPending)return;p.focusOffset=p.focusPages.pop()||0;agfocusRefresh(p);};
  agfocusPaint(p);
}

function agfocusTick(p) {
  if (p.view !== 'focus' || p.focusDetail) return;
  if (Date.now() >= (p.nextFocus || 0)) agfocusRefresh(p);
  if (p.view === 'focus') agfocusPaint(p);
}

async function agfocusRefresh(p) {
  if (p.focusPending || !agpadActive(p)) return;
  p.focusPending = true;
  const pageKey=agfocusPageKey(p);
  try {
    const data = coreData(await post("/root/operations/focus_get",{scope:"global",include_archived:!!p.focusIncludearchived,include_deferred:!!p.focusIncludedeferred,offset:p.focusOffset||0}));
    if (!agpadActive(p) || pageKey!==agfocusPageKey(p)) return;
    if (!Array.isArray(data.items) || !Number.isInteger(data.revision)) throw new Error('Invalid Focus response');
    if(p.focusPageKey!==pageKey) { const list=$('.agfocus-items',p.el);if(list)list.replaceChildren(); }
    p.focusPageKey=pageKey;
    p.focus = data; p.focusError = '';
    p.focusRefreshThreads=true;
  } catch (error) {
    if (agpadActive(p)) p.focusError = 'Could not refresh Focus. ' + error.message;
  } finally {
    p.focusPending = false; p.nextFocus = pageKey===agfocusPageKey(p)?Date.now() + AGFOCUS_POLL_MS:0;
    if (agpadActive(p)) agfocusPaint(p);
  }
}

function agfocusPaint(p) {
  const data = p.focus;
  const attention = data?.browser?.state === 'pending' || data?.items.some(item => item.needs_input);
  const tab = $('[data-v="focus"]', p.el);
  tab?.classList.toggle('agfocus-attention', !!attention);
  if (tab) tab.title = attention ? 'Focus — your input is needed' : 'Focus';
  if (p.view !== 'focus' || p.focusDetail) return;
  const host = $('.agfocus', p.el);
  if (!host) return;
  const error = $('.agfocus-error', host);
  agfocusText(error, p.focusError || ''); error.hidden = !p.focusError;
  if (!data) return;
  agfocusText($('.agfocus-status', host), data.status || (data.items.length?'Owner attention':'Nothing on Focus yet'));
  agfocusText($('.agfocus-updated', host), data.updated_at
    ? `${data.stale ? 'Earlier session · ' : ''}Updated ${agpadInboxAge(data.updated_at)}` : '');
  $('.agfocus-updated',host).hidden=!data.updated_at;
  const activity = $('.agfocus-activity', host), a = data.activity;
  activity.hidden = !a;
  if (a) {
    const running = a.phase === 'running' && Date.parse(a.until) > Date.now();
    agfocusText(activity, running ? `Browser · ${a.command} running` :
      `Browser · ${a.command} ${a.phase === 'running' ? 'outcome unknown' : a.phase} · ${agpadInboxAge(a.at)}`);
  }
  const list = $('.agfocus-items', host);
  const signature=JSON.stringify([data.items,p.focusPageKey,data.next_offset]);
  if(list.dataset.signature===signature) {
    if(p.focusRefreshThreads){p.focusRefreshThreads=false;for(const node of list.querySelectorAll('.agfocus-conversation'))node._conversation?.refresh();}
    agfocusBrowser(p, $('.agfocus-browser', host));return;
  }
  list.dataset.signature=signature;
  const focused=document.activeElement,selection=focused?.selectionStart!=null?[focused.selectionStart,focused.selectionEnd]:null;
  const ids=new Set(data.items.map(item=>item.id));
  for(const node of [...list.children]) if(!ids.has(node.dataset.cardId)) {
    const conversation=node.querySelector('.agfocus-conversation')?._conversation;
    if(conversation?.state.text || conversation?.isExpanded()) {
      node.querySelector('.agfocus-meta').textContent='No longer on this page · conversation retained';
      node.classList.add('agfocus-retained');
    } else node.remove();
  }
  let position=0;
  for(const item of data.items) {
    let node=[...list.children].find(n=>n.dataset.cardId===item.id);
    if(node?.dataset.unavailable && !item.unavailable){node.remove();node=null;}
    if(!node) {
      node=document.createElement('article');node.className='agfocus-card';node.dataset.cardId=item.id;
      node.innerHTML='<div class="agfocus-meta"></div><h3></h3><p class="agfocus-reason"></p><div class="agfocus-framing"><section data-framing="recommendation"><h4>Recommendation</h4><p class="agfocus-recommendation"></p></section><section data-framing="requested_response"><h4>From you</h4><p class="agfocus-request"></p></section></div><p class="agfocus-context muted"></p><div class="agfocus-actions"><button class="quiet" data-manage aria-label="Manage Focus card" title="Manage Focus card"><svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg></button></div><div class="agfocus-related"></div><div class="agfocus-conversation"></div>';
      list.appendChild(node);
    }
    if(list.children[position]!==node)list.insertBefore(node,list.children[position]||null);
    position++;node.classList.remove('agfocus-retained');
    if(item.unavailable) {
      node.dataset.unavailable='true';node.removeAttribute('tabindex');node.onclick=null;node.onkeydown=null;
      node.querySelector('h3').textContent='This item is no longer available.';
      node.querySelector('.agfocus-meta').textContent='Unavailable';
      node.querySelector('.agfocus-framing').hidden=true;
      for(const el of node.querySelectorAll('p,.agfocus-related,.agfocus-conversation,.agfocus-actions')) {el.replaceChildren();delete el.dataset.conversation;delete el._conversation;}
      continue;
    }
    node.querySelector('h3').textContent=item.label;
    node.querySelector('.agfocus-meta').textContent=[item.resolved_source?.business_name,item.source?.type || 'Reference',item.state,item.archived?'Archived':''].filter(Boolean).join(' · ');
    node.querySelector('.agfocus-reason').textContent=item.reason;
    node.querySelector('.agfocus-recommendation').textContent=item.recommendation;
    node.querySelector('.agfocus-request').textContent=item.requested_response;
    for(const field of ['recommendation','requested_response'])node.querySelector('[data-framing="'+field+'"]').hidden=!item[field];
    node.querySelector('.agfocus-context').textContent=item.context_label?'Conversation: '+item.context_label:'';
    const actions=node.querySelector('.agfocus-actions');
    node.tabIndex=0;node.setAttribute('role','group');node.setAttribute('aria-label',item.label);
    const open=()=>agfocusOpenItem(p,item.resolved_source || item.opening_target).catch(e=>toast(e.message));
    node.onclick=event=>{if(event.target.closest('button,a,input,textarea,select,.agfocus-conversation,.conversation-panel'))return;open();};
    node.onkeydown=event=>{if(event.target===node && (event.key==='Enter' || event.key===' ')){event.preventDefault();open();}};
    const mutate=async(op,args)=>{try{coreData(await post('/root/operations/'+op,{scope:'global',item_id:item.id,expected_card_revision:item.revision,idempotency_key:newRequestId(),...args}));await agfocusRefresh(p);}catch(e){toast(e.message);}};
    actions.querySelector('[data-manage]').onclick=event=>dropdown(event.currentTarget,[
      {value:'top',label:'Move to top'},{value:'bottom',label:'Move to bottom'},
      {value:'archive',label:item.archived?'Restore to Focus':'Archive from Focus'}
    ],value=>value==='archive'?mutate('focus_set_archived',{archived:!item.archived}):mutate('focus_move',{position:value}));
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
    const comment=node.querySelector('.agfocus-conversation .conversation-toggle');
    if(comment)actions.appendChild(comment);
  }
  if(focused?.isConnected && list.contains(focused)){focused.focus({preventScroll:true});if(selection)focused.setSelectionRange(...selection);}
  if(p.focusRefreshThreads){p.focusRefreshThreads=false;for(const node of list.querySelectorAll('.agfocus-conversation'))node._conversation?.refresh();}
  $('[data-focus-more]',host).hidden=data.next_offset==null;
  $('[data-focus-previous]',host).hidden=!p.focusPages?.length;
  $('[data-focus-page]',host).textContent=(p.focusPages?.length || data.next_offset!=null)?'Page '+((p.focusPages?.length||0)+1):'';
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
  const detail=agfocusDetail(p,'Desktop control');
  detail.host.innerHTML=`<div class="agfocus-desktop-help"><strong>${esc(data.browser.prompt)}</strong><p>${esc(data.browser.app)} · ${esc(data.browser.tab)}</p>
    <p>Return to Focus when finished, then click “I’m done”.</p></div>
    <iframe class="agfocus-reference-frame" title="Desktop control" src="${esc(url)}" referrerpolicy="no-referrer" allow="clipboard-read; clipboard-write; fullscreen"></iframe>`;
}

function agfocusDetail(p,title) {
  p.focusDetail?.dispose?.();
  const body=$('#agpad-body',p.el),board=$('.agfocus',body),scroll=body.scrollTop;
  const restore=document.activeElement;
  board.hidden=true;
  const shell=document.createElement('section');shell.className='agfocus-detail';
  shell.innerHTML='<header class="agfocus-detail-header"><button type="button" class="quiet" aria-label="Back to Focus">← Focus</button><span></span></header><div class="agfocus-detail-body"></div>';
  shell.querySelector('header span').textContent=title || '';
  body.appendChild(shell);body.scrollTop=0;
  let cleanup;
  const detail={host:shell.querySelector('.agfocus-detail-body'),current:()=>p.focusDetail===detail && shell.isConnected && p.view==='focus',
    dispose:()=>{cleanup?.();shell.remove();},
    onDispose:fn=>{cleanup=fn;},
    back:()=>{if(!detail.current())return;detail.dispose();p.focusDetail=null;board.hidden=false;body.scrollTop=scroll;agfocusPaint(p);if(restore?.isConnected)restore.focus({preventScroll:true});}};
  p.focusDetail=detail;shell.querySelector('button').onclick=detail.back;
  shell.querySelector('button').focus({preventScroll:true});
  return detail;
}

async function agfocusOpenItem(p,item) {
  if(!item || item.unavailable)throw new Error('This item is no longer available.');
  if(!agpadActive(p))return;
  const detail=agfocusDetail(p,item.label || item.title || '');
  detail.host.innerHTML='<p class="muted" role="status">Loading…</p>';
  try {
    if(item.url) {
      const url=agfocusURL(item.url);
      if(!url)throw new Error('This reference URL is unavailable.');
      detail.host.innerHTML=`<iframe class="agfocus-reference-frame" title="${esc(item.label || 'Reference')}" src="${esc(url)}" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads" allow="clipboard-read; clipboard-write; fullscreen"></iframe><p class="muted agfocus-reference-help">If this site prevents embedding, <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">open it in a new tab</a>.</p>`;
      return;
    }
    if(item.type==='task') {
      if(!item.business_id){globalTasksMount(detail.host,{initialTaskId:item.id,onBack:detail.back});return;}
      const task=await tFetchTask(item.business_slug,item.id);
      if(detail.current())tDetail(task,{host:detail.host,onClose:detail.back});
      return;
    }
    if(item.type==='signoff') {
      detail.host.classList.add('agfocus-signoff');
      const view=sgCreateView({active:detail.current,body:()=>detail.host,
        workspace:()=>item.business_slug || 'scope:global',global:!item.business_id,category:item.category,
        item:()=>({id:item.id,businessId:item.business_id}),waitingOnly:false,execution:true});
      detail.onDispose(()=>view.dispose());view.paint();return;
    }
    if(item.type==='story' && item.business_slug) {
      detail.onDispose(()=>{if(typeof storyDispose==='function')storyDispose();});
      await renderStories(detail.host,{biz:item.business_slug,initialStoryId:item.id,onBack:detail.back});return;
    }
    if(item.resource_type==='documents') {
      const {document:doc}=coreData(await api(agfocusPath(p)+'/documents/'+encodeURIComponent(item.id)));
      if(detail.current())detail.host.innerHTML=`<div class="story-source">${esc(doc.body || 'No text preview available.')}</div>`;
      return;
    }
    detail.host.innerHTML=`<div class="story-source">${esc(item.summary || item.reason || 'No preview available.')}</div>`;
  } catch(error) {
    if(detail.current())detail.host.innerHTML=`<p class="ferr" role="alert">${esc(error.message)}</p>`;
  }
}
