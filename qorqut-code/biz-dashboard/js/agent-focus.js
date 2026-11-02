// Shared row structure for Pinned/Focus and Attention. All visual rules stay in the Focus stylesheet.
function agfocusRowHtml(actions = '') {
  return `<div class="trow agfocus-row"><span class="agfocus-notification" aria-hidden="true"><span class="agfocus-unread" hidden></span></span><span class="agfocus-marker"></span><span class="task-business-column agfocus-scope"></span><div class="task-headline"><p class="ttl"></p><span class="agfocus-flag" hidden></span></div><div class="agfocus-actions">${actions}</div></div>`;
}

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
  const shared=typeof replyNotifications!=='undefined'?replyNotifications.unread(item.conversation || item.source):null;
  const unread=value ?? shared ?? (!!p.focusPrivateUnread?.has(item.id) || (!!item.private_unread && !p.focusPrivateRead?.has(item.id)));
  node.querySelector('.agfocus-unread').hidden=!unread;
  const name=item.display_name?.trim() || item.label;
  node.setAttribute('aria-label',name+(agfocusSnoozed(item)?', postponed':'')+(unread?', unread private reply':''));
}
function agfocusMoveEligible(p,node) {
  return !!node && node.parentElement?.classList.contains('agfocus-items') && !node._item?.unavailable && !node._item?._workParent && !node._item?.archived &&
    !p.focusSnoozes?.get(node.dataset.cardId)?.pending && node.querySelector('.agfocus-task-state')?.getAttribute('aria-busy')!=='true';
}
function agfocusRowPending(p,node) {
  if(!node || node._item?.unavailable)return;
  const snoozePending=!!p.focusSnoozes?.get(node.dataset.cardId)?.pending;
  node.dataset.moveDisabled=String(!!p.focusOrderPending || !!p.focusMoveSubmission || !agfocusMoveEligible(p,node));
  const snooze=node.querySelector('[data-focus-snooze]');
  snooze.disabled=snoozePending || !!p.focusOrderPending || !!node._item.archived;
  snooze.setAttribute('aria-busy',String(snoozePending));
}
const agfocusTextHost=()=>typeof focusText!=='undefined'?focusText:null;
// Rows that take part in ordering: movable Focus cards and text blocks.
const agfocusMoveRow=(p,id)=>p.focusCards.get(id)||p.focusTextNodes?.get(id)||null;
const agfocusRowId=node=>node.dataset.cardId||node.dataset.textId;
const agfocusMoveAble=(p,node)=>!!node && (node.dataset.textId?node.isConnected && node.parentElement?.classList.contains('agfocus-items'):agfocusMoveEligible(p,node));
const agfocusRowDisabled=(p,node)=>node.dataset.textId?agfocusTextHost().disabled(p,node):node.dataset.moveDisabled==='true';
function agfocusAnnounce(p,text) { agfocusText(p.focusMount?.querySelector('[data-focus-announcement]'),text); }

async function agfocusMove(p,id,anchorId,side,retry=false) {
  if(p.focusOrderPending)return;
  const mount=p.focusMount;
  if(!mount?.isConnected)return;
  if(!retry) {
    if(p.focusMoveSubmission)return;
    const row=agfocusMoveRow(p,id),anchor=agfocusMoveRow(p,anchorId),text=agfocusTextHost();
    if(id===anchorId || !agfocusMoveAble(p,row) || !agfocusMoveAble(p,anchor) || !['before_id','after_id'].includes(side))return;
    // Cards and text blocks share one visible order; text IDs never reach focus_move.
    let plan=text?text.plan(p,id,anchorId,side):null;
    if(!text) {
      // Without the text module the visible order is the card order alone.
      const order=[...$('.agfocus-items',mount).children].map(node=>node.dataset.cardId),moved=order.filter(value=>value!==id),index=moved.indexOf(anchorId);
      if(index<0)return;
      moved.splice(index+(side==='after_id'?1:0),0,id);
      plan={cardsChanged:!moved.every((value,i)=>value===order[i]),blocksChanged:false,newCards:moved};
    }
    if(!plan || (!plan.cardsChanged && !plan.blocksChanged))return;
    if(!plan.cardsChanged) {
      p.focusOrderPending=true;agfocusPaint(p);
      const saved=await plan.apply();
      p.focusOrderPending=false;
      if(p.focusMount!==mount)return;
      if(saved)agfocusAnnounce(p,'Pinned item moved.');
      else {p.focusError='Could not save the text position. Your text is kept; use Retry beside the notes.';agfocusAnnounce(p,p.focusError);}
      agfocusPaint(p);return;
    }
    // A card keeps the drop target as its anchor unless that target is text; then the nearest card in the new order.
    let anchorCard=row.dataset.cardId && anchor.dataset.cardId?{side,id:anchorId}:null;
    if(!anchorCard) {
      const at=plan.newCards.indexOf(id),eligible=cardId=>agfocusMoveEligible(p,p.focusCards.get(cardId));
      const previous=plan.newCards[at-1],following=plan.newCards[at+1];
      anchorCard=previous && eligible(previous)?{side:'after_id',id:previous}:following && eligible(following)?{side:'before_id',id:following}:null;
    }
    if(!anchorCard)return;
    p.focusMoveSubmission={scope:'global',item_id:id,expected_card_revision:row._item.revision,[anchorCard.side]:anchorCard.id,idempotency_key:newRequestId()};
    p.focusMoveText=plan.blocksChanged?plan.apply:null;
  }
  const submission=p.focusMoveSubmission;
  if(!submission)return;
  p.focusOrderPending=true;p.focusError='';agfocusPaint(p);
  let failure,textSaved=true;
  try {
    coreData(await post('/root/operations/focus_move',submission));
    p.focusMoveSubmission=null;
    if(p.focusMoveText){const apply=p.focusMoveText;p.focusMoveText=null;textSaved=await apply();}
  } catch(error) {
    failure=error;
    if(['revision_conflict','validation_error','not_found','forbidden','policy_denied'].includes(error.code)){p.focusMoveSubmission=null;p.focusMoveText=null;}
  }
  await agfocusRefresh(p);
  p.focusOrderPending=false;
  if(p.focusMount!==mount)return;
  if(failure){p.focusError='Could not move Pinned item. '+failure.message;agfocusAnnounce(p,p.focusError);}
  else if(!textSaved){p.focusError='Pinned item moved, but the text position could not be saved. Your text is kept; use Retry beside the notes.';agfocusAnnounce(p,p.focusError);}
  else agfocusAnnounce(p,'Pinned item moved.');
  agfocusPaint(p);
}

function agfocusOrderMount(p,board,body) {
  let drag=null,frame=null;
  const clearMarker=()=>{for(const row of board.querySelectorAll('.agfocus-drop-before,.agfocus-drop-after'))row.classList.remove('agfocus-drop-before','agfocus-drop-after');};
  const target=()=>{
    clearMarker();drag.anchor=null;
    const list=$('.agfocus-items',board),box=list.getBoundingClientRect(),pane=body.getBoundingClientRect();
    if(drag.x<box.left || drag.x>box.right || drag.y<Math.max(box.top,pane.top) || drag.y>Math.min(box.bottom,pane.bottom))return;
    const rows=[...list.children].filter(row=>agfocusMoveAble(p,row));
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
    clearTimeout(current.timer);
    if(current.row.hasPointerCapture(current.pointerId))current.row.releasePointerCapture(current.pointerId);
    if(current.active)p.focusSuppressClickUntil=Date.now()+400;
    if(!cancel && current.active && current.anchor)void agfocusMove(p,agfocusRowId(current.row),agfocusRowId(current.anchor),current.side);
  };
  const down=event=>{
    if(!drag)p.focusSuppressClickUntil=0;
    // A text block moves only by its handle, so editing and caret selection never start a drag.
    const grip=event.target.closest('.agfocus-text-grip'),textRow=grip?.closest('.agfocus-text');
    if(textRow) {
      if(event.button!==0 || drag || agfocusRowDisabled(p,textRow))return;
      drag={row:textRow,pointerId:event.pointerId,startX:event.clientX,startY:event.clientY,x:event.clientX,y:event.clientY,active:true};
      textRow.classList.add('agfocus-dragging');textRow.setPointerCapture(event.pointerId);
      frame=requestAnimationFrame(scroll);return;
    }
    const line=event.target.closest('.agfocus-row'),row=line?.closest('.agfocus-card');
    if(!line || row.dataset.moveDisabled==='true' || event.button!==0 || drag || !agfocusMoveEligible(p,row))return;
    p.focusSuppressClickUntil=0;
    drag={row,pointerId:event.pointerId,startX:event.clientX,startY:event.clientY,x:event.clientX,y:event.clientY,active:false};
    const pending=drag;
    pending.timer=setTimeout(()=>{
      if(drag!==pending || !row.isConnected || row.dataset.moveDisabled==='true'){stop(true);return;}
      pending.active=true;row.classList.add('agfocus-dragging');row.setPointerCapture(event.pointerId);
      frame=requestAnimationFrame(scroll);
    },128);
  };
  const move=event=>{
    if(drag?.pointerId!==event.pointerId)return;
    drag.x=event.clientX;drag.y=event.clientY;
    if(!drag.active && Math.hypot(drag.x-drag.startX,drag.y-drag.startY)>7){stop(true);return;}
    if(drag.active){event.preventDefault();target();}
  };
  const up=event=>{if(drag?.pointerId===event.pointerId)stop();};
  const cancel=event=>{if(drag?.pointerId===event.pointerId)stop(true);};
  const key=event=>{
    if(event.key==='Escape' && drag){event.preventDefault();event.stopImmediatePropagation();stop(true);return;}
    const grip=event.target.closest('.agfocus-text-grip'),row=grip?grip.closest('.agfocus-text'):event.target.closest('.agfocus-card');
    if(!row || !board.contains(row) || (!grip && event.target!==row && !event.target.closest('.agfocus-row')) || agfocusRowDisabled(p,row) || !event.altKey || !['ArrowUp','ArrowDown'].includes(event.key))return;
    event.preventDefault();event.stopPropagation();
    const rows=[...$('.agfocus-items',board).children].filter(node=>agfocusMoveAble(p,node));
    const anchor=rows[rows.indexOf(row)+(event.key==='ArrowUp'?-1:1)];
    if(anchor)void agfocusMove(p,agfocusRowId(row),agfocusRowId(anchor),event.key==='ArrowUp'?'before_id':'after_id');
  };
  const blur=()=>stop(true);
  const touch=event=>{if(drag?.active)event.preventDefault();};
  const context=event=>{if(drag?.active){event.preventDefault();event.stopImmediatePropagation();}};
  const suppress=event=>{if(event.detail && Date.now()<(p.focusSuppressClickUntil || 0)){event.preventDefault();event.stopImmediatePropagation();}};
  board.addEventListener('touchmove',touch,{passive:false});
  board.addEventListener('contextmenu',context,true);
  board.addEventListener('pointerdown',down);board.addEventListener('pointermove',move);window.addEventListener('pointerup',up);board.addEventListener('pointercancel',cancel);board.addEventListener('lostpointercapture',cancel);window.addEventListener('blur',blur);board.addEventListener('click',suppress,true);
  document.addEventListener('keydown',key);
  return ()=>{stop(true);board.removeEventListener('contextmenu',context,true);board.removeEventListener('touchmove',touch);board.removeEventListener('pointerdown',down);board.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);board.removeEventListener('pointercancel',cancel);board.removeEventListener('lostpointercapture',cancel);window.removeEventListener('blur',blur);board.removeEventListener('click',suppress,true);document.removeEventListener('keydown',key);};
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
  const popup=tControlPopover(anchor,`<div class="task-snooze-picker"><p class="ferr" role="alert" hidden></p>${saved?.submission?'<button type="button" class="quiet" data-focus-snooze-retry>Retry same postpone</button>':`${agfocusSnoozed(node._item)?'<button type="button" class="quiet" data-focus-snooze-clear>Return to Pinned now</button>':''}<div class="task-snooze-shortcuts">${AGFOCUS_SNOOZE_OPTIONS.map(([key,label])=>`<button type="button" class="quiet" data-focus-snooze-preset="${key}" title="${key.endsWith('h') ? label + ' from now' : label + ' at 6 AM (San Juan time)'}">${label}</button>`).join('')}</div>`}<button type="button" class="quiet" data-focus-snooze-close>Close</button></div>`,'Postpone Pinned item');
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
    agfocusAnnounce(p,until===null?'Returned to Pinned.':'Pinned item postponed.');
  } catch(error) {
    failure=error;
    if(['revision_conflict','validation_error','not_found','forbidden','policy_denied'].includes(error.code))p.focusSnoozes.delete(id);
  }
  await agfocusRefresh(p);state.pending=false;
  if(p.focusMount!==mount)return;
  agfocusRowPending(p,node);
  if(failure){
    p.focusError='Could not postpone Pinned item. '+failure.message;agfocusAnnounce(p,p.focusError);
    // Reopening exposes either the same frozen retry or fresh controls after a conflict.
    if(popup.el.isConnected){popup.close();agfocusSnoozePicker(p,node,node.querySelector('[data-focus-snooze]'));}
  }
  agfocusPaint(p);
}

// The workspace keeps board controls beside its tabs, with secondary actions
// tucked into one local menu. Native handlers still own the same buttons.
function agfocusWorkspaceTools(p,board) {
  const head=p.el.querySelector('.thead'),tools=board.querySelector('.agfocus-toolbar');
  if(!document.body.classList.contains('agent-workspace-app') || !head || !tools)return ()=>{};
  const options=document.createElement('details');options.className='agfocus-options';
  options.innerHTML='<summary aria-label="Pinned options"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></svg></summary><div class="agfocus-options-menu"></div>';
  const menu=options.querySelector('.agfocus-options-menu');
  menu.append(tools.querySelector('[data-focus-refresh]'),tools.querySelector('.agfocus-controls'));
  tools.append(options);tools.classList.add('agfocus-head-tools');head.append(tools);
  const close=event=>{if(options.open && !options.contains(event.target))options.open=false;};
  const escape=event=>{if(event.key==='Escape' && options.open){event.preventDefault();event.stopPropagation();options.open=false;options.querySelector('summary').focus({preventScroll:true});}};
  document.addEventListener('pointerdown',close);options.addEventListener('keydown',escape);
  return ()=>{document.removeEventListener('pointerdown',close);tools.remove();};
}

function agfocusMount(p) {
  const body = $('#agpad-body', p.el);
  body.innerHTML = `<div class="agfocus"><p class="agfocus-error" role="alert" hidden></p><button type="button" class="quiet" data-focus-retry-move hidden>Retry same move</button><p class="agfocus-sr" data-focus-announcement role="status" aria-live="polite"></p><span class="agfocus-sr" id="agfocus-move-instructions">Use Alt+ArrowUp or Alt+ArrowDown to move one visible row.</span>
    <header><div class="agfocus-heading"><div class="global-eyebrow">Pinned</div><div class="agfocus-toolbar"><button type="button" class="quiet agfocus-icon" data-focus-add aria-label="Add to Pinned"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button><button class="quiet agfocus-icon" data-focus-refresh aria-label="Refresh Pinned"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.6-1L20 9M4 15l2.3 3A7 7 0 0 0 18 17"/></svg></button><div class="agfocus-controls"><button class="quiet" data-focus-archived aria-pressed="false">Archived</button><button class="quiet" data-focus-deferred aria-pressed="false">Postponed</button></div></div></div><p class="agfocus-empty muted" role="status">Loading Pinned…</p></header>
    <section class="attention-queue" aria-label="New attention"></section><p class="agfocus-activity muted" hidden></p><section class="agfocus-surface" aria-label="Browser, Desktop or Viewbox" hidden><div class="agfocus-surface-bar"><button type="button" class="quiet" data-surface-back aria-label="Back to Pinned">← Pinned</button></div><div class="agfocus-surface-body"></div></section><div class="agfocus-layout"><div class="agfocus-items"></div><aside class="agfocus-dock" aria-label="Selected record" hidden><div class="agfocus-dock-content"></div><div class="agfocus-thread-resize" role="separator" aria-orientation="horizontal" aria-label="Record pane height" tabindex="0"></div></aside></div><div class="agfocus-retained" hidden></div><nav class="agfocus-pages" aria-label="Pinned pages"><button class="quiet" data-focus-previous hidden>Previous</button><span class="muted" data-focus-page></span><button class="quiet" data-focus-more hidden>Next</button></nav></div>`;
  p.focusDrafts ||= new Map();
  p.focusCards = new Map();
  p.focusWork=agfocusRestoreWork();
  p.focusSelected = null;
  const board = $('.agfocus', body);
  p.focusMount = board;
  const disposeTools=agfocusWorkspaceTools(p,board);
  let todayCalendar;
  if(p.workspaceScope === 'global' && p.workspaceRole === 'ordubashy') {
    const excerpt=document.createElement('section');
    board.querySelector('.attention-queue').before(excerpt);
    todayCalendar=calendarTodayMount(excerpt);
    excerpt.classList.add('agfocus-calendar');
  }
  // An agent with Focus gets the Browser, Desktop and pending Signoffs in one strip; agents without Focus keep their views and queue.
  p.focusSurfaces=agpadFocusSurfaces(p) && typeof agviewboxSurfacesMount==='function'?agviewboxSurfacesMount(p,board.querySelector('.attention-queue')):null;
  const disposeAttention=p.focusSurfaces?()=>{p.focusSurfaces.dispose();p.focusSurfaces=null;}
    :typeof attentionQueue!=='undefined'?attentionQueue.mount(board.querySelector('.attention-queue')):()=>{};
  const signoffQueue=p.focusSurfaces?.signoffHost || document.createElement('section');
  if(!p.focusSurfaces){
    signoffQueue.className='focus-signoff-queue';signoffQueue.setAttribute('aria-label','Signoff queue');signoffQueue.hidden=true;
    board.querySelector('.attention-queue').after(signoffQueue);
  }
  const disposeSignoffs=typeof attentionQueue!=='undefined'?attentionQueue.mountSignoffs(signoffQueue,id=>p.focus?.items.find(item=>item.source?.type==='signoff' && item.source.id===id)?.display_name,summary=>agfocusOpenSignoff(p,summary)):()=>{};
  // Focus uses the shared record workspace. Its header owns Back, the type and
  // Chat icon tabs and Close; selecting a row no longer mounts a conversation inside it.
  p.focusWorkspacePaired=false;
  p.focusWorkspace=recordWorkspaceMount($('.agfocus-dock-content',board),{
    entry:'focus',
    paired:false,
    onPairedSource:(record,parentTable)=>agfocusPairedSource(record,parentTable),
    visible:()=>agpadActive(p) && p.view==='focus' && p.focusMount===board && !board.hidden,
    onClose:()=>agfocusClearSelection(p),
    onRead:source=>agfocusWorkspaceUnread(p,source,false),
    onUnread:source=>agfocusWorkspaceUnread(p,source,true),
    onTitleChange:record=>agfocusWorkspaceTitle(p,record),
  });
  const pairedUnavailable=source=>p.focusWorkspace?.invalidate?.(source) ?? false;
  const pairedReference=ref=>agfocusOpenPairedReference(p,ref);
  window.agentWorkspacePairedUnavailable=pairedUnavailable;
  window.agentWorkspacePairedReference=pairedReference;
  const storedHeight=Number(ls(AGFOCUS_THREAD_HEIGHT_KEY));
  p.focusThreadHeight=Number.isFinite(storedHeight) && storedHeight>=AGFOCUS_THREAD_MIN_HEIGHT ? storedHeight : 320;
  const disposeResize=agfocusThreadResizeMount(p,board,body);
  const disposeOrdering=agfocusOrderMount(p,board,$('.agfocus-layout',board));
  const disposeText=agfocusTextHost()?.mount(p,board) || (()=>{});
  p.focusSnoozes ||= new Map();
  p.focusOrderPending ||= false;
  p.focusMoveSubmission ||= null;
  $('[data-focus-retry-move]',board).onclick=()=>agfocusMove(p,null,null,null,true);
  $('[data-surface-back]',board).onclick=()=>agpadSurfaceClose(p);
  const repliesChanged=()=>{for(const node of p.focusCards.values())agfocusUnread(p,node,node._item);};
  document.addEventListener('reply-notifications-changed',repliesChanged);
  p.workspaceDispose=()=>{
    p.el.classList.remove('agfocus-record-selected');
    disposeTools();
    todayCalendar?.dispose();
    disposeAttention();
    disposeSignoffs();
    document.removeEventListener('reply-notifications-changed',repliesChanged);
    agfocusOverlayClose(p);disposeResize();disposeOrdering();disposeText();p.focusSnoozePopup?.close();p.focusSnoozePopup=null;
    p.focusWorkspace?.dispose();p.focusWorkspace=null;
    if(window.agentWorkspacePairedUnavailable===pairedUnavailable)delete window.agentWorkspacePairedUnavailable;
    if(window.agentWorkspacePairedReference===pairedReference)delete window.agentWorkspacePairedReference;
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
  $('[data-focus-refresh]',p.el).onclick=()=>{agfocusRefresh(p);todayCalendar?.refresh();if(p.focusSurfaces)p.focusSurfaces.refresh();if(typeof attentionQueue!=='undefined'){if(!p.focusSurfaces)void attentionQueue.refresh();void attentionQueue.refreshSignoffs();}};
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
  if (!p.focusPending && (Date.now() >= (p.nextFocus || 0))) agfocusRefresh(p);
  else if (!p.focusPending) agfocusRecoverWork(p);
  if (p.view === 'focus') agfocusPaint(p);
}

// An expanded branch (board row or nested row) whose first read was discarded (no flight, never loaded) starts that read again on the next
// visible tick instead of waiting for the next full-board poll. A genuine first-load failure collapses the branch, so
// this never loops; branches that already have a read in flight are left alone and nothing is expanded recursively.
function agfocusRecoverWork(p) {
  if (!agpadActive(p) || !p.focusMount?.isConnected || !p.focusWork?.size) return;
  for (const item of agfocusVisibleItems(p, p.focus?.items || [])) {
    if (!agfocusExpandable(item) || item.unavailable) continue;
    const branch = p.focusWork.get(item.id);
    if (branch?.expanded && !branch.loaded && !branch.flight) agfocusExpandWork(p, item);
  }
}

// Task, Project and CRM Table cards expand; an ordinary Table's data rows are never operational child records.
const agfocusSourceOf=item=>item.resolved_source || item.source || null;
const agfocusIsCrm=item=>{const source=agfocusSourceOf(item);return source?.type==='table' && source.table_kind==='crm';};
const agfocusExpandable=item=>['task','project'].includes(item.source?.type) || agfocusIsCrm(item);
const agfocusRestorable=(p,item)=>agfocusExpandable(item) && !item.unavailable && !!p.focusWork?.get(item.id)?.expanded;

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
      // A completed read for this exact pane survives the tab being hidden; only starting a read needs visibility.
      if (!agpadActive(p,true) || pageKey!==agfocusPageKey(p) || (focusMount && p.focusMount!==focusMount)) {
        if(agpadActive(p) && p.view==='focus')p.focusRefreshAgain=true;
        return;
      }
      if (!Array.isArray(data.items) || !Number.isInteger(data.revision)) throw new Error('Invalid Focus response');
      p.focusPageKey=pageKey;
      p.focus = data; p.focusError = p.focusLinkError || ''; p.focusLinkError = '';
      void agfocusTextHost()?.load(p);
      for(const [id,seq] of p.focusPrivateRead || [])if(requestSeq>seq)p.focusPrivateRead.delete(id);
      for(const [id,seq] of p.focusPrivateUnread || [])
        if(requestSeq>seq && !data.items.some(item=>item.id===id && item.private_unread))p.focusPrivateUnread.delete(id);
      p.focusRefreshThreads=true;
      // Each expanded branch is restored on its own: once a parent settles its already-expanded visible children start at
      // once, so a slow sibling never delays a completed parent's descendants. Every row is read at most once per refresh.
      const visited=new Set();
      const same=()=>agpadActive(p,true) && pageKey===agfocusPageKey(p) && (!focusMount || p.focusMount===focusMount);
      const restore=async item=>{
        if(visited.has(item.id))return;
        visited.add(item.id);
        await agfocusExpandWork(p,item,true);
        // Descendants come from the parent's current snapshot only: a removed, collapsed, denied or unread parent has none.
        if(!same() || !agpadActive(p) || !p.focusWork?.get(item.id)?.expanded)return;
        await Promise.all(agfocusVisibleItems(p,data.items).filter(row=>row._workParent===item.id && agfocusRestorable(p,row)).map(restore));
      };
      await Promise.all(agfocusVisibleItems(p,data.items).filter(row=>!row._workParent && agfocusRestorable(p,row)).map(restore));
    } catch (error) {
      if (agpadActive(p) && pageKey===agfocusPageKey(p) && (!focusMount || p.focusMount===focusMount)) p.focusError = 'Could not refresh Pinned. ' + error.message;
      else if(agpadActive(p) && p.view==='focus')p.focusRefreshAgain=true;
    } finally {
      p.focusPending = false; p.nextFocus = pageKey===agfocusPageKey(p)?Date.now() + AGFOCUS_POLL_MS:0;
      if (agpadActive(p)) agfocusPaint(p);
      if (p.focusRefreshAgain) { p.focusRefreshAgain = false; await agfocusRefresh(p); }
    }
  })();
  return p.focusRequest;
}

// Grouped work: a Task row expands to its direct child Tasks (`parent_id`, hierarchy) and to its `concerns`
// relationships (related work). Derived rows are never Focus cards: they cannot be moved or snoozed and are not
// adopted as children. Expansion state is kept under its own key; nothing is migrated from older UI state.
const AGFOCUS_WORK_KEY='agfocus:expanded-work:v1',AGFOCUS_WORK_PAGE=50;
function agfocusRestoreWork(){
  try {
    const ids=JSON.parse(ls(AGFOCUS_WORK_KEY) || '[]');
    return new Map((Array.isArray(ids)?ids:[]).filter(id=>typeof id==='string').slice(0,500).map(id=>[id,agfocusNewBranch()]));
  } catch(_){return new Map();}
}
function agfocusSaveWork(p){
  try {ls(AGFOCUS_WORK_KEY,JSON.stringify([...p.focusWork].filter(([,branch])=>branch.expanded).map(([id])=>id).slice(-500)));}catch(_){}
}
// Type + scope + id, never a bare id: two Projects can present the same Task id.
const agfocusWorkKey=source=>[source.type,source.business_id ?? source.business_slug ?? 'global',source.id].join(':');
const agfocusWorkId=(parent,kind,source)=>`${parent.id}/${kind}/${agfocusWorkKey(source)}`;

function agfocusVisibleItems(p,items) {
  const result=[];
  // A derived row whose Task is already an ancestor on its own path is refused before it is rendered, so a
  // concerns cycle (A -> B -> A) never grows the path. Independent Focus cards are never deduplicated.
  function append(item,ancestors=new Set()) {
    // Cached child/related snapshots include history; the board toggle applies at every depth.
    if(item._workParent && item.archived && !p.focusIncludearchived)return;
    const source=item.resolved_source || item.source || null,key=source?agfocusWorkKey(source):null;
    if(item._workParent && key && ancestors.has(key))return;
    result.push(item);
    if(!source || item.unavailable)return;
    const branch=p.focusWork?.get(item.id);
    // A snapshot read for another resolved source is never shown under this one.
    if(!branch?.expanded || (branch.sourceKey && branch.sourceKey!==key))return;
    const path=new Set([...ancestors,key]);
    for(const child of branch.items || [])append(child,path);
  }
  for(const item of items)append(item);
  return result;
}

function agfocusWorkRow(parent,source,target,kind) {
  const link={type:'task',resource_type:'tasks',task_type:target.task_type,id:target.id,business_id:source.business_id ?? null,business_slug:source.business_slug ?? null,business_name:source.business_name};
  return {id:agfocusWorkId(parent,'task',link),revision:target.revision || 1,label:target.title || target.label || 'Task',source:link,resolved_source:link,
    state:target.status || target.state,source_revision:target.revision,unavailable:false,archived:!!target.archived,
    conversation:{type:'task',id:target.id,business_id:source.business_id ?? null},references:[],
    _workParent:parent.id,_workBusinessId:source.business_id,_workKind:kind,_depth:(parent._depth || 0)+1};
}
function agfocusWorkReference(parent,source,other) {
  const resourceTypes={table:'data_tables',signoff:'signoffs',action:'action_logs',entity:'context_entities',agent:'agent_profiles',material:'documents',project:'businesses'};
  const target={...other,type:other.resource_type==='email_conversations'?'email':other.type,resource_type:other.resource_type || resourceTypes[other.type] || other.type,
    business_id:source.business_id ?? null,business_slug:source.business_slug ?? null,business_name:source.business_name};
  return {id:agfocusWorkId(parent,other.type,target),label:other.label || 'Related item',resolved_source:target,unavailable:false};
}

// A branch keeps its last successful snapshot mounted. `loaded` is false only before the first successful read; a
// background refresh reads a candidate snapshot offscreen and commits it only if this exact branch is still current.
const agfocusNewBranch=()=>({expanded:true,loaded:false,items:[],references:[],children:[],related:[],childCursor:null,relationCursor:null,childPages:0,relationPages:0,moving:false,flight:null,sourceKey:null});

// Reads pages into a copy of `base`, never the branch: `target` is the page count each route should reach (or end at).
async function agfocusFetchWork(source,base,target) {
  const biz=source.business_slug;
  // Task reads infer Global from the root route; the Global relationship read declares an explicit scope (section G contract).
  const scope=op=>!biz && op==='relationships_list'?{scope:'global'}:{};
  const path=op=>biz?`/biz/${encodeURIComponent(biz)}/operations/${op}`:`/root/operations/${op}`;
  const call=async(op,args)=>coreData(await post(path(op),{...args,...scope(op)}));
  const snapshot={children:[...base.children],related:[...base.related],childCursor:base.childCursor,relationCursor:base.relationCursor,childPages:base.childPages,relationPages:base.relationPages};
  const pull=async(pagesKey,cursorKey,limit,read,take)=>{
    while(snapshot[pagesKey]<limit && (snapshot[pagesKey]===0 || snapshot[cursorKey])) {
      const data=await read(snapshot[cursorKey]);
      snapshot[cursorKey]=data.next_cursor || null;snapshot[pagesKey]++;take(data);
    }
  };
  // Both route chains settle before this returns, so the branch's flight stays occupied until no read of it is running.
  const settled = await Promise.allSettled([
    pull('childPages','childCursor',target.child,cursor=>call('tasks_list',{parent_id:source.id,include_closed:true,include_archived:true,snooze_filter:'all',limit:AGFOCUS_WORK_PAGE,...(cursor?{cursor}:{})}),
      data=>{for(const task of data.tasks || [])snapshot.children.push(task);}),
    pull('relationPages','relationCursor',target.relation,cursor=>call('relationships_list',{item:{type:'task',id:source.id},role:'concerns',direction:'both',include_archived:true,limit:AGFOCUS_WORK_PAGE,...(cursor?{cursor}:{})}),
      data=>{for(const edge of data.items || []){
        const other=edge.source?.type==='task' && edge.source.id===source.id ? edge.target_item : edge.source_item;
        if(other)snapshot.related.push(other);
      }}),
  ]);
  const failures = settled.filter(result => result.status === 'rejected').map(result => result.reason);
  if (failures.length) throw failures.find(error => accessLost(error) || sourceGone(error)) || failures[0];
  return snapshot;
}
// Children first (hierarchy); related work never repeats a child, this Task or an earlier related item.
function agfocusBuildWork(item,source,snapshot) {
  const seen=new Set([source.id]),items=[],references=[];
  for(const task of snapshot.children){if(seen.has(task.id))continue;seen.add(task.id);items.push(agfocusWorkRow(item,source,task,'child'));}
  for(const other of snapshot.related){
    const key=other.type+':'+other.id;
    if(seen.has(other.id) && other.type==='task' || seen.has(key))continue;
    seen.add(key);if(other.type==='task')seen.add(other.id);
    if(other.type==='task')items.push(agfocusWorkRow(item,source,other,'related'));
    else references.push(agfocusWorkReference(item,source,other));
  }
  return {items,references};
}
// CRM expansion: a Table card lists its active member records (crm_entries_list) in membership order, 50 per page, in the
// Table's own scope. Entry rows are derived presentation: the row id carries the scoped Table path and the membership id,
// the record keeps its own canonical Task scope, and nothing writes parent_id or creates a Focus card.
async function agfocusFetchCrmWork(table,base,target) {
  const slug=crmSlug(table);
  if(table.business_id!=null && !slug)throw Object.assign(new Error('This table is unavailable.'),{code:'not_found'});
  const snapshot={children:[...base.children],related:[],childCursor:base.childCursor,relationCursor:null,childPages:base.childPages,relationPages:0};
  // Names and archive state come from the Table itself; a failed read here only drops the stage names.
  // The parallel Table read always settles into a value, so a failed entry read can never leave it as an unhandled rejection.
  const tableRead=base.childPages===0?crmCall(slug,'tables_get',{table_id:table.id}).then(data=>({table:data.table})).catch(error=>({error})):null;
  while(snapshot.childPages<target && (snapshot.childPages===0 || snapshot.childCursor)) {
    const data=await crmCall(slug,'crm_entries_list',{table_id:table.id,include_archived:true,limit:AGFOCUS_WORK_PAGE,...(snapshot.childCursor?{cursor:snapshot.childCursor}:{})});
    snapshot.childCursor=data.next_cursor || null;snapshot.childPages++;
    snapshot.revision=data.table_revision;
    for(const entry of data.items || [])snapshot.children.push(entry);
  }
  const read=await tableRead;
  if(read?.error && (accessLost(read.error) || sourceGone(read.error)))throw read.error;
  snapshot.table=read?.table || base.table || null;
  return snapshot;
}
function agfocusCrmWorkRow(parent,table,entry,config) {
  const base={revision:entry.revision || 1,_workParent:parent.id,_workKind:'crm',_depth:(parent._depth || 0)+1,_crmEntry:entry.id,_crmTable:{id:table.id,business_id:table.business_id ?? null}};
  const ref=entry.record_ref,id=`${parent.id}/crm/${agfocusWorkKey({type:'table',id:table.id,business_id:table.business_id})}/${entry.id}`;
  const slug=ref && crmSlug(ref);
  // No cached title, scope or cells for an unavailable record, or for one whose Project cannot be routed.
  if(entry.unavailable || !ref || (ref.business_id!=null && !slug))return {...base,id,label:'',unavailable:true,archived:false,references:[]};
  const record=entry.record || {};
  const link={type:'task',resource_type:'tasks',task_type:record.task_type,id:ref.id,business_id:ref.business_id ?? null,business_slug:slug,label:record.title,customer_state:record.customer_state};
  const stage=entry.stage_id==null?'':(config?.stages || []).find(candidate=>candidate.id===entry.stage_id)?.name || entry.stage_id;
  return {...base,id,label:record.title || 'Record',source:link,resolved_source:link,state:record.status,source_revision:record.revision,unavailable:false,archived:!!record.archived,
    conversation:{type:'task',id:ref.id,business_id:ref.business_id ?? null},references:[],_crmStage:stage,_workBusinessId:table.business_id ?? null,_sameScope:(ref.business_id ?? null)===(table.business_id ?? null)};
}
function agfocusBuildCrmWork(item,table,snapshot) {
  const seen=new Set([agfocusWorkKey(table)]),items=[];
  for(const entry of snapshot.children) {
    if(entry.removed)continue;
    const ref=entry.record_ref;
    // The same record is listed once per CRM; a repeat across pages (after the list shifted) is dropped.
    const key=ref && !entry.unavailable?agfocusWorkKey({type:'task',id:ref.id,business_id:ref.business_id}):'entry:'+entry.id;
    if(seen.has(key))continue;
    seen.add(key);
    items.push(agfocusCrmWorkRow(item,table,entry,snapshot.table?.crm_config));
  }
  return {items,references:[]};
}
// Project expansion reads its own inventory: top-level work derived from authoritative ownership and
// explicit links, 25 rows per page. Root_tasks_only keeps the Task hierarchy under its own parent rows.
async function agfocusFetchProjectWork(project,base,target) {
  const snapshot={children:[...base.children],childCursor:base.childCursor,childPages:base.childPages};
  while(snapshot.childPages<target && (snapshot.childPages===0 || snapshot.childCursor)) {
    const data=coreData(await post('/root/operations/project_items_list',{scope:'global',project_id:project.id,section:'work',
      root_tasks_only:true,include_closed:true,include_archived:true,limit:25,...(snapshot.childCursor?{cursor:snapshot.childCursor}:{})}));
    snapshot.childCursor=data.next_cursor || null;snapshot.childPages++;
    for(const item of data.items || [])if(item?.type==='task')snapshot.children.push(item);
  }
  return snapshot;
}
// A derived Project row keeps its target's real scope. Deduplication and cycle detection use
// type+scope+id, never a bare id; a separately pinned Task stays its own card.
function agfocusProjectWorkRow(parent,project,item) {
  const source={type:item.type,id:item.id,business_id:item.business_id ?? null,business_slug:item.business_slug ?? null,
    business_name:item.business_name,label:item.label,task_type:item.task_type,state:item.state,revision:item.revision};
  return {id:agfocusWorkId(parent,'work',source),revision:item.revision || 1,label:item.label || 'Task',source,resolved_source:source,
    state:item.state,source_revision:item.revision,unavailable:false,archived:!!item.archived,
    conversation:{type:'task',id:item.id,business_id:item.business_id ?? null},references:[],
    _workParent:parent.id,_workBusinessId:project.business_id ?? project.id,_workKind:'project',_depth:(parent._depth || 0)+1};
}
function agfocusBuildProjectWork(item,project,snapshot) {
  const seen=new Set([agfocusWorkKey(project)]),items=[],references=[];
  for(const child of snapshot.children) {
    const key=agfocusWorkKey({type:child.type,id:child.id,business_id:child.business_id,business_slug:child.business_slug});
    if(seen.has(key))continue;
    seen.add(key);
    items.push(agfocusProjectWorkRow(item,project,child));
  }
  return {items,references};
}

// One read of a branch (first load, refresh of the loaded depth, or one more page), staged and committed atomically.
// Returns true when the snapshot was applied, false when it failed or no longer matches; failures keep the loaded snapshot.
async function agfocusRunWork(p,item,branch,kind) {
  const source=item.resolved_source || item.source,key=agfocusWorkKey(source),mount=p.focusMount,token={kind};
  const isProject=source.type==='project',isTable=source.type==='table';
  branch.flight=token;if(kind==='more'){branch.moving=true;agfocusPaint(p);}
  const current=()=>branch.flight===token && p.focusWork?.get(item.id)===branch && branch.expanded && !!mount?.isConnected && p.focusMount===mount && agpadActive(p,true) &&
    agfocusVisibleItems(p,p.focus?.items || []).some(row=>row.id===item.id && !row.unavailable && agfocusWorkKey(row.resolved_source || row.source)===key);
  const empty={children:[],related:[],childCursor:null,relationCursor:null,childPages:0,relationPages:0};
  let applied=false;
  try {
    const base=kind==='more'?branch:empty;
    const target=kind==='first'?{child:1,relation:1}:kind==='refresh'?{child:Math.max(1,branch.childPages),relation:Math.max(1,branch.relationPages)}:
      {child:branch.childPages+(branch.childCursor?1:0),relation:branch.relationPages+(branch.relationCursor?1:0)};
    const snapshot=isTable?await agfocusFetchCrmWork(source,base,target.child):isProject?await agfocusFetchProjectWork(source,base,target.child):await agfocusFetchWork(source,base,target);
    if(!current())return false;
    const built=isTable?agfocusBuildCrmWork(item,source,snapshot):isProject?agfocusBuildProjectWork(item,source,snapshot):agfocusBuildWork(item,source,snapshot);
    // Unchanged data keeps the existing row objects so nothing downstream sees a different snapshot.
    if(JSON.stringify(built.items)!==JSON.stringify(branch.items))branch.items=built.items;
    if(JSON.stringify(built.references)!==JSON.stringify(branch.references))branch.references=built.references;
    Object.assign(branch,{children:snapshot.children,related:snapshot.related,childCursor:snapshot.childCursor,relationCursor:snapshot.relationCursor,childPages:snapshot.childPages,relationPages:snapshot.relationPages,sourceKey:key,
      ...(isTable?{table:snapshot.table,revision:snapshot.revision}:{})});
    const first=!branch.loaded;branch.loaded=true;applied=true;
    if(first || kind==='more'){p.focusError='';agfocusAnnounce(p,isTable?(branch.items.length?`${branch.items.length} CRM entries`:'No CRM entries'):branch.items.length+branch.references.length?`${branch.items.length+branch.references.length} related work items`:'No related work');}
    return true;
  } catch(error) {
    if(current()) {
      if(branch.loaded && !accessLost(error) && !sourceGone(error))p.focusError=(isTable?(kind==='more'?'Could not load more CRM entries. ':'Could not refresh CRM entries. '):(kind==='more'?'Could not load more related work. ':'Could not refresh related work. '))+error.message;
      else {
        // First load, lost access or a gone source: the branch and everything beneath it leaves the board.
        branch.expanded=false;
        Object.assign(branch,{loaded:false,items:[],references:[],children:[],related:[],childCursor:null,relationCursor:null,childPages:0,relationPages:0});
        for(const id of [...p.focusWork.keys()])if(id.startsWith(item.id+'/'))p.focusWork.delete(id);
        p.focusError=(isTable?'Could not load CRM entries. ':'Could not load related work. ')+error.message;agfocusSaveWork(p);
      }
    }
    return false;
  } finally {
    const own=branch.flight===token;
    if(own){branch.flight=null;branch.moving=false;}
    if(p.focusMount===mount && mount?.isConnected && (own || applied))agfocusPaint(p);
  }
}
async function agfocusExpandWork(p,item,refresh=false) {
  p.focusWork ||= new Map();
  const key=agfocusWorkKey(item.resolved_source || item.source);
  let branch=p.focusWork.get(item.id);
  // A collapsed branch or one bound to a different resolved source never carries its snapshot forward.
  if(branch && (!branch.expanded || (branch.sourceKey && branch.sourceKey!==key)))branch=null;
  // Only a branch that is loaded or already being read is left alone; an idle unloaded one starts its first read.
  if(branch && !refresh && (branch.loaded || branch.flight))return;
  // Only an in-flight read may finish while hidden; a new read starts on the next visible tick.
  if(!agpadActive(p))return;
  if(!branch){branch=agfocusNewBranch();p.focusWork.set(item.id,branch);agfocusSaveWork(p);}
  if(branch.flight) {
    const kind=branch.flight.kind;
    await branch.flight.promise;
    if(kind!=='more' || p.focusWork.get(item.id)!==branch || !branch.expanded || !agpadActive(p))return;
  }
  const kind=branch.loaded?'refresh':'first';
  if(kind==='first'){agfocusPaint(p);agfocusAnnounce(p,agfocusIsCrm(item)?'Loading CRM entries…':'Loading related work…');}
  const flight=agfocusRunWork(p,item,branch,kind);
  branch.flight.promise=flight;
  await flight;
}
function agfocusToggleWork(p,item) {
  const branch=p.focusWork?.get(item.id);
  if(!branch?.expanded)return agfocusExpandWork(p,item);
  branch.expanded=false;
  agfocusSaveWork(p);
  agfocusPaint(p);
}
async function agfocusMoreWork(p,item) {
  const branch=p.focusWork?.get(item.id);
  if(!branch || !branch.loaded || branch.flight)return;
  const flight=agfocusRunWork(p,item,branch,'more');
  branch.flight.promise=flight;
  await flight;
}

function agfocusWorkExtras(p,node,item) {
  node._referenceObserver?.disconnect();
  node.querySelector('.agfocus-work-extras')?.remove();
  const branch=p.focusWork?.get(item.id);
  if(!branch?.expanded)return;
  const hasMore=!!(branch.childCursor || branch.relationCursor);
  const crm=agfocusIsCrm(item);
  if(!branch.references?.length && !hasMore && branch.loaded && !(crm && !branch.items.length))return;
  const moreFocused=node.contains(document.activeElement) && document.activeElement.classList.contains('agfocus-work-more');
  const row=document.createElement('div');row.className='agfocus-work-extras';row.setAttribute('aria-label','Related work');
  node.querySelector('.agfocus-row').after(row);
  if(!branch.loaded){const loading=document.createElement('span');loading.className='muted';loading.textContent=crm?'Loading CRM entries…':'Loading related work…';row.append(loading);return;}
  if(crm && !branch.items.length && !hasMore){const none=document.createElement('span');none.className='muted agfocus-crm-empty';none.setAttribute('role','status');none.textContent='No entries in this CRM';row.append(none);return;}
  const icons={file:'M14 3H5v18h14V8z M14 3v5h5 M8 13h8 M8 17h8',table:'M3 4h18v16H3z M3 10h18 M9 4v16',link:'M10 14l4-4 M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0 M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0'};
  for(const ref of branch.references){
    const target=ref.resolved_source,kind=target.type==='table'?'table':['material','email'].includes(target.type)?'file':'link';
    const chip=document.createElement('button');chip.type='button';chip.className='quiet agfocus-reference-chip';chip.setAttribute('aria-label',ref.label);chip.dataset.kind=kind;
    chip.innerHTML=`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="${icons[kind]}"/></svg><span></span>`;
    chip.querySelector('span').textContent=ref.label;
    chip.onclick=event=>{event.stopPropagation();agfocusOpenTarget(p,target,{root:false}).catch(error=>toast(error.message));};
    row.append(chip);
  }
  if(hasMore){
    const more=document.createElement('button');more.type='button';more.className='quiet agfocus-work-more';more.textContent=branch.moving?(crm?'Loading more entries…':'Loading more related work…'):(crm?'More entries':'More related work');
    more.setAttribute('aria-disabled',String(!!branch.moving));
    more.onclick=event=>{event.stopPropagation();if(!branch.moving)agfocusMoreWork(p,item);};row.append(more);
    if(moreFocused)more.focus({preventScroll:true});
  }
}

function agfocusPaint(p) {
  // A standalone Focus renderer can still show Signoff cards without the queue module.
  const separateSignoffs=typeof attentionQueue!=='undefined';
  const data = p.focus && {...p.focus,items:agfocusVisibleItems(p,p.focus.items.filter(item=>!separateSignoffs || item.source?.type!=='signoff'))};
  if(separateSignoffs)attentionQueue.paintSignoffs();
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
  agfocusTextHost()?.paintStatus(p);
  if (!data) {
    const empty=$('.agfocus-empty',host);empty.hidden=false;
    agfocusText(empty,p.focusError?'':'Loading Pinned…');
    $('[data-focus-more]',host).hidden=true;$('[data-focus-previous]',host).hidden=true;$('[data-focus-page]',host).textContent='';
    return;
  }
  const empty=$('.agfocus-empty',host);
  empty.hidden=!!data.items.length || (!!agfocusTextHost()?.hasBlocks(p));
  agfocusText(empty,'Nothing pinned yet');
  const activity = $('.agfocus-activity', host), a = data.activity;
  activity.hidden = !a;
  if (a) {
    const running = a.phase === 'running' && Date.parse(a.until) > Date.now();
    agfocusText(activity, running ? `Browser · ${a.command} running` :
      `Browser · ${a.command} ${a.phase === 'running' ? 'outcome unknown' : a.phase} · ${agpadInboxAge(a.at)}`);
  }
  const list = $('.agfocus-items', host);
  host.dataset.revision=data.revision;
  const signature=JSON.stringify([data.items,p.focusPageKey,data.next_offset,agfocusTextHost()?.signature(p),[...(p.focusWork || [])].map(([id,b])=>[id,b.expanded,b.loaded,b.moving,b.references,!!b.childCursor,!!b.relationCursor])]);
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
  if(p.focusSelected && !ids.has(p.focusSelected))agfocusForceClose(p);
  const retained=$('.agfocus-retained',host);
  for(const node of [...list.children])if(node.dataset.cardId!==undefined && !ids.has(node.dataset.cardId))retained.appendChild(node);
  let position=0;
  // Text blocks share the list; card positions count cards only.
  const cardAt=index=>{let seen=0;for(const child of list.children)if(child.dataset.cardId!==undefined && seen++===index)return child;return null;};
  for(const item of data.items) {
    let node=p.focusCards.get(item.id);
    if(node?.dataset.unavailable && !item.unavailable){node.remove();p.focusCards.delete(item.id);node=null;}
    if(!node) {
      node=document.createElement('article');node.className='agfocus-card';node.dataset.cardId=item.id;
      // The card is only a row: the selected record's dashboard or chat lives in the shared workspace dock.
      node.innerHTML=agfocusRowHtml('<button type="button" class="quiet" data-focus-snooze aria-label="Snooze Pinned item" aria-haspopup="dialog" aria-expanded="false"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></svg></button>');
      p.focusCards.set(item.id,node);
      list.appendChild(node);
    }
    const slot=cardAt(position);
    if(slot!==node)list.insertBefore(node,slot);
    position++;
    node._item=item;
    if(item.unavailable) {
      if(p.focusSelected===item.id)agfocusForceClose(p);
      node.dataset.unavailable='true';node.classList.add('agfocus-unavailable','agfocus-collapsed');
      node.removeAttribute('tabindex');node.removeAttribute('aria-expanded');node.onclick=null;node.onkeydown=null;
      node.setAttribute('aria-label','Unavailable Pinned item');
      p.focusSnoozePopup?.itemId===item.id && p.focusSnoozePopup.close();
      for(const el of node.querySelectorAll('.agfocus-scope,.agfocus-marker,.agfocus-actions,.agfocus-unread')) {el.replaceChildren();delete el._html;}
      node.querySelector('.agfocus-unread').hidden=true;
      if(item._workKind==='crm')agfocusRowControls(p,node,item,node.querySelector('.agfocus-actions'));
      node.querySelector('.ttl').textContent='This item is no longer available.';
      const flag=node.querySelector('.agfocus-flag');agfocusText(flag,'Unavailable');flag.hidden=false;
      continue;
    }
    const source=item.resolved_source || item.source || item.conversation || {};
    const projectItem=source.type==='project';
    const tableItem=source.type==='table',crmItem=tableItem && source.table_kind==='crm';
    const taskItem=source.type==='task';
    // A Project card keeps its real Project name; a Focus display-name override never renames it.
    const name=projectItem ? (item.resolved_source?.label || item.label || 'Project') : (item.display_name?.trim() || item.label);
    const taskRecord=taskItem?agfocusTaskRecord(item):null;
    const taskState=taskRecord?.status || item.state || 'open';
    const marker=node.querySelector('.agfocus-marker');
    if(projectItem) {
      const expanded=!!p.focusWork?.get(item.id)?.expanded;
      agfocusHtml(marker,`<button type="button" class="task-type-marker task-subtasks-toggle agfocus-project-toggle" aria-expanded="${expanded}" aria-label="${expanded?'Collapse':'Expand'} project work">${RW_PROJECT_ICON}</button>`);
      const toggle=marker.querySelector('button');
      toggle.onpointerdown=event=>event.stopPropagation();
      toggle.onclick=event=>{event.stopPropagation();agfocusToggleWork(p,item);};
    } else if(tableItem) {
      if(crmItem) {
        const expanded=!!p.focusWork?.get(item.id)?.expanded;
        agfocusHtml(marker,`<button type="button" class="task-type-marker task-subtasks-toggle agfocus-crm-toggle" aria-expanded="${expanded}" aria-label="${expanded?'Collapse':'Expand'} CRM entries">${RW_TABLE_ICON}</button>`);
        const toggle=marker.querySelector('button');
        toggle.onpointerdown=event=>event.stopPropagation();
        toggle.onclick=event=>{event.stopPropagation();agfocusToggleWork(p,item);};
      } else agfocusHtml(marker,`<span class="task-type-marker" role="img" aria-label="Table">${RW_TABLE_ICON}</span>`);
    } else if(taskItem && !taskShowsState(taskRecord)){
      if(taskRecord.task_type==='story'){
        const expanded=!!p.focusWork?.get(item.id)?.expanded;
        agfocusHtml(marker,`<button type="button" class="task-type-marker task-subtasks-toggle" aria-expanded="${expanded}" aria-label="${expanded?'Collapse':'Expand'} subtasks">${taskTypeIcon('story')}</button>`);
        const toggle=marker.querySelector('button');
        toggle.onpointerdown=event=>event.stopPropagation();
        toggle.onclick=event=>{event.stopPropagation();agfocusToggleWork(p,item);};
      } else agfocusHtml(marker,tBall(taskRecord));
    } else if(taskItem){
      delete marker._html;
      if(!marker.querySelector('.agfocus-task-state'))marker.innerHTML=`<button type="button" class="ball ${esc(taskState)} agfocus-task-state" aria-label="${esc(T_LABEL[taskState] || taskState)}"><span class="core"></span></button>`;
    } else agfocusHtml(marker,`<span class="sdot ${T_STATES.includes(item.state)?esc(item.state):''}" aria-hidden="true"></span>`);
    agfocusText(node.querySelector('.ttl'),name);
    node.querySelector('.ttl').removeAttribute('title');
    agfocusWorkExtras(p,node,item);
    node.classList.toggle('agfocus-work-child',!!item._workParent);
    node.style.setProperty('--focus-depth',item._depth || 0);
    agfocusContext(node,item);
    const flag=node.querySelector('.agfocus-flag');
    const snoozed=agfocusSnoozed(item);
    node.classList.toggle('agfocus-snoozed',snoozed);
    const stateLabel=taskItem?customerStateText({task_type:taskRecord.task_type,customer_state:source.customer_state}):'';
    // A CRM stage is shown beside, never instead of, the Task's own state.
    const flagText=[tableItem?(crmItem?'CRM':'Table'):'',stateLabel,item._crmStage?'Stage: '+item._crmStage:'',source.archived?'Archived table':'',item.archived?(tableItem?'Card archived':'Archived'):''].filter(Boolean).join(' · ');
    agfocusText(flag,flagText);flag.title=flagText;flag.hidden=!flagText;
    const actions=node.querySelector('.agfocus-actions');
    node.tabIndex=0;node.setAttribute('role','group');node.setAttribute('aria-label',name);
    // Following a derived row opens that record; it never pins it, changes its parent or invents a channel.
    const openRow=()=>{
      if(item._workParent){
        const parent=p.focusCards.get(item._workParent)?._item;
        const parentTable=item._workKind==='crm' && item._crmTable ? {...item._crmTable,type:'table',label:parent?.label || 'CRM'} : null;
        agfocusOpenTarget(p,item,{root:false,paired:item._workKind==='crm',parentTable}).catch(error=>toast(error.message));return;
      }
      void agfocusSelect(p,item.id);
    };
    node.onclick=event=>{
      if(event.composedPath().some(el=>el.matches?.('button,a,input,textarea,select,.agfocus-marker')) || Date.now()<(p.focusSuppressClickUntil || 0))return;
      if((event.ctrlKey || event.metaKey) && !item._workParent){event.preventDefault();agfocusNewTab(item);return;}
      if(event.target.closest('.agfocus-row'))openRow();
    };
    node.onkeydown=event=>{if(event.target===node && ['Enter',' '].includes(event.key)){event.preventDefault();openRow();}};
    if(taskItem && taskShowsState(taskRecord)){
      const ball=node.querySelector('.agfocus-task-state');
      const taskScope=source.business_slug || item.context_business_slug || null;
      if (agfocusTaskScopeMissing(item, taskScope)) {ball.disabled=true; ball.removeAttribute('data-task-state-key'); ball._taskStateRecord=null; ball._taskStateChange=null; ball.setAttribute('aria-label','Task project unavailable');}
      else {ball.disabled=false; taskStateBind(ball, taskScope, taskRecord, snapshot => {
        item.state = snapshot.task.status; item.source_revision = snapshot.task.revision;
        if(item.task && !agfocusTaskScopeMissing(item, source.business_slug || item.context_business_slug || null)) taskWorkerMount(actions, source.business_slug || item.context_business_slug || null, taskRecord, p.agentId);
      });}
      ball.onclick=event=>{event.stopPropagation();agfocusTaskState(p,item,ball);};
      ball.oncontextmenu=event=>{event.preventDefault();event.stopPropagation();if(!agfocusTaskScopeMissing(item,taskScope))taskBoardStateMenu(ball,taskScope,taskRecord);};
      ball.onkeydown=event=>{if(event.key==='ArrowDown'){event.preventDefault();event.stopPropagation();if(!agfocusTaskScopeMissing(item,taskScope))taskBoardStateMenu(ball,taskScope,taskRecord);}};
    }
    node.setAttribute('aria-describedby','agfocus-move-instructions');
    actions.querySelector('[data-focus-snooze]').onclick=event=>{event.stopPropagation();agfocusSnoozePicker(p,node,event.currentTarget);};
    actions.querySelector('[data-focus-snooze]').setAttribute('aria-pressed',String(snoozed));
    agfocusRowPending(p,node);
    actions.querySelector('[data-focus-snooze]').hidden=!!item._workParent;
    agfocusRowControls(p,node,item,actions);
    if(taskItem) {
      const scope=source.business_slug || item.context_business_slug || null;
      if(item.task && !agfocusTaskScopeMissing(item, scope)) taskWorkerMount(actions, scope, taskRecord, p.agentId); else actions.querySelector('[data-task-workers]')?.remove();
      let stateButton=actions.querySelector('[data-task-state-menu]');
      if(taskShowsState(taskRecord) && !agfocusTaskScopeMissing(item, scope)) {
        if(!stateButton){stateButton=document.createElement('button');stateButton.type='button';stateButton.className='quiet';stateButton.dataset.taskStateMenu='';stateButton.textContent='State';actions.append(stateButton);}
        stateButton.setAttribute('aria-label','Change state of '+name);
        stateButton.onpointerdown=event=>event.stopPropagation();
        stateButton.onclick=event=>{event.stopPropagation();taskBoardStateMenu(stateButton,scope,taskRecord);};
      } else stateButton?.remove();
      let add=actions.querySelector('[data-attention-add]');
      if(p.workspaceRole === 'ordubashy' && !agfocusTaskScopeMissing(item, scope)) {
        if(!add){add=document.createElement('button');add.type='button';add.className='quiet';add.dataset.attentionAdd='';add.textContent='Add to Attention';actions.append(add);}
        add.onpointerdown=event=>event.stopPropagation();
        add.setAttribute('aria-label','Add '+name+' to Attention board');
        add.onclick=async event=>{event.stopPropagation();add.disabled=true;try{coreData(await post('/root/operations/attention_add',{scope:'global',task:{type:'task',id:source.id,business_id:Object.hasOwn(source,'business_id') ? source.business_id : item.source?.business_id ?? item.conversation?.business_id ?? null},idempotency_key:newRequestId()}));toast('Added to Attention board');}catch(error){toast(error.message);}finally{add.disabled=false;}};
      } else add?.remove();
    } else {actions.querySelector('[data-task-workers]')?.remove();actions.querySelector('[data-task-state-menu]')?.remove();actions.querySelector('[data-attention-add]')?.remove();}
    agfocusUnread(p,node,item);
    node.classList.toggle('agfocus-collapsed',p.focusSelected!==item.id);
    node.setAttribute('aria-expanded',String(p.focusSelected===item.id));
  }
  agfocusTextHost()?.reconcile(p,host,list);
  if(focused?.isConnected && host.contains(focused)){focused.focus({preventScroll:true});if(selection)focused.setSelectionRange(...selection);}
  agfocusRefreshThreads(p);
  agfocusThreadHeight(p);
  $('[data-focus-more]',host).hidden=data.next_offset==null;
  $('[data-focus-previous]',host).hidden=!p.focusPages?.length;
  $('[data-focus-page]',host).textContent=(p.focusPages?.length || data.next_offset!=null)?'Page '+((p.focusPages?.length||0)+1):'';
  agfocusDesktopTab(p);
}

// Distinct controls, each named for what it changes: the Focus card, the Table itself, a CRM membership. None of them
// touches the others: archiving a card never archives the Table, removing an entry never archives its record.
function agfocusRowControls(p,node,item,actions) {
  const wanted=[];
  if(item._workKind==='crm') wanted.push(['remove',item.unavailable?'Remove this unavailable entry from CRM':`Remove ${item.label} from CRM`,'Remove from CRM']);
  const source=agfocusSourceOf(item);
  if(!item._workParent && source?.type==='table' && !item.unavailable) {
    wanted.push(['card',item.archived?'Restore Pinned card':'Archive Pinned card',item.archived?'Restore card':'Archive card']);
    wanted.push(['table',source.archived?'Table is archived':'Archive table (the table itself)',source.archived?'Archived table':'Archive table']);
  }
  for(const existing of [...actions.querySelectorAll('[data-focus-extra]')])if(!wanted.some(([kind])=>existing.dataset.focusExtra===kind))existing.remove();
  for(const [kind,label,text] of wanted) {
    let button=actions.querySelector(`[data-focus-extra="${kind}"]`);
    if(!button){button=document.createElement('button');button.type='button';button.className='quiet agfocus-extra';button.dataset.focusExtra=kind;actions.append(button);}
    button.setAttribute('aria-label',label);button.title=label;agfocusText(button,text);
    button.disabled=kind==='table'&&!!source.archived;
    button.onpointerdown=event=>event.stopPropagation();
    button.onclick=event=>{event.stopPropagation();void agfocusExtraAction(p,item,kind,button);};
  }
}
async function agfocusExtraAction(p,item,kind,button) {
  if(button.disabled || button.getAttribute('aria-busy')==='true')return;
  button.setAttribute('aria-busy','true');
  const fail=error=>{p.focusError=`Could not ${({pin:'pin the record',remove:'remove the entry',card:'change the Pinned card',table:'archive the table'})[kind]}. `+(error.message || '');agfocusAnnounce(p,p.focusError);agfocusPaint(p);};
  try {
    if(kind==='pin') {
      await crmPinRecord({id:item.source.id,business_id:item.source.business_id ?? null});
      agfocusAnnounce(p,'Added to Pinned.');
      await agfocusRefresh(p);
    } else if(kind==='card') {
      coreData(await post('/root/operations/focus_set_archived',{scope:'global',item_id:item.id,expected_card_revision:item.revision,archived:!item.archived,idempotency_key:newRequestId()}));
      agfocusAnnounce(p,item.archived?'Pinned card restored.':'Pinned card archived.');
      await agfocusRefresh(p);
    } else if(kind==='table') {
      const table=agfocusSourceOf(item),slug=crmSlug(table);
      if(table.business_id!=null && !slug)throw new Error('This table is unavailable.');
      if(!await taskConfirm({title:'Archive this table?',message:'This archives the table itself, so its entries and columns become read only. Member records, their states and their other Pinned cards are unchanged. The Pinned card stays.',confirmLabel:'Archive table'}))return;
      const current=(await crmCall(slug,'tables_get',{table_id:table.id})).table;
      await crmCall(slug,'tables_update',{table_id:table.id,expected_revision:current.revision,changes:{archived:true},idempotency_key:newRequestId()});
      agfocusAnnounce(p,'Table archived.');
      await agfocusRefresh(p);
    } else if(kind==='remove') {
      if(!await taskConfirm({title:'Remove from this CRM?',message:'This removes the entry from this CRM only. The record, its history, its state and its other tables are unchanged.',confirmLabel:'Remove from CRM'}))return;
      const table=item._crmTable,slug=crmSlug(table);
      if(table.business_id!=null && !slug)throw new Error('This table is unavailable.');
      const current=(await crmCall(slug,'tables_get',{table_id:table.id})).table;
      await crmCall(slug,'crm_entries_remove',{table_id:table.id,entry_id:item._crmEntry,expected_revision:current.revision,idempotency_key:newRequestId()});
      agfocusAnnounce(p,'Removed from CRM.');
      const parent=agfocusVisibleItems(p,p.focus?.items || []).find(row=>row.id===item._workParent);
      if(parent)await agfocusExpandWork(p,parent,true);
    }
  } catch(error){fail(error);}
  finally{button.removeAttribute('aria-busy');}
}

function agfocusNewTab(item) {
  const url=new URL(location.href);
  url.searchParams.set('focus_item',item.id);url.hash='/global/agents';
  window.open(url.href,'_blank','noopener');
}

// Open a Focus/related record in the same shared workspace (used by deep links, reference chips and derived rows).
async function agfocusOpenTarget(p,target,{root=true,paired=target?._workKind==='crm',parentTable=null}={}) {
  const record=rwRecordFromItem(target,{can_thread:target?.can_thread});
  if(!record)throw new Error('This item is no longer available.');
  if(!await agfocusWorkspaceMode(p,paired))return false;
  agfocusShowDock(p);
  if(parentTable?.id) return p.focusWorkspace?.navigate(record,{parentTable});
  return p.focusWorkspace?.open(record,{root,tab:record.can_thread?'chat':'dashboard'});
}
// Native Chat Task references preserve their canonical scope and open this record's private Focus thread.
async function agfocusOpenChatTask(agentId, reference) {
  const pane=typeof _agpad==='undefined'?null:_agpad;
  if(!pane || String(pane.agentId ?? '')!==String(agentId ?? ''))throw new Error('This agent is no longer open.');
  if(!agpadFocusSurfaces(pane))throw new Error('This agent has no Pinned view.');
  if(!reference || reference.id==null)throw new Error('This Task is no longer available.');
  const source={type:'task',id:reference.id,business_id:reference.business_id ?? null,business_slug:reference.business_slug || null,
    label:reference.label || null,task_type:reference.task_type || null,state:reference.state || null};
  const record=rwRecordFromItem({source,can_thread:true},{can_thread:true});
  if(!record)throw new Error('This Task is no longer available.');
  if(record.scopeMissing)throw new Error('That Task belongs to a Project that is not available here.');
  if(pane.view!=='focus' || pane.surface)await agpadSetView(pane,'focus');
  if(pane.view!=='focus' || !pane.focusMount?.isConnected)throw new Error('Pinned is unavailable for this agent.');
  const opened=await agfocusOpenTarget(pane,{source,can_thread:true},{root:true});
  if(typeof _agpad==='undefined' || _agpad!==pane || String(pane.agentId ?? '')!==String(agentId ?? ''))throw new Error('This agent is no longer open.');
  if(!pane.focusWorkspace?.isOpen?.())throw new Error('This Task could not be opened.');
  return opened;
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
function agfocusShowDock(p) {
  const board=p.focusMount;if(!board?.isConnected)return null;
  if(!p.focusWorkspace?.alive?.())p.focusWorkspace=recordWorkspaceMount($('.agfocus-dock-content',board),agfocusWorkspaceOptions(p));
  const dock=$('.agfocus-dock',board);
  dock.hidden=false;$('.agfocus-layout',board).classList.add('agfocus-thread-open');
  if(p.workspaceRole==='ordubashy' && p.focusWorkspacePaired){
    p.el.classList.add('agfocus-record-selected');
    $('.agfocus-layout',board).classList.add('agfocus-paired-workspace');
    agfocusThreadHeight(p);
    return dock;
  }
  agfocusOverlayOpen(p,board,dock,()=>{void p.focusWorkspace?.close();});
  agfocusThreadHeight(p);
  return dock;
}
function agfocusClearSelection(p) {
  p.el.classList.remove('agfocus-record-selected');
  p.focusSelected=null;
  p.focusWorkspacePaired=false;
  const board=p.focusMount;if(!board?.isConnected)return;
  $('.agfocus-dock',board).hidden=true;
  $('.agfocus-layout',board).classList.remove('agfocus-thread-open','agfocus-paired-workspace');
  agfocusOverlayClose(p);
  for(const node of p.focusCards.values()){node.classList.add('agfocus-collapsed');node.setAttribute('aria-expanded','false');}
}
// Eviction, lost access or a source disappearing closes the mounted record without a save prompt.
function agfocusForceClose(p) {
  void agfocusPairedSource(null,null);
  p.focusWorkspace?.dispose();
  p.focusWorkspacePaired=false;
  p.focusSelected=null;
  const board=p.focusMount;if(board?.isConnected)p.focusWorkspace=recordWorkspaceMount($('.agfocus-dock-content',board),agfocusWorkspaceOptions(p));
  agfocusClearSelection(p);
  agfocusPaint(p);
}
function agfocusWorkspaceOptions(p) {
  return {entry:'focus',
    paired:!!p.focusWorkspacePaired && p.workspaceRole === 'ordubashy',
    onPairedSource:(record,parentTable)=>agfocusPairedSource(record,parentTable),
    visible:()=>agpadActive(p) && p.view==='focus' && p.focusMount && !p.focusMount.hidden,
    onClose:()=>agfocusClearSelection(p),
    onRead:source=>agfocusWorkspaceUnread(p,source,false),
    onUnread:source=>agfocusWorkspaceUnread(p,source,true),
    onTitleChange:record=>agfocusWorkspaceTitle(p,record)};
}
function agfocusPairedSource(record,parentTable) {
  const source=record?.source || null;
  if(!source)return window.parent?.agentWorkspacePairedRecord?.(window,null,null) ?? true;
  const parent=parentTable?{type:'table',id:parentTable.id,business_id:parentTable.business_id ?? null}:null;
  if(window.parent!==window && typeof window.parent.agentWorkspacePairedRecord==='function')
    return window.parent.agentWorkspacePairedRecord(window,{type:source.type,id:source.id,business_id:source.business_id ?? null},parent);
  return window.agentWorkspacePairedRecord?.(window,{type:source.type,id:source.id,business_id:source.business_id ?? null},parent) ?? true;
}
async function agfocusOpenPairedReference(p,ref) {
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
// Unread state is still owned by the Focus row, not by the workspace header.
function agfocusWorkspaceRows(p,source) {
  if(!source)return[];
  const key=`${source.type}:${source.business_id ?? 'global'}:${source.id}`;
  return agfocusVisibleItems(p,p.focus?.items || []).filter(item=>{
    const src=item.resolved_source || item.source || item.conversation;
    return src && `${src.type}:${src.business_id ?? 'global'}:${src.id}`===key;
  });
}
function agfocusWorkspaceUnread(p,source,unread) {
  if(!agfocusActive(p))return;
  for(const item of agfocusWorkspaceRows(p,source)) {
    item.private_unread=unread;
    const node=p.focusCards.get(item.id);
    if(node)agfocusUnread(p,node,item,unread);
    if(unread){p.focusPrivateUnread?.set(item.id,p.focusBoardSeq || 0);p.focusPrivateRead?.delete(item.id);}
    else {p.focusPrivateUnread?.delete(item.id);p.focusPrivateRead?.set(item.id,p.focusBoardSeq || 0);}
  }
  for(const item of agfocusVisibleItems(p,p.focus?.items || [])) {
    if(item._workParent)continue;
    const node=p.focusCards.get(item.id);
    if(node)agfocusUnread(p,node,item);
  }
}
function agfocusWorkspaceTitle(p,record) {
  if(!agfocusActive(p)||!record)return;
  for(const item of agfocusWorkspaceRows(p,record.source || record)) {
    if((item.resolved_source || item.source)?.type!=='project')continue;
    if(item.resolved_source)item.resolved_source={...item.resolved_source,label:record.label};
    if(item.source)item.source={...item.source,label:record.label};
    item.label=record.label;
    const node=p.focusCards.get(item.id);
    if(node)agfocusText(node.querySelector('.ttl'),record.label);
  }
}
function agfocusActive(p) { return !!p && agpadActive(p) && p.view==='focus'; }

// A pending-Signoff chip opens the same shared record workspace as a pinned Signoff row. The queue passes the
// canonical list summary, so an unpinned Signoff still gets its ordinary source/scope descriptor and reader.
async function agfocusOpenSignoff(p, summary) {
  if (!summary || summary.type !== 'signoff' || !summary.id) return false;
  const record=rwRecordFromItem({source: {type: 'signoff', id: summary.id,
    business_id: summary.review_business_id ?? summary.business_id ?? null, label: summary.title || summary.label,
    resource_type: 'signoffs', revision: summary.status_revision, business_slug: summary.business_slug,
    business_name: summary.business_name}}, {can_thread: true});
  if (!record) {
    if (agfocusActive(p)) { p.focusError='This Signoff is no longer available.'; agfocusPaint(p); }
    return false;
  }
  if(!await agfocusWorkspaceMode(p,false))return false;
  agfocusShowDock(p);
  try {
    const opened=await p.focusWorkspace?.open(record, {root: true, tab: 'chat'}) ?? false;
    if (opened) {
      // The Signoff, not a pinned Focus card, now owns the dock. Clear the old visual selection without hiding the
      // dock, so clicking that card again can reopen it.
      const previous=p.focusCards.get(p.focusSelected);
      if (previous) { previous.classList.add('agfocus-collapsed'); previous.setAttribute('aria-expanded','false'); }
      p.focusSelected=null;
    }
    return opened;
  } catch (error) {
    // The workspace owns denial/read failures; never fall back to the retired Signoff modal.
    if (agfocusActive(p)) { p.focusError=error.message; agfocusPaint(p); }
    return false;
  }
}

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
  const id=url.searchParams.get('focus_item');
  if(!id)return;
  url.searchParams.delete('focus_item');history.replaceState(null,'',url);
  try {
    const data=coreData(await post('/root/operations/focus_get',{scope:'global',item_id:id}));
    if(!agpadActive(p) || p.view!=='focus')return;
    if(!data.item || data.item.unavailable)throw new Error('This Pinned item is no longer available.');
    const record=rwRecordFromFocusCard(data.item);
    if(!record)throw new Error('This Pinned item is no longer available.');
    if(!await agfocusWorkspaceMode(p,(record.source || data.item.source)?.type==='table'))return;
    agfocusShowDock(p);
    await p.focusWorkspace?.open(record,{root:true,tab:record.can_thread?'chat':'dashboard'});
  } catch(error){if(agpadActive(p)){p.focusError=error.message;agfocusPaint(p);}}
}

// Selecting a card opens the shared record workspace, Chat first on every fresh selection.
// Clicking the already selected row keeps its tab, dashboard edits and chat draft.
async function agfocusSelect(p,id) {
  const board=p.focusMount;
  if(!board?.isConnected)return;
  const next=id && p.focusCards.get(id);
  if(id && (!next || next._item?.unavailable))return;
  if(id===p.focusSelected){if(next?.tabIndex===0)next.focus({preventScroll:true});return;}
  const previous=p.focusCards.get(p.focusSelected);
  if(!id) {
    await p.focusWorkspace?.close();
    return;
  }
  const record=rwRecordFromFocusCard(next._item);
  if(!record)return;
  const source=record.source || next._item.resolved_source || next._item.source;
  if(!await agfocusWorkspaceMode(p,source?.type==='table'))return;
  agfocusShowDock(p);
  const opened=await p.focusWorkspace?.open(record,{root:true,tab:record.can_thread?'chat':'dashboard'});
  if(opened===false){if(!p.focusSelected)agfocusClearSelection(p);return;}
  if(!agpadActive(p) || p.focusMount!==board || p.focusCards.get(id)!==next)return;
  p.focusSelected=id;
  if(previous && previous!==next){previous.classList.add('agfocus-collapsed');previous.setAttribute('aria-expanded','false');}
  next.classList.remove('agfocus-collapsed');next.setAttribute('aria-expanded','true');
  agfocusThreadHeight(p);
  // An ordinary Task still opens its existing hierarchy; a Project only expands through its chevron.
  if(source?.type==='task' && (agfocusTaskRecord(next._item).task_type!=='story' || p.focusWork?.get(next._item.id)?.expanded!==false))void agfocusExpandWork(p,next._item);
  // A selected CRM expands like a Story unless its entries were collapsed on purpose.
  else if(agfocusIsCrm(next._item) && p.focusWork?.get(next._item.id)?.expanded!==false)void agfocusExpandWork(p,next._item);
}

function agfocusRefreshThreads(p) {
  if(!p.focusRefreshThreads)return;
  p.focusRefreshThreads=false;
  // Only the open record's Chat is refreshed; the dashboard is not polled.
  p.focusWorkspace?.refresh?.();
}

function agfocusThreadHeight(p,height) {
  const board=p.focusMount,dock=board && $('.agfocus-dock',board);
  if(!dock || dock.hidden || board.hidden)return;
  if(p.focusOverlay){p.focusOverlay.fit();return;}
  if(p.workspaceRole==='ordubashy'){dock.style.height='auto';return;}
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

function agfocusTaskScopeMissing(item,scope) {
  const source=item.resolved_source || item.source || item.conversation || {};
  const businessId=Object.hasOwn(source,'business_id') ? source.business_id : item.source?.business_id ?? item.conversation?.business_id ?? null;
  return businessId != null && !scope;
}
function agfocusTaskRecord(item) {
  const source=item.resolved_source || item.source || item.conversation || {};
  const scope=source.business_slug || item.context_business_slug || null;
  const task={...source,...(item.task || {}),id:source.id,status:item.task?.status || item.state || source.state || 'open',revision:item.task?.revision || item.source_revision,...(source.task_type?{task_type:source.task_type}:{})};
  return agfocusTaskScopeMissing(item,scope) ? task : taskStateObserve(scope, task);
}
function agfocusTaskState(p,item,button) {
  const source=item.resolved_source || item.source || item.conversation || {},task=agfocusTaskRecord(item);
  const scope=source.business_slug || item.context_business_slug || null;
  if(!taskShowsState(task) || agfocusTaskScopeMissing(item, scope))return;
  const status=tNext(task.status);
  return taskStateSet(scope,task,status);
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
      if (current.browser?.request_id !== requestId || current.browser.state !== 'pending') throw new Error('This input request changed. Refresh Pinned.');
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

function agfocusDesktopError(p, body, message, ctx = null) {
  body.innerHTML = '<section class="agfocus-desktop-view"><div class="agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') +
    '<span class="agbrowser-actions">' + agbrowserReconnectButton('data-desktop-retry') + '</span></div><p class="ferr" role="alert">' + esc(message) + '</p></section>';
  $('[data-desktop-retry]', body).onclick = () => agfocusDesktop(p, true, ctx);
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
  body.innerHTML = '<section class="agfocus-desktop-view"><div class="agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') + '</div><p class="muted" role="status">Connecting to Ordubashy’s desktop…</p></section>';
  agbrowserWireMode(p, body);
  let data, surfaces;
  try {
    [data, surfaces] = await Promise.all([api(agfocusPath(p)).then(coreData), agbrowserSurfaces(p, reconnect)]);
    if (!live() || p.desktopRequest !== request || !body.isConnected) return;
    // Focus owns p.focus while mounted; the standalone Desktop view still publishes its read.
    if (!ctx) p.focus = data;
    agfocusDesktopTab(p);
  } catch (error) {
    if (body.isConnected && live() && p.desktopRequest === request) agfocusDesktopError(p, body, error.message, ctx);
    return;
  }
  const surface = surfaces.desktop;
  const desktop = surface && agfocusDesktopURL(surface.url);
  if (!desktop) {
    body.innerHTML = '<section class="agfocus-desktop-view"><div class="agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') + '<span class="agbrowser-actions">' + agbrowserReconnectButton('data-desktop-retry') + '</span></div><p class="muted" role="status">Desktop is not configured. Set DESKTOP_CONTROL_URL in Global Settings or add Ordubashy’s desktop binding.</p></section>';
    $('[data-desktop-retry]', body).onclick = () => agfocusDesktop(p, true, ctx);
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
  body.innerHTML='<section class="agfocus-desktop-view"><div class="agfocus-desktop-help agbrowser-bar">' + agbrowserModeToggle(p, 'desktop') + '<span class="agbrowser-actions">' + agbrowserReconnectButton('data-desktop-retry') + '</span></div><div class="agfocus-desktop-help agfocus-desktop-notes"></div><iframe class="agfocus-reference-frame agfocus-desktop-frame" aria-label="Desktop control" referrerpolicy="no-referrer" allow="clipboard-read; clipboard-write; fullscreen"></iframe>' +
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
  $('[data-desktop-retry]', host).onclick = () => agfocusDesktop(p, true, ctx);
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

// One atomic create-and-present call; a retry keeps the exact request and key.
function agfocusAdd(p) {
  let submission=null, pending=false, typeEditor, tableReady=false;
  const text=agfocusTextHost(),textReady=!!text && !!p.agentId && text.ready(p);
  const projects=(state.bizs || []);
  const kinds=`<div class="focus-add-kind" role="radiogroup" aria-label="What to add"><label><input type="radio" name="kind" value="item" checked> Task or link</label><label><input type="radio" name="kind" value="project"> Existing Project</label><label><input type="radio" name="kind" value="table"> Existing Table or CRM</label>${text && p.agentId?`<label><input type="radio" name="kind" value="text"${textReady?'':' disabled'}> Text or space</label>`:''}</div>${text && p.agentId && !textReady?`<p class="muted">${esc(text.unavailable(p))}</p>`:''}`;
  modal({title:'Add to Pinned', body:`<form id="focus-add-form">${kinds}<div data-add-item><label>Title<input class="inp" name="title" required maxlength="200" autocomplete="off"></label><label>Link <span class="muted">(optional)</span><input class="inp" name="link" type="url" placeholder="https://…"></label><p class="muted">Leave the link empty to create a task.</p>${taskTypeFields()}</div><div data-add-project hidden><label>Project<select class="inp" name="project">${projects.map(biz=>`<option value="${esc(biz.business_id||biz.id)}">${esc(biz.name||biz.slug)}${biz.archived?' (archived)':''}</option>`).join('')}</select></label><p class="muted">${projects.length?'Pins the actual accessible Project. Pinning never creates a Project or a new conversation.':'No accessible Projects.'}</p></div><div data-add-table hidden><label>Scope<select class="inp" name="table_scope" aria-label="Table scope"><option value="">Global</option>${projects.map(biz=>`<option value="${esc(biz.slug)}">${esc(biz.name||biz.slug)}</option>`).join('')}</select></label><label>Show<select class="inp" name="table_kind" aria-label="Table kind"><option value="">Tables and CRMs</option><option value="general">Tables only</option><option value="crm">CRMs only</option></select></label><label>Table<select class="inp" name="table" aria-label="Table to pin"></select></label><button type="button" class="quiet" data-add-table-more hidden>More tables</button><p class="muted" data-add-table-status role="status"></p><p class="muted">Pins an existing table. Pinning never creates a table and changes nothing about it or its entries.</p></div><div data-add-text hidden><label>Text <span class="muted">(optional)</span><textarea class="inp" name="text" rows="5" maxlength="8000" placeholder="A heading, a note, or leave empty for blank space"></textarea></label><p class="muted">Saved on this agent's Pinned board only. Agents never read it.</p></div><p role="alert" class="bad" hidden></p></form>`,
    actions:[{label:'Cancel',onClick:close=>close()}, {label:'Add', primary:true, onClick:async(close,el)=>{
      if(pending)return;
      const form=el.querySelector('form'), error=form.querySelector('[role="alert"]');
      const mode=form.elements.kind?.value || 'item';
      if(mode==='text') {
        if(!text.add(p,form.elements.text.value)){error.textContent=text.unavailable(p) || 'Could not add the text block.';error.hidden=false;return;}
        close();return;
      }
      if(mode==='table') {
        const select=form.elements.table,option=select.selectedOptions[0];
        if(!option || !option.value || !tableReady){error.textContent='Choose a table.';error.hidden=false;return;}
        // A key is kept only for the exact table it was made for; any other selection starts a new request.
        const same=submission?.args.source?.id===option.value && (submission.args.source.business_id ?? '')===(option.dataset.businessId || '');
        if(!same)submission={operation:'focus_present',args:{scope:'global',source:{type:'table',id:option.value,business_id:option.dataset.businessId||null},idempotency_key:newRequestId()}};
      } else if(mode==='project') {
        const projectId=form.elements.project.value;
        if(!projectId){error.textContent='Choose a Project.';error.hidden=false;return;}
        const key=newRequestId();
        if(!submission)submission={operation:'focus_present',args:{scope:'global',source:{type:'project',id:projectId,business_id:projectId},idempotency_key:key}};
      } else if(!submission) {
        if(!form.reportValidity())return;
        const title=form.elements.title.value.trim(),link=form.elements.link.value.trim();
        if(!title)return;
        if(link && !/^https?:\/\//i.test(link)){error.textContent='Use an HTTP or HTTPS link.';error.hidden=false;return;}
        let taskFields={};
        if(!link){try{taskFields=typeEditor.values();}catch(err){error.textContent=err.message;error.hidden=false;return;}}
        const key=newRequestId();
        submission=link ? {operation:'focus_present',args:{scope:'global',reference:{key,title,target:{type:'url',url:link}},idempotency_key:key}}
          : {operation:'tasks_create',args:{title,...taskFields,focus:{display_name:title},idempotency_key:key}};
      }
      pending=true;error.hidden=true;
      form.querySelectorAll('input,select,textarea,button').forEach(input=>input.disabled=true);
      try {
        coreData(await post('/root/operations/'+submission.operation,submission.args));
        close();toast('Added to Pinned');p.focusOffset=0;p.focusPages=[];await agfocusRefresh(p);
      } catch(err) {
        error.textContent=err.message;error.hidden=false;
        if(['validation_error','forbidden','policy_denied','not_found'].includes(err.code)){submission=null;form.querySelectorAll('input,select,textarea,button').forEach(input=>input.disabled=false);}
      } finally {pending=false;}
    }}],onOpen:el=>{
      const form=el.querySelector('form');
      typeEditor=taskTypeMount(el,null);
      el.querySelector('[name=link]').oninput=e=>{el.querySelector('.task-type-editor').hidden=!!e.target.value.trim();};
      for(const radio of form.querySelectorAll('[name=kind]'))radio.onchange=()=>{
        const mode=form.elements.kind.value;
        el.querySelector('[data-add-item]').hidden=mode!=='item';el.querySelector('[data-add-project]').hidden=mode!=='project';el.querySelector('[data-add-text]').hidden=mode!=='text';el.querySelector('[data-add-table]').hidden=mode!=='table';
        form.elements.title.required=mode==='item';
        if(mode==='table')loadTables();else{tableSeq++;tableReady=false;addButton().disabled=pending;}
        submission=null;
        const target=mode==='project'?el.querySelector('[name=project]'):mode==='text'?el.querySelector('[name=text]'):mode==='table'?form.elements.table_scope:form.elements.title;
        target.focus();
      };
      // Existing tables of the chosen scope; the picker lists, it never creates.
      let tableCursor=null,tableSeq=0;
      const addButton=()=>el.querySelector('.mfoot .primary');
      const syncAdd=()=>{if(form.elements.kind.value==='table')addButton().disabled=pending || !tableReady || !form.elements.table.value;};
      const loadTables=async(more=false)=>{
        const own=++tableSeq,select=form.elements.table,status=el.querySelector('[data-add-table-status]'),moreButton=el.querySelector('[data-add-table-more]');
        const slug=form.elements.table_scope.value||null,kind=form.elements.table_kind.value;
        // A new scope/kind/mode invalidates the previous choices and any frozen request at once; Add stays off until this query succeeds.
        if(!more){select.replaceChildren();tableCursor=null;moreButton.hidden=true;submission=null;}
        tableReady=false;syncAdd();
        status.textContent='Loading tables…';
        try {
          const data=await crmCall(slug,'tables_list',{limit:50,...(kind?{table_kind:kind}:{}),...(more&&tableCursor?{cursor:tableCursor}:{})});
          if(own!==tableSeq || !el.isConnected)return;
          if(!more)select.replaceChildren();
          for(const table of data.items || []){
            const option=new Option(`${table.table_kind==='crm'?'CRM · ':'Table · '}${table.title}${table.archived?' (archived)':''}`,table.id);
            option.dataset.businessId=table.business_id ?? '';select.append(option);
          }
          tableCursor=data.next_cursor || null;moreButton.hidden=!tableCursor;
          tableReady=true;syncAdd();
          status.textContent=select.options.length?'':'No tables in this scope.';
        } catch(failure){
          if(own===tableSeq && el.isConnected){select.replaceChildren();moreButton.hidden=true;tableReady=false;syncAdd();status.textContent='Could not load tables. '+(accessLost(failure)||sourceGone(failure)?'This scope is unavailable.':uiFailure(failure,{load:true}));}
        }
      };
      form.elements.table_scope.onchange=()=>loadTables();form.elements.table_kind.onchange=()=>loadTables();
      el.querySelector('[data-add-table-more]').onclick=()=>loadTables(true);
      form.elements.table.onchange=()=>{submission=null;syncAdd();};
      form.elements.title.focus();
      form.onsubmit=event=>{event.preventDefault();el.querySelector('.mfoot button:last-child')?.click();};
    }});
}


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
    Object.assign(dock.style,{left:left+'px',top:top+'px',width:Math.max(0,right-left)+'px',height:Math.max(0,bottom-top)+'px'});
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
