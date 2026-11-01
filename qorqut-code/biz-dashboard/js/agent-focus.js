// Owner Focus presentation; source state and conversations retain their existing writers.
const AGFOCUS_POLL_MS = 60000;
const AGFOCUS_THREAD_HEIGHT_KEY = 'ag:focus-thread-height:v1';
const AGFOCUS_THREAD_MIN_HEIGHT = 180;
function agfocusPath(p) { return `/me/agents/${encodeURIComponent(p.agentId)}/focus`; }
function agfocusText(el, value) { if (el && el.textContent !== value) el.textContent = value; }
function agfocusPageKey(p) { return JSON.stringify([p.focusOffset||0,!!p.focusIncludearchived,!!p.focusIncludedeferred]); }

function agfocusSnoozed(item) { return Date.parse(item.deferred_until)>Date.now(); }
function agfocusUnread(p,node,item,value) {
  if(!node || item.unavailable)return;
  const unread=value ?? (!!p.focusPrivateUnread?.has(item.id) || (!!item.private_unread && !p.focusPrivateRead?.has(item.id)));
  node.querySelector('.agfocus-unread').hidden=!unread;
  const name=item.display_name?.trim() || item.label;
  node.setAttribute('aria-label',name+(agfocusSnoozed(item)?', postponed':'')+(unread?', unread private reply':''));
  node._conversationHost?._conversation?.setUnread(unread);
}
function agfocusMoveEligible(p,node) {
  return !!node && node.parentElement?.classList.contains('agfocus-items') && !node._item?.unavailable && !node._item?._storyParent && !node._item?.archived &&
    !p.focusSnoozes?.get(node.dataset.cardId)?.pending && node.querySelector('.agfocus-task-state')?.getAttribute('aria-busy')!=='true';
}
function agfocusRowPending(p,node) {
  if(!node || node._item?.unavailable)return;
  const snoozePending=!!p.focusSnoozes?.get(node.dataset.cardId)?.pending;
  const grip=node.querySelector('[data-focus-grip]');
  grip.disabled=!!p.focusOrderPending || !!p.focusMoveSubmission || !agfocusMoveEligible(p,node);
  const snooze=node.querySelector('[data-focus-snooze]');
  snooze.disabled=snoozePending || !!p.focusOrderPending || !!node._item.archived;
  snooze.setAttribute('aria-busy',String(snoozePending));
}
function agfocusAnnounce(p,text) { agfocusText(p.focusMount?.querySelector('[data-focus-announcement]'),text); }

async function agfocusMove(p,id,anchorId,side,retry=false) {
  if(p.focusOrderPending)return;
  const mount=p.focusMount;
  if(!mount?.isConnected)return;
  if(!retry) {
    if(p.focusMoveSubmission)return;
    const row=p.focusCards.get(id),anchor=p.focusCards.get(anchorId);
    if(id===anchorId || !agfocusMoveEligible(p,row) || !agfocusMoveEligible(p,anchor) || !['before_id','after_id'].includes(side))return;
    const order=[...$('.agfocus-items',mount).children].map(node=>node.dataset.cardId);
    const moved=order.filter(value=>value!==id),index=moved.indexOf(anchorId);
    if(index<0)return;
    moved.splice(index+(side==='after_id'?1:0),0,id);
    if(moved.every((value,i)=>value===order[i]))return;
    p.focusMoveSubmission={scope:'global',item_id:id,expected_card_revision:row._item.revision,[side]:anchorId,idempotency_key:newRequestId()};
  }
  const submission=p.focusMoveSubmission;
  if(!submission)return;
  p.focusOrderPending=true;p.focusError='';agfocusPaint(p);
  let failure;
  try {
    coreData(await post('/root/operations/focus_move',submission));
    p.focusMoveSubmission=null;
  } catch(error) {
    failure=error;
    if(['revision_conflict','validation_error','not_found','forbidden','policy_denied'].includes(error.code))p.focusMoveSubmission=null;
  }
  await agfocusRefresh(p);
  p.focusOrderPending=false;
  if(p.focusMount!==mount)return;
  if(failure){p.focusError='Could not move Focus item. '+failure.message;agfocusAnnounce(p,p.focusError);}
  else agfocusAnnounce(p,'Focus item moved.');
  agfocusPaint(p);
}

function agfocusOrderMount(p,board,body) {
  let drag=null,frame=null;
  const clearMarker=()=>{for(const row of board.querySelectorAll('.agfocus-drop-before,.agfocus-drop-after'))row.classList.remove('agfocus-drop-before','agfocus-drop-after');};
  const target=()=>{
    clearMarker();drag.anchor=null;
    const list=$('.agfocus-items',board),box=list.getBoundingClientRect(),pane=body.getBoundingClientRect();
    if(drag.x<box.left || drag.x>box.right || drag.y<Math.max(box.top,pane.top) || drag.y>Math.min(box.bottom,pane.bottom))return;
    const rows=[...list.children].filter(row=>agfocusMoveEligible(p,row));
    const row=rows.find(row=>{const rect=row.getBoundingClientRect();return drag.y>=rect.top && drag.y<=rect.bottom;});
    if(!row || row===drag.row)return;
    drag.side=drag.y<row.getBoundingClientRect().top+row.getBoundingClientRect().height/2?'before_id':'after_id';
    drag.anchor=row;row.classList.add(drag.side==='before_id'?'agfocus-drop-before':'agfocus-drop-after');
  };
  const scroll=()=>{
    frame=null;
    if(!drag?.active)return;
    const box=body.getBoundingClientRect();
    const distance=drag.y<box.top+32?-Math.ceil((box.top+32-drag.y)/4):drag.y>box.bottom-32?Math.ceil((drag.y-box.bottom+32)/4):0;
    if(distance){body.scrollTop+=Math.max(-12,Math.min(12,distance));target();}
    frame=requestAnimationFrame(scroll);
  };
  const stop=(cancel=false)=>{
    if(!drag)return;
    const current=drag;drag=null;
    if(frame!=null){cancelAnimationFrame(frame);frame=null;}
    clearMarker();current.row.classList.remove('agfocus-dragging');
    if(current.grip.hasPointerCapture(current.pointerId))current.grip.releasePointerCapture(current.pointerId);
    if(current.active)p.focusSuppressClickUntil=Date.now()+400;
    if(!cancel && current.active && current.anchor)void agfocusMove(p,current.row.dataset.cardId,current.anchor.dataset.cardId,current.side);
  };
  const down=event=>{
    const grip=event.target.closest('[data-focus-grip]'),row=grip?.closest('.agfocus-card');
    if(!grip || grip.disabled || event.button!==0 || drag || !agfocusMoveEligible(p,row))return;
    event.preventDefault();event.stopPropagation();grip.focus({preventScroll:true});
    drag={grip,row,pointerId:event.pointerId,startY:event.clientY,x:event.clientX,y:event.clientY,active:false};
    grip.setPointerCapture(event.pointerId);
  };
  const move=event=>{
    if(drag?.pointerId!==event.pointerId)return;
    drag.x=event.clientX;drag.y=event.clientY;
    if(!drag.active && Math.abs(drag.y-drag.startY)>=6){drag.active=true;drag.row.classList.add('agfocus-dragging');frame=requestAnimationFrame(scroll);}
    if(drag.active){event.preventDefault();target();}
  };
  const up=event=>{if(drag?.pointerId===event.pointerId)stop();};
  const cancel=event=>{if(drag?.pointerId===event.pointerId)stop(true);};
  const key=event=>{
    if(event.key==='Escape' && drag){event.preventDefault();stop(true);return;}
    const grip=event.target.closest('[data-focus-grip]');
    if(!grip || grip.disabled || !event.altKey || !['ArrowUp','ArrowDown'].includes(event.key))return;
    event.preventDefault();event.stopPropagation();
    const row=grip.closest('.agfocus-card'),rows=[...$('.agfocus-items',board).children].filter(node=>agfocusMoveEligible(p,node));
    const anchor=rows[rows.indexOf(row)+(event.key==='ArrowUp'?-1:1)];
    if(anchor)void agfocusMove(p,row.dataset.cardId,anchor.dataset.cardId,event.key==='ArrowUp'?'before_id':'after_id');
  };
  board.addEventListener('pointerdown',down);board.addEventListener('pointermove',move);board.addEventListener('pointerup',up);board.addEventListener('pointercancel',cancel);
  document.addEventListener('keydown',key);
  return ()=>{stop(true);board.removeEventListener('pointerdown',down);board.removeEventListener('pointermove',move);board.removeEventListener('pointerup',up);board.removeEventListener('pointercancel',cancel);document.removeEventListener('keydown',key);};
}

const AGFOCUS_SNOOZE_OPTIONS = [
  ['1h', '1h'], ['3h', '3h'], ['tomorrow', 'Tomorrow'], ['day-after', 'Day after tomorrow'],
  ['saturday', 'Saturday'], ['monday', 'Monday'], ['week', 'In a week'], ['two-weeks', 'In 2 weeks']
];
function agfocusSnoozeTime(option, now = new Date()) {
  if (option === '1h' || option === '3h') return new Date(now.getTime() + (option === '1h' ? 1 : 3) * 3600000);
  const today = sjDate(now), days = {tomorrow:1, 'day-after':2, week:7, 'two-weeks':14};
  if (Object.hasOwn(days, option)) return sjInstant(sjAddDays(today, days[option]), 6);
  const weekday = {saturday:6, monday:1}[option];
  if (weekday === undefined) throw new Error('Unknown snooze option');
  let offset = (weekday - new Date(today + 'T12:00:00Z').getUTCDay() + 7) % 7;
  let target = sjInstant(sjAddDays(today, offset), 6);
  if (target <= now) target = sjInstant(sjAddDays(today, offset + 7), 6);
  return target;
}
function agfocusSnoozePicker(p,node,anchor) {
  if(anchor.disabled || node._item.unavailable)return;
  p.focusSnoozePopup?.close();
  const saved=p.focusSnoozes.get(node.dataset.cardId);
  const popup=tControlPopover(anchor,`<div class="task-snooze-picker"><p class="ferr" role="alert" hidden></p>${saved?.submission?'<button type="button" class="quiet" data-focus-snooze-retry>Retry same postpone</button>':`${agfocusSnoozed(node._item)?'<button type="button" class="quiet" data-focus-snooze-clear>Return to Focus now</button>':''}<div class="task-snooze-shortcuts">${AGFOCUS_SNOOZE_OPTIONS.map(([key,label])=>`<button type="button" class="quiet" data-focus-snooze-preset="${key}" title="${key.endsWith('h') ? label + ' from now' : label + ' at 6 AM (San Juan time)'}">${label}</button>`).join('')}</div>`}<button type="button" class="quiet" data-focus-snooze-close>Close</button></div>`,'Postpone Focus item');
  if(!popup)return;
  popup.itemId=node.dataset.cardId;p.focusSnoozePopup=popup;
  popup.el.classList.add('agfocus-snooze-picker');popup.el.setAttribute('role','dialog');
  const choose=until=>{
    if(until!==null && (!Number.isFinite(until.getTime()) || until.getTime()<=Date.now())){
      const error=popup.el.querySelector('[role="alert"]');error.hidden=false;error.textContent='Choose a future date and time.';return;
    }
    void agfocusSnooze(p,node,until?.toISOString() ?? null,popup);
  };
  popup.el.querySelectorAll('[data-focus-snooze-preset]').forEach(button=>button.addEventListener('click',()=>choose(agfocusSnoozeTime(button.dataset.focusSnoozePreset))));
  popup.el.querySelector('[data-focus-snooze-clear]')?.addEventListener('click',()=>choose(null));
  popup.el.querySelector('[data-focus-snooze-retry]')?.addEventListener('click',()=>agfocusSnooze(p,node,null,popup,true));
  popup.el.querySelector('[data-focus-snooze-close]').onclick=()=>popup.close();
}
async function agfocusSnooze(p,node,until,popup,retry=false) {
  const id=node.dataset.cardId,mount=p.focusMount;
  let state=p.focusSnoozes.get(id);
  if(state?.pending || node._item.unavailable)return;
  if(!retry) {
    if(state?.submission)return;
    state={submission:{scope:'global',item_id:id,expected_card_revision:node._item.revision,changes:{deferred_until:until},idempotency_key:newRequestId()}};
    p.focusSnoozes.set(id,state);
  }
  if(!state?.submission)return;
  state.pending=true;
  for(const control of popup.el.querySelectorAll('button,input'))control.disabled=true;
  agfocusRowPending(p,node);
  let failure;
  try {
    const result=coreData(await post('/root/operations/focus_edit',state.submission));
    p.focusSnoozes.delete(id);popup.close();
    // The applied card is authoritative even if a following board read fails.
    if(p.focusMount===mount && result.item && !p.focusIncludedeferred && agfocusSnoozed(result.item)){
      p.focus.items=p.focus.items.filter(item=>item.id!==id);agfocusPaint(p);
    }
    agfocusAnnounce(p,until===null?'Returned to Focus.':'Focus item postponed.');
  } catch(error) {
    failure=error;
    if(['revision_conflict','validation_error','not_found','forbidden','policy_denied'].includes(error.code))p.focusSnoozes.delete(id);
  }
  await agfocusRefresh(p);state.pending=false;
  if(p.focusMount!==mount)return;
  agfocusRowPending(p,node);
  if(failure){
    p.focusError='Could not postpone Focus item. '+failure.message;agfocusAnnounce(p,p.focusError);
    // Reopening exposes either the same frozen retry or fresh controls after a conflict.
    if(popup.el.isConnected){popup.close();agfocusSnoozePicker(p,node,node.querySelector('[data-focus-snooze]'));}
  }
  agfocusPaint(p);
}

function agfocusMount(p) {
  const body = $('#agpad-body', p.el);
  body.innerHTML = `<div class="agfocus"><p class="agfocus-error" role="alert" hidden></p><button type="button" class="quiet" data-focus-retry-move hidden>Retry same move</button><p class="agfocus-sr" data-focus-announcement role="status" aria-live="polite"></p><span class="agfocus-sr" id="agfocus-move-instructions">Use Alt+ArrowUp or Alt+ArrowDown to move one visible row.</span>
    <header><div class="agfocus-heading"><div class="global-eyebrow">Focus</div><div class="agfocus-toolbar"><button type="button" class="quiet agfocus-icon" data-focus-add aria-label="Add to Focus" title="Add to Focus"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button><button class="quiet agfocus-icon" data-focus-refresh aria-label="Refresh Focus"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.6-1L20 9M4 15l2.3 3A7 7 0 0 0 18 17"/></svg></button><div class="agfocus-controls"><button class="quiet" data-focus-archived aria-pressed="false">Archived</button><button class="quiet" data-focus-deferred aria-pressed="false">Postponed</button></div></div></div><p class="agfocus-empty muted" role="status">Loading Focus…</p></header>
    <p class="agfocus-activity muted" hidden></p><div class="agfocus-layout"><div class="agfocus-items"></div><aside class="agfocus-dock" aria-label="Selected conversation" hidden><header class="agfocus-thread-heading"><span data-focus-thread-title></span><button type="button" class="quiet" data-focus-thread-close aria-label="Close thread"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header><div class="agfocus-dock-content"></div><div class="agfocus-thread-resize" role="separator" aria-orientation="horizontal" aria-label="Thread height" tabindex="0"></div></aside></div><div class="agfocus-retained" hidden></div><nav class="agfocus-pages" aria-label="Focus pages"><button class="quiet" data-focus-previous hidden>Previous</button><span class="muted" data-focus-page></span><button class="quiet" data-focus-more hidden>Next</button></nav></div>`;
  p.focusDrafts ||= new Map();
  p.focusCards = new Map();
  p.focusSelected = null;
  const board = $('.agfocus', body);
  p.focusMount = board;
  const storedHeight=Number(ls(AGFOCUS_THREAD_HEIGHT_KEY));
  p.focusThreadHeight=Number.isFinite(storedHeight) && storedHeight>=AGFOCUS_THREAD_MIN_HEIGHT ? storedHeight : 320;
  const disposeResize=agfocusThreadResizeMount(p,board,body);
  const disposeOrdering=agfocusOrderMount(p,board,body);
  p.focusSnoozes ||= new Map();
  p.focusOrderPending ||= false;
  p.focusMoveSubmission ||= null;
  $('[data-focus-retry-move]',board).onclick=()=>agfocusMove(p,null,null,null,true);
  $('[data-focus-thread-close]',board).onclick=()=>{
    const row=p.focusCards.get(p.focusSelected);
    agfocusSelect(p,null);
    row?.focus({preventScroll:true});
  };
  p.workspaceDispose=()=>{
    agfocusOverlayClose(p);disposeResize();disposeOrdering();p.focusSnoozePopup?.close();p.focusSnoozePopup=null;
    for(const node of p.focusCards.values()){node._conversationHost?._conversation?.dispose();node._referenceObserver?.disconnect();}
    p.focusCards.clear();p.focusSelected=null;p.focusMount=null;
  };
  p.focusPrivateUnread ||= new Map();
  // Cards acknowledged locally, each with the board-request counter at the time of
  // the acknowledgment. Only a response from a request that started afterwards can
  // mark that card unread again, so a read already in flight cannot revive the dot.
  p.focusPrivateRead ||= new Map();
  p.focusBoardSeq ||= 0;
  p.focusPages ||= [];
  $('[data-focus-add]',p.el).onclick=()=>agfocusAdd(p);
  $('[data-focus-refresh]',p.el).onclick=()=>agfocusRefresh(p);
  for(const kind of ['archived','deferred']) {
    const toggle=$('[data-focus-'+kind+']',p.el);
    toggle.setAttribute('aria-pressed',String(!!p['focusInclude'+kind]));
    toggle.onclick=()=>{p['focusInclude'+kind]=!p['focusInclude'+kind];toggle.setAttribute('aria-pressed',String(p['focusInclude'+kind]));p.focusPages=[];p.focusOffset=0;agfocusRefresh(p);};
  }
  $('[data-focus-more]',p.el).onclick=()=>{if(p.focusPending)return;p.focusPages.push(p.focusOffset||0);p.focusOffset=p.focus.next_offset;agfocusRefresh(p);};
  $('[data-focus-previous]',p.el).onclick=()=>{if(p.focusPending)return;p.focusOffset=p.focusPages.pop()||0;agfocusRefresh(p);};
  agfocusPaint(p);
  agfocusOpenLinkedItem(p);
}

function agfocusTick(p) {
  if (p.view !== 'focus') return;
  if (!p.focusPending && Date.now() >= (p.nextFocus || 0)) agfocusRefresh(p);
  if (p.view === 'focus') agfocusPaint(p);
}

async function agfocusRefresh(p) {
  if (!agpadActive(p)) return;
  if (p.focusPending) { p.focusRefreshAgain = true; return p.focusRequest; }
  p.focusPending = true;
  const focusMount=p.focusMount;
  const requestSeq = p.focusBoardSeq = (p.focusBoardSeq || 0) + 1;
  p.focusRequest = (async () => {
    const pageKey=agfocusPageKey(p);
    try {
      const data = coreData(await post("/root/operations/focus_get",{scope:"global",include_archived:!!p.focusIncludearchived,include_deferred:!!p.focusIncludedeferred,offset:p.focusOffset||0}));
      if (!agpadActive(p) || pageKey!==agfocusPageKey(p) || (focusMount && p.focusMount!==focusMount)) {
        if(agpadActive(p) && p.view==='focus')p.focusRefreshAgain=true;
        return;
      }
      if (!Array.isArray(data.items) || !Number.isInteger(data.revision)) throw new Error('Invalid Focus response');
      p.focusPageKey=pageKey;
      p.focus = data; p.focusError = '';
      for(const [id,seq] of p.focusPrivateRead || [])if(requestSeq>seq)p.focusPrivateRead.delete(id);
      for(const [id,seq] of p.focusPrivateUnread || [])
        if(requestSeq>seq && !data.items.some(item=>item.id===id && item.private_unread))p.focusPrivateUnread.delete(id);
      p.focusRefreshThreads=true;
      await Promise.all(agfocusVisibleItems(p,data.items).filter(item=>item.source?.type==='story' && !item.unavailable && p.focusStories?.get(item.id)?.expanded).map(item=>agfocusToggleStory(p,item,true)));
    } catch (error) {
      if (agpadActive(p) && (!focusMount || p.focusMount===focusMount)) p.focusError = 'Could not refresh Focus. ' + error.message;
      else if(agpadActive(p) && p.view==='focus')p.focusRefreshAgain=true;
    } finally {
      p.focusPending = false; p.nextFocus = pageKey===agfocusPageKey(p)?Date.now() + AGFOCUS_POLL_MS:0;
      if (agpadActive(p)) agfocusPaint(p);
      if (p.focusRefreshAgain) { p.focusRefreshAgain = false; await agfocusRefresh(p); }
    }
  })();
  return p.focusRequest;
}

function agfocusVisibleItems(p,items) {
  const result=[];
  function append(item,ancestors=new Set()) {
    result.push(item);
    const key=item.source?.type==='story'?item.source.id:null;
    if(!key || item.unavailable || ancestors.has(key))return;
    const branch=p.focusStories?.get(item.id);
    if(!branch?.expanded)return;
    for(const child of branch.items || [])append(child,new Set([...ancestors,key]));
  }
  for(const item of items)append(item);
  return result;
}
async function agfocusToggleStory(p,item,refresh=false) {
  p.focusStories ||= new Map();
  let branch=p.focusStories.get(item.id);
  if(branch?.expanded && !refresh){branch.expanded=false;agfocusPaint(p);return;}
  branch={expanded:true,items:refresh?(branch?.items || []):[],references:refresh?(branch?.references || []):[],showReferences:branch?.showReferences || false,pending:true};p.focusStories.set(item.id,branch);
  agfocusPaint(p);agfocusAnnounce(p,'Loading Story items…');
  const source=item.resolved_source || item.source,biz=source.business_slug,mount=p.focusMount;
  try {
    const data=coreData(await post(`/biz/${encodeURIComponent(biz)}/operations/stories_get`,{story_id:source.id}));
    if(p.focusStories.get(item.id)!==branch || p.focusMount!==mount || !mount?.isConnected)return;
    const seen=new Set();
    const links=[...(data.story.links || []),...(item.references || []).map(ref=>({
      resource_type:ref.resource_type || ({task:'tasks',story:'stories',agent:'agent_profiles'})[ref.type] || ref.type,resource_id:ref.id || ref.url,current:ref.unavailable?null:ref,focusReference:ref
    }))];
    const children=links.filter(link=>{
      if(link.resource_type==='agent_profiles' || link.current?.type==='agent' || link.item?.type==='agent')return false;
      const businessId=link.focusReference?link.focusReference.business_id:source.business_id;
      const key=String(businessId ?? 'global')+':'+link.resource_type+':'+link.resource_id;
      if(seen.has(key) || link.resource_id===source.id)return false;
      seen.add(key);return true;
    }).map(link=>{
      const type=({tasks:'task',stories:'story',signoffs:'signoff',email_conversations:'email'})[link.resource_type] || link.item?.type || 'reference';
      const current=link.current;
      const scope=link.focusReference || source;
      const target={...current,type,resource_type:link.resource_type,id:link.resource_id,business_id:scope.business_id ?? null,business_slug:scope.business_slug ?? null,business_name:scope.business_name};
      return {id:`${item.id}/${link.resource_type}/${link.resource_id}`,revision:current?.revision || 1,
        label:current?.label || 'Unavailable item',source:target,resolved_source:target,
        state:current?.state,source_revision:current?.revision,unavailable:!current,
        conversation:['task','story','signoff','email'].includes(type)?{type,id:link.resource_id,business_id:target.business_id}:null,
        references:[],_storyParent:item.id,_storyBusinessId:source.business_id,_depth:(item._depth || 0)+1,_storyLink:link};
    });
    branch.items=children.filter(child=>['task','story'].includes(child.source.type));
    branch.references=children.filter(child=>!['task','story'].includes(child.source.type));
    branch.pending=false;agfocusAnnounce(p,branch.items.length?`${branch.items.length} Story items`:'No linked Story items');
    agfocusPaint(p);
  } catch(error) {
    branch.expanded=false;branch.pending=false;p.focusError='Could not load Story items. '+error.message;agfocusPaint(p);
  }
}

function agfocusStoryReferences(p,node,item) {
  node._referenceObserver?.disconnect();
  node.querySelector('.agfocus-story-references')?.remove();
  const branch=p.focusStories?.get(item.id);
  if(!branch?.expanded || !branch.references?.length)return;
  const row=document.createElement('div');row.className='agfocus-story-references';row.setAttribute('aria-label','Story references');
  node.querySelector('.agfocus-row').after(row);
  const icons={file:'M14 3H5v18h14V8z M14 3v5h5 M8 13h8 M8 17h8',table:'M3 4h18v16H3z M3 10h18 M9 4v16',link:'M10 14l4-4 M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0 M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0'};
  const chips=branch.references.map(ref=>{
    const target=ref.resolved_source,kind=target.resource_type==='data_tables'?'table':target.resource_type==='documents' || target.path?'file':'link';
    const chip=document.createElement('button');chip.type='button';chip.className='quiet agfocus-reference-chip';chip.title=ref.label;chip.disabled=ref.unavailable;
    chip.innerHTML=`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="${icons[kind]}"/></svg><span></span>`;
    chip.querySelector('span').textContent=ref.label;chip.dataset.kind=kind;
    chip.onclick=event=>{
      event.stopPropagation();
      if(target.resource_type==='documents'){
        const query=new URLSearchParams({document_id:target.id});if(target.business_id)query.set('business_id',target.business_id);
        window.open('/files/view?'+query,'_blank','noopener');
      } else agfocusOpenItem(p,target).catch(e=>toast(e.message));
    };
    row.append(chip);return chip;
  });
  const more=document.createElement('button');more.type='button';more.className='quiet agfocus-reference-more';row.append(more);
  const fit=()=>{
    if(!row.isConnected)return;
    row.classList.toggle('expanded',branch.showReferences);
    chips.forEach(chip=>chip.hidden=false);more.hidden=true;
    const width=row.clientWidth;
    const widths=chips.map(chip=>chip.getBoundingClientRect().width);
    if(branch.showReferences){more.hidden=false;more.textContent='Less';more.setAttribute('aria-expanded','true');return;}
    if(widths.reduce((a,b)=>a+b+4,0)-4<=width)return;
    let used=0,count=0;
    for(const chipWidth of widths){if(used+chipWidth+64>width)break;used+=chipWidth+4;count++;}
    chips.forEach((chip,index)=>chip.hidden=index>=count);
    more.hidden=false;more.textContent=`+${chips.length-count} more`;more.setAttribute('aria-expanded','false');
  };
  more.onclick=event=>{event.stopPropagation();branch.showReferences=!branch.showReferences;fit();};
  node._referenceObserver=new ResizeObserver(fit);node._referenceObserver.observe(row);fit();
}

function agfocusPaint(p) {
  const data = p.focus && {...p.focus,items:agfocusVisibleItems(p,p.focus.items)};
  const attention = data?.browser?.state === 'pending' || data?.items.some(item => item.needs_input);
  const tab = $('[data-v="focus"]', p.el);
  tab?.classList.toggle('agfocus-attention', !!attention);
  if (tab) tab.removeAttribute('title');
  agfocusDesktopTab(p);
  if (p.view !== 'focus' || p.focusDetail) return;
  const host = $('.agfocus', p.el);
  if (!host) return;
  const error = $('.agfocus-error', host);
  agfocusText(error, p.focusError || ''); error.hidden = !p.focusError;
  $('[data-focus-retry-move]',host).hidden=!p.focusMoveSubmission || p.focusOrderPending;
  if (!data) return;
  const empty=$('.agfocus-empty',host);
  empty.hidden=!!data.items.length;
  agfocusText(empty,'Nothing on Focus yet');
  const activity = $('.agfocus-activity', host), a = data.activity;
  activity.hidden = !a;
  if (a) {
    const running = a.phase === 'running' && Date.parse(a.until) > Date.now();
    agfocusText(activity, running ? `Browser · ${a.command} running` :
      `Browser · ${a.command} ${a.phase === 'running' ? 'outcome unknown' : a.phase} · ${agpadInboxAge(a.at)}`);
  }
  const list = $('.agfocus-items', host);
  host.dataset.revision=data.revision;
  const signature=JSON.stringify([data.items,p.focusPageKey,data.next_offset,[...(p.focusStories || [])].map(([id,b])=>[id,b.expanded,b.references])]);
  if(list.dataset.signature===signature) {
    for(const item of data.items) {
      agfocusUnread(p,p.focusCards.get(item.id),item);
      agfocusRowPending(p,p.focusCards.get(item.id));
    }
    agfocusRefreshThreads(p);
    agfocusThreadHeight(p);
    agfocusDesktopTab(p);return;
  }
  list.dataset.signature=signature;
  const focused=document.activeElement,selection=focused?.selectionStart!=null?[focused.selectionStart,focused.selectionEnd]:null;
  const ids=new Set(data.items.map(item=>item.id));
  if(p.focusSelected && !ids.has(p.focusSelected))agfocusSelect(p,null);
  const retained=$('.agfocus-retained',host);
  for(const node of [...list.children])if(!ids.has(node.dataset.cardId))retained.appendChild(node);
  let position=0;
  for(const item of data.items) {
    let node=p.focusCards.get(item.id);
    if(node?.dataset.unavailable && !item.unavailable){node.remove();p.focusCards.delete(item.id);node=null;}
    if(!node) {
      node=document.createElement('article');node.className='agfocus-card';node.dataset.cardId=item.id;
      node.innerHTML='<div class="trow agfocus-row"><span class="agfocus-marker"></span><span class="task-business-column agfocus-scope"></span><div class="task-headline"><p class="ttl"></p><span class="agfocus-unread" aria-hidden="true" hidden></span><span class="agfocus-flag" hidden></span></div><div class="agfocus-actions"><button type="button" class="quiet agfocus-grip" data-focus-grip aria-describedby="agfocus-move-instructions"><svg viewBox="0 0 16 20" width="12" height="15" fill="currentColor" aria-hidden="true"><path d="M4 4h2v2H4zm6 0h2v2h-2zM4 9h2v2H4zm6 0h2v2h-2zM4 14h2v2H4zm6 0h2v2h-2z"/></svg></button><button type="button" class="quiet" data-manage aria-label="Open details"><svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg></button><button type="button" class="quiet" data-focus-snooze aria-label="Snooze Focus item" aria-haspopup="dialog" aria-expanded="false"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></svg></button></div></div><div class="agfocus-region" hidden><div class="agfocus-thread-context" aria-label="Task details and context"><p class="agfocus-source-title"></p><div class="agfocus-task-details"></div><div class="agfocus-framing"><section data-framing="requested_response"><p class="agfocus-request"></p></section></div><p class="agfocus-context muted"></p><div class="agfocus-related"></div></div><div class="agfocus-conversation"></div></div>';
      node._region=node.querySelector('.agfocus-region');
      node._region.dataset.focusThreadId=item.id;
      node._conversationHost=node.querySelector('.agfocus-conversation');
      p.focusCards.set(item.id,node);
      list.appendChild(node);
    }
    if(list.children[position]!==node)list.insertBefore(node,list.children[position]||null);
    position++;
    node._item=item;
    agfocusStoryReferences(p,node,item);
    node.classList.toggle('agfocus-story-child',!!item._storyParent);
    node.style.setProperty('--focus-depth',item._depth || 0);
    if(item.unavailable) {
      if(p.focusSelected===item.id)agfocusSelect(p,null);
      node._conversationHost?._conversation?.dispose();
      node._region.replaceChildren();
      node.dataset.unavailable='true';node.classList.add('agfocus-unavailable','agfocus-collapsed');
      node.removeAttribute('tabindex');node.removeAttribute('aria-expanded');node.onclick=null;node.onkeydown=null;
      node.setAttribute('aria-label','Unavailable Focus item');
      p.focusSnoozePopup?.itemId===item.id && p.focusSnoozePopup.close();
      for(const el of node.querySelectorAll('.agfocus-scope,.agfocus-marker,.agfocus-actions,.agfocus-unread')) {el.replaceChildren();delete el._html;}
      node.querySelector('.agfocus-unread').hidden=true;
      node.querySelector('.ttl').textContent='This item is no longer available.';
      const flag=node.querySelector('.agfocus-flag');agfocusText(flag,'Unavailable');flag.hidden=false;
      continue;
    }
    const name=item.display_name?.trim() || item.label;
    const taskItem=item.source?.type==='task';
    const taskRecord=taskItem?agfocusTaskRecord(item):null;
    const taskState=taskRecord?.status || item.state || 'open';
    const marker=node.querySelector('.agfocus-marker');
    // Keep the state button alive across polling paints, including mid-click.
    if(taskItem){
      if(!marker.querySelector('.agfocus-task-state'))marker.innerHTML=`<button type="button" class="ball ${esc(taskState)} agfocus-task-state" aria-label="${esc(T_LABEL[taskState] || taskState)}"><span class="core"></span></button>`;
    } else if(item.source?.type==='story') {
      agfocusHtml(marker,`<button type="button" class="quiet agfocus-story-toggle" aria-label="Expand Story items"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 4h6v6H4zM14 14h6v6h-6zM7 10v7h7"/></svg></button>`);
      const toggle=marker.querySelector('button'),expanded=!!p.focusStories?.get(item.id)?.expanded;
      toggle.setAttribute('aria-expanded',String(expanded));toggle.setAttribute('aria-label',expanded?'Collapse Story items':'Expand Story items');
      toggle.onclick=event=>{event.stopPropagation();agfocusToggleStory(p,item);};
    } else agfocusHtml(marker,`<span class="sdot ${T_STATES.includes(item.state)?esc(item.state):''}" aria-hidden="true"></span>`);
    agfocusText(node.querySelector('.ttl'),name);
    agfocusContext(node,item);
    const flag=node.querySelector('.agfocus-flag');
    const snoozed=agfocusSnoozed(item);
    node.classList.toggle('agfocus-snoozed',snoozed);
    agfocusText(flag,item.archived?'Archived':'');flag.hidden=!item.archived;
    agfocusText(node._region.querySelector('.agfocus-source-title'),item.resolved_source?.label || item.label);
    node._region.querySelector('.agfocus-request').textContent=item.requested_response || '';
    node._region.querySelector('[data-framing="requested_response"]').hidden=!item.requested_response;
    node._region.querySelector('.agfocus-context').textContent=item.context_label?'Conversation: '+item.context_label:'';
    const actions=node.querySelector('.agfocus-actions');
    node.tabIndex=0;node.setAttribute('role','group');node.setAttribute('aria-label',name);
    // The row owns selection; conversation controls live in the dock and never toggle it.
    const toggleCard=()=>{
      if(item._storyLink && !item.conversation){open();return;}
      agfocusSelect(p,p.focusSelected===item.id?null:item.id);
      if(p.focusSelected===item.id)node._conversationHost.querySelector('.conversation-inline-compose textarea')?.focus({preventScroll:true});
    };
    node.onclick=event=>{
      if(event.composedPath().some(el=>el.matches?.('button,a,input,textarea,select,.agfocus-marker,.agfocus-conversation')) || Date.now()<(p.focusSuppressClickUntil || 0))return;
      if((event.ctrlKey || event.metaKey) && !item._storyParent){event.preventDefault();agfocusNewTab(item);return;}
      if(event.target.closest('.agfocus-row'))toggleCard();
    };
    node.onkeydown=event=>{if(event.target===node && ['Enter',' '].includes(event.key)){event.preventDefault();toggleCard();}};
    const open=()=>agfocusOpenItem(p,item.resolved_source || item.opening_target).catch(e=>toast(e.message));
    if(taskItem){
      const ball=node.querySelector('.agfocus-task-state');
      taskStateBind(ball, (item.resolved_source || item.source).business_slug || null, taskRecord, snapshot => {
        item.state = snapshot.task.status; item.source_revision = snapshot.task.revision;
      });
      ball.onclick=event=>{event.stopPropagation();agfocusTaskState(p,item,ball);};
      ball.oncontextmenu=event=>{event.preventDefault();event.stopPropagation();agfocusTaskState(p,item,ball,true);};
    }
    actions.querySelector('[data-manage]').onclick=event=>{event.stopPropagation();if((event.ctrlKey || event.metaKey) && !item._storyParent)agfocusNewTab(item);else open();};
    const grip=actions.querySelector('[data-focus-grip]');grip.setAttribute('aria-label','Move '+name);
    actions.querySelector('[data-focus-snooze]').onclick=event=>{event.stopPropagation();agfocusSnoozePicker(p,node,event.currentTarget);};
    actions.querySelector('[data-focus-snooze]').setAttribute('aria-pressed',String(snoozed));
    agfocusRowPending(p,node);
    grip.hidden=!!item._storyParent;actions.querySelector('[data-focus-snooze]').hidden=!!item._storyParent;
    const related=node._region.querySelector('.agfocus-related');related.replaceChildren();
    for(const ref of item.references||[]) {
      const button=document.createElement('button');button.className='quiet';button.textContent=ref.unavailable?'Related item unavailable':ref.label;button.disabled=!!ref.unavailable;
      button.onclick=()=>agfocusOpenItem(p,ref).catch(e=>toast(e.message));related.appendChild(button);
    }
    const binding=item.conversation;
    if(!binding){node.classList.add('agfocus-collapsed');continue;}
    const conversationHost=node._conversationHost;
    const conversationBinding=JSON.stringify([binding,item.resolved_source?.business_slug || item.context_business_slug]);
    if(node._conversationBinding!==conversationBinding){conversationHost._conversation?.dispose();node._conversationBinding=conversationBinding;}
    const newConversation=!conversationHost._conversation;
    const conversation=conversationMount(conversationHost,{type:binding.type,id:binding.id,biz:item.resolved_source?.business_slug || item.context_business_slug || null,revision:item.source_revision},{
      inlineComposer:true,
      dismissOnOutside:false,
      isVisible:()=>p.focusMount===host && !host.hidden && p.focusSelected===item.id && p.view==='focus' && agpadActive(p),
      // Focus is a Global projection: the private channel names the owning
      // business inside the canonical source instead of switching routes.
      privateGlobal:true,
      privateSource:{type:binding.type,id:binding.id,...(binding.business_id?{business_id:binding.business_id}:{})},
      onUnavailable:()=>{if(p.focusMount!==host || p.focusCards.get(item.id)!==node)return;node._item.unavailable=true;const current=p.focus?.items.find(other=>other.id===item.id);if(current)current.unavailable=true;agfocusPaint(p);},
      onRead:()=>{agfocusUnread(p,node,{...node._item,private_unread:false},false);item.private_unread=false;p.focusPrivateUnread?.delete(item.id);p.focusPrivateRead?.set(item.id,p.focusBoardSeq || 0);const current=p.focus?.items.find(other=>other.id===item.id);if(current)current.private_unread=false;},
      onUnread:()=>{agfocusUnread(p,node,{...node._item,private_unread:true},true);item.private_unread=true;p.focusPrivateUnread?.set(item.id,p.focusBoardSeq || 0);p.focusPrivateRead?.delete(item.id);const current=p.focus?.items.find(other=>other.id===item.id);if(current)current.private_unread=true;},
      ...(!item._storyParent?{read:async pagination=>coreData(await post('/root/operations/focus_get',{scope:'global',item_id:item.id,conversation:true,...pagination})).item.thread,
      send:async request=>coreData(await post('/root/operations/focus_reply',{scope:'global',item_id:item.id,text:request.text,idempotency_key:request.idempotency_key,...(request.revision?{expected_source_revision:request.revision}:{})}))}:{}),
    });
    // Focus has no actionable Comment toggle. Its title carries unread state.
    if(newConversation)conversationHost.querySelector('.conversation-toggle')?.remove();
    agfocusUnread(p,node,item);
    if(newConversation)conversation.preload();
    node.classList.toggle('agfocus-collapsed',p.focusSelected!==item.id);
    node.setAttribute('aria-expanded',String(p.focusSelected===item.id));
    if(p.focusSelected===item.id){agfocusText($('[data-focus-thread-title]',host),name);if(!conversation.isExpanded())conversation.expand();}
  }
  if(focused?.isConnected && host.contains(focused)){focused.focus({preventScroll:true});if(selection)focused.setSelectionRange(...selection);}
  agfocusRefreshThreads(p);
  agfocusThreadHeight(p);
  $('[data-focus-more]',host).hidden=data.next_offset==null;
  $('[data-focus-previous]',host).hidden=!p.focusPages?.length;
  $('[data-focus-page]',host).textContent=(p.focusPages?.length || data.next_offset!=null)?'Page '+((p.focusPages?.length||0)+1):'';
  agfocusDesktopTab(p);
}

function agfocusNewTab(item) {
  const url=new URL(location.href);
  url.searchParams.set('focus_item',item.id);url.hash='/global/agents';
  window.open(url.href,'_blank','noopener');
}

async function agfocusOpenLinkedItem(p) {
  const url=new URL(location.href),id=url.searchParams.get('focus_item');
  if(!id)return;
  url.searchParams.delete('focus_item');history.replaceState(null,'',url);
  try {
    const data=coreData(await post('/root/operations/focus_get',{scope:'global',item_id:id}));
    if(!agpadActive(p) || p.view!=='focus')return;
    if(!data.item || data.item.unavailable)throw new Error('This Focus item is no longer available.');
    await agfocusOpenItem(p,data.item.resolved_source || data.item.opening_target);
  } catch(error){if(agpadActive(p)){p.focusError=error.message;agfocusPaint(p);}}
}

function agfocusSelect(p,id) {
  const board=p.focusMount;
  if(!board?.isConnected || id===p.focusSelected)return;
  const next=id && p.focusCards.get(id);
  if(id && (!next || next._item?.unavailable))return;
  const previous=p.focusCards.get(p.focusSelected);
  p.focusSelected=id;
  if(previous){
    previous._conversationHost?._conversation?.collapse();
    previous._region.hidden=true;previous.appendChild(previous._region);
    previous.classList.add('agfocus-collapsed');previous.setAttribute('aria-expanded','false');
  }
  const dock=$('.agfocus-dock',board);
  dock.hidden=!next;$('.agfocus-layout',board).classList.toggle('agfocus-thread-open',!!next);
  agfocusOverlayClose(p);
  if(next){
    agfocusOverlayOpen(p,board,dock);
    agfocusText($('[data-focus-thread-title]',dock),next._item.display_name?.trim() || next._item.label);
    $('.agfocus-dock-content',dock).appendChild(next._region);next._region.hidden=false;
    next.classList.remove('agfocus-collapsed');next.setAttribute('aria-expanded','true');
    agfocusThreadHeight(p);
    next._conversationHost._conversation?.expand();
    agfocusLoadThreadContext(p,next);
  }
}

async function agfocusLoadThreadContext(p,node) {
  const item=node._item,source=item.resolved_source || item.source || item.context;
  const target=node._region.querySelector('.agfocus-task-details');
  if(!source || !['task','story'].includes(source.type) || !target)return;
  const request={};node._contextRequest=request;
  target.textContent='Loading details…';
  const biz=source.business_slug || item.context_business_slug;
  try {
    const operation=source.type==='story'?'stories_get':'tasks_get';
    const data=coreData(await post(biz?`/biz/${encodeURIComponent(biz)}/operations/${operation}`:`/root/operations/${operation}`,
      {[source.type+'_id']:source.id,...(!biz?{scope:'global'}:{})}));
    if(node._contextRequest!==request || node._item.unavailable || p.focusSelected!==item.id)return;
    const task=data.task || data.story;
    if(source.type==='story'){
      target.textContent=[task.purpose,task.situation].filter(Boolean).join('\n\n');
      return;
    }
    const details=[task.status && (T_LABEL[task.status] || task.status),task.due && `Due ${task.due}`,
      ...(task.tags || []),...Object.entries(task.fields || {}).map(([key,value])=>`${key}: ${String(value)}`)].filter(Boolean);
    target.textContent=details.join(' · ');
    if(task.title)agfocusText(node._region.querySelector('.agfocus-source-title'),task.title);
  } catch(error) {
    if(node._contextRequest===request && p.focusSelected===item.id)target.textContent='Details unavailable. '+error.message;
  }
}

function agfocusRefreshThreads(p) {
  if(!p.focusRefreshThreads)return;
  p.focusRefreshThreads=false;
  for(const item of agfocusVisibleItems(p,p.focus?.items || []))p.focusCards.get(item.id)?._conversationHost?._conversation?.refresh();
}

function agfocusThreadHeight(p,height) {
  const board=p.focusMount,dock=board && $('.agfocus-dock',board);
  if(!dock || dock.hidden || board.hidden)return;
  if(p.focusOverlay){p.focusOverlay.fit();return;}
  const body=$('#agpad-body',p.el),bodyBox=body.getBoundingClientRect(),layoutBox=$('.agfocus-layout',board).getBoundingClientRect();
  const available=Math.floor(Math.min(innerHeight,bodyBox.bottom)-Math.max(bodyBox.top,layoutBox.top)-4);
  // The pane scroll container makes even a shorter-than-minimum viewport reachable.
  const maximum=Math.max(AGFOCUS_THREAD_MIN_HEIGHT,available);
  const actual=Math.min(maximum,Math.max(AGFOCUS_THREAD_MIN_HEIGHT,height ?? p.focusThreadHeight));
  p.focusThreadActual=actual;dock.style.height=actual+'px';
  const handle=$('.agfocus-thread-resize',dock);
  handle.setAttribute('aria-valuemin',String(AGFOCUS_THREAD_MIN_HEIGHT));
  handle.setAttribute('aria-valuemax',String(maximum));
  handle.setAttribute('aria-valuenow',String(Math.round(actual)));
  handle.setAttribute('aria-valuetext',Math.round(actual)+' pixels');
  if(height!==undefined){p.focusThreadHeight=actual;ls(AGFOCUS_THREAD_HEIGHT_KEY,String(actual));}
}

function agfocusThreadResizeMount(p,board,body) {
  const handle=$('.agfocus-thread-resize',board);
  let drag=null,frame=null;
  const refresh=()=>{if(frame==null)frame=requestAnimationFrame(()=>{frame=null;agfocusThreadHeight(p);});};
  const begin=event=>{
    if(event.button!==0)return;
    event.preventDefault();handle.focus({preventScroll:true});
    drag={id:event.pointerId,y:event.clientY,height:p.focusThreadActual};handle.setPointerCapture(event.pointerId);
  };
  const move=event=>{if(drag && drag.id===event.pointerId)agfocusThreadHeight(p,drag.height+event.clientY-drag.y);};
  const finish=event=>{if(drag?.id!==event.pointerId)return;if(handle.hasPointerCapture(event.pointerId))handle.releasePointerCapture(event.pointerId);drag=null;};
  const key=event=>{if(!['ArrowUp','ArrowDown'].includes(event.key))return;event.preventDefault();agfocusThreadHeight(p,p.focusThreadActual+(event.key==='ArrowUp'?-20:20));};
  handle.addEventListener('pointerdown',begin);handle.addEventListener('pointermove',move);
  handle.addEventListener('pointerup',finish);handle.addEventListener('pointercancel',finish);handle.addEventListener('keydown',key);
  const observer=new ResizeObserver(refresh);observer.observe(body);observer.observe(board);
  window.addEventListener('resize',refresh);body.addEventListener('scroll',refresh);
  return ()=>{
    observer.disconnect();window.removeEventListener('resize',refresh);body.removeEventListener('scroll',refresh);
    if(frame!=null)cancelAnimationFrame(frame);
    if(drag && handle.hasPointerCapture(drag.id))handle.releasePointerCapture(drag.id);
    handle.removeEventListener('pointerdown',begin);handle.removeEventListener('pointermove',move);
    handle.removeEventListener('pointerup',finish);handle.removeEventListener('pointercancel',finish);handle.removeEventListener('keydown',key);
  };
}

function agfocusTaskRecord(item) {
  const source=item.resolved_source || item.source;
  return taskStateObserve(source.business_slug || null, {id:source.id,status:item.state || source.state || 'open',revision:item.source_revision});
}
function agfocusTaskState(p,item,button,block=false) {
  const source=item.resolved_source || item.source,task=agfocusTaskRecord(item);
  const status=block ? (task.status==='blocked'?'open':'blocked') : tNext(task.status);
  return taskStateSet(source.business_slug || null,task,status);
}

function agfocusHtml(el,html) { if(el && el._html!==html){el._html=html;el.innerHTML=html;} }
function agfocusContext(node,item) {
  const source=item.resolved_source || item.context || item.conversation || {};
  const biz=source.business_slug || item.context_business_slug;
  const businessId=source.business_id ?? item.source?.business_id ?? item.context?.business_id;
  const scope=node.querySelector('.agfocus-scope');
  scope.hidden=!!item._storyParent && item.source?.type==='task' && !!item._storyBusinessId && businessId===item._storyBusinessId;
  const businessName=source.business_name || (state.bizs || []).find(b=>b.slug===biz)?.name || 'Business';
  agfocusHtml(node.querySelector('.agfocus-scope'),businessId || biz
    ? `<span class="agfocus-scope-chip" aria-label="${esc(businessName)}">${esc(businessName)}</span>`
    : '<span class="agfocus-scope-chip agfocus-global-chip" role="img" aria-label="Global"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></svg></span>');
}

// Ordubashy has one Browser entry for both modes, so desktop input requests mark it.
function agfocusDesktopTab(p) {
  const tab = p.workspaceRole === 'ordubashy' ? $('[data-v="browser"]', p.el) : null;
  if (!tab) return;
  const pending = p.focus?.browser?.state === 'pending';
  tab.dataset.attention = pending ? 'true' : 'false';
  tab.setAttribute('aria-label', pending ? 'Browser, desktop input requested' : 'Browser');
  tab.title = pending ? 'Ordubashy is waiting for desktop input' : 'Browser';
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
    const availableHeight = Math.max(0, bodyBox.bottom - bottomPadding - frameBox.top - scrollOffset - footerHeight - (completion ? 10 : 0));
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

function agfocusDesktopError(p, body, message) {
  body.innerHTML = '<section class="agfocus-desktop-view"><div class="agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') +
    '<span class="agbrowser-actions">' + agbrowserReconnectButton('data-desktop-retry') + '</span></div><p class="ferr" role="alert">' + esc(message) + '</p></section>';
  $('[data-desktop-retry]', body).onclick = () => agfocusDesktop(p, true);
  agbrowserWireMode(p, body);
}

// Ordubashy's full desktop: its explicit desktop binding, or core's validated
// DESKTOP_CONTROL_URL fallback. Focus supplies only the pending-input request.
async function agfocusDesktop(p, reconnect = false) {
  const body = $('#agpad-body', p.el);
  if (!body || !agpadActive(p) || p.view !== 'desktop') return;
  const request = p.desktopRequest = (p.desktopRequest || 0) + 1;
  p.workspaceDispose?.();
  p.workspaceDispose = null;
  body.innerHTML = '<section class="agfocus-desktop-view"><div class="agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') + '</div><p class="muted" role="status">Connecting to Ordubashy’s desktop…</p></section>';
  agbrowserWireMode(p, body);
  let data, surfaces;
  try {
    [data, surfaces] = await Promise.all([api(agfocusPath(p)).then(coreData), agbrowserSurfaces(p, reconnect)]);
    if (!agpadActive(p) || p.view !== 'desktop' || p.desktopRequest !== request || !body.isConnected) return;
    p.focus = data; agfocusDesktopTab(p);
  } catch (error) {
    if (body.isConnected && p.view === 'desktop' && p.desktopRequest === request) agfocusDesktopError(p, body, error.message);
    return;
  }
  const surface = surfaces.desktop;
  const desktop = surface && agfocusDesktopURL(surface.url);
  if (!desktop) {
    body.innerHTML = '<section class="agfocus-desktop-view"><div class="agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') + '<span class="agbrowser-actions">' + agbrowserReconnectButton('data-desktop-retry') + '</span></div><p class="muted" role="status">Desktop is not configured. Set DESKTOP_CONTROL_URL in Global Settings or add Ordubashy’s desktop binding.</p></section>';
    $('[data-desktop-retry]', body).onclick = () => agfocusDesktop(p, true);
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
  body.innerHTML='<section class="agfocus-desktop-view"><div class="agfocus-desktop-help agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') + '<span class="agbrowser-actions">' + agbrowserReconnectButton('data-desktop-retry') + '</span></div><div class="agfocus-desktop-help agfocus-desktop-notes"></div><iframe class="agfocus-reference-frame agfocus-desktop-frame" title="Desktop control" referrerpolicy="no-referrer" allow="clipboard-read; clipboard-write; fullscreen"></iframe>' +
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
  $('[data-desktop-retry]', host).onclick = () => agfocusDesktop(p, true);
  agbrowserWireMode(p, host);
  const frame = $('.agfocus-desktop-frame', host);
  frame.dataset.surface = surface.id;
  frame.src = desktop.url;
  const disposers = [];
  p.workspaceDispose = () => { disposers.splice(0).forEach(fn => fn()); frame.src = 'about:blank'; frame.remove(); };
  agfocusDesktopFrameFit({host, onDispose: fn => disposers.push(fn)}, desktop);
  if (pending) {
    const input = $('.agfocus-note', host);
    input.value = draft.note;
    input.disabled = !!draft.submission;
    input.oninput = () => { draft.note = input.value; };
    $('.agfocus-done', host).onclick = () => agfocusDone(p, requestId, host, draft);
  }
}

function agfocusDesktopMount(p) {
  p.focusDrafts ||= new Map();
  agfocusDesktop(p).catch(error => {
    const body = $('#agpad-body', p.el);
    if (body && p.view === 'desktop') agfocusDesktopError(p, body, error.message);
  });
}

function agfocusDetail(p,title) {
  agfocusSelect(p,null);
  p.focusDetail?.dispose?.();
  const body=$('#agpad-body',p.el),board=$('.agfocus',body),scroll=body.scrollTop;
  const restore=document.activeElement;
  p.focusCards?.get(p.focusSelected)?._conversationHost?._conversation?.collapse();
  board.hidden=true;
  const shell=document.createElement('section');shell.className='agfocus-detail';
  shell.innerHTML='<header class="agfocus-detail-header"><button type="button" class="quiet" aria-label="Back to Focus">← Focus</button><span></span></header><div class="agfocus-detail-body"></div>';
  shell.querySelector('header span').textContent=title || '';
  body.appendChild(shell);body.scrollTop=0;
  let cleanup;
  const detail={host:shell.querySelector('.agfocus-detail-body'),current:()=>p.focusDetail===detail && shell.isConnected && p.view==='focus',
    dispose:()=>{cleanup?.();shell.remove();},
    onDispose:fn=>{cleanup=fn;},
    back:()=>{if(!detail.current())return;detail.dispose();p.focusDetail=null;board.hidden=false;body.scrollTop=scroll;agfocusPaint(p);p.focusCards?.get(p.focusSelected)?._conversationHost?._conversation?.expand();if(restore?.isConnected)restore.focus({preventScroll:true});}};
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
    if(item.type==='email' && item.business_slug) {
      await emailThreadMount(detail.host,item.business_slug,item.id,{active:detail.current});return;
    }
    if(item.type==='email' && !item.business_id) {
      await emailGlobalThreadMount(detail.host,item.id,{active:detail.current});return;
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
    if(item.resource_type==='data_tables'){await tablesOpen(item.business_slug,item.id);return;}
    if(item.resource_type==='products'){await productsOpen(item.business_slug,item.id);return;}
    if(item.resource_type==='documents') {
      const {document:doc}=coreData(await api(agfocusPath(p)+'/documents/'+encodeURIComponent(item.id)));
      if(detail.current())detail.host.innerHTML=`<div class="story-source">${esc(doc.body || 'No text preview available.')}</div>`;
      return;
    }
    detail.host.innerHTML=`<div class="story-source">${esc(item.summary || 'No preview available.')}</div>`;
  } catch(error) {
    if(detail.current())detail.host.innerHTML=`<p class="ferr" role="alert">${esc(error.message)}</p>`;
  }
}


// One atomic create-and-present call; a retry keeps the exact request and key.
function agfocusAdd(p) {
  let submission=null, pending=false;
  modal({title:'Add to Focus', body:`<form id="focus-add-form"><label>Title<input class="inp" name="title" required maxlength="200" autocomplete="off"></label><label>Link <span class="muted">(optional)</span><input class="inp" name="link" type="url" placeholder="https://…"></label><p class="muted">Leave the link empty to create a task.</p><p role="alert" class="bad" hidden></p></form>`,
    actions:[{label:'Cancel',onClick:close=>close()}, {label:'Add', primary:true, onClick:async(close,el)=>{
      if(pending)return;
      const form=el.querySelector('form'), error=form.querySelector('[role="alert"]');
      if(!submission) {
        if(!form.reportValidity())return;
        const title=form.elements.title.value.trim(),link=form.elements.link.value.trim();
        if(!title)return;
        if(link && !/^https?:\/\//i.test(link)){error.textContent='Use an HTTP or HTTPS link.';error.hidden=false;return;}
        const key=newRequestId();
        submission=link ? {operation:'focus_present',args:{scope:'global',reference:{key,title,target:{type:'url',url:link}},idempotency_key:key}}
          : {operation:'tasks_create',args:{scope:'global',title,focus:{display_name:title},idempotency_key:key}};
      }
      pending=true;error.hidden=true;
      form.querySelectorAll('input').forEach(input=>input.disabled=true);
      try {
        coreData(await post('/root/operations/'+submission.operation,submission.args));
        close();toast('Added to Focus');p.focusOffset=0;p.focusPages=[];await agfocusRefresh(p);
      } catch(err) {
        error.textContent=err.message;error.hidden=false;
        if(['validation_error','forbidden','policy_denied','not_found'].includes(err.code)){submission=null;form.querySelectorAll('input').forEach(input=>input.disabled=false);}
      } finally {pending=false;}
    }}],onOpen:el=>{el.querySelector('input').focus();el.querySelector('form').onsubmit=event=>{event.preventDefault();el.querySelector('.mfoot button:last-child')?.click();};}});
}


function agfocusOverlayClose(p) {
  if(!p.focusOverlay)return;
  p.focusOverlay.dispose();p.focusOverlay=null;
}
function agfocusOverlayOpen(p,board,dock) {
  const terminal=p.el.closest('.agsplit')?.querySelector('.global-agent-session, #chat');
  if(!terminal || terminal.getBoundingClientRect().width<240)return;
  const layout=board.querySelector('.agfocus-layout'),wasInert=terminal.inert;
  const fit=()=>{const rect=terminal.getBoundingClientRect();Object.assign(dock.style,{left:rect.left+'px',top:rect.top+'px',width:rect.width+'px',height:rect.height+'px'});};
  dock.classList.add('agfocus-chat-overlay');layout.classList.add('agfocus-overlay-open');
  dock.setAttribute('role','dialog');dock.setAttribute('aria-label','Task conversation');
  terminal.inert=true;
  const escape=event=>{if(event.key==='Escape' && !document.querySelector('.overlay')){event.preventDefault();event.stopPropagation();agfocusSelect(p,null);}};
  const observer=new ResizeObserver(fit);observer.observe(terminal);
  window.addEventListener('resize',fit);document.addEventListener('keydown',escape);
  p.focusOverlay={fit,dispose:()=>{observer.disconnect();window.removeEventListener('resize',fit);document.removeEventListener('keydown',escape);terminal.inert=wasInert;dock.classList.remove('agfocus-chat-overlay');layout.classList.remove('agfocus-overlay-open');dock.removeAttribute('role');dock.setAttribute('aria-label','Selected conversation');for(const key of ['left','top','width','height'])dock.style[key]='';}};
  fit();
}
