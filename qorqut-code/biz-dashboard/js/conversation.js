// One source-owned group discussion and isolated owner–agent private channels.
// Business task/Story peers are discovered by exact identity. Local drafts only, and
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
  return {source:conversationKey(source),channel,peer,text:'',notes:[],participants:[],loaded:false,cursor:null,before:null,mounts:new Set(),ageTimer:null,conversation:null,readState:null,unavailable:null};
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
function conversationAge(at) {
  if (typeof agpadInboxAge === 'function') return agpadInboxAge(at);
  const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(at))/1000));
  const [n,unit]=seconds<60?[seconds,'second']:seconds<3600?[Math.floor(seconds/60),'minute']:seconds<86400?[Math.floor(seconds/3600),'hour']:[Math.floor(seconds/86400),'day'];
  return `${n} ${unit}${n===1?'':'s'} ago`;
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
function conversationMessage(note, open) {
  const body=`<div data-conversation-body="${esc(note.id)}">${renderSafeLinks(note.body || '')}</div>`;
  const preview=messagePreview(note.body,note.summary);
  if(!note.summary && String(note.body || '').trim().split(/\s+/u).length<=10)return body;
  return `<details data-message-id="${esc(note.id)}"${open?' open':''}><summary aria-label="${esc(preview)} — expand full message">${esc(preview)}</summary>${body}</details>`;
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
  return coreData(await conversationRequest(post(`${path}/operations/${operation}`,{...args,...(!source.biz?{scope:'global'}:{}),...options})));
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
function conversationMount(host,source,adapter={}) {
  if (!host) return;
  const key=conversationKey(source);
  let expanded=false;
  const expandedMessages={private:new Set(),marketing:new Set(),team:new Set()};
  if (host.dataset.conversation===key) { host._source=source; return host._conversation; }
  host._conversation?.dispose?.();
  let disposed=false,view;
  const scroll={private:0,marketing:0,team:0};
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
  const readOnly=()=>channel==='team' && viewerIsOwner() && !(businessSource() && ['task','story'].includes(source.type));
  const lost=()=>channel!=='team' && !!st().unavailable;
  const mountId=++conversationMountId;
  const panelId=`conversation-panel-${mountId}`;
  const tabId=name=>`conversation-tab-${mountId}-${name}`;
  // Icon-only, matching the pane's .ttab icon tabs: the label stays as the
  // accessible name (aria-label) instead of visible text.
  const tabs=CONVERSATION_CHANNELS.map(([name,label])=>`<button type="button" role="tab" id="${tabId(name)}" class="quiet conversation-channel" data-channel="${name}" aria-selected="false" tabindex="-1" aria-controls="${panelId}" aria-label="${esc(label)}"><span class="conversation-channel-icon" data-channel-icon="${name}">${name==='private'?conversationOrdubashyGlyph(18):name==='marketing'?conversationMarketingGlyph(18):conversationChannelAvatars([])}</span><span class="conversation-unread" data-peer-unread hidden></span></button>`).join('');
  host.innerHTML=`<button type="button" class="quiet conversation-toggle" aria-label="Comment" aria-expanded="false" aria-controls="${panelId}"><span class="conversation-toggle-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 12.5z"/></svg></span><span class="conversation-unread" data-conversation-unread aria-hidden="true" hidden></span></button><section id="${panelId}" class="conversation-panel" role="tabpanel" aria-labelledby="${tabId('private')}" hidden><div class="conversation-channels" role="tablist" aria-label="Conversation channel">${tabs}</div><button type="button" class="quiet conversation-older" hidden>Older messages</button><div class="conversation-feed" role="log" aria-label="Conversation"></div><label class="conversation-compose">Message<textarea class="inp" rows="3" maxlength="20000"></textarea></label><button type="button" class="btn conversation-send">Send</button><p class="ferr" role="alert" hidden></p></section>`;
  const toggle=host.querySelector('.conversation-toggle'),panel=host.querySelector('.conversation-panel'),feed=host.querySelector('.conversation-feed'),input=host.querySelector('textarea'),send=host.querySelector('.conversation-send'),older=host.querySelector('.conversation-older'),error=host.querySelector('[role="alert"]');
  const unreadDot=host.querySelector('[data-conversation-unread]');
  const composeLabel=host.querySelector('.conversation-compose');
  if(adapter.inlineComposer) {
    host.dataset.inlineConversation='true';
    const composer=document.createElement('div');composer.className='conversation-inline-compose';
    composeLabel.firstChild.textContent='';input.rows=1;input.placeholder='Leave a comment…';
    input.setAttribute('aria-label','Leave a comment');
    composer.append(composeLabel,send);host.append(composer,error);
  }
  st().mounts.add(host);
  host.dataset.conversationChannel=channel;host.dataset.conversationPeer=st().peer || '';
  const syncDraft=(sourceInput,s=st())=>{for(const mount of [...s.mounts]){if(!mount.isConnected || mount.dataset.conversation!==key || mount.dataset.conversationChannel!==s.channel || mount.dataset.conversationPeer!==(s.peer || '')){s.mounts.delete(mount);continue;}const other=mount.querySelector('textarea');if(other && other!==sourceInput && other.value!==s.text)other.value=s.text;}};
  input.value=st().text;input.oninput=()=>{st().text=input.value;syncDraft(input);adapter.onInput?.();};
  const current=()=>!disposed && host.isConnected && host.dataset.conversation===key && host._conversation===view;
  feed.onscroll=()=>{if(expanded)scroll[channel]=feed.scrollTop;};
  function syncSending() {
    const s=st();
    // No exact Marketing agent has loaded yet, so there is nobody to send to.
    const unresolved=s.channel==='marketing' && !s.peer;
    for(const mount of s.mounts)if(mount.isConnected && mount.dataset.conversation===key && mount.dataset.conversationChannel===s.channel && mount.dataset.conversationPeer===(s.peer || '')){
      const tx=mount.querySelector('textarea'),button=mount.querySelector('.conversation-send');
      if(tx)tx.disabled=!!s.sending || !!s.submission || unresolved;
      if(button){button.disabled=!!s.sending || unresolved;button.textContent=s.submission?'Retry Send':'Send';}
    }
  }
  function syncChannel() {
    for(const tab of host.querySelectorAll('.conversation-channel')) {
      const selected=tab.dataset.channel===channel;
      tab.setAttribute('aria-selected',String(selected));
      tab.tabIndex=selected?0:-1;
    }
    panel.setAttribute('aria-labelledby',tabId(channel));
    const hidden=readOnly() || lost();
    // Only the composer goes away: the inline wrapper still carries the Focus
    // message icon, which must stay reachable in the read-only team view.
    composeLabel.hidden=hidden;send.hidden=hidden;
    if(adapter.inlineComposer)input.placeholder=adapter.placeholder ?? (channel==='private'?'Private message to Ordubashy…':channel==='marketing'?'Private message to Marketing…':channel.startsWith('agent:')?'Private message…':'Message the group…');
  }
  syncChannel();
  syncSending();
  // The team tab's avatar stack reflects whoever has actually posted, which can
  // arrive after mount (or already be cached from an earlier mount of the same source).
  function paintTeamIcon() {
    const icon=host.querySelector('[data-channel-icon="team"]');
    if(icon)icon.innerHTML=conversationChannelAvatars(conversationPeople(channels.team));
  }
  paintTeamIcon();
  let painted='';
  function paint() {
    const s=st();
    const signature=JSON.stringify([channel,s.peer,s.unavailable,!!s.text,s.notes,s.participants,s.cursor,s.before]);
    if(painted===signature)return;
    painted=signature;
    const top=scroll[channel];
    const groups=[];
    for (const note of s.notes) {
      const last=groups.at(-1);
      if(note.entry_kind!=='system' && last && last[0].entry_kind!=='system' && last[0].created_by===note.created_by) last.push(note);
      else groups.push([note]);
    }
    const latestSystem=s.notes.findLast(note=>note.entry_kind==='system');
    feed.innerHTML=s.unavailable?`<div class="conversation-system" data-marketing-unavailable>${esc(s.unavailable)}${s.text?' Your unsent draft for that agent is kept separately and is not sent to anyone else.':''}</div>`
      :!groups.length?'<div class="conversation-system">No messages yet</div>':groups.map(group=>{
      const first=group[0],last=group.at(-1),time=`<time datetime="${esc(last.created_at)}" title="${esc(last.created_at)}">${esc(conversationAge(last.created_at))}</time>`;
      if(first.entry_kind==='system') return `<div class="conversation-system">${esc(first.body)}${first===latestSystem?time:''}</div>`;
      const owner=first.is_owner ?? (first.author_kind==='human');
      const name=conversationPersonName(first);
      const avatar=conversationGlyph(first,31);
      return `<div class="conversation-group ${owner?'conversation-owner':'conversation-agent'}">${!owner?`<div class="conversation-agent-header"><span class="conversation-avatar" aria-hidden="true">${avatar}</span><small>${esc(name)}</small></div>`:''}<div>${group.map(note=>`<div class="conversation-bubble">${note.title && note.title!=='Message'?`<strong>${esc(note.title)}</strong>`:''}${note.parent_event_id && adapter.openParent?`<button type="button" class="quiet conversation-parent" data-note-parent="${esc(note.id)}">About an earlier entry</button>`:''}${conversationMessage(note,(expandedMessages[channel] ||= new Set()).has(note.id))}${(note.refs||[]).map((r,i)=>`<button type="button" class="quiet" data-note-ref="${esc(note.id)}:${i}" ${r.unavailable?'disabled':''}>${esc(r.label || r.type || 'Reference')}</button>`).join('')}</div>`).join('')}${time}</div></div>`;
    }).join('');
    const messageState=expandedMessages[channel];
    feed.querySelectorAll('[data-message-id]').forEach(details=>details.addEventListener('toggle',()=>{if(!details.isConnected)return;if(details.open)messageState.add(details.dataset.messageId);else messageState.delete(details.dataset.messageId);}));
    if(typeof StoryNotePane==='function') for(const note of s.notes) {
      if(note.statuses_json) {const body=[...feed.querySelectorAll('[data-conversation-body]')].find(el=>el.dataset.conversationBody===note.id);if(body)new StoryNotePane(body,{body:note.body,statuses:note.statuses_json},{readonly:true});}
    }
    feed.querySelectorAll('[data-note-ref]').forEach(button=>button.onclick=()=>{
      const [id,index]=button.dataset.noteRef.split(':');const note=s.notes.find(n=>n.id===id);
      if(note)taskProgressOpenReference(source.biz,note.refs[Number(index)]).catch(e=>showError(e));
    });
    feed.querySelectorAll('[data-note-parent]').forEach(button=>button.onclick=()=>{
      const note=s.notes.find(n=>n.id===button.dataset.noteParent);
      if(note)Promise.resolve().then(()=>adapter.openParent(note)).catch(e=>showError(e));
    });
    if(adapter.inlineComposer)host.querySelectorAll('[title]').forEach(el=>el.removeAttribute('title'));
    older.hidden=!s.cursor && !s.before;feed.scrollTop=top;
  }
  function scheduleAgeRefresh() {
    const s=st();
    if(s.ageTimer || !s.mounts.size)return;
    s.ageTimer=setTimeout(()=>{s.ageTimer=null;for(const mount of [...s.mounts]){if(!mount.isConnected)s.mounts.delete(mount);else if(mount._conversation?.isExpanded())for(const time of mount.querySelectorAll('time[datetime]'))time.textContent=conversationAge(time.dateTime);}if(s.mounts.size)scheduleAgeRefresh();},60000);
  }
  function showError(e) { error.hidden=false;error.textContent=e.message || String(e); }
  // Moves this mount onto another Marketing state without carrying any history, draft or retry across.
  function attachMarketing(next) {
    const previous=marketing.state;
    if(previous===next)return;
    previous.mounts.delete(host);
    marketing.state=next;
    if(channel==='marketing'){next.mounts.add(host);host.dataset.conversationPeer=next.peer || '';input.value=next.text;}
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
    s.loading=true;s.loadOwner=view;if(target===channel)error.hidden=true;
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
      else if(source.type==='story') {
        const result=await conversationCall(source,'stories_timeline',{story_id:source.id,notes_only:true,limit:20,...pagination});
        data={notes:result.events.slice().reverse().map(n=>({...n,created_by:n.actor_id,created_by_name:n.actor?.name,author_kind:n.actor?.kind})),next_cursor:result.next_cursor,participants:result.participants};
      } else data=await conversationCall(source,source.type==='email'?'email_comments_list':source.type==='task'?'tasks_thread_get':'signoffs_notes_list',{[source.type==='email'?'conversation_id':source.type+'_id']:source.id,limit:20,...pagination});
      if(!current())return;
      s.participants=data.participants || s.participants;
      const oldHeight=feed.scrollHeight;
      const merged=new Map((more?[...data.notes,...s.notes]:[...s.notes,...data.notes]).map(n=>[n.id,n]));
      s.notes=[...merged.values()].sort((a,b)=>a.sequence!=null && b.sequence!=null?a.sequence-b.sequence:(a.created_at||'').localeCompare(b.created_at||'') || a.id.localeCompare(b.id));
      const missingPrivateHistory=target!=='team' && s.notes.length<(s.conversation?.message_count || 0);
      if(more || !s.loaded || (!s.cursor && !s.before && missingPrivateHistory)){s.cursor=data.next_cursor;s.before=data.next_before;}
      s.loaded=true;
      if(target===channel && channels[target]===s && current()){paint();if(more)feed.scrollTop=Math.max(0,feed.scrollHeight-oldHeight+feed.scrollTop);}
      for(const mount of s.mounts)if(mount!==host && mount.isConnected && mount.dataset.conversation===key && mount.dataset.conversationChannel===target && mount.dataset.conversationPeer===(s.peer || '') && mount._conversation?.isExpanded())mount._conversation._paint?.();
      s.rejectionRequiresMessage=data.rejection_requires_message;
      // Card-level people projections describe the team, not the private pair,
      // and stay current even while the private channel is the one on screen.
      if(target==='team'){adapter.onParticipants?.(conversationPeople(s));paintTeamIcon();}
      if(target!=='team' && !more && options.acknowledge)await acknowledge(s);
    } catch(e){
      if(!current())return;
      if(target!=='team' && s.peer && CONVERSATION_LOST.includes(e.code))markUnavailable(s,e.code==='revision_conflict'?e.message:s.channel.startsWith('agent:')?'This agent is no longer available for this task.':CONVERSATION_CHANGED);
      else if(['forbidden','not_found'].includes(e.code)){
        s.notes=[];s.participants=[];s.loaded=false;s.cursor=null;s.before=null;
        if(target===channel && current())paint();
        adapter.onUnavailable?.(e);
      }
      if(target===channel && channels[target]===s && current() && !(target==='marketing' && s.unavailable))showError(e);
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
    if((sending==='marketing' || sending.startsWith('agent:')) && !s.peer){showError(new Error('The Marketing agent has not loaded yet. Try again once its messages appear.'));return;}
    s.sending=true;syncSending();error.hidden=true;
    let saved=false;
    try {
      if(!s.submission) {
        let revision=host._source.revision;
        if(sending==='team' && (source.type==='task' || source.type==='story')) {
          const data=await conversationCall(source,source.type==='task'?'tasks_get':'stories_get',{[source.type+'_id']:source.id});revision=data[source.type].revision;
        }
        s.submission={text:s.text,idempotency_key:newRequestId(),peer:s.peer,...(sending==='team'?{revision}:{})};
      }
      const request=s.submission;
      // A retry belongs to the agent it was first sent for.
      if(request.peer!==s.peer)throw Object.assign(new Error(CONVERSATION_CHANGED),{code:'revision_conflict'});
      if(sending!=='team') await conversationPrivateCall(source,adapter,'owner_conversation_send',{text:request.text,...conversationPeer(s)},{idempotency_key:request.idempotency_key});
      else if(adapter.send) await conversationRequest(adapter.send(request));
      else await conversationCall(source,{task:'tasks_comment',story:'stories_note_add',signoff:'signoffs_comment',email:'email_comment'}[source.type],
        {[source.type==='email'?'conversation_id':source.type+'_id']:source.id,...(source.type==='story'?{body:request.text}:{text:request.text})},
        {idempotency_key:request.idempotency_key,...(request.revision?{expected_revision:request.revision}:{})});
      s.text='';s.submission=null;saved=true;if(current() && channel===sending){input.value='';send.textContent='Send';}syncDraft(channel===sending?input:null,s);
    } catch(e){
      // The server owns the channel rule: move the draft to the private channel
      // rather than losing it or retrying a write the owner may not make.
      if(e.code==='channel_required'){
        const draft=s.text;s.submission=null;s.text='';
        channels.private.text=draft;
        selectChannel('private',{acknowledge:false});
        if(current()){input.value=draft;showError(e);}
      }
      else if(sending==='marketing' && CONVERSATION_LOST.includes(e.code) && s.peer){
        // The pinned agent can no longer be written to: keep the draft with that agent, drop the retry, and never re-aim.
        s.submission=null;markUnavailable(s,e.code==='revision_conflict'?e.message:s.channel.startsWith('agent:')?'This agent is no longer available for this task.':CONVERSATION_CHANGED);
      }
      else if(current() && channel===sending){showError(e);if(['revision_conflict','stale_signoff','not_found','forbidden','policy_denied'].includes(e.code)){s.submission=null;send.textContent='Send';error.textContent+=' Review the current source before sending again.';}else if(s.submission){send.textContent='Retry Send';if(e.code==='request_timeout')error.textContent='Could not confirm delivery. Retry Send to check the same message.';}}
    } finally{s.sending=false;syncSending();adapter.onInput?.();}
    // A confirmed write is complete. Refresh history without holding the
    // composer disabled, and preserve the same submission key on uncertain writes.
    if(saved){s.refreshAfterSend=true;void load(false,{channel:s.channel});}
  }
  // Selecting the Marketing tab (or reopening the conversation) after the channel was lost is the explicit
  // step that looks the current Marketing agent up again by role, with a clean slate.
  function selectChannel(next,options={}) {
    if(!channels[next])return;
    const reset=next==='marketing' && !!channels.marketing.unavailable;
    if(next===channel && !reset)return;
    if(reset)attachMarketing(conversationNewState(source,'marketing'));
    if(next!==channel) {
      scroll[channel]=feed.scrollTop;
      channels[channel].mounts.delete(host);
      channel=next;host.dataset.conversationChannel=next;host.dataset.conversationPeer=channels[next].peer || '';
      channels[next].mounts.add(host);
    }
    syncChannel();
    input.value=st().text;
    syncSending();painted='';paint();feed.scrollTop=scroll[channel] || 0;
    if(expanded)void load(false,{acknowledge:options.acknowledge!==false && next!=='team'});
    void loadPeers();
  }
  function setUnread(value) {
    unreadDot.hidden=!value;
    host.dataset.conversationUnread=value?'true':'false';
    // A button's aria-label replaces its subtree, so the state belongs in the name.
    toggle.setAttribute('aria-label',value?'Comment, unread private reply':'Comment');
  }
  input.onkeydown=event=>{
    if(event.key==='Enter' && event.ctrlKey && !event.isComposing && !send.disabled && !input.disabled){event.preventDefault();submit();}
  };
  function expand(value,notify=true,explicit=false) {if(disposed)return;if(!value)scroll[channel]=feed.scrollTop;expanded=value;panel.hidden=!value;toggle.setAttribute('aria-expanded',String(value));if(notify)adapter.onToggle?.(value);if(value) {void loadPeers();if(explicit && channel==='marketing' && lost()){attachMarketing(conversationNewState(source,'marketing'));syncChannel();syncSending();painted='';input.value=st().text;}if(st().loaded)paint();feed.scrollTop=scroll[channel] || 0;void load(false,{acknowledge:channel!=='team'});scheduleAgeRefresh();}}
  toggle.onclick=()=>expand(!expanded,true,true);older.onclick=()=>load(true);send.onclick=submit;
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
          expandedMessages[peer.channel]=new Set();scroll[peer.channel]=0;
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
        if(!s.mounts.size){clearTimeout(s.ageTimer);s.ageTimer=null;}
      }
      input.oninput=input.onkeydown=send.onclick=older.onclick=toggle.onclick=feed.onscroll=null;
      input.disabled=send.disabled=true;
      tablist.onclick=tablist.onkeydown=null;
      if(host._conversation===view){delete host._conversation;delete host.dataset.conversation;}
      toggle.remove();host.replaceChildren();
    },
    get state(){return st();},
    states:channels,_paint:paint};
  // Collapsed by default at the component level: mounting is not itself an
  // explicit open (Focus mounts every card up front, long before any of them
  // are looked at). Consumers that only ever mount once a specific item is
  // already the one the owner is looking at (a selected task/story/signoff,
  // an opened email thread, the Focus card the owner just clicked, the inbox
  // modal) call .expand() themselves right after mounting, once, so the chat
  // still shows by default there without baking an eager private-channel
  // read-acknowledgment into this shared default.
  expand(false,false);return host._conversation;
}
