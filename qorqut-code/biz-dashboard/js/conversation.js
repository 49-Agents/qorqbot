// One owner–Ordubashy private conversation per source. Local drafts only, and no viewing telemetry
// beyond the explicit private read acknowledgment. Record threads have exactly one channel: the owner's
// default Ordubashy conversation. No request names an agent, peer or team channel; the host rejects any
// other selector separately.
const conversationStates = new Map();
let conversationMountId = 0;
// Inline Focus conversations dismiss on outside clicks, while their contents stay interactive.
document.addEventListener('click',event=>{
  // composedPath() is frozen at dispatch time: a reactive re-render can leave
  // event.target detached, which would make a plain .contains(event.target)
  // check wrongly read as "outside" and collapse the panel the click opened.
  const path=event.composedPath();
  for(const host of document.querySelectorAll('[data-inline-conversation]')) {
    if(host._conversation?.dismissOnOutside===false)continue;
    if(!path.includes(host.closest('.agfocus-card') || host) && host._conversation?.isExpanded())host._conversation.collapse();
  }
});
function conversationKey(source) { return `${source.type}:${source.biz || 'global'}:${source.id}`; }
// Shared state is keyed by source only: one draft, cursor and retry per record thread.
function conversationNewState(source) {
  return {source:conversationKey(source),text:'',files:[],notes:[],participants:[],loaded:false,before:null,mounts:new Set(),conversation:null,readState:null};
}
function conversationState(source) {
  const key=conversationKey(source);
  if (!conversationStates.has(key)) conversationStates.set(key,conversationNewState(source));
  return conversationStates.get(key);
}
function conversationPersonName(person) {
  const profile=typeof globalAgentProfile==='function'?globalAgentProfile(person.created_by || person.actor_id):null;
  return person.created_by_name || person.author_name || person.display_name || person.name || profile?.display_name || profile?.name || 'Unknown participant';
}
function conversationGlyph(person,size) {
  const profile=typeof globalAgentProfile==='function'?globalAgentProfile(person.created_by):null;
  return person.author_kind==='agent' && typeof agentGlyph==='function'
    ? agentGlyph({...person.presentation,...profile},size)
    : esc(conversationPersonName(person).slice(0,2));
}
// Presentation only: canonical bodies and transport payloads stay complete.
function messagePreview(body, summary) {
  const words=String(summary || plainSafeLinks(body) || '').trim().split(/\s+/u).filter(Boolean);
  return words.slice(0,10).join(' ') + (words.length>10?'…':'');
}
async function conversationRequest(promise) {
  let timer;
  try {
    return await Promise.race([promise,new Promise((_,reject)=>{
      timer=setTimeout(()=>reject(Object.assign(new Error('The request took too long. Try again.'),{code:'request_timeout'})),15000);
    })]);
  } finally {clearTimeout(timer);}
}
// A private call is Global whenever the mount has no business slug of its own
// (Focus cards), and then names the owning business inside the canonical source.
function conversationPrivateGlobal(source,adapter) {
  return adapter.privateGlobal!==undefined?!!adapter.privateGlobal:!source.biz;
}
function conversationPrivateSource(source,adapter) {
  if(adapter.privateSource)return adapter.privateSource;
  const value={type:source.type,id:source.id};
  if(conversationPrivateGlobal(source,adapter) && source.business_id)value.business_id=source.business_id;
  return value;
}
async function conversationPrivateCall(source,adapter,operation,args,options={}) {
  const global=conversationPrivateGlobal(source,adapter);
  const path=global?'/root':`/biz/${encodeURIComponent(source.biz)}`;
  return coreData(await conversationRequest(post(`${path}/operations/${operation}`,
    {scope:global?'global':'business',source:conversationPrivateSource(source,adapter),...args,...options})));
}
// Fixed, user-facing text for thread failures. Transport errors never reach the UI.
const CONVERSATION_TEXT = {load:"Couldn't load. Try again",unconfirmed:"Couldn't confirm delivery. Retry Send",changed:'Conversation changed. Review it before sending again',blocked:"Can't send here"};
// Send refusals that mean the source changed under the draft: the retry is dropped and the owner must review.
const CONVERSATION_REVIEW = ['revision_conflict','stale_signoff','not_found','forbidden','policy_denied','invalid_principal'];
// Normalizes canonical notes (already in controller order) into shared view entries.
function conversationEntries(notes,adapter) {
  let previous=null;
  return notes.map(note=>{
    const system=note.entry_kind==='system',owner=!system && (note.is_owner ?? (note.author_kind==='human'));
    const entry={key:note.id,speaker:system?'notice':owner?'user':'agent',text:note.body || '',timestamp:note.created_at,
      references:(note.refs || []).filter(ref=>ref.type!=='dev_ticket').map((ref,index)=>({key:`ref${index}`,label:ref.label || ref.type || 'Reference',disabled:!!ref.unavailable,value:ref})),
      actions:note.parent_event_id && adapter.openParent?[{key:'parent',label:'About an earlier entry'}]:[]};
    if(note.title && note.title!=='Message')entry.title=note.title;
    // Consecutive messages from the same sender share one header.
    const sender=system?null:owner?'\u0000owner':String(note.created_by ?? '');
    if(!system && !owner){
      entry.author={name:conversationPersonName(note),avatarHtml:conversationGlyph(note,31)};
      entry.showAuthor=previous!==sender;
    }
    previous=sender;
    return entry;
  });
}
// Related records have their own identities and pagination; they never enter the message store.
function conversationRecordKey(record) { return `${record.type}:${record.business_id || 'global'}:${record.id}`; }
function conversationRelatedRecords(s,source) {
  const records=[...(s.related || [])];
  // The source signoff itself is not returned by the relationships query.
  if(source.type==='signoff')records.unshift({...source,label:'Signoff'});
  for(const note of s.notes)for(const ref of note.refs || []) {
    if(ref.type==='signoff' && !ref.unavailable)records.push({...ref,created_at:note.created_at});
  }
  return [...new Map(records.map(record=>[conversationRecordKey(record),record])).values()];
}
// A related record is drawn by the existing reference components: the task chip, the inline Signoff card or an ordinary reference
// button. It is a visual entry only, never a message, and an unresolvable review scope is shown as unavailable rather than rerouted.
function conversationRecordEntry(record) {
  const business=record.business_id?(state.bizs || []).find(b=>b.id===record.business_id || b.business_id===record.business_id):null;
  const slug=record.business_slug || business?.slug || null;
  const value=slug?{...record,business_slug:slug}:record;
  const entry={key:`related:${conversationRecordKey(record)}`,speaker:'reference',referenceOnly:true,timestamp:record.created_at,references:[],typedReferences:[]};
  // A Project's canonical scope is its own id; unlike a business-scoped Signoff it needs no business slug.
  const typed={type:record.type,id:record.id,business_id:record.business_id || null,business_slug:slug,label:record.label,state:record.state,
    available:!record.unavailable && (record.type==='project' || !(record.business_id && !slug))};
  if(record.type==='signoff')entry.typedReferences.push(typed);
  else if(record.type==='project'){
    entry.typedReferences.push(typed);
    // The chip stays informational. An explicit action opens the Project reader without inventing a private channel.
    if(typed.available)entry.references.push({key:'open',label:'Open Project',value});
  } else if(record.type==='task'){
    entry.typedReferences.push(typed);
    if(typed.available)entry.references.push({key:'open',label:'Open task',value});
  } else entry.references.push({key:'open',label:record.label || 'Related item',disabled:!!record.unavailable,value});
  return entry;
}
function conversationTimeline(s,adapter,source) {
  const entries=conversationEntries(s.notes,adapter);
  if(!adapter.related)return entries;
  const earliest=s.before?s.notes[0]?.created_at:null;
  for(const record of conversationRelatedRecords(s,source)) {
    if(earliest && record.created_at<earliest)continue;
    entries.push(conversationRecordEntry(record));
  }
  return entries.sort((a,b)=>(a.timestamp || '').localeCompare(b.timestamp || ''));
}
function conversationMount(host,source,adapter={}) {
  if (!host) return;
  const key=conversationKey(source);
  let expanded=false;
  if (host.dataset.conversation===key) { host._source=source; return host._conversation; }
  host._conversation?.dispose?.();
  let disposed=false,view,chat,errorText='',restoring=false,saved=null;
  host.dataset.conversation=key;host._source=source;
  const s=conversationState(source);
  const recordSource={...source,business_slug:source.biz,business_id:source.business_id ?? adapter.privateSource?.business_id ?? (state.bizs || []).find(b=>b.slug===source.biz)?.id ?? null};
  let relatedReady=false;
  // Mounted Signoff cards keep their own reader lifecycle; this mount only refreshes and disposes them with the thread.
  const signoffViews=new Set();
  const mountId=++conversationMountId;
  const panelId=`conversation-panel-${mountId}`;
  host.innerHTML=`<button type="button" class="quiet conversation-toggle" aria-label="Comment" aria-expanded="false" aria-controls="${panelId}"><span class="conversation-toggle-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 12.5z"/></svg></span><span class="conversation-unread" data-conversation-unread aria-hidden="true" hidden></span></button><section id="${panelId}" class="conversation-panel" role="region" aria-label="Conversation with Ordubashy" hidden><div class="conversation-view"></div></section>`;
  const toggle=host.querySelector('.conversation-toggle'),panel=host.querySelector('.conversation-panel');
  const unreadDot=host.querySelector('[data-conversation-unread]');
  // Focus keeps its composer reachable while the feed is collapsed: the same shared form is placed outside the panel.
  let composerHost=null;
  if(adapter.inlineComposer) {
    host.dataset.inlineConversation='true';
    composerHost=document.createElement('div');composerHost.className='conversation-inline-compose';
    host.append(composerHost);
  }
  const composerLabel=adapter.inlineComposer?(adapter.ariaLabel ?? 'Leave a comment'):'Message';
  const defaultPlaceholder=()=>adapter.inlineComposer?(adapter.placeholder ?? 'Private message to Ordubashy…'):'Message…';
  s.mounts.add(host);
  const sameState=mount=>mount.isConnected && mount.dataset.conversation===key;
  // Silent for every mounted view of this exact state; a view whose text already matches keeps its caret.
  const syncDraft=()=>{for(const mount of [...s.mounts]){if(!sameState(mount)){s.mounts.delete(mount);continue;}mount._conversation?._setDraft(s.text);}};
  const current=()=>!disposed && host.isConnected && host.dataset.conversation===key && host._conversation===view;
  const captureScroll=()=>{if(expanded && !restoring)saved=chat.getScrollState();};
  function composerState() {
    return {hidden:false,inputDisabled:!!s.sending || !!s.submission,sendDisabled:!!s.sending,
      filesDisabled:!!s.sending || !!s.submission,attachments:s.files.map(item=>({key:item.key,name:item.file.name})),
      sendLabel:s.submission?'Retry Send':'Send',placeholder:defaultPlaceholder(),ariaLabel:composerLabel,maxLength:20000};
  }
  function renderComposer() {if(!disposed)chat.render({composer:composerState()},{scroll:'preserve'});}
  function syncSending() {
    for(const mount of s.mounts)if(sameState(mount))mount._conversation?._renderComposer();
  }
  function setError(text) {errorText=text;if(!disposed)chat.render({banner:{text,role:'alert'}},{scroll:'preserve'});}
  const clearError=()=>{if(errorText)setError('');};
  let painted='';
  function paint(mode='auto') {
    const signature=JSON.stringify([!!s.text,!!s.loaded,s.notes,s.participants,s.before,adapter.related?s.related:null,relatedReady]);
    if(painted===signature && mode==='auto')return;
    painted=signature;
    chat.render({entries:conversationTimeline(s,{...adapter,related:adapter.related && relatedReady},recordSource),older:{visible:!!s.before,disabled:false},empty:{text:s.loaded && !s.notes.length?'No messages yet':''}},{scroll:mode});
    if(adapter.inlineComposer)host.querySelectorAll('[title]').forEach(el=>el.removeAttribute('title'));
  }
  let relatedPending=false,relatedGeneration=0;
  const relatedMore=document.createElement('button');relatedMore.type='button';relatedMore.className='quiet conversation-related-more';relatedMore.hidden=true;
  panel.append(relatedMore);
  async function loadRelated(more=false) {
    if(!adapter.related || relatedPending || !current() || !expanded)return;
    relatedPending=true;relatedMore.disabled=true;
    const generation=relatedGeneration;
    try {
      let result=await conversationPrivateCall(source,adapter,'owner_conversation_related',{limit:50,...(more?{offset:s.relatedOffset}:{})});
      if(!current() || generation!==relatedGeneration)return;
      const records=more?[...(s.related || []),...(result.items || [])]:[...(result.items || [])];
      let pages=more?(s.relatedPages || 1)+1:1;
      // Refresh the depth already opened, so polling neither discards older related rows nor retains revoked ones.
      while(!more && pages<(s.relatedPages || 1) && result.next_offset!=null) {
        result=await conversationPrivateCall(source,adapter,'owner_conversation_related',{limit:50,offset:result.next_offset});
        if(!current() || generation!==relatedGeneration)return;
        records.push(...(result.items || []));pages++;
      }
      s.related=[...new Map(records.map(r=>[`${r.type}:${r.business_id || 'global'}:${r.id}`,r])).values()];
      s.relatedOffset=result.next_offset;s.relatedPages=pages;
      relatedMore.hidden=s.relatedOffset==null;relatedMore.textContent='More related items';
      relatedReady=true;
      paint();adapter.onRelated?.(s.related);
    } catch(e) {
      if(!current() || generation!==relatedGeneration)return;
      s.related=[];relatedReady=false;s.relatedOffset=null;s.relatedPages=0;paint();adapter.onRelated?.([]);
      relatedMore.hidden=false;relatedMore.textContent="Couldn't load related items · Retry";
    } finally {relatedPending=false;relatedMore.disabled=false;}
  }
  relatedMore.onclick=()=>loadRelated(s.relatedOffset!=null);
  // Acknowledge only what the server has already returned to this owner, and only
  // after an interactive load that included the newest page.
  async function acknowledge() {
    const stillVisible=()=>expanded && current() && adapter.isVisible?.()!==false;
    if(!stillVisible() || s.marking)return;
    const latest=s.conversation?.latest_sequence || 0;
    if(!latest || latest<=(s.readState?.last_read_sequence || 0))return;
    s.marking=true;
    try {
      // Do not acknowledge a load that the owner has already closed while the history request was in flight.
      if(!stillVisible())return;
      const data=await conversationPrivateCall(source,adapter,'owner_conversation_mark_read',{sequence:latest},{idempotency_key:newRequestId()});
      if(!current())return;
      const readState=data?.read_state;
      s.readState=readState || {last_read_sequence:latest,unread:false};
      if(typeof replyNotifications!=='undefined')void replyNotifications.refresh();
      // A reply can arrive between GET and mark-read. The server's unread result is authoritative.
      if(!stillVisible())return;
      if(readState?.unread===true || readState?.any_unread===true || Number(readState?.last_read_sequence ?? latest)<latest) {
        setUnread(true);
        adapter.onUnread?.(latest);
        return;
      }
      setUnread(false);
      adapter.onRead?.(latest);
    } catch(_) {
      // A failed acknowledgment leaves the unread indicator exactly as it was.
    } finally {s.marking=false;}
  }
  async function load(more=false,options={}) {
    if(!current() || s.loading)return;
    s.loading=true;s.loadOwner=view;clearError();
    s.refreshAfterSend=false;
    try {
      const result=await conversationPrivateCall(source,adapter,'owner_conversation_get',{limit:20,...(more && s.before?{before:s.before}:{})});
      if(!current())return;
      s.conversation=result.conversation || null;s.readState=result.read_state || null;
      const data={notes:result.messages || [],next_before:result.next_before,participants:result.participants};
      s.participants=data.participants || s.participants;
      const merged=new Map((more?[...data.notes,...s.notes]:[...s.notes,...data.notes]).map(n=>[n.id,n]));
      s.notes=[...merged.values()].sort((a,b)=>a.sequence!=null && b.sequence!=null?a.sequence-b.sequence:(a.created_at||'').localeCompare(b.created_at||'') || a.id.localeCompare(b.id));
      const missingHistory=s.notes.length<(s.conversation?.message_count || 0);
      if(more || !s.loaded || (!s.before && missingHistory))s.before=data.next_before;
      s.loaded=true;
      if(!more && !expanded && s.readState)setUnread(s.readState.unread===true);
      if(current()){paint(more?'prepend':'auto');void loadRelated();}
      for(const mount of s.mounts)if(mount!==host && sameState(mount) && mount._conversation?.isExpanded())mount._conversation._paint?.();
      if(!more && options.acknowledge)await acknowledge();
    } catch(e){
      if(!current())return;
      if(['forbidden','not_found','policy_denied','invalid_principal'].includes(e.code)){
        s.notes=[];s.related=[];relatedReady=false;relatedGeneration++;s.participants=[];s.loaded=false;s.before=null;adapter.onRelated?.([]);
        paint();
        adapter.onUnavailable?.(e);
      }
      setError(CONVERSATION_TEXT.load);
    } finally{
      if(s.loadOwner===view){s.loading=false;s.loadOwner=null;}
      // A send may finish while an earlier read is still in flight. Fetch again
      // afterwards so that response cannot leave the new message out of view.
      if(current() && s.refreshAfterSend)void load(false);
    }
  }
  async function submit() {
    if(!current() || s.sending || (!s.text.trim() && !s.files.length))return;
    s.sending=true;syncSending();clearError();
    let saved_=false;
    try {
      if(!s.submission)s.submission={text:s.text,files:[...s.files],idempotency_key:newRequestId()};
      const request=s.submission;
      const references=[];
      for(const item of request.files) {
        if(!item.saved) {
          try { item.saved=coreData(await conversationRequest(api('/root/thread-attachments',{method:'POST',
            headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(item.file.name),'Idempotency-Key':item.key},body:item.file}))); }
          catch(error){error.attachment=true;throw error;}
        }
        references.push(item.saved.reference);
      }
      const text=references.length?[request.text,references.join('\n')].filter(part=>part.trim()).join('\n\n'):request.text;
      if(text.length>20000)throw Object.assign(new Error('Message too long'),{code:'message_too_long',attachment:true});
      await conversationPrivateCall(source,adapter,'owner_conversation_send',{text},{idempotency_key:request.idempotency_key});
      const sent=new Set(request.files.map(item=>item.key));s.files=s.files.filter(item=>!sent.has(item.key));
      s.text='';s.submission=null;saved_=true;syncDraft();
    } catch(e){
      if(e.attachment){if(e.code==='message_too_long')s.submission=null;if(current())setError(e.code==='message_too_long'?'Shorten your message and try again.':"Couldn't attach the file. Retry Send");}
      else if(current()){
        if(CONVERSATION_REVIEW.includes(e.code)){s.submission=null;setError(CONVERSATION_TEXT.changed);}
        else setError(s.submission?CONVERSATION_TEXT.unconfirmed:CONVERSATION_TEXT.blocked);
      }
    } finally{s.sending=false;syncSending();adapter.onInput?.();}
    // A confirmed write is complete. Refresh history without holding the
    // composer disabled, and preserve the same submission key on uncertain writes.
    if(saved_){s.refreshAfterSend=true;void load(false);adapter.onSent?.();}
  }
  function setUnread(value) {
    unreadDot.hidden=!value;
    host.dataset.conversationUnread=value?'true':'false';
    // A button's aria-label replaces its subtree, so the state belongs in the name.
    toggle.setAttribute('aria-label',value?'Comment, unread private reply':'Comment');
  }
  function expand(value,notify=true) {
    if(disposed)return;
    if(!value)captureScroll();
    expanded=value;panel.hidden=!value;toggle.setAttribute('aria-expanded',String(value));
    if(notify)adapter.onToggle?.(value);
    if(value) {
      // Opening re-lays the feed out at the top; the closed position is taken first and put back afterwards.
      const target=saved;
      restoring=true;
      try {
        if(s.loaded)paint();
        chat.layout();
        if(target)chat.setScrollState(target);
      } finally {restoring=false;}
      void load(false,{acknowledge:true});
    }
  }
  toggle.onclick=()=>expand(!expanded,true);
  // The shared view owns the feed, composer, keyboard and scrolling; this mount only supplies state and callbacks.
  chat=mountChatView(panel.querySelector('.conversation-view'),{
    logLabel:'Conversation',composerLabel,placeholder:defaultPlaceholder(),maxLength:20000,
    // A collapsed inline composer keeps the same shared banner beside its form so a failed send stays visible.
    ...(composerHost?{composerHost,bannerHost:composerHost}:{}),
    onDraft:text=>{if(!current())return;s.text=text;syncDraft();adapter.onInput?.();},
    onSubmit:()=>{void submit();},
    ...(source.type==='task'?{
      onFiles:files=>{
        if(!current() || s.sending || s.submission || !files.length)return;
        if(s.files.length+files.length>10 || files.some(file=>file.size>25*1024*1024)){setError('Choose up to 10 files, each 25 MB or smaller.');return;}
        s.files.push(...files.map(file=>({key:newRequestId(),file})));clearError();syncSending();adapter.onInput?.();
      },
      onAttachmentRemove:key=>{if(s.sending || s.submission)return;s.files=s.files.filter(item=>item.key!==key);syncSending();adapter.onInput?.();},
    }:{}),
    onOlder:()=>{void load(true);},
    onViewportChange:()=>captureScroll(),
    // The view hands over the opaque reference value itself; an unavailable reference never reaches this callback.
    mountSignoff:(ref,signoffHost)=>{
      const card=sgChatMount(ref,signoffHost,{active:current,dropOnDenied:true,omitConversationFor:source.type==='signoff'?conversationKey(source):null});
      if(!card)return null;
      signoffViews.add(card);
      return {dispose:()=>{signoffViews.delete(card);card.dispose();}};
    },
    onReference:ref=>{
      if(ref)Promise.resolve(adapter.openReference?adapter.openReference(ref):taskProgressOpenReference(ref.business_slug ?? source.biz,ref)).catch(()=>setError(CONVERSATION_TEXT.load));
    },
    onEntryAction:(actionKey,entryKey)=>{
      const note=actionKey==='parent'?s.notes.find(n=>n.id===entryKey):null;
      if(note && adapter.openParent)Promise.resolve().then(()=>adapter.openParent(note)).catch(()=>setError(CONVERSATION_TEXT.load));
    }});
  chat.setDraft(s.text);
  view=host._conversation={
    dismissOnOutside:adapter.dismissOnOutside!==false,
    // Refresh must also revalidate a retained but hidden Chat: a source denial there has to
    // reach the host even though the pane is collapsed. load() itself never acknowledges a
    // hidden conversation; only expand() asks it to acknowledge.
    refresh:()=>{if(expanded){for(const view of signoffViews)void view.refresh();}return load(false);},
    isExpanded:()=>expanded,expand:()=>expand(true),collapse:()=>expand(false),
    setUnread,
    hasDraft:()=>!!(s.text || s.files.length),
    dispose:()=>{
      if(disposed)return;
      disposed=true;expanded=false;
      s.mounts.delete(host);
      if(s.loadOwner===view){s.loading=false;s.loadOwner=null;}
      chat.dispose();
      toggle.onclick=null;
      if(host._conversation===view){delete host._conversation;delete host.dataset.conversation;}
      toggle.remove();host.replaceChildren();
    },
    get state(){return s;},
    _paint:paint,_setDraft:text=>chat.setDraft(text),_renderComposer:renderComposer};
  renderComposer();
  syncSending();
  // Collapsed by default at the component level: mounting is not itself an
  // explicit open (Focus mounts every card up front, long before any of them
  // are looked at). Consumers that only ever mount once a specific item is
  // already the one the owner is looking at (a selected task/signoff,
  // an opened email thread, the Focus card the owner just clicked, the inbox
  // modal) call .expand() themselves right after mounting, once, so the chat
  // still shows by default there without baking an eager private-channel
  // read-acknowledgment into this shared default.
  expand(false,false);return host._conversation;
}
