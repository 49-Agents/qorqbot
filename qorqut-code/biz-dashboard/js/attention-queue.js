// Shared owner queue. Message read cursors and Signoff decisions remain independent.
const attentionQueue=(()=>{
  const visibleLimit=6;
  let items=[],loading=false,error='',timer,loadPromise=null,loadAgain=false,generation=0,saving=0,refreshAfterSave=false,writes=Promise.resolve();
  const mounts=new Set(),lists=new Set(),acknowledged=new Set(),signoffMounts=new Map();
  let signoffs=[],signoffsLoading=false,signoffsError='',signoffTimer,signoffPromise=null,signoffAgain=false;
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
      if(item.kind==='signoff' || item.signoff_id || item.source?.type==='signoff')continue;
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
  const messageOperations=new Set(['whatsapp_send','telegram_send','telegram_send_file','agents_message_send']);
  const signoffPaths={
    message:'M21 11.5a8.5 8.5 0 0 1-8.5 8.5 9 9 0 0 1-4-.9L3 21l1.9-5.5a9 9 0 0 1-.9-4A8.5 8.5 0 0 1 12.5 3H13a8.5 8.5 0 0 1 8 8v.5Z',
    email:'M3 5h18v14H3zM3 5l9 7 9-7',
    approval:'M9 5H5v16h14V5h-4M9 3h6v4H9zM8 14l3 3 5-6',
  };
  const signoffIcon=s=>{
    const kind=/^(account_email_|email_)(send|reply|forward)$/.test(s.operation_name || '')
      || ['email.send','email.batch'].includes(s.platform?.submission?.tool) ? 'email'
      : messageOperations.has(s.operation_name) ? 'message' : 'approval';
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
  const paintAll=()=>{mounts.forEach(paint);const current=remaining();lists.forEach(host=>{if(!host.isConnected){lists.delete(host);return;}host.innerHTML=current.length?current.map(button).join(''):'<p class="muted">Nothing new</p>';});};
  function paintSignoffs(){
    for(const [host,mount] of signoffMounts){
      if(!host.isConnected){signoffMounts.delete(host);continue;}
      const markup=signoffs.map(s=>{const title=mount.name?.(s.id)||s.title;return `<button type="button" class="focus-signoff-chip" data-signoff-key="${esc(s.id)}" aria-label="Review ${esc(title)}" title="${esc(title)}">${signoffIcon(s)}</button>`;}).join('');
      const html=markup+(signoffsError?'<button type="button" class="quiet" data-signoffs-retry>Retry signoffs</button>':'');
      if(host._signoffHTML!==html){host.innerHTML=html;host._signoffHTML=html;}
      host.hidden=!signoffs.length&&!signoffsError;
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
      signoffs=[...new Map(rows.filter(s=>s.state==='pending').map(s=>[s.id,s])).values()];signoffsError='';
    }catch(e){signoffsError=e.message;}
    finally{signoffsLoading=false;paintSignoffs();}
  }
  // One read at a time; an explicit call in flight records a single trailing read.
  function refreshSignoffs(){
    if(!signoffMounts.size)return Promise.resolve();
    if(signoffPromise){signoffAgain=true;return signoffPromise;}
    signoffPromise=(async()=>{
      try{do{signoffAgain=false;await loadSignoffs();}while(signoffAgain && signoffMounts.size);}
      finally{signoffPromise=null;clearTimeout(signoffTimer);if(signoffMounts.size)signoffTimer=setTimeout(()=>{void (signoffPromise||refreshSignoffs());},60000);}
    })();
    return signoffPromise;
  }
  function signoffClick(event){
    if(event.target.closest('[data-signoffs-retry]')){void refreshSignoffs();return;}
    const key=event.target.closest('[data-signoff-key]')?.dataset.signoffKey,s=signoffs.find(item=>item.id===key);
    if(!s)return;
    const mount=signoffMounts.get(event.currentTarget);
    if(mount?.opener){
      // A configured opener owns selection. It builds the normal shared workspace entry from this canonical
      // summary; a false result (a cancelled discard, for example) leaves the old pane in place and never falls
      // through to the unrelated legacy Signoff modal. The opener owns its workspace error surface.
      const summary={type:'signoff',id:s.id,title:s.title,label:s.title,review_business_id:s.review_business_id??null,
        business_id:s.review_business_id??null,category:s.category,review_scope:s.review_scope,status_revision:s.status_revision};
      void Promise.resolve(mount.opener(summary)).catch(()=>{});
      return;
    }
    void open({kind:'signoff',signoff_id:s.id,business_id:s.review_business_id,category:s.category,globalReview:true}).catch(e=>toast(e.message));
  }
  async function loadQueue(){
    loading=true;const request=++generation;
    try{const data=coreData(await api('/root/attention-queue'));if(request===generation){items=distinctDestinations(data.items || []);error='';}}
    catch(e){if(request===generation)error=e.message;}
    finally{loading=false;paintAll();}
  }
  // One read at a time. Explicit triggers (mount, focus, reply change, retry) arriving in flight
  // record a single trailing read and resolve after it; the poll timer never does.
  function refresh(){
    // During a save the caller gets the write chain: the deferred read starts after it settles and is not awaited.
    if(saving){refreshAfterSave=true;return writes;}
    if(loadPromise){loadAgain=true;return loadPromise;}
    loadPromise=(async()=>{
      try{
        do{loadAgain=false;await loadQueue();}while(loadAgain && !saving && mounts.size);
        if(loadAgain && saving)refreshAfterSave=true;
      }finally{loadPromise=null;clearTimeout(timer);if(mounts.size)timer=setTimeout(()=>{void (loadPromise||refresh());},10000);}
    })();
    return loadPromise;
  }
  function acknowledge(ids){
    const keys=[...new Set(ids || [])].filter(id=>!acknowledged.has(id));
    if(!keys.length)return Promise.resolve();
    keys.forEach(id=>acknowledged.add(id));saving++;generation++;
    // Clear every mounted copy immediately; keep the source rows for rollback on failure.
    paintAll();
    // Serialize additive preference writes. An older poll cannot restore a dismissed
    // item, and one completed dismissal cannot be discarded by another in flight.
    const saved=writes.then(async()=>{
      const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
      try{
        const data=coreData(await post('/root/attention-queue',{viewed:keys},{signal:controller.signal}));
        items=distinctDestinations(data.items || []);error='';paintAll();
      }catch(e){keys.forEach(id=>acknowledged.delete(id));paintAll();toast('Could not clear attention: '+(e.name==='AbortError'?'Save timed out. Right-click again to retry.':e.message));}
      finally{
        clearTimeout(timeout);
        saving--;
        if(!saving && refreshAfterSave){refreshAfterSave=false;void refresh();}
      }
    });
    writes=saved.catch(()=>{});return saved;
  }
  function signoffViewed(s){
    if(document.hidden)return;
    const key=`signoff:${s.id}:${s.proposal_revision}:${s.payload_hash}`;
    void acknowledge([key]);
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
        modal({title:esc(item.agent_name),body:`<p class="attention-message">${esc(item.body || item.text)}</p>`,
          actions:[{label:'Open chat',onClick:close=>{close();void replyNotifications.open(item);}}],onOpen:()=>acknowledge(item.viewed_ids)});
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
    let view,host;
    modal({title:'Signoff',width:760,stack:true,body:'<div class="attention-signoff-view"></div>',onOpen:el=>{
      host=el.querySelector('.attention-signoff-view');
      view=sgCreateView({active:()=>host.isConnected,body:()=>host,workspace:()=>item.biz || 'scope:global',
        global:item.globalReview || !item.business_id,category:item.category,item:()=>({id:item.signoff_id,businessId:item.business_id}),
        waitingOnly:false,execution:true});
      view.paint();
    },onClose:()=>{view?.dispose();void refreshSignoffs();}});
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
      let host;
      modal({title:'New attention',width:540,body:`<div class="attention-all">${remaining().map(button).join('')}</div>`,
        onOpen:el=>{host=el.querySelector('.attention-all');lists.add(host);host.addEventListener('click',clicks);host.addEventListener('contextmenu',dismiss);},
        onClose:()=>{lists.delete(host);host?.removeEventListener('click',clicks);host?.removeEventListener('contextmenu',dismiss);}});return;
    }
    const item=items.find(row=>row.id===target.dataset.attentionKey);
    if(item){
      if((item.kind==='viewbox' || item.kind==='global' && item.chat) && target.closest('.attention-all'))_modal?.close();
      void open(item).catch(e=>toast(e.message));
    }
  }
  document.addEventListener('reply-notifications-changed',()=>{if(mounts.size)void refresh();});
  window.addEventListener('focus',()=>{if(mounts.size)void refresh();void refreshSignoffs();});
  if(typeof onChange==='function')onChange(['signoffs'],null,()=>{void refreshSignoffs();});
  return {signoffViewed,viewboxViewed,refresh,refreshSignoffs,paintSignoffs,
    mountSignoffs(host,name,opener){signoffMounts.set(host,{name,opener});host.addEventListener('click',signoffClick);paintSignoffs();void refreshSignoffs();
      return ()=>{signoffMounts.delete(host);host.removeEventListener('click',signoffClick);if(!signoffMounts.size)clearTimeout(signoffTimer);};},
    mount(host){mounts.add(host);host.addEventListener('click',clicks);host.addEventListener('contextmenu',dismiss);paint(host);void refresh();
    return ()=>{mounts.delete(host);host.removeEventListener('click',clicks);host.removeEventListener('contextmenu',dismiss);if(!mounts.size)clearTimeout(timer);};}};
})();
