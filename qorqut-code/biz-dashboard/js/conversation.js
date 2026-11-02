// One source-owned group discussion and isolated owner–agent private channels.
// Business task peers are discovered by exact identity. Local drafts only, and
// no viewing telemetry beyond the explicit private read acknowledgment.
const conversationStates = new Map();
let conversationMountId = 0;
const CONVERSATION_CHANNELS = [['private', 'Private with Ordubashy'], ['marketing', 'Private with Marketing'], ['team', 'Agent discussion']];
// Omitting the peer keeps the original Ordubashy channel. The Marketing channel is found by role exactly once:
// the agent the service returns is then pinned, and every later read, send, acknowledgment, page and retry
// names that agent. A role-only request is never repeated once a peer is pinned.
const CONVERSATION_MARKETING_ROLE = 'global-marketing';
const conversationPeer=s=>s.channel.startsWith('agent:')?{agent_id:s.peer}:s.channel==='marketing'?{peer_role:CONVERSATION_MARKETING_ROLE,...(s.peer?{agent_id:s.peer}:{})}:{};
const CONVERSATION_CHANGED = 'The Marketing agent changed or is no longer available. Select the Marketing tab again to choose the current agent.';
// Refusals that mean this exact peer channel cannot be used now.
const CONVERSATION_LOST = ['forbidden','not_found','policy_denied','revision_conflict','invalid_principal'];
// Inline Focus conversations dismiss on outside clicks, while their contents stay interactive.
document.addEventListener('click',event=>{
  // composedPath() is frozen at dispatch time: a reactive re-render (the team
  // tab's avatar stack can replace itself between dispatch and here) can leave
  // event.target detached, which would make a plain .contains(event.target)
  // check wrongly read as "outside" and collapse the panel the click opened.
  const path=event.composedPath();
  for(const host of document.querySelectorAll('[data-inline-conversation]')) {
    if(host._conversation?.dismissOnOutside===false)continue;
    if(!path.includes(host.closest('.agfocus-card') || host) && host._conversation?.isExpanded())host._conversation.collapse();
  }
});
function conversationKey(source) { return `${source.type}:${source.biz || 'global'}:${source.id}`; }
// Shared state is keyed by source, channel and the exact peer, so one agent's history, cursors, draft and retry
// can never be shown to, or sent to, another.
function conversationStateKey(source,channel,peer=null) { return `${conversationKey(source)}:${channel}${peer?`:${peer}`:''}`; }
function conversationNewState(source,channel,peer=null) {
  return {source:conversationKey(source),channel,peer,text:'',notes:[],participants:[],loaded:false,cursor:null,before:null,mounts:new Set(),conversation:null,readState:null,unavailable:null};
}
function conversationState(source,channel='private',peer=null) {
  const key=conversationStateKey(source,channel,peer);
  if (!conversationStates.has(key)) conversationStates.set(key,conversationNewState(source,channel,peer));
  return conversationStates.get(key);
}
// The dashboard is the owner interface. A session that explicitly reports another
// human keeps the old team composer and never sees the owner's private channel.
function conversationViewerIsOwner() {
  const session=typeof state==='object' && state?state.session:null;
  return !session || session.is_owner!==false;
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
// Same defensive fallback as conversationGlyph, for a harness that doesn't load core.js.
function conversationOrdubashyGlyph(size) {
  return typeof agentGlyph==='function' ? agentGlyph({role:{id:'ordubashy'}},size) : 'O';
}
function conversationMarketingGlyph(size) {
  return typeof agentGlyph==='function' ? agentGlyph({role:{id:'global-marketing'}},size) : 'M';
}
// Up to 4 overlapping avatars for the team-channel toast; no overflow count.
function conversationChannelAvatars(people) {
  if(!people.length) return '<span class="conversation-channel-avatar conversation-channel-avatar-empty" aria-hidden="true"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 20v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1"/><circle cx="9" cy="7" r="3"/><path d="M22 20v-1a3.99 3.99 0 0 0-3-3.85"/><path d="M16 3.13a3.99 3.99 0 0 1 0 7.75"/></svg></span>';
  return people.slice(0,4).map(person=>`<span class="conversation-channel-avatar" aria-hidden="true">${conversationGlyph(person,18)}</span>`).join('');
}
function conversationPeople(s) {
  const people=new Map();
  for (const n of [...(s.participants || []),...s.notes]) if(n.entry_kind!=='system') {
    const participantId=n.created_by || n.actor_id || n.author_id || conversationPersonName(n);
    if(!people.has(participantId))people.set(participantId,n);
  }
  return [...people.values()];
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
async function conversationCall(source,operation,args,options={}) {
  const path=source.biz?`/biz/${encodeURIComponent(source.biz)}`:'/root';
  // Task and Action thread operations infer Global from the root route and their schemas reject `scope`;
  // only the private conversation operations below declare it.
  return coreData(await conversationRequest(post(`${path}/operations/${operation}`,{...args,...options})));
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
const CONVERSATION_REVIEW = ['revision_conflict','stale_signoff','not_found','forbidden','policy_denied'];
const conversationLostText = s => s.channel.startsWith('agent:')?'This agent is no longer available for this task.':CONVERSATION_CHANGED;
// Normalizes canonical notes (already in controller order) into shared view entries.
function conversationEntries(notes,adapter) {
  let previous=null;
  return notes.map(note=>{
    const system=note.entry_kind==='system',owner=!system && (note.is_owner ?? (note.author_kind==='human'));
    const entry={key:note.id,speaker:system?'notice':owner?'user':'agent',text:note.body || '',timestamp:note.created_at,
      references:(note.refs || []).map((ref,index)=>({key:`ref${index}`,label:ref.label || ref.type || 'Reference',disabled:!!ref.unavailable,value:ref})),
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
function conversationMount(host,source,adapter={}) {
  if (!host) return;
  const key=conversationKey(source);
  let expanded=false;
  if (host.dataset.conversation===key) { host._source=source; return host._conversation; }
  host._conversation?.dispose?.();
  let disposed=false,view,chat,errorText='',restoring=false;
  // Last real scroll position of each channel, captured only while the panel is shown.
  const saved={};
  host.dataset.conversation=key;host._source=source;
  // The session can arrive after a mount, so the owner test is read on demand
  // rather than frozen: a late non-owner session restores the team composer.
  const viewerIsOwner=()=>conversationViewerIsOwner();
  // Until the service names a Marketing agent this mount holds an empty, unsendable discovery state of its own.
  const marketing={state:conversationNewState(source,'marketing')};
  const channels={private:conversationState(source,'private'),team:conversationState(source,'team'),get marketing(){return marketing.state;}};
  let channel=adapter.channel || (viewerIsOwner()?'private':'team');
  const st=()=>channels[channel];
  const businessSource=()=>!!(source.biz || source.business_id || adapter.privateSource?.business_id);
  const readOnly=()=>channel==='team' && viewerIsOwner() && !(businessSource() && source.type==='task');
  const lost=()=>channel!=='team' && !!st().unavailable;
  const mountId=++conversationMountId;
  const panelId=`conversation-panel-${mountId}`;
  const tabId=name=>`conversation-tab-${mountId}-${name}`;
  // Icon-only, matching the pane's .ttab icon tabs: the label stays as the
  // accessible name (aria-label) instead of visible text.
  const tabs=CONVERSATION_CHANNELS.map(([name,label])=>`<button type="button" role="tab" id="${tabId(name)}" class="quiet conversation-channel" data-channel="${name}" aria-selected="false" tabindex="-1" aria-controls="${panelId}" aria-label="${esc(label)}"><span class="conversation-channel-icon" data-channel-icon="${name}">${name==='private'?conversationOrdubashyGlyph(18):name==='marketing'?conversationMarketingGlyph(18):conversationChannelAvatars([])}</span><span class="conversation-unread" data-peer-unread hidden></span></button>`).join('');
  host.innerHTML=`<button type="button" class="quiet conversation-toggle" aria-label="Comment" aria-expanded="false" aria-controls="${panelId}"><span class="conversation-toggle-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 12.5z"/></svg></span><span class="conversation-unread" data-conversation-unread aria-hidden="true" hidden></span></button><section id="${panelId}" class="conversation-panel" role="tabpanel" aria-labelledby="${tabId('private')}" hidden><div class="conversation-channels" role="tablist" aria-label="Conversation channel">${tabs}</div><div class="conversation-view"></div></section>`;
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
  const defaultPlaceholder=()=>adapter.inlineComposer?(adapter.placeholder ?? (channel==='private'?'Private message to Ordubashy…':channel==='marketing'?'Private message to Marketing…':channel.startsWith('agent:')?'Private message…':'Message the group…')):'Message…';
  st().mounts.add(host);
  host.dataset.conversationChannel=channel;host.dataset.conversationPeer=st().peer || '';
  const sameState=(mount,s)=>mount.isConnected && mount.dataset.conversation===key && mount.dataset.conversationChannel===s.channel && mount.dataset.conversationPeer===(s.peer || '');
  // Silent for every mounted view of this exact state; a view whose text already matches keeps its caret.
  const syncDraft=(s=st())=>{for(const mount of [...s.mounts]){if(!sameState(mount,s)){s.mounts.delete(mount);continue;}mount._conversation?._setDraft(s.text);}};
  const current=()=>!disposed && host.isConnected && host.dataset.conversation===key && host._conversation===view;
  const captureScroll=()=>{if(expanded && !restoring)saved[channel]=chat.getScrollState();};
  function composerState() {
    const s=st();
    // No exact Marketing agent has loaded yet, so there is nobody to send to.
    const unresolved=s.channel==='marketing' && !s.peer;
    return {hidden:readOnly() || lost(),inputDisabled:!!s.sending || !!s.submission || unresolved,sendDisabled:!!s.sending || unresolved,
      sendLabel:s.submission?'Retry Send':'Send',placeholder:defaultPlaceholder(),ariaLabel:composerLabel,maxLength:20000};
  }
  function renderComposer() {if(!disposed)chat.render({composer:composerState()},{scroll:'preserve'});}
  function syncSending() {
    const s=st();
    for(const mount of s.mounts)if(sameState(mount,s))mount._conversation?._renderComposer();
  }
  function setError(text) {errorText=text;if(!disposed)chat.render({banner:{text,role:'alert'}},{scroll:'preserve'});}
  const clearError=()=>{if(errorText)setError('');};
  function syncChannel() {
    for(const tab of host.querySelectorAll('.conversation-channel')) {
      const selected=tab.dataset.channel===channel;
      tab.setAttribute('aria-selected',String(selected));
      tab.tabIndex=selected?0:-1;
    }
    panel.setAttribute('aria-labelledby',tabId(channel));
    // Only the composer goes away: the inline wrapper still carries the Focus
    // message icon, which must stay reachable in the read-only team view.
    renderComposer();
  }
  // The team tab's avatar stack reflects whoever has actually posted, which can
  // arrive after mount (or already be cached from an earlier mount of the same source).
  function paintTeamIcon() {
    const icon=host.querySelector('[data-channel-icon="team"]');
    if(icon)icon.innerHTML=conversationChannelAvatars(conversationPeople(channels.team));
  }
  let painted='';
  function paint(mode='auto') {
    const s=st();
    const signature=JSON.stringify([channel,s.peer,s.unavailable,!!s.text,!!s.loaded,s.notes,s.participants,s.cursor,s.before]);
    if(painted===signature && mode==='auto')return;
    painted=signature;
    const emptyText=s.unavailable?`${s.unavailable}${s.text?' Your unsent draft for that agent is kept separately and is not sent to anyone else.':''}`:s.loaded && !s.notes.length?'No messages yet':'';
    chat.render({entries:conversationEntries(s.notes,adapter),older:{visible:!!(s.cursor || s.before),disabled:false},empty:{text:emptyText}},{scroll:mode});
    if(chat.elements.empty){if(s.unavailable)chat.elements.empty.dataset.marketingUnavailable='';else delete chat.elements.empty.dataset.marketingUnavailable;}
    if(adapter.inlineComposer)host.querySelectorAll('[title]').forEach(el=>el.removeAttribute('title'));
  }
  // Moves this mount onto another Marketing state without carrying any history, draft or retry across.
  function attachMarketing(next) {
    const previous=marketing.state;
    if(previous===next)return;
    previous.mounts.delete(host);
    marketing.state=next;
    if(channel==='marketing'){next.mounts.add(host);host.dataset.conversationPeer=next.peer || '';chat.setDraft(next.text);}
  }
  // The exact agent is gone or changed: show the channel as unavailable and never fall back to another agent.
  // The old draft stays in that agent's own state; nothing is re-sent or shown elsewhere.
  function markUnavailable(s,message) {
    s.unavailable=message || CONVERSATION_CHANGED;
    s.notes=[];s.participants=[];s.loaded=false;s.cursor=null;s.before=null;s.conversation=null;s.readState=null;
    if(st()===s && current()){syncChannel();syncSending();painted='';paint();}
  }
  // Acknowledge only what the server has already returned to this owner, and only
  // after an interactive private load that included the newest page.
  async function acknowledge(s) {
    const stillPrivateVisible=()=>viewerIsOwner() && expanded && st()===s && channel!=='team' && current() && adapter.isVisible?.()!==false;
    if(!stillPrivateVisible() || s.marking || s.unavailable)return;
    const latest=s.conversation?.latest_sequence || 0;
    if(!latest || latest<=(s.readState?.last_read_sequence || 0))return;
    s.marking=true;
    try {
      // Do not acknowledge a load that the owner has already closed or switched
      // away from while the private history request was in flight.
      if(!stillPrivateVisible())return;
      const data=await conversationPrivateCall(source,adapter,'owner_conversation_mark_read',{sequence:latest,...conversationPeer(s)},{idempotency_key:newRequestId()});
      if(!current())return;
      // A receipt for a different Marketing agent than the one this channel is pinned to never updates its read state.
      if(s.peer && data?.conversation?.agent_id!==s.peer){markUnavailable(s);return;}
      const readState=data?.read_state;
      s.readState=readState || {last_read_sequence:latest,unread:false};
      void loadPeers();
      if(typeof replyNotifications!=='undefined')void replyNotifications.refresh();
      // A coordinator reply can arrive between GET and mark-read. The server's
      // unread result is authoritative: keep or restore the dot and projection.
      if(!stillPrivateVisible())return;
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
    if(!current())return;
    const target=options.channel || channel,origin=channels[target];
    let s=origin;
    // A lost Marketing agent is never re-read or replaced here; only an explicit selection or reopen finds the current one.
    if(s.unavailable){if(target===channel && current())paint();return;}
    if(s.loading)return;
    s.loading=true;s.loadOwner=view;if(target===channel)clearError();
    s.refreshAfterSend=false;
    try {
      let data;
      const pagination=more?{...(s.cursor?{cursor:s.cursor}:{}),...(s.before?{before:s.before}:{})}:{};
      if(target!=='team') {
        const result=await conversationPrivateCall(source,adapter,'owner_conversation_get',{limit:20,...conversationPeer(s),...(more && s.before?{before:s.before}:{})});
        if(!current())return;
        if(target==='marketing') {
          const bound=result.conversation?.agent_id;
          if(!s.peer) {
            // The one role-only discovery: pin the exact agent it returned, unless a newer selection replaced it.
            if(!bound)throw Object.assign(new Error('The Marketing channel did not report its agent.'),{code:'invalid_response'});
            if(channels.marketing!==origin)return;
            s=conversationState(source,'marketing',bound);
            s.unavailable=null;
            attachMarketing(s);
            syncChannel();syncSending();
          } else if(bound!==s.peer) throw Object.assign(new Error(CONVERSATION_CHANGED),{code:'revision_conflict'});
        }
        if(target.startsWith('agent:') && result.conversation?.agent_id!==s.peer)throw Object.assign(new Error('The selected agent changed.'),{code:'revision_conflict'});
        s.conversation=result.conversation || null;s.readState=result.read_state || null;
        const dot=host.querySelector(`[data-channel="${CSS.escape(target)}"] [data-peer-unread]`);if(dot)dot.hidden=!s.readState?.unread;
        data={notes:result.messages || [],next_before:result.next_before,participants:result.participants};
      }
      else if(adapter.read) data=await conversationRequest(adapter.read(pagination));
      else data=await conversationCall(source,source.type==='email'?'email_comments_list':source.type==='task'?'tasks_thread_get':'signoffs_notes_list',{[source.type==='email'?'conversation_id':source.type+'_id']:source.id,limit:20,...pagination});
      if(!current())return;
      s.participants=data.participants || s.participants;
      const merged=new Map((more?[...data.notes,...s.notes]:[...s.notes,...data.notes]).map(n=>[n.id,n]));
      s.notes=[...merged.values()].sort((a,b)=>a.sequence!=null && b.sequence!=null?a.sequence-b.sequence:(a.created_at||'').localeCompare(b.created_at||'') || a.id.localeCompare(b.id));
      const missingPrivateHistory=target!=='team' && s.notes.length<(s.conversation?.message_count || 0);
      if(more || !s.loaded || (!s.cursor && !s.before && missingPrivateHistory)){s.cursor=data.next_cursor;s.before=data.next_before;}
      s.loaded=true;
      if(target===channel && channels[target]===s && current())paint(more?'prepend':'auto');
      for(const mount of s.mounts)if(mount!==host && sameState(mount,s) && mount._conversation?.isExpanded())mount._conversation._paint?.();
      s.rejectionRequiresMessage=data.rejection_requires_message;
      // Card-level people projections describe the team, not the private pair,
      // and stay current even while the private channel is the one on screen.
      if(target==='team'){adapter.onParticipants?.(conversationPeople(s));paintTeamIcon();}
      if(target!=='team' && !more && options.acknowledge)await acknowledge(s);
    } catch(e){
      if(!current())return;
      if(target!=='team' && s.peer && CONVERSATION_LOST.includes(e.code))markUnavailable(s,conversationLostText(s));
      else if(['forbidden','not_found'].includes(e.code)){
        s.notes=[];s.participants=[];s.loaded=false;s.cursor=null;s.before=null;
        if(target===channel && current())paint();
        adapter.onUnavailable?.(e);
      }
      if(target===channel && channels[target]===s && current() && !(target==='marketing' && s.unavailable))setError(CONVERSATION_TEXT.load);
    } finally{
      if(origin.loadOwner===view){origin.loading=false;origin.loadOwner=null;}
      // A send may finish while an earlier read is still in flight. Fetch again
      // afterwards so that response cannot leave the new message out of view.
      if(current() && origin.refreshAfterSend)void load(false,{channel:target});
    }
  }
  async function submit() {
    // A tab switch mid-send must not redirect the message: the channel and its
    // state are fixed for the whole submission.
    const sending=channel,s=channels[sending];
    if(!current() || s.sending || !s.text.trim() || readOnly() || lost())return;
    // Nothing is sent to Marketing until the exact agent has loaded and been pinned.
    if((sending==='marketing' || sending.startsWith('agent:')) && !s.peer){setError(CONVERSATION_TEXT.blocked);return;}
    s.sending=true;syncSending();clearError();
    let saved_=false;
    try {
      if(!s.submission) {
        let revision=host._source.revision;
        if(sending==='team' && source.type==='task') {
          const data=await conversationCall(source,'tasks_get',{task_id:source.id});revision=data.task.revision;
        }
        s.submission={text:s.text,idempotency_key:newRequestId(),peer:s.peer,...(sending==='team'?{revision}:{})};
      }
      const request=s.submission;
      // A retry belongs to the agent it was first sent for.
      if(request.peer!==s.peer)throw Object.assign(new Error(CONVERSATION_CHANGED),{code:'revision_conflict'});
      if(sending!=='team') await conversationPrivateCall(source,adapter,'owner_conversation_send',{text:request.text,...conversationPeer(s)},{idempotency_key:request.idempotency_key});
      else if(adapter.send) await conversationRequest(adapter.send(request));
      else await conversationCall(source,{task:'tasks_comment',signoff:'signoffs_comment',email:'email_comment'}[source.type],
        {[source.type==='email'?'conversation_id':source.type+'_id']:source.id,text:request.text},
        {idempotency_key:request.idempotency_key,...(request.revision?{expected_revision:request.revision}:{})});
      s.text='';s.submission=null;saved_=true;syncDraft(s);
    } catch(e){
      // The server owns the channel rule: move the draft to the private channel
      // rather than losing it or retrying a write the owner may not make.
      if(e.code==='channel_required'){
        const draft=s.text;s.submission=null;s.text='';
        channels.private.text=draft;
        selectChannel('private',{acknowledge:false});
        if(current()){chat.setDraft(draft);setError(CONVERSATION_TEXT.blocked);}
      }
      else if(sending==='marketing' && CONVERSATION_LOST.includes(e.code) && s.peer){
        // The pinned agent can no longer be written to: keep the draft with that agent, drop the retry, and never re-aim.
        s.submission=null;markUnavailable(s,conversationLostText(s));
      }
      else if(current() && channel===sending){
        if(CONVERSATION_REVIEW.includes(e.code)){s.submission=null;setError(CONVERSATION_TEXT.changed);}
        else setError(s.submission?CONVERSATION_TEXT.unconfirmed:CONVERSATION_TEXT.blocked);
      }
    } finally{s.sending=false;syncSending();adapter.onInput?.();}
    // A confirmed write is complete. Refresh history without holding the
    // composer disabled, and preserve the same submission key on uncertain writes.
    if(saved_){s.refreshAfterSend=true;void load(false,{channel:s.channel});}
  }
  // Selecting the Marketing tab (or reopening the conversation) after the channel was lost is the explicit
  // step that looks the current Marketing agent up again by role, with a clean slate.
  function selectChannel(next,options={}) {
    if(!channels[next])return;
    const reset=next==='marketing' && !!channels.marketing.unavailable;
    if(next===channel && !reset)return;
    if(reset)attachMarketing(conversationNewState(source,'marketing'));
    if(next!==channel) {
      captureScroll();
      channels[channel].mounts.delete(host);
      channel=next;host.dataset.conversationChannel=next;host.dataset.conversationPeer=channels[next].peer || '';
      channels[next].mounts.add(host);
    }
    // The channel's own reading position wins over the shared bottom-follow default; take it before any repaint re-measures the feed.
    const target=saved[channel];
    restoring=true;
    try {
      syncChannel();
      chat.setDraft(st().text);
      syncSending();
      painted='';paint('bottom');
      if(target)chat.setScrollState(target);
    } finally {restoring=false;}
    if(expanded)void load(false,{acknowledge:options.acknowledge!==false && next!=='team'});
    void loadPeers();
  }
  function setUnread(value) {
    unreadDot.hidden=!value;
    host.dataset.conversationUnread=value?'true':'false';
    // A button's aria-label replaces its subtree, so the state belongs in the name.
    toggle.setAttribute('aria-label',value?'Comment, unread private reply':'Comment');
  }
  function expand(value,notify=true,explicit=false) {
    if(disposed)return;
    if(!value)captureScroll();
    expanded=value;panel.hidden=!value;toggle.setAttribute('aria-expanded',String(value));
    if(notify)adapter.onToggle?.(value);
    if(value) {
      void loadPeers();
      if(explicit && channel==='marketing' && lost()){attachMarketing(conversationNewState(source,'marketing'));syncChannel();syncSending();painted='';chat.setDraft(st().text);}
      // Opening re-lays the feed out at the top; the closed position is taken first and put back afterwards.
      const target=saved[channel];
      restoring=true;
      try {
        if(st().loaded)paint();
        chat.layout();
        if(target)chat.setScrollState(target);
      } finally {restoring=false;}
      void load(false,{acknowledge:channel!=='team'});
    }
  }
  toggle.onclick=()=>expand(!expanded,true,true);
  const tablist=host.querySelector('.conversation-channels');
  tablist.onclick=event=>{const tab=event.target.closest('[data-channel]');if(tab)selectChannel(tab.dataset.channel);};
  tablist.onkeydown=event=>{
    const tabs=[...tablist.querySelectorAll('[data-channel]')].filter(tab=>!tab.disabled),tab=event.target.closest('[data-channel]'),index=tabs.indexOf(tab);
    const next=event.key==='ArrowRight'?tabs[(index+1)%tabs.length]:event.key==='ArrowLeft'?tabs[(index+tabs.length-1)%tabs.length]:event.key==='Home'?tabs[0]:event.key==='End'?tabs.at(-1):null;
    if(next){event.preventDefault();selectChannel(next.dataset.channel);next.focus();}
  };
  let peersPending=false,peersAgain=false,peerWaiters=[];
  async function loadPeers() {
    if(!current() || !viewerIsOwner())return;
    if(peersPending){peersAgain=true;return new Promise(resolve=>peerWaiters.push(resolve));}
    peersPending=true;
    try {
      const data=await conversationPrivateCall(source,adapter,'owner_conversation_peers',{});
      if(!current())return;
      const peers=data.peers || [],available=new Set(peers.map(peer=>peer.channel));
      for(const peer of peers) {
        let tab=tablist.querySelector(`[data-channel="${CSS.escape(peer.channel)}"]`);
        if(peer.channel.startsWith('agent:') && !channels[peer.channel]) {
          channels[peer.channel]=conversationState(source,peer.channel,peer.agent_id);
        }
        if(!tab && peer.channel.startsWith('agent:')) {
          tab=document.createElement('button');tab.type='button';tab.className='quiet conversation-channel';tab.dataset.channel=peer.channel;tab.id=tabId(peer.channel);tab.setAttribute('role','tab');tab.setAttribute('aria-controls',panelId);
          tab.innerHTML=`<span class="conversation-channel-icon">${typeof agentGlyph==='function'?agentGlyph({role:peer.role},18):esc(peer.name.slice(0,2))}</span><span class="conversation-unread" data-peer-unread hidden></span>`;
          tablist.insertBefore(tab,tablist.querySelector('[data-channel="team"]'));
        }
        if(tab){tab.dataset.agentId=peer.agent_id;tab.title=peer.name;tab.setAttribute('aria-label','Private with '+peer.name+(peer.unread?', unread messages':''));tab.querySelector('[data-peer-unread]').hidden=!peer.unread;}
      }
      for(const [key,s] of Object.entries(channels))if(key.startsWith('agent:')) {
        const tab=tablist.querySelector(`[data-channel="${CSS.escape(key)}"]`);
        if(!available.has(key)){markUnavailable(s,'This agent is no longer available for this task.');if(tab){tab.disabled=true;tab.querySelector('[data-peer-unread]').hidden=true;}}
        else {s.unavailable=null;if(tab)tab.disabled=false;}
      }
      setUnread(peers.some(peer=>peer.unread));syncChannel();syncSending();
    } catch(_) { /* Keep the existing indicators on an unavailable metadata read. */ }
    finally {peersPending=false;peerWaiters.splice(0).forEach(resolve=>resolve());if(peersAgain){peersAgain=false;void loadPeers();}}
  }
  // The shared view owns the feed, composer, keyboard and scrolling; this mount only supplies state and callbacks.
  chat=mountChatView(panel.querySelector('.conversation-view'),{
    logLabel:'Conversation',composerLabel,placeholder:defaultPlaceholder(),maxLength:20000,
    // A collapsed inline composer keeps the same shared banner beside its form so a failed send stays visible.
    ...(composerHost?{composerHost,bannerHost:composerHost}:{}),
    onDraft:text=>{if(!current())return;st().text=text;syncDraft();adapter.onInput?.();},
    onSubmit:()=>{void submit();},
    onOlder:()=>{void load(true);},
    onViewportChange:()=>captureScroll(),
    // The view hands over the opaque reference value itself; an unavailable reference never reaches this callback.
    onReference:ref=>{
      if(ref)taskProgressOpenReference(source.biz,ref).catch(()=>setError(CONVERSATION_TEXT.load));
    },
    onEntryAction:(actionKey,entryKey)=>{
      const note=actionKey==='parent'?st().notes.find(n=>n.id===entryKey):null;
      if(note && adapter.openParent)Promise.resolve().then(()=>adapter.openParent(note)).catch(()=>setError(CONVERSATION_TEXT.load));
    }});
  chat.setDraft(st().text);
  paintTeamIcon();
  view=host._conversation={
    dismissOnOutside:adapter.dismissOnOutside!==false,
    refresh:()=>{if(expanded)void loadPeers();return expanded || adapter.inlineComposer?load(false,{channel:expanded?channel:'team'}):null;},
    // Preloading is a background read of the team discussion only: it never
    // acknowledges private messages and never reads the private channel.
    preload:()=>load(false,{channel:'team'}),
    isExpanded:()=>expanded,expand:()=>expand(true),collapse:()=>expand(false),
    channel:()=>channel,selectChannel,setUnread,
    openPeer:async peer=>{
      await loadPeers();
      const tab=[...tablist.querySelectorAll('[data-agent-id]')].find(tab=>tab.dataset.agentId===peer && !tab.disabled);
      if(!tab)throw new Error('This agent thread is no longer available.');
      if(tab.dataset.channel==='marketing')attachMarketing(conversationState(source,'marketing',peer));
      selectChannel(tab.dataset.channel);expand(true);
    },
    // A draft kept for a lost Marketing agent still counts: it is preserved, just never re-aimed.
    hasDraft:()=>[...conversationStates.values()].some(state=>state.source===key && state.text),
    dispose:()=>{
      if(disposed)return;
      disposed=true;expanded=false;
      for(const s of Object.values(channels)){
        s.mounts.delete(host);
        if(s.loadOwner===view){s.loading=false;s.loadOwner=null;}
      }
      chat.dispose();
      toggle.onclick=null;tablist.onclick=tablist.onkeydown=null;
      if(host._conversation===view){delete host._conversation;delete host.dataset.conversation;}
      toggle.remove();host.replaceChildren();
    },
    get state(){return st();},
    states:channels,_paint:paint,_setDraft:text=>chat.setDraft(text),_renderComposer:renderComposer};
  syncChannel();
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
