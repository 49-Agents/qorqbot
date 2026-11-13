// Shared owner queue. Message read cursors and Signoff decisions remain independent.
const attentionQueue=(()=>{
  const visibleLimit=6;
  let items=[],loading=false,error='',timer,loadPromise=null,loadAgain=false,generation=0,saving=0,refreshAfterSave=false,writes=Promise.resolve();
  let queueRows=[],sideChats=[];
  // Calendar events join the strip an hour before they start and stay until the owner discards them (saved on the server).
  let calendarRows=[],calendarCalendars=[],calendarError='',discardedKeys=new Set();
  const pendingDiscards=new Set();
  const mounts=new Set(),lists=new Set(),acknowledged=new Set(),signoffMounts=new Map();
  let queueOkAt=0,signoffOkAt=0;
  let signoffs=[],signoffsLoading=false,signoffsError='',signoffTimer,signoffPromise=null,signoffAgain=false;
  // Due reminders addressed to the owner. Opening or reading one never changes it; only an explicit dismiss/snooze/cancel does.
  let dueReminders=[],remindersError='',recoveryCount=0,recoveryError=false,recoveryGeneration=0;
  async function refreshReminderRecovery(){
    const request=++recoveryGeneration; recoveryCount=0; recoveryError=false; paintSignoffs();
    try { const owner=await remVerifiedOwner(); if(request===recoveryGeneration) recoveryCount=[...remAttempts.values()].filter(a=>a.owner===owner).length; }
    catch (_) { if(request===recoveryGeneration) recoveryError=remAttempts.size>0; }
    if(request===recoveryGeneration)paintSignoffs();
  }
  const reminderIcon=typeof SG_BELL==='string' ? SG_BELL : '';
  const viewedIds=item=>item.viewed_ids?.length?item.viewed_ids:[item.id];
  const remaining=()=>items.filter(item=>!viewedIds(item).every(id=>acknowledged.has(id)));
  const destination=item=>{
    if(item.kind==='global' && item.agent_id)return JSON.stringify(['global',item.agent_id]);
    if(item.kind==='thread' && item.thread_id)return JSON.stringify(['thread',item.thread_id]);
    if(item.kind==='thread' && item.source?.id)return JSON.stringify(['thread',item.agent_id,item.source.type,item.source.business_id || item.biz || null,item.source.id]);
    if(item.kind==='viewbox')return JSON.stringify(['viewbox',item.agent_id,item.viewbox_item_id]);
    if(item.kind==='signoff')return JSON.stringify(['signoff',item.signoff_id]);
    return item.id;
  };
  function distinctDestinations(rows){
    const groups=new Map();
    // The API orders newest first. Keep that destination's newest preview and
    // every underlying ID so dismissing its one tile clears the complete group.
    for(const item of rows){
      if(item.kind==='signoff' || item.kind==='reminder' || item.signoff_id || item.source?.type==='signoff')continue;
      const key=destination(item),previous=groups.get(key),ids=item.viewed_ids?.length?item.viewed_ids:[item.id];
      if(previous){
        previous.viewed_ids=[...new Set([...previous.viewed_ids,...ids])];
        // Legacy delivery and native chat share one DM: keep the newest preview, but reach the live conversation.
        if(item.kind==='global' && item.chat)previous.chat=true;
      }
      else groups.set(key,{...item,viewed_ids:[...ids]});
    }
    return [...groups.values()];
  }
  // Classify from operation metadata, never the editable title or Focus display name.
  const messageOperations=new Set(['telegram_send','telegram_send_file','agents_message_send']);
  const signoffPaths={
    message:'M21 11.5a8.5 8.5 0 0 1-8.5 8.5 9 9 0 0 1-4-.9L3 21l1.9-5.5a9 9 0 0 1-.9-4A8.5 8.5 0 0 1 12.5 3H13a8.5 8.5 0 0 1 8 8v.5Z',
    email:'M3 5h18v14H3zM3 5l9 7 9-7',
    approval:'M9 5H5v16h14V5h-4M9 3h6v4H9zM8 14l3 3 5-6',
  };
  const signoffIcon=s=>{
    const kind=(window.signoffMessageRenderer?.(s.operation_name)?.email || /^(account_email_|email_)(send|reply|forward)$/.test(s.operation_name || ''))
      || ['email.send','email.batch'].includes(s.platform?.submission?.tool) ? 'email'
      : (messageOperations.has(s.operation_name) || window.signoffMessageRenderer?.(s.operation_name)?.message) ? 'message' : 'approval';
    return `<span class="attention-signoff" data-signoff-kind="${kind}" aria-hidden="true"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="${signoffPaths[kind]}"/></svg></span>`;
  };
  const icon=item=>item.kind==='signoff'
    ? signoffIcon(item)
    : item.kind==='viewbox' ? '<span class="attention-viewbox" aria-hidden="true"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 3H6v18h12V7zM14 3v5h4M9 12h6M9 16h6"/></svg></span>'
    : typeof identityAvatar==='function' ? identityAvatar('agent',item.agent_name,{agent_id:item.agent_id,...(typeof globalAgentProfile==='function'?globalAgentProfile(item.agent_id):null),...item.presentation}) : '<span aria-hidden="true">✉</span>';
  const label=item=>item.kind==='signoff'?'Review · '+item.title:item.agent_name+' · '+item.text;
  const button=item=>`<button type="button" class="attention-chip" data-attention-key="${esc(item.id)}" title="${esc(label(item))}\nRight-click to dismiss" aria-label="${esc(label(item))}">${icon(item)}<span class="attention-label">${esc(item.kind==='signoff'?item.title:item.text)}</span></button>`;
  function paint(host){
    if(!host.isConnected){mounts.delete(host);return;}
    const current=remaining(),visible=current.slice(0,visibleLimit);
    host.innerHTML=`<div class="attention-stack">${visible.map(button).join('')}${current.length>visibleLimit?`<button type="button" class="attention-more" data-attention-more aria-label="Show all ${current.length} attention items">+${current.length-visibleLimit}</button>`:''}${!current.length?`<span class="attention-empty">${error?'Attention unavailable':loading?'Loading attention…':'Nothing new'}</span>`:''}</div>${error?'<button type="button" class="quiet attention-retry" data-attention-retry>Retry</button>':''}`;
  }
  const paintAll=()=>{mounts.forEach(paint);const current=remaining();lists.forEach(host=>{if(!host.isConnected){lists.delete(host);return;}host.innerHTML=current.length?current.map(button).join(''):'<p class="muted">Nothing new</p>';});paintSignoffs();};
  const unseen=item=>!viewedIds(item).every(id=>acknowledged.has(id));
  const signoffUnread=id=>queueRows.some(item=>unseen(item) && (item.signoff_id===id || item.source?.type==='signoff' && item.source.id===id));
  const reminderKey=r=>`reminder:${r.reminder_id}:${r.occurrence_id}:${r.revision}`;
  const reminderUnread=r=>queueRows.some(item=>item.id===reminderKey(r) && unseen(item));
  function acceptQueue(data){queueRows=data.items || [];items=distinctDestinations(queueRows);sideChats=data.side_chats || [];discardedKeys=new Set(data.discarded || []);}
  const calendarIcon=()=>typeof T_VIEW_ICON==='object' ? T_VIEW_ICON.calendar || '' : '';
  const calendarVisible=()=>{const now=Date.now();return calendarRows.filter(row=>row.appear<=now && !discardedKeys.has(row.key) && !pendingDiscards.has(row.key));};
  function paintSignoffs(){
    for(const [host,mount] of signoffMounts){
      if(!host.isConnected){signoffMounts.delete(host);continue;}
      // Open side chats stay at the left in last-message order, including chats already read.
      const chats=sideChats.map(s=>`<button type="button" class="focus-signoff-chip focus-side-chat-chip" data-side-chat-key="${esc(s.id)}" data-unread="${!!s.unread}" aria-label="${esc(s.title)}${s.unread?' · Unread':''}" title="${esc(s.title+'\n'+s.preview)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="${signoffPaths.message}"/></svg></button>`).join('');
      // Due reminders and calendar events share one run, oldest first; a calendar chip stays until it is discarded.
      const now=Date.now();
      const bells=dueReminders.map(r=>{const unread=reminderUnread(r);return {at:Date.parse(r.due_at || r.at) || 0,html:`<button type="button" class="focus-signoff-chip focus-reminder-chip" data-reminder-key="${esc(r.reminder_id)}" data-reminder-state="due" data-unread="${unread}" aria-label="${esc('Reminder due: '+r.text)}${unread?' · Unread':''}" title="${esc(r.text)}">${reminderIcon}</button>`};});
      const events=calendarVisible().map(row=>({at:row.appear,html:`<button type="button" class="focus-signoff-chip focus-calendar-chip" data-calendar-key="${esc(row.key)}" data-calendar-state="${row.appear+(row.event.start.length===10?0:3600000)<=now?'past':'upcoming'}" aria-label="${esc('Calendar: '+row.label)}" title="${esc(row.label+'\nRight-click to discard')}">${calendarIcon()}</button>`}));
      const due=[...bells,...events].sort((a,b)=>a.at-b.at).map(item=>item.html).join('');
      const recovery=recoveryCount ? '<button type="button" class="quiet" data-reminder-recovery>Unconfirmed reminder changes</button>' : recoveryError ? '<button type="button" class="quiet" data-reminder-recovery-check>Retry reminder session</button>' : '';
      if(mount.sideChatHost){
        const chatHost=mount.sideChatHost;
        if(chatHost._signoffHTML!==chats){chatHost.innerHTML=chats;chatHost._signoffHTML=chats;}
        chatHost.hidden=!chats;
        const divider=chatHost.parentElement?.querySelector('.agfocus-divider');
        if(divider)divider.hidden=!chats;
      }
      const markup=(mount.sideChatHost?'':chats)+due+recovery+signoffs.map(s=>{const title=mount.name?.(s.id)||s.title,unread=signoffUnread(s.id);return `<button type="button" class="focus-signoff-chip" data-signoff-key="${esc(s.id)}" data-unread="${unread}" aria-label="Review ${esc(title)}${unread?' · Unread':''}" title="${esc(title)}">${signoffIcon(s)}</button>`;}).join('');
      const html=markup+(signoffsError?'<button type="button" class="quiet" data-signoffs-retry>Retry signoffs</button>':'')+(remindersError?'<button type="button" class="quiet" data-reminders-retry>Retry reminders</button>':'')+(calendarError?'<button type="button" class="quiet" data-calendar-retry title="'+esc(calendarError)+'">Retry calendar</button>':'');
      if(host._signoffHTML!==html){host.innerHTML=html;host._signoffHTML=html;}
      host.hidden=!markup && !signoffsError && !remindersError && !calendarError;
    }
  }
  async function loadSignoffs(){
    signoffsLoading=true;
    try{
      const rows=[],seen=new Set();let cursor=null;
      do{
        const query=new URLSearchParams({states:'pending',limit:'50'});if(cursor)query.set('cursor',cursor);
        const data=coreData(await api('/root/signoffs?'+query));
        if(!Array.isArray(data.items))throw new Error('Invalid signoff list');
        rows.push(...data.items);cursor=data.next_cursor;
        if(cursor && seen.has(cursor))throw new Error('Repeated signoff page');
        seen.add(cursor);
      }while(cursor);
      signoffs=[...new Map(rows.filter(s=>s.state==='pending').map(s=>[s.id,s])).values()];signoffsError='';signoffOkAt=Date.now();
    }catch(e){signoffsError=e.message;if(accessLost(e)){signoffs=[];signoffOkAt=0;}}
    finally{signoffsLoading=false;paintSignoffs();}
  }
  // A failed or denied reminder read keeps the last list; losing access hides reminders entirely.
  async function loadReminders(){
    try{dueReminders=await remindersOwnerDue();remindersError='';}
    catch(e){
      if(accessLost(e)){dueReminders=[];remindersError='';}
      else remindersError=e.message||'Reminders unavailable';
    }
  }
  // The browser reads the owner's calendars itself. A failed read keeps the last events; losing access removes them.
  async function loadCalendar(){
    if(typeof calendarQueueLoad!=='function')return;
    try{const data=await calendarQueueLoad();calendarRows=data.items;calendarCalendars=data.calendars;calendarError=data.errors.join(' · ');}
    catch(e){
      if(accessLost(e)){calendarRows=[];calendarCalendars=[];calendarError='';}
      else calendarError=e.message || 'Calendar unavailable';
    }
  }
  // One read at a time; an explicit call in flight records a single trailing read.
  // `ordinary` is a mount: it reuses an in-flight read or a recent success and never records a trailing read.
  function refreshSignoffs(ordinary=false){
    if(!ordinary)signoffOkAt=0;
    if(!signoffMounts.size)return Promise.resolve();
    if(ordinary && signoffPromise)return signoffPromise;
    if(ordinary && !signoffsError && Date.now()-signoffOkAt<60000){
      clearTimeout(signoffTimer);signoffTimer=setTimeout(()=>{void (signoffPromise||refreshSignoffs());},60000-(Date.now()-signoffOkAt));
      return Promise.resolve();
    }
    if(signoffPromise){signoffAgain=true;return signoffPromise;}
    signoffPromise=(async()=>{
      try{do{signoffAgain=false;await Promise.all([loadSignoffs(),typeof remindersOwnerDue==='function'?loadReminders():null,loadCalendar()]);paintSignoffs();}while(signoffAgain && signoffMounts.size);}
      finally{signoffPromise=null;clearTimeout(signoffTimer);if(signoffMounts.size)signoffTimer=setTimeout(()=>{void (signoffPromise||refreshSignoffs());},60000);}
    })();
    return signoffPromise;
  }
  function reminderContextMenu(event){
    const calendar=event.target.closest('[data-calendar-key]');
    if(calendar && event.currentTarget.contains(calendar)){event.preventDefault();void discardCalendar(calendar.dataset.calendarKey);return;}
    const chip=event.target.closest('[data-reminder-key]'); if(!chip || !event.currentTarget.contains(chip))return;
    event.preventDefault();
    const reminder=dueReminders.find(r=>r.reminder_id===chip.dataset.reminderKey);
    if(reminder)void remindersDismissDue(reminder).catch(error=>toast(error.message || 'Could not dismiss reminder.'));
  }
  function signoffClick(event){
    if(event.target.closest('[data-reminder-recovery-check]')){void refreshReminderRecovery();return;}
    if(event.target.closest('[data-reminder-recovery]')){void remindersOpenUnconfirmed();return;}
    const chatKey=event.target.closest('[data-side-chat-key]')?.dataset.sideChatKey;
    if(chatKey){const chat=sideChats.find(s=>s.id===chatKey);if(chat)sideChatOpen(chat);return;}
    if(event.target.closest('[data-signoffs-retry],[data-reminders-retry],[data-calendar-retry]')){void refreshSignoffs();return;}
    const calendarChip=event.target.closest('[data-calendar-key]');
    if(calendarChip){
      const row=calendarRows.find(item=>item.key===calendarChip.dataset.calendarKey);
      // Provider writes made from the event dialog reach the strip through the calendar change listener.
      if(row)void Promise.resolve(calendarOpen(row.event,calendarCalendars,()=>{},row.event,calendarChip)).catch(e=>toast(e.message));
      return;
    }
    const reminderKey=event.target.closest('[data-reminder-key]')?.dataset.reminderKey;
    if(reminderKey){
      const reminder=dueReminders.find(r=>r.reminder_id===reminderKey);
      if(reminder)remindersOpenDetail(reminder,{onChange:(next,op)=>{if(op!=='reminders_get' || next?.state!=='due' || next.occurrence_id!==reminder.occurrence_id)void refreshSignoffs();}});
      return;
    }
    const key=event.target.closest('[data-signoff-key]')?.dataset.signoffKey,s=signoffs.find(item=>item.id===key);
    if(!s)return;
    const mount=signoffMounts.get(event.currentTarget);
    if(mount?.opener){
      // A caller may supply an opener that owns selection. It receives this canonical summary; without one the
      // queue opens the standard Signoff review modal below from the same source identity.
      const summary={type:'signoff',id:s.id,title:s.title,label:s.title,review_business_id:s.review_business_id??null,
        business_id:s.review_business_id??null,category:s.category,review_scope:s.review_scope,status_revision:s.status_revision};
      void Promise.resolve(mount.opener(summary)).catch(()=>{});
      return;
    }
    void open({kind:'signoff',signoff_id:s.id,business_id:s.review_business_id,category:s.category,globalReview:true,proposal_revision:s.proposal_revision,payload_hash:s.payload_hash,actionable:s.actionable}).catch(e=>toast(e.message));
  }
  async function loadQueue(){
    loading=true;const request=++generation;
    try{const data=coreData(await api('/root/attention-queue'));if(request===generation){acceptQueue(data);error='';queueOkAt=Date.now();}}
    catch(e){if(request===generation){error=e.message;if(accessLost(e)){acceptQueue({});queueOkAt=0;}}}
    finally{loading=false;paintAll();}
  }
  // One read at a time. Explicit triggers (focus, reply change, retry) arriving in flight
  // record a single trailing read and resolve after it; the poll timer never does.
  // `ordinary` is a mount: it reuses an in-flight read or a recent success and never records a trailing read.
  function refresh(ordinary=false){
    if(ordinary && !saving){
      if(loadPromise)return loadPromise;
      if(!error && Date.now()-queueOkAt<10000){
        clearTimeout(timer);timer=setTimeout(()=>{void (loadPromise||refresh());},10000-(Date.now()-queueOkAt));
        return Promise.resolve();
      }
    }
    // During a save the caller gets the write chain: the deferred read starts after it settles and is not awaited.
    if(saving){refreshAfterSave=true;return writes;}
    if(loadPromise){loadAgain=true;return loadPromise;}
    loadPromise=(async()=>{
      try{
        do{loadAgain=false;await loadQueue();}while(loadAgain && !saving && (mounts.size || signoffMounts.size));
        if(loadAgain && saving)refreshAfterSave=true;
      }finally{loadPromise=null;clearTimeout(timer);if(mounts.size || signoffMounts.size)timer=setTimeout(()=>{void (loadPromise||refresh());},10000);}
    })();
    return loadPromise;
  }
  // Serialize additive preference writes. An older poll cannot restore a dismissed
  // item, and one completed dismissal cannot be discarded by another in flight.
  function persist(change,rollback,done,failure){
    saving++;generation++;
    const saved=writes.then(async()=>{
      const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
      try{
        const data=coreData(await post('/root/attention-queue',change,{signal:controller.signal}));
        acceptQueue(data);error='';queueOkAt=Date.now();done?.();paintAll();
      }catch(e){rollback();paintAll();toast(failure+': '+(e.name==='AbortError'?'Save timed out. Right-click again to retry.':e.message));}
      finally{
        clearTimeout(timeout);
        saving--;
        if(!saving && refreshAfterSave){refreshAfterSave=false;void refresh();}
      }
    });
    writes=saved.catch(()=>{});return saved;
  }
  function acknowledge(ids){
    const keys=[...new Set(ids || [])].filter(id=>!acknowledged.has(id));
    if(!keys.length)return Promise.resolve();
    keys.forEach(id=>acknowledged.add(id));
    // Clear every mounted copy immediately; keep the source rows for rollback on failure.
    paintAll();
    return persist({viewed:keys},()=>keys.forEach(id=>acknowledged.delete(id)),null,'Could not clear attention');
  }
  // Right-click on a calendar chip: gone at once, kept off the strip by the server, never touching the provider event.
  function discardCalendar(key){
    if(!key || pendingDiscards.has(key) || discardedKeys.has(key))return Promise.resolve();
    pendingDiscards.add(key);paintAll();
    return persist({discarded:[key]},()=>pendingDiscards.delete(key),()=>pendingDiscards.delete(key),'Could not discard calendar event');
  }
  function signoffViewed(s,element){
    if(document.hidden)return;
    if(element && (!element.isConnected || !element.getClientRects().length || element.closest('[hidden],[inert],[aria-hidden="true"]') || getComputedStyle(element).visibility==='hidden'))return;
    const key=`signoff:${s.id}:${s.proposal_revision}:${s.payload_hash}`;
    void acknowledge([key]);
  }
  function reminderViewed(r){
    if(document.hidden || r.state!=='due' || !r.occurrence_id)return;
    void acknowledge([reminderKey(r)]);
  }
  function viewboxViewed(item){
    const key=item.attention_id;
    if(document.hidden || !key)return;
    void acknowledge([key]);
  }
  async function open(item){
    if(item.kind==='viewbox'){await agviewboxNavigate(item);return;}
    if(item.kind!=='signoff'){
      // Native/private read cursors clear the queue only after the conversation actually loads.
      // Legacy Telegram/capture entries have no read cursor: show their exact text before clearing.
      if(item.kind==='global' && !item.chat){
        await window.focusExpansion.open({title:item.agent_name,agentId:item.agent_id,mount(host,session){
          const message=document.createElement('p');message.className='attention-message';message.textContent=item.body || item.text || '';
          const button=document.createElement('button');button.type='button';button.className='quiet';button.textContent='Open chat';
          button.onclick=()=>void replyNotifications.open(item);
          host.append(message,button);void acknowledge(item.viewed_ids);
          return ()=>{button.onclick=null;};
        }});
      }else{
        const chat=item.chat && document.querySelector(`[data-global-chat="${CSS.escape(item.agent_id)}"]`);
        if(chat?.getClientRects().length && !chat.closest('[hidden],[inert],[aria-hidden="true"]') && getComputedStyle(chat).visibility!=='hidden'){
          chat.querySelector('.gchat-jump')?.click();
          chat.querySelector('.gchat-composer textarea')?.focus({preventScroll:true});return;
        }
        await replyNotifications.open(item);
      }
      return;
    }
    await sgFocusOpen('Signoff', {workspace:()=>item.biz || 'scope:global',
      global:item.globalReview || !item.business_id,category:item.category,item:()=>({id:item.signoff_id,businessId:item.business_id}),
      waitingOnly:false,execution:true,...(item.proposal_revision != null && item.payload_hash ? {expectedBinding:{signoff_id:item.signoff_id,proposal_revision:item.proposal_revision,payload_hash:item.payload_hash,actionable:item.actionable !== false}} : {})}, () => { void refreshSignoffs(); });
  }
  function dismiss(event){
    const target=event.target.closest('[data-attention-key]');
    const item=target && items.find(row=>row.id===target.dataset.attentionKey);
    if(!item)return;
    event.preventDefault();event.stopPropagation();
    void acknowledge(viewedIds(item));
  }
  function clicks(event){
    const target=event.target.closest('button');if(!target)return;
    if(target.hasAttribute('data-attention-retry')){void refresh();return;}
    if(target.hasAttribute('data-attention-more')){
      void window.focusExpansion.open({title:'New attention',mount(host){
        host.classList.add('attention-all');host.innerHTML=remaining().map(button).join('');
        lists.add(host);host.addEventListener('click',clicks);host.addEventListener('contextmenu',dismiss);
        return ()=>{lists.delete(host);host.removeEventListener('click',clicks);host.removeEventListener('contextmenu',dismiss);};
      }});return;
    }
    const item=items.find(row=>row.id===target.dataset.attentionKey);
    if(item){
      void open(item).catch(e=>toast(e.message));
    }
  }
  document.addEventListener('reminder-attempts-changed',()=>{if(signoffMounts.size)void refreshReminderRecovery();});
  document.addEventListener('reply-notifications-changed',()=>{if(mounts.size || signoffMounts.size)void refresh();});
  window.addEventListener('focus',()=>{if(mounts.size || signoffMounts.size)void refresh();void refreshSignoffs();if(signoffMounts.size)void refreshReminderRecovery();});
  if(typeof onChange==='function')onChange(['signoffs'],null,()=>{void refreshSignoffs();if(mounts.size || signoffMounts.size)void refresh();});
  return {signoffViewed,reminderViewed,viewboxViewed,refresh,refreshSignoffs,paintSignoffs,
    mountSignoffs(host,name,opener,options={}){const sideChatHost=options.sideChatHost;const calendarListener=()=>void refreshSignoffs();if(typeof calendarChanges!=='undefined')calendarChanges.add(calendarListener);signoffMounts.set(host,{name,opener,sideChatHost,calendarListener});host.addEventListener('click',signoffClick);sideChatHost?.addEventListener('click',signoffClick);host.addEventListener('contextmenu',reminderContextMenu);paintSignoffs();void refreshSignoffs(true);void refresh(true);void refreshReminderRecovery();
      return ()=>{if(typeof calendarChanges!=='undefined')calendarChanges.delete(calendarListener);signoffMounts.delete(host);host.removeEventListener('click',signoffClick);sideChatHost?.removeEventListener('click',signoffClick);host.removeEventListener('contextmenu',reminderContextMenu);delete host._signoffHTML;if(sideChatHost)delete sideChatHost._signoffHTML;if(!signoffMounts.size){clearTimeout(signoffTimer);if(!mounts.size)clearTimeout(timer);}};},
    mount(host){mounts.add(host);host.addEventListener('click',clicks);host.addEventListener('contextmenu',dismiss);paint(host);void refresh(true);
    return ()=>{mounts.delete(host);host.removeEventListener('click',clicks);host.removeEventListener('contextmenu',dismiss);if(!mounts.size && !signoffMounts.size)clearTimeout(timer);};}};
})();
