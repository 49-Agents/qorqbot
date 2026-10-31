// One source-owned conversation with two separate channels: the owner's private
// channel with Ordubashy and the team's agent discussion. Local drafts only, and
// no viewing telemetry beyond the explicit private read acknowledgment.
const conversationStates = new Map();
let conversationMountId = 0;
const CONVERSATION_CHANNELS = [['private', 'Private with Ordubashy'], ['team', 'Agent discussion']];
// Inline Focus conversations dismiss on outside clicks, while their contents stay interactive.
document.addEventListener('click',event=>{
  // composedPath() is frozen at dispatch time: a reactive re-render (the team
  // tab's avatar stack can replace itself between dispatch and here) can leave
  // event.target detached, which would make a plain .contains(event.target)
  // check wrongly read as "outside" and collapse the panel the click opened.
  const path=event.composedPath();
  for(const host of document.querySelectorAll('[data-inline-conversation]')) {
    if(!path.includes(host.closest('.agfocus-card') || host) && host._conversation?.isExpanded())host._conversation.collapse();
  }
});
function conversationKey(source) { return `${source.type}:${source.biz || 'global'}:${source.id}`; }
function conversationStateKey(source,channel) { return `${conversationKey(source)}:${channel}`; }
function conversationState(source,channel='private') {
  const key=conversationStateKey(source,channel);
  if (!conversationStates.has(key)) conversationStates.set(key,{channel,text:'',notes:[],participants:[],loaded:false,cursor:null,before:null,mounts:new Set(),ageTimer:null,conversation:null,readState:null});
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
// Up to 4 overlapping avatars for the team-channel toast; no overflow count,
// unlike the Focus people row — if more than 4 have posted, just show 4.
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
  const words=String(summary || body || '').trim().split(/\s+/u).filter(Boolean);
  return words.slice(0,10).join(' ') + (words.length>10?'…':'');
}
function conversationMessage(note, open) {
  const body=`<div data-conversation-body="${esc(note.id)}">${esc(note.body || '')}</div>`;
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
  const expandedMessages=new Set();
  if (host.dataset.conversation===key) { host._source=source; return host._conversation; }
  host.dataset.conversation=key;host._source=source;
  // The session can arrive after a mount, so the owner test is read on demand
  // rather than frozen: a late non-owner session restores the team composer.
  const viewerIsOwner=()=>conversationViewerIsOwner();
  const channels={private:conversationState(source,'private'),team:conversationState(source,'team')};
  let channel=adapter.channel || (viewerIsOwner()?'private':'team');
  const st=()=>channels[channel];
  const readOnly=()=>channel==='team' && viewerIsOwner();
  const mountId=++conversationMountId;
  const panelId=`conversation-panel-${mountId}`;
  const tabId=name=>`conversation-tab-${mountId}-${name}`;
  // Icon-only, matching the pane's .ttab icon tabs: the label stays as the
  // accessible name (aria-label) instead of visible text.
  const tabs=CONVERSATION_CHANNELS.map(([name,label])=>`<button type="button" role="tab" id="${tabId(name)}" class="quiet conversation-channel" data-channel="${name}" aria-selected="false" tabindex="-1" aria-controls="${panelId}" aria-label="${esc(label)}"><span class="conversation-channel-icon" data-channel-icon="${name}">${name==='private'?conversationOrdubashyGlyph(18):conversationChannelAvatars([])}</span></button>`).join('');
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
  host.dataset.conversationChannel=channel;
  const syncDraft=sourceInput=>{const s=st();for(const mount of [...s.mounts]){if(!mount.isConnected || mount.dataset.conversation!==key || mount.dataset.conversationChannel!==s.channel){s.mounts.delete(mount);continue;}const other=mount.querySelector('textarea');if(other && other!==sourceInput && other.value!==s.text)other.value=s.text;}};
  input.value=st().text;input.oninput=()=>{st().text=input.value;syncDraft(input);adapter.onInput?.();};
  const current=()=>host.isConnected && host.dataset.conversation===key;
  function syncSending() {
    const s=st();
    for(const mount of s.mounts)if(mount.isConnected && mount.dataset.conversation===key && mount.dataset.conversationChannel===s.channel){
      const tx=mount.querySelector('textarea'),button=mount.querySelector('.conversation-send');
      if(tx)tx.disabled=!!s.sending || !!s.submission;
      if(button){button.disabled=!!s.sending;button.textContent=s.submission?'Retry Send':'Send';}
    }
  }
  function syncChannel() {
    for(const tab of host.querySelectorAll('.conversation-channel')) {
      const selected=tab.dataset.channel===channel;
      tab.setAttribute('aria-selected',String(selected));
      tab.tabIndex=selected?0:-1;
    }
    panel.setAttribute('aria-labelledby',tabId(channel));
    const hidden=readOnly();
    // Only the composer goes away: the inline wrapper still carries the Focus
    // message icon, which must stay reachable in the read-only team view.
    composeLabel.hidden=hidden;send.hidden=hidden;
    if(adapter.inlineComposer)input.placeholder=adapter.placeholder ?? (channel==='private'?'Private message to Ordubashy…':'Leave a comment…');
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
    const signature=JSON.stringify([channel,s.notes,s.participants,s.cursor,s.before]);
    if(painted===signature)return;
    painted=signature;
    const top=feed.scrollTop;
    const groups=[];
    for (const note of s.notes) {
      const last=groups.at(-1);
      if(note.entry_kind!=='system' && last && last[0].entry_kind!=='system' && last[0].created_by===note.created_by) last.push(note);
      else groups.push([note]);
    }
    const latestSystem=s.notes.findLast(note=>note.entry_kind==='system');
    feed.innerHTML=!groups.length?'<div class="conversation-system">No messages yet</div>':groups.map(group=>{
      const first=group[0],last=group.at(-1),time=`<time datetime="${esc(last.created_at)}" title="${esc(last.created_at)}">${esc(conversationAge(last.created_at))}</time>`;
      if(first.entry_kind==='system') return `<div class="conversation-system">${esc(first.body)}${first===latestSystem?time:''}</div>`;
      const owner=first.is_owner ?? (first.author_kind==='human');
      const name=conversationPersonName(first);
      const avatar=conversationGlyph(first,31);
      return `<div class="conversation-group ${owner?'conversation-owner':'conversation-agent'}">${!owner?`<div class="conversation-agent-header"><span class="conversation-avatar" aria-hidden="true">${avatar}</span><small>${esc(name)}</small></div>`:''}<div>${group.map(note=>`<div class="conversation-bubble">${note.title && note.title!=='Message'?`<strong>${esc(note.title)}</strong>`:''}${note.parent_event_id && adapter.openParent?`<button type="button" class="quiet conversation-parent" data-note-parent="${esc(note.id)}">About an earlier entry</button>`:''}${conversationMessage(note,expandedMessages.has(note.id))}${(note.refs||[]).map((r,i)=>`<button type="button" class="quiet" data-note-ref="${esc(note.id)}:${i}" ${r.unavailable?'disabled':''}>${esc(r.label || r.type || 'Reference')}</button>`).join('')}</div>`).join('')}${time}</div></div>`;
    }).join('');
    feed.querySelectorAll('[data-message-id]').forEach(details=>details.addEventListener('toggle',()=>{if(!details.isConnected)return;if(details.open)expandedMessages.add(details.dataset.messageId);else expandedMessages.delete(details.dataset.messageId);}));
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
  // Acknowledge only what the server has already returned to this owner, and only
  // after an interactive private load that included the newest page.
  async function acknowledge(s) {
    const stillPrivateVisible=()=>viewerIsOwner() && expanded && channel==='private' && current();
    if(!stillPrivateVisible() || s.marking)return;
    const latest=s.conversation?.latest_sequence || 0;
    if(!latest || latest<=(s.readState?.last_read_sequence || 0))return;
    s.marking=true;
    try {
      // Do not acknowledge a load that the owner has already closed or switched
      // away from while the private history request was in flight.
      if(!stillPrivateVisible())return;
      const data=await conversationPrivateCall(source,adapter,'owner_conversation_mark_read',{sequence:latest},{idempotency_key:newRequestId()});
      const readState=data?.read_state;
      s.readState=readState || {last_read_sequence:latest,unread:false};
      // A coordinator reply can arrive between GET and mark-read. The server's
      // unread result is authoritative: keep or restore the dot and projection.
      if(!stillPrivateVisible())return;
      if(readState?.unread===true || Number(readState?.last_read_sequence ?? latest)<latest) {
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
    const target=options.channel || channel,s=channels[target];
    if(s.loading)return;
    s.loading=true;if(target===channel)error.hidden=true;
    s.refreshAfterSend=false;
    try {
      let data;
      const pagination=more?{...(s.cursor?{cursor:s.cursor}:{}),...(s.before?{before:s.before}:{})}:{};
      if(target==='private') {
        const result=await conversationPrivateCall(source,adapter,'owner_conversation_get',{limit:20,...(more && s.before?{before:s.before}:{})});
        s.conversation=result.conversation || null;s.readState=result.read_state || null;
        data={notes:result.messages || [],next_before:result.next_before,participants:result.participants};
      }
      else if(adapter.read) data=await conversationRequest(adapter.read(pagination));
      else if(source.type==='story') {
        const result=await conversationCall(source,'stories_timeline',{story_id:source.id,notes_only:true,limit:20,...pagination});
        data={notes:result.events.slice().reverse().map(n=>({...n,created_by:n.actor_id,created_by_name:n.actor?.name,author_kind:n.actor?.kind})),next_cursor:result.next_cursor,participants:result.participants};
      } else data=await conversationCall(source,source.type==='email'?'email_comments_list':source.type==='task'?'tasks_thread_get':'signoffs_notes_list',{[source.type==='email'?'conversation_id':source.type+'_id']:source.id,limit:20,...pagination});
      s.participants=data.participants || s.participants;
      const oldHeight=feed.scrollHeight;
      const merged=new Map((more?[...data.notes,...s.notes]:[...s.notes,...data.notes]).map(n=>[n.id,n]));
      s.notes=[...merged.values()].sort((a,b)=>a.sequence!=null && b.sequence!=null?a.sequence-b.sequence:(a.created_at||'').localeCompare(b.created_at||'') || a.id.localeCompare(b.id));
      if(more || !s.loaded){s.cursor=data.next_cursor;s.before=data.next_before;}
      s.loaded=true;
      if(target===channel && current()){paint();if(more)feed.scrollTop=Math.max(0,feed.scrollHeight-oldHeight+feed.scrollTop);}
      for(const mount of s.mounts)if(mount!==host && mount.isConnected && mount.dataset.conversation===key && mount.dataset.conversationChannel===target && mount._conversation?.isExpanded())mount._conversation._paint?.();
      s.rejectionRequiresMessage=data.rejection_requires_message;
      // Card-level people projections describe the team, not the private pair,
      // and stay current even while the private channel is the one on screen.
      if(target==='team'){adapter.onParticipants?.(conversationPeople(s));paintTeamIcon();}
      if(target==='private' && !more && options.acknowledge)await acknowledge(s);
    } catch(e){
      if(['forbidden','not_found'].includes(e.code)){
        s.notes=[];s.participants=[];s.loaded=false;s.cursor=null;s.before=null;
        if(target===channel && current())paint();
      }
      if(target===channel && current())showError(e);
    } finally{
      s.loading=false;
      // A send may finish while an earlier read is still in flight. Fetch again
      // afterwards so that response cannot leave the new message out of view.
      if(s.refreshAfterSend)void load(false,{channel:target});
    }
  }
  async function submit() {
    // A tab switch mid-send must not redirect the message: the channel and its
    // state are fixed for the whole submission.
    const sending=channel,s=channels[sending];
    if(s.sending || !s.text.trim() || readOnly())return;
    s.sending=true;syncSending();error.hidden=true;
    let saved=false;
    try {
      if(!s.submission) {
        let revision=host._source.revision;
        if(sending==='team' && (source.type==='task' || source.type==='story')) {
          const data=await conversationCall(source,source.type==='task'?'tasks_get':'stories_get',{[source.type+'_id']:source.id});revision=data[source.type].revision;
        }
        s.submission={text:s.text,idempotency_key:newRequestId(),...(sending==='team'?{revision}:{})};
      }
      const request=s.submission;
      if(sending==='private') await conversationPrivateCall(source,adapter,'owner_conversation_send',{text:request.text},{idempotency_key:request.idempotency_key});
      else if(adapter.send) await conversationRequest(adapter.send(request));
      else await conversationCall(source,{task:'tasks_comment',story:'stories_note_add',signoff:'signoffs_comment',email:'email_comment'}[source.type],
        {[source.type==='email'?'conversation_id':source.type+'_id']:source.id,...(source.type==='story'?{body:request.text}:{text:request.text})},
        {idempotency_key:request.idempotency_key,...(request.revision?{expected_revision:request.revision}:{})});
      s.text='';s.submission=null;saved=true;if(current() && channel===sending){input.value='';send.textContent='Send';}syncDraft(input);
    } catch(e){
      // The server owns the channel rule: move the draft to the private channel
      // rather than losing it or retrying a write the owner may not make.
      if(e.code==='channel_required'){
        const draft=s.text;s.submission=null;s.text='';
        channels.private.text=draft;
        selectChannel('private',{acknowledge:false});
        if(current()){input.value=draft;showError(e);}
      }
      else if(current() && channel===sending){showError(e);if(['revision_conflict','stale_signoff','not_found','forbidden','policy_denied'].includes(e.code)){s.submission=null;send.textContent='Send';error.textContent+=' Review the current source before sending again.';}else if(s.submission){send.textContent='Retry Send';if(e.code==='request_timeout')error.textContent='Could not confirm delivery. Retry Send to check the same message.';}}
    } finally{s.sending=false;syncSending();adapter.onInput?.();}
    // A confirmed write is complete. Refresh history without holding the
    // composer disabled, and preserve the same submission key on uncertain writes.
    if(saved){s.refreshAfterSend=true;void load(false,{channel:s.channel});}
  }
  function selectChannel(next,options={}) {
    if(!channels[next] || next===channel)return;
    channels[channel].mounts.delete(host);
    channel=next;host.dataset.conversationChannel=next;
    channels[next].mounts.add(host);
    syncChannel();
    input.value=st().text;
    syncSending();painted='';paint();
    if(expanded)void load(false,{acknowledge:options.acknowledge!==false && next==='private'});
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
  function expand(value,notify=true) {expanded=value;panel.hidden=!value;toggle.setAttribute('aria-expanded',String(value));if(notify)adapter.onToggle?.(value);if(value) {if(st().loaded)paint();void load(false,{acknowledge:channel==='private'});scheduleAgeRefresh();}}
  toggle.onclick=()=>expand(!expanded);older.onclick=()=>load(true);send.onclick=submit;
  const channelTabs=[...host.querySelectorAll('.conversation-channel')];
  for(const tab of channelTabs) {
    tab.onclick=()=>selectChannel(tab.dataset.channel);
    tab.onkeydown=event=>{
      const moves={ArrowRight:1,ArrowLeft:-1};
      const index=channelTabs.indexOf(tab);
      const next=event.key in moves?channelTabs[(index+moves[event.key]+channelTabs.length)%channelTabs.length]
        :event.key==='Home'?channelTabs[0]:event.key==='End'?channelTabs.at(-1):null;
      if(!next)return;
      event.preventDefault();
      selectChannel(next.dataset.channel);
      next.focus();
    };
  }
  host._conversation={
    refresh:()=>expanded || adapter.inlineComposer?load(false,{channel:expanded?channel:'team'}):null,
    // Preloading is a background read of the team discussion only: it never
    // acknowledges private messages and never reads the private channel.
    preload:()=>load(false,{channel:'team'}),
    isExpanded:()=>expanded,expand:()=>expand(true),collapse:()=>expand(false),
    channel:()=>channel,selectChannel,setUnread,
    hasDraft:()=>!!(channels.private.text || channels.team.text),
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
