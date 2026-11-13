// Shared row structure for the Attention board. All visual rules stay in the Focus stylesheet.
function agfocusRowHtml(actions = '') {
  return `<div class="trow agfocus-row"><span class="agfocus-notification" aria-hidden="true"><span class="agfocus-unread" hidden></span></span><span class="agfocus-marker"></span><span class="task-business-column agfocus-scope"></span><div class="task-headline"><p class="ttl"></p><span class="agfocus-flag" hidden></span></div><div class="agfocus-actions">${actions}</div></div>`;
}

// Owner Focus: the launcher and side-chat row, the attention row, then the owner's own notes.
// Records opened from Chat references, side chats and other expansions fill the same area while open.
const AGFOCUS_POLL_MS = 60000;
function agfocusPath(p) { return `/me/agents/${encodeURIComponent(p.agentId)}/focus`; }
function agfocusText(el, value) { if (el && el.textContent !== value) el.textContent = value; }

function agfocusSnoozed(item) { return Date.parse(item.deferred_until)>Date.now(); }
function agfocusUnread(p,node,item,value) {
  if(!node || item.unavailable)return;
  const shared=typeof replyNotifications!=='undefined'?replyNotifications.unread(item.conversation || item.source):null;
  const unread=value ?? shared ?? !!item.private_unread;
  node.querySelector('.agfocus-unread').hidden=!unread;
  const name=item.display_name?.trim() || item.label;
  node.setAttribute('aria-label',name+(agfocusSnoozed(item)?', postponed':'')+(unread?', unread private reply':''));
}
const agfocusTextHost=()=>typeof focusText!=='undefined'?focusText:null;

function agfocusQueueMount(p,queue) {
  // An agent with Focus gets launchers and open side chats above attention; other agents keep their views and queue.
  p.focusSurfaces=agpadFocusSurfaces(p) && typeof agviewboxSurfacesMount==='function'?agviewboxSurfacesMount(p,queue):null;
  const strip=p.focusSurfaces;
  const disposeAttention=strip?()=>{strip.dispose();if(p.focusSurfaces===strip)p.focusSurfaces=null;}
    :typeof attentionQueue!=='undefined'?attentionQueue.mount(queue):()=>{};
  const signoffQueue=strip?.signoffHost || document.createElement('section');
  if(!strip){
    signoffQueue.className='focus-signoff-queue';signoffQueue.setAttribute('aria-label','Signoff queue');signoffQueue.hidden=true;
    queue.after(signoffQueue);
  }
  const disposeSignoffs=typeof attentionQueue!=='undefined'?attentionQueue.mountSignoffs(signoffQueue,undefined,undefined,{sideChatHost:strip?.sideChatHost}):()=>{};
  return ()=>{disposeAttention();disposeSignoffs();if(!strip)signoffQueue.remove();};
}

function agfocusMount(p) {
  const body = $('#agpad-body', p.el);
  body.innerHTML = `<div class="agfocus"><p class="agfocus-error" role="alert" hidden></p><section class="attention-queue" aria-label="New attention"></section><section class="agfocus-surface" aria-label="Browser, Desktop or Viewbox" hidden><div class="agfocus-surface-bar"><button type="button" class="quiet agfocus-close" data-surface-close aria-label="Close"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg><span>Close</span></button><span class="agfocus-surface-title"></span></div><div class="agfocus-surface-body"></div></section><div class="agfocus-layout"><div class="agfocus-notes"></div><aside class="agfocus-dock" aria-label="Selected record" hidden><div class="agfocus-dock-content"></div></aside></div></div>`;
  const board = $('.agfocus', body);
  p.focusMount = board;
  const disposeQueue=agfocusQueueMount(p,board.querySelector('.attention-queue'));
  // Records open in the shared record workspace. Its header owns Back, the type and
  // Chat icon tabs and the pill Close; an open record fills the area below the control rows.
  p.focusWorkspacePaired=false;
  p.focusWorkspace=recordWorkspaceMount($('.agfocus-dock-content',board),agfocusWorkspaceOptions(p));
  const pairedUnavailable=source=>p.focusWorkspace?.invalidate?.(source) ?? false;
  const pairedReference=ref=>agfocusOpenPairedReference(p,ref);
  const pairedClose=()=>p.focusWorkspace?.close?.();
  window.agentWorkspacePairedUnavailable=pairedUnavailable;
  window.agentWorkspacePairedReference=pairedReference;
  window.agentWorkspacePairedClose=pairedClose;
  const disposeText=agfocusTextHost()?.mount(p,board) || (()=>{});
  $('[data-surface-close]',board).onclick=()=>agpadSurfaceClose(p);
  // Escape closes an open expansion or record.
  const escape=event=>{
    if(event.key!=='Escape' || document.querySelector('.overlay'))return;
    if(p.surface?.kind==='expansion'){event.preventDefault();event.stopPropagation();void agpadSurfaceClose(p);return;}
    if(p.surface || !p.focusWorkspace?.isOpen?.())return;
    event.preventDefault();event.stopPropagation();void p.focusWorkspace?.close();
  };
  document.addEventListener('keydown',escape);
  p.workspaceDispose=()=>{
    disposeQueue();
    document.removeEventListener('keydown',escape);
    disposeText();
    p.focusWorkspace?.dispose();p.focusWorkspace=null;
    if(window.agentWorkspacePairedUnavailable===pairedUnavailable)delete window.agentWorkspacePairedUnavailable;
    if(window.agentWorkspacePairedReference===pairedReference)delete window.agentWorkspacePairedReference;
    if(window.agentWorkspacePairedClose===pairedClose)delete window.agentWorkspacePairedClose;
    p.focusMount=null;
  };
  p.nextFocus=Date.now()+AGFOCUS_POLL_MS;
  agfocusPaint(p);
  agfocusOpenLinkedItem(p);
}

// While Focus is shown, the open record's Chat and the notes are reread on a slow cadence.
function agfocusTick(p) {
  if (p.view !== 'focus') return;
  if (Date.now() >= (p.nextFocus || 0)) agfocusRefresh(p);
}
function agfocusRefresh(p) {
  if (!agpadActive(p) || !p.focusMount?.isConnected) return;
  p.nextFocus = Date.now() + AGFOCUS_POLL_MS;
  p.focusWorkspace?.refresh?.();
  void agfocusTextHost()?.load(p);
  agfocusPaint(p);
}

function agfocusPaint(p) {
  if(typeof attentionQueue!=='undefined')attentionQueue.paintSignoffs();
  const tab = $('[data-v="focus"]', p.el);
  tab?.classList.toggle('agfocus-attention', p.focus?.browser?.state === 'pending');
  if (tab) tab.removeAttribute('title');
  agfocusDesktopTab(p);
  if (p.view !== 'focus' || p.focusDetail) return;
  const host = $('.agfocus', p.el);
  if (!host) return;
  const error = $('.agfocus-error', host);
  agfocusText(error, p.focusError || ''); error.hidden = !p.focusError;
  agfocusTextHost()?.paint(p);
}

// Open a record in the shared workspace (used by deep links and Chat references).
async function agfocusOpenTarget(p,target,{root=true,paired,parentTable=null}={}) {
  if (p.surface) { await agpadSurfaceClose(p); if (p.surface || _agpad !== p) return false; }
  const record=rwRecordFromItem(target,{can_thread:target?.can_thread});
  if(!record)throw new Error('This item is no longer available.');
  // A record with a private thread shows that thread in the Chat area on the left and its details here.
  if(paired===undefined)paired=target?._workKind==='crm' || (!!record.can_thread && agfocusLeftChatReady());
  if(!await agfocusWorkspaceMode(p,paired))return false;
  agfocusShowDock(p);
  const opened=parentTable?.id ? await p.focusWorkspace?.navigate(record,{parentTable})
    : await p.focusWorkspace?.open(record,{root,tab:record.can_thread?'chat':'dashboard'});
  // The Chat area can refuse the thread (a side chat still sending); then nothing opens here either.
  if(opened===false && !p.focusWorkspace?.isOpen?.())agfocusClearSelection(p);
  return opened;
}
// Native Chat Task references preserve their canonical scope and open this record's private thread.
async function agfocusOpenChatTask(agentId, reference) {
  const pane=typeof _agpad==='undefined'?null:_agpad;
  if(!pane || String(pane.agentId ?? '')!==String(agentId ?? ''))throw new Error('This agent is no longer open.');
  if(!agpadFocusSurfaces(pane))throw new Error('This agent has no Focus view.');
  if(!reference || reference.id==null)throw new Error('This Task is no longer available.');
  const source={type:'task',id:reference.id,business_id:reference.business_id ?? null,business_slug:reference.business_slug || null,
    label:reference.label || null,task_type:reference.task_type || null,state:reference.state || null};
  const record=rwRecordFromItem({source,can_thread:true},{can_thread:true});
  if(!record)throw new Error('This Task is no longer available.');
  if(record.scopeMissing)throw new Error('That Task belongs to a Project that is not available here.');
  if(pane.view!=='focus' || pane.surface)await agpadSetView(pane,'focus');
  if(pane.view!=='focus' || !pane.focusMount?.isConnected)throw new Error('Focus is unavailable for this agent.');
  const opened=await agfocusOpenTarget(pane,{source,can_thread:true},{root:true});
  if(typeof _agpad==='undefined' || _agpad!==pane || String(pane.agentId ?? '')!==String(agentId ?? ''))throw new Error('This agent is no longer open.');
  if(!pane.focusWorkspace?.isOpen?.())throw new Error('This Task could not be opened.');
  return opened;
}
// The Chat area beside this pane can show a thread: the parent's when Focus runs in a frame, or this page's.
function agfocusLeftChatReady() {
  if(window.parent!==window)return typeof window.parent.agentWorkspacePairedRecord==='function';
  return typeof globalAgentViewActive!=='undefined' && typeof globalAgentViewActive?.selectPairedSource==='function';
}
async function agfocusWorkspaceMode(p,paired) {
  paired=!!paired && p.workspaceRole==='ordubashy';
  if(p.focusWorkspacePaired===paired && p.focusWorkspace?.alive?.())return true;
  const previous=p.focusWorkspace;
  if(previous?.alive?.() && previous.isOpen?.()) {
    await previous.close();
    if(previous.isOpen?.())return false; // a dirty guard cancelled the mode switch
  }
  previous?.dispose?.();
  const board=p.focusMount;
  if(!board?.isConnected)return false;
  p.focusWorkspacePaired=paired;
  p.focusWorkspace=recordWorkspaceMount($('.agfocus-dock-content',board),agfocusWorkspaceOptions(p));
  return true;
}
// A selected record fills the board below the persistent control row. The record's own header holds
// its pill Close, title and tabs; the agent Chat beside the pane keeps its state and stays usable.
function agfocusShowDock(p) {
  const board=p.focusMount;if(!board?.isConnected)return null;
  if(!p.focusWorkspace?.alive?.())p.focusWorkspace=recordWorkspaceMount($('.agfocus-dock-content',board),agfocusWorkspaceOptions(p));
  const dock=$('.agfocus-dock',board),layout=$('.agfocus-layout',board);
  if(dock.hidden)p.focusLayoutScroll=layout?.scrollTop || 0;
  dock.hidden=false;layout.classList.add('agfocus-record-open');board.classList.add('agfocus-record-open');
  return dock;
}
function agfocusClearSelection(p) {
  const board=p.focusMount,dock=board?.isConnected?$('.agfocus-dock',board):null;
  p.focusWorkspacePaired=false;
  if(!board?.isConnected)return;
  dock.hidden=true;
  board.classList.remove('agfocus-record-open');
  const layout=$('.agfocus-layout',board);
  layout.classList.remove('agfocus-record-open');
  if(p.focusLayoutScroll!=null && !p.surface)layout.scrollTop=p.focusLayoutScroll;
  p.focusLayoutScroll=null;
}
// Eviction, lost access or a source disappearing closes the mounted record without a save prompt.
function agfocusForceClose(p) {
  void agfocusPairedSource(null,null);
  p.focusWorkspace?.dispose();
  p.focusWorkspacePaired=false;
  const board=p.focusMount;if(board?.isConnected)p.focusWorkspace=recordWorkspaceMount($('.agfocus-dock-content',board),agfocusWorkspaceOptions(p));
  agfocusClearSelection(p);
  agfocusPaint(p);
}
function agfocusWorkspaceOptions(p) {
  return {entry:'focus',
    paired:!!p.focusWorkspacePaired && p.workspaceRole === 'ordubashy',
    closeFirst:true,
    onPairedSource:(record,parentTable)=>agfocusPairedSource(record,parentTable),
    visible:()=>agpadActive(p) && p.view==='focus' && p.focusMount && !p.focusMount.hidden,
    onClose:()=>agfocusClearSelection(p),
    // Reading or receiving a reply updates the shared unread markers, including pinned threads in the top row.
    onRead:()=>{if(typeof replyNotifications!=='undefined')void replyNotifications.refresh?.();},
    onUnread:()=>{if(typeof replyNotifications!=='undefined')void replyNotifications.refresh?.();}};
}
function agfocusPairedSource(record,parentTable) {
  const source=record?.source || null;
  if(!source)return window.parent?.agentWorkspacePairedRecord?.(window,null,null) ?? true;
  const parent=parentTable?{type:'table',id:parentTable.id,business_id:parentTable.business_id ?? null}:null;
  if(window.parent!==window && typeof window.parent.agentWorkspacePairedRecord==='function')
    return window.parent.agentWorkspacePairedRecord(window,{type:source.type,id:source.id,business_id:source.business_id ?? null,label:record.label || source.label || null},parent);
  return window.agentWorkspacePairedRecord?.(window,{type:source.type,id:source.id,business_id:source.business_id ?? null,label:record.label || source.label || null},parent) ?? true;
}
async function agfocusOpenPairedReference(p,ref) {
  if(ref?.type==='task')return agentOpenRecordReference(ref);
  if (p.surface) { await agpadSurfaceClose(p); if (p.surface || _agpad !== p) return false; }
  if(!ref?.type || !ref.id)return false;
  const target=rwRecordFromItem({source:{type:ref.type,id:ref.id,business_id:ref.business_id ?? null,
    business_slug:ref.business_slug,business_name:ref.business_name,label:ref.label,state:ref.state,resource_type:ref.resource_type,
    task_type:ref.task_type,revision:ref.revision}}, {can_thread:ref.can_thread});
  if(!target)return false;
  const suppliedParent=ref.parent_table || ref.crm_parent || null;
  const parentTable=target.source.type==='task' && suppliedParent?.type==='table' && suppliedParent.id
    ? {type:'table',id:suppliedParent.id,business_id:suppliedParent.business_id ?? null,label:suppliedParent.label || suppliedParent.title || 'CRM'} : null;
  if(!await agfocusWorkspaceMode(p,true))return false;
  agfocusShowDock(p);
  if(!p.focusWorkspace?.alive?.())return false;
  return p.focusWorkspace.navigate(target,{parentTable});
}

function agfocusActive(p) { return !!p && agpadActive(p) && p.view==='focus'; }

// A one-time canonical Task link (/?open_task=...&scope=global|business_id=<uuid>#/global/agents) from the standalone CRM
// viewer. It validates the whole route before any read, consumes it, reads the authorized Task and opens it in the shared
// record workspace. It never selects, creates or pins a Focus card, and an invalid, mixed or unknown target never falls
// through to another scope or record.
async function agfocusOpenTaskLink(p,url) {
  const route=openTaskRoute(url.search);
  const consume=()=>{url.search='';history.replaceState(null,'',url);};
  const refuse=message=>{consume();if(agpadActive(p)){p.focusError=p.focusLinkError=message;agfocusPaint(p);}};
  if(route.invalid || url.searchParams.has('focus_item'))return refuse('This record link is not valid.');
  let slug=null;
  if(route.businessId!==null) {
    const business=(state.bizs || []).find(b=>b.id===route.businessId || b.business_id===route.businessId);
    if(!business?.slug)return refuse('This project is unavailable.');
    slug=business.slug;
  }
  consume();
  try {
    const data=await crmCall(slug,'tasks_get',{task_id:route.taskId});
    if(!agpadActive(p) || p.view!=='focus' || !p.focusMount?.isConnected)return;
    if(!data.task)throw new Error('missing');
    const task=data.task;
    const record=rwRecordFromItem({source:{type:'task',id:task.id,business_id:route.businessId,business_slug:slug,label:task.title,task_type:task.task_type,state:task.status}},{can_thread:true});
    if(!record || record.scopeMissing)throw new Error('missing');
    if(!await agfocusWorkspaceMode(p,false))return;
    agfocusShowDock(p);
    await p.focusWorkspace?.open(record,{root:true,tab:'dashboard'});
  } catch(error){
    // The ordinary unavailable state: no identity or cached data from the link is repeated.
    if(agpadActive(p)){p.focusError=p.focusLinkError='This record is unavailable.';agfocusPaint(p);}
  }
}


async function agfocusOpenLinkedItem(p) {
  const url=new URL(location.href);
  if(typeof openTaskRoute==='function' && url.searchParams.has('open_task'))return agfocusOpenTaskLink(p,url);
  // Links to retired Focus cards have nothing left to open.
  if(url.searchParams.has('focus_item')){url.searchParams.delete('focus_item');history.replaceState(null,'',url);}
}

function agfocusHtml(el,html) { if(el && el._html!==html){el._html=html;el.innerHTML=html;} }
function agfocusContext(node,item) {
  const source=item.resolved_source || item.context || item.conversation || {};
  const biz=source.business_slug || item.context_business_slug;
  const businessId=source.business_id ?? item.source?.business_id ?? item.context?.business_id;
  const scope=node.querySelector('.agfocus-scope');
  // A Project card already carries its exact Project name as its title: no same-name scope badge.
  const project=source.type==='project';
  scope.hidden=project || !!item._sameScope || (!!item._workParent && (item.source?.type==='task' || item.source?.type==='project') && item._workBusinessId!=null && businessId===item._workBusinessId);
  if(project){scope.replaceChildren();delete scope._html;return;}
  const businessName=source.business_name || (state.bizs || []).find(b=>b.slug===biz)?.name || (source.type==='task'?'Project':'Project');
  agfocusHtml(scope,businessId || biz
    ? `<span class="agfocus-scope-chip" aria-label="${esc(businessName)}">${esc(businessName)}</span>`
    : '<span class="agfocus-scope-chip agfocus-global-chip" role="img" aria-label="Global"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></svg></span>');
}

// Desktop input requests mark the Desktop chip on Focus (Ordubashy without Focus keeps the Browser tab).
function agfocusDesktopTab(p) {
  const chip = $('[data-focus-surface="desktop"]:not([aria-disabled="true"])', p.el), tab = chip || p.workspaceRole !== 'ordubashy' ? null : $('[data-v="browser"]', p.el);
  const pending = p.focus?.browser?.state === 'pending';
  if (chip) {
    chip.dataset.attention = pending ? 'true' : 'false';
    chip.setAttribute('aria-label', pending ? 'Desktop, input requested' : 'Desktop');
    return;
  }
  if (!tab) return;
  tab.dataset.attention = pending ? 'true' : 'false';
  tab.setAttribute('aria-label', pending ? 'Browser, desktop input requested' : 'Browser');
  tab.removeAttribute('title');
}

async function agfocusDone(p, requestId, host, draft) {
  if (draft.sending) return;
  draft.sending = true;
  // Freeze the original note on ambiguous failure. Retrying cannot become a new response.
  const button = $('.agfocus-done', host), input = $('.agfocus-note', host), error = $('.agfocus-done-error', host);
  button.disabled = true; input.disabled = true; error.hidden = true;
  try {
    // A new completion must target the request currently shown by the service.
    // Once a submission exists, an uncertain retry reuses it even if the service
    // has already applied it.
    if (!draft.submission) {
      const current = coreData(await api(agfocusPath(p)));
      if (current.browser?.request_id !== requestId || current.browser.state !== 'pending') throw new Error('This input request changed. Refresh Focus.');
      draft.submission = {request_id: requestId, note: draft.note};
    }
    coreData(await post(agfocusPath(p) + '/done', draft.submission));
    p.nextFocus = 0;
    await agfocusRefresh(p);
    if (host.isConnected) {
      const completion = host.querySelector('.agfocus-desktop-completion');
      if (completion) completion.innerHTML = '<p class="muted agfocus-desktop-success">Input sent to Ordubashy.</p>';
    }
  } catch (e) {
    if (host.isConnected) { error.hidden = false; error.textContent = e.message; button.textContent = 'Retry same response'; }
    p.nextFocus = 0;
  } finally {
    draft.sending = false;
    if (button.isConnected) button.disabled = false;
    if (input.isConnected) input.disabled = !!draft.submission;
  }
}

function agfocusURL(value) {
  try {
    if (!value || /\s/.test(value)) return null;
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch (_) { return null; }
}

function agfocusDisplayDimension(value) {
  if (!/^\d+$/.test(value || '')) return null;
  const dimension = Number(value);
  return Number.isSafeInteger(dimension) && dimension > 0 && dimension <= 32768 ? dimension : null;
}

function agfocusDesktopURL(value) {
  const safe = agfocusURL(value);
  if (!safe) return null;
  const url = new URL(safe);
  const width = agfocusDisplayDimension(url.searchParams.get('qorqut_width'));
  const height = agfocusDisplayDimension(url.searchParams.get('qorqut_height'));
  url.searchParams.delete('qorqut_width');
  url.searchParams.delete('qorqut_height');
  return {url: url.href, width: width && height ? width : null, height: width && height ? height : null};
}

function agfocusDesktopFit(availableWidth, availableHeight, displayWidth, displayHeight) {
  const width = Math.max(0, availableWidth), height = Math.max(0, availableHeight);
  if (!displayWidth || !displayHeight) return {width, height};
  const scale = Math.min(width / displayWidth, height / displayHeight);
  return {width: displayWidth * scale, height: displayHeight * scale};
}

function agfocusDesktopFrameFit(detail, display) {
  const frame = $('.agfocus-desktop-frame', detail.host);
  if (!frame) return;
  const body = detail.host.closest('.tbody') || detail.host.parentElement;
  let frameRequest = 0;
  const fit = () => {
    frameRequest = 0;
    if (!frame.isConnected) return;
    const frameBox = frame.getBoundingClientRect();
    const bodyBox = body?.getBoundingClientRect() || frameBox;
    const bodyStyle = body ? getComputedStyle(body) : null;
    const detailStyle = getComputedStyle(detail.host.closest('.agfocus-detail') || detail.host);
    const completion = $('.agfocus-desktop-completion', detail.host);
    const footerHeight = completion?.getBoundingClientRect().height || 0;
    const bottomPadding = (parseFloat(bodyStyle?.paddingBottom) || 0) + (parseFloat(detailStyle.paddingBottom) || 0);
    const hostStyle = getComputedStyle(detail.host);
    const availableWidth = Math.max(0, detail.host.clientWidth -
      (parseFloat(hostStyle.paddingLeft) || 0) - (parseFloat(hostStyle.paddingRight) || 0));
    // Convert the frame's viewport position back to the body's content space.
    // Otherwise scrolling the detail upward makes the remaining height appear
    // larger and can produce a frame that no longer fits the pane.
    const scrollOffset = body?.scrollTop || 0;
    const availableHeight = Math.max(detail.minHeight || 0, bodyBox.bottom - bottomPadding - frameBox.top - scrollOffset - footerHeight - (completion ? 10 : 0));
    const size = agfocusDesktopFit(availableWidth, availableHeight, display.width, display.height);
    frame.style.width = `${size.width}px`;
    frame.style.height = `${size.height}px`;
  };
  const schedule = () => { if (!frameRequest) frameRequest = requestAnimationFrame(fit); };
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
  observer?.observe(detail.host);
  if (body && body !== detail.host) observer?.observe(body);
  for (const element of detail.host.querySelectorAll('.agfocus-desktop-help,.agfocus-desktop-completion')) observer?.observe(element);
  window.addEventListener('resize', schedule);
  detail.onDispose(() => {
    if (frameRequest) cancelAnimationFrame(frameRequest);
    observer?.disconnect();
    window.removeEventListener('resize', schedule);
  });
  schedule();
}

// The switch and the reconnect belong to the pane's own Desktop view; a Focus surface is opened
// and reopened from the persistent launcher row, so neither control appears in the expanded area.
function agfocusDesktopBar(p, ctx) {
  if (ctx) return '';
  return '<div class="agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') +
    '<span class="agbrowser-actions">' + agbrowserReconnectButton('data-desktop-retry') + '</span></div>';
}
function agfocusDesktopError(p, body, message, ctx = null) {
  body.innerHTML = '<section class="agfocus-desktop-view">' + agfocusDesktopBar(p, ctx) +
    '<p class="ferr" role="alert">' + esc(message) + '</p></section>';
  const retry = $('[data-desktop-retry]', body);
  if (retry) retry.onclick = () => agfocusDesktop(p, true, ctx);
  agbrowserWireMode(p, body);
}

// Ordubashy's full desktop: its explicit desktop binding, or core's validated
// DESKTOP_CONTROL_URL fallback. Focus supplies only the pending-input request.
// A nested surface passes its mount context (host, currentness, cleanup registration); without one the pane body is the host.
async function agfocusDesktop(p, reconnect = false, ctx = null) {
  const body = ctx ? ctx.host : $('#agpad-body', p.el);
  const live = () => ctx ? ctx.current() : agpadActive(p) && p.view === 'desktop';
  const own = fn => ctx ? ctx.onDispose(fn) : (p.workspaceDispose = fn);
  if (!body || !live()) return;
  if (ctx) ctx.reset(); else { p.workspaceDispose?.(); p.workspaceDispose = null; }
  const request = p.desktopRequest = (p.desktopRequest || 0) + 1;
  if (ctx) ctx.onDispose(() => { p.desktopRequest++; });
  body.innerHTML = '<section class="agfocus-desktop-view">' + agfocusDesktopBar(p, ctx) + '<p class="muted" role="status">Connecting to Ordubashy’s desktop…</p></section>';
  agbrowserWireMode(p, body);
  let data, surfaces;
  try {
    [data, surfaces] = await Promise.all([api(agfocusPath(p)).then(coreData), agbrowserSurfaces(p, reconnect)]);
    if (!live() || p.desktopRequest !== request || !body.isConnected) return;
    // The latest Desktop read also marks a pending input request on the Desktop chip.
    p.focus = data;
    agfocusDesktopTab(p);
  } catch (error) {
    if (body.isConnected && live() && p.desktopRequest === request) agfocusDesktopError(p, body, error.message, ctx);
    return;
  }
  const surface = surfaces.desktop;
  const desktop = surface && agfocusDesktopURL(surface.url);
  if (!desktop) {
    body.innerHTML = '<section class="agfocus-desktop-view">' + agfocusDesktopBar(p, ctx) + '<p class="muted" role="status">Desktop is not configured. Set DESKTOP_CONTROL_URL in Global Settings or add Ordubashy’s desktop binding.</p></section>';
    const retry = $('[data-desktop-retry]', body);
    if (retry) retry.onclick = () => agfocusDesktop(p, true, ctx);
    agbrowserWireMode(p, body);
    return;
  }
  if (surface.width && surface.height) { desktop.width = surface.width; desktop.height = surface.height; }
  p.focusDrafts ||= new Map();
  const browser = data.browser;
  const pending = browser?.state === 'pending';
  const requestId = browser?.request_id;
  let draft = requestId ? p.focusDrafts.get(requestId) : null;
  if (requestId && !draft) { draft = {note: ''}; p.focusDrafts.set(requestId, draft); }
  body.innerHTML='<section class="agfocus-desktop-view">' + agfocusDesktopBar(p, ctx) + '<div class="agfocus-desktop-help agfocus-desktop-notes"></div><iframe class="agfocus-reference-frame agfocus-desktop-frame" aria-label="Desktop control" referrerpolicy="no-referrer" allow="clipboard-read; clipboard-write; fullscreen"></iframe>' +
    (pending ? '<div class="agfocus-desktop-completion"><label>Note for Ordubashy <span class="muted">(optional)</span><textarea class="inp agfocus-note" rows="2" maxlength="4000"></textarea></label><button class="btn agfocus-done" type="button">I’m done</button><p class="agfocus-done-error" role="alert" hidden></p><p class="muted">Input completion only. This does not approve a signoff.</p></div>' : '') + '</section>';
  const host = $('.agfocus-desktop-view', body);
  const help = $('.agfocus-desktop-notes', host);
  const heading = document.createElement('strong');
  heading.textContent = browser?.prompt || 'Desktop control';
  help.appendChild(heading);
  if (browser?.app || browser?.tab) {
    const where = document.createElement('p');
    where.textContent = [browser.app, browser.tab].filter(Boolean).join(' · ');
    help.appendChild(where);
  }
  const instruction = document.createElement('p');
  instruction.textContent = pending ? 'Complete the input in the desktop, then click “I’m done”.' : (browser?.note || 'No browser input is currently pending.');
  help.appendChild(instruction);
  const retry = $('[data-desktop-retry]', host);
  if (retry) retry.onclick = () => agfocusDesktop(p, true, ctx);
  agbrowserWireMode(p, host);
  const frame = $('.agfocus-desktop-frame', host);
  frame.dataset.surface = surface.id;
  frame.src = desktop.url;
  const disposers = [];
  own(() => { disposers.splice(0).forEach(fn => fn()); frame.src = 'about:blank'; frame.remove(); });
  agfocusDesktopFrameFit({host, minHeight: ctx?.minHeight, onDispose: fn => disposers.push(fn)}, desktop);
  if (pending) {
    const input = $('.agfocus-note', host);
    input.value = draft.note;
    input.disabled = !!draft.submission;
    input.oninput = () => { draft.note = input.value; };
    $('.agfocus-done', host).onclick = () => agfocusDone(p, requestId, host, draft);
  }
}

function agfocusDesktopMount(p, ctx = null) {
  p.focusDrafts ||= new Map();
  agfocusDesktop(p, false, ctx).catch(error => {
    const body = ctx ? ctx.host : $('#agpad-body', p.el);
    if (body && (ctx ? ctx.current() : p.view === 'desktop')) agfocusDesktopError(p, body, error.message, ctx);
  });
}

// Focus notes use the existing inline editor and persistence.
function agfocusOverlayClose(p) {
  if(!p.focusOverlay)return;
  p.focusOverlay.dispose();p.focusOverlay=null;
}
function agfocusOverlayOpen(p,board,dock,onClose=()=>{void p.focusWorkspace?.close();}) {
  // The selected Record pane stays one overlay: re-selecting a row re-fits the existing one
  // instead of stacking a second inert/escape scope on the native Chat surface.
  if(p.focusOverlay){p.focusOverlay.fit?.();return;}
  const terminal=p.el.closest('.agsplit')?.querySelector('.global-agent-session, #chat');
  if(!terminal || terminal.getBoundingClientRect().width<240)return;
  const layout=board.querySelector('.agfocus-layout'),wasInert=terminal.inert;
  const fit=()=>{
    const rect=terminal.getBoundingClientRect(),left=Math.max(0,rect.left),top=Math.max(0,rect.top);
    const right=Math.min(window.innerWidth,rect.right),bottom=Math.min(window.innerHeight,rect.bottom);
    // Client rectangles include Appearance's CSS zoom; fixed offsets use the dock's unscaled coordinates.
    let zoom=1;
    for(let node=dock;node;node=node.parentElement)zoom*=parseFloat(getComputedStyle(node).zoom) || 1;
    Object.assign(dock.style,{left:left/zoom+'px',top:top/zoom+'px',width:Math.max(0,right-left)/zoom+'px',height:Math.max(0,bottom-top)/zoom+'px'});
  };
  dock.classList.add('agfocus-chat-overlay');layout.classList.add('agfocus-overlay-open');
  dock.setAttribute('role','dialog');dock.setAttribute('aria-label','Record workspace');
  terminal.inert=true;
  const escape=event=>{if(event.key==='Escape' && !document.querySelector('.overlay')){event.preventDefault();event.stopPropagation();onClose();}};
  const observer=new ResizeObserver(fit);observer.observe(terminal);
  window.addEventListener('resize',fit);document.addEventListener('keydown',escape);
  p.focusOverlay={fit,dispose:()=>{observer.disconnect();window.removeEventListener('resize',fit);document.removeEventListener('keydown',escape);terminal.inert=wasInert;dock.classList.remove('agfocus-chat-overlay');layout.classList.remove('agfocus-overlay-open');dock.removeAttribute('role');dock.setAttribute('aria-label','Selected record');for(const key of ['left','top','width','height'])dock.style[key]='';}};
  fit();
}
