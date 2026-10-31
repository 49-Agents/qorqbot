// One source-owned conversation, with local drafts and no viewing telemetry.
const conversationStates = new Map();
function conversationKey(source) { return `${source.type}:${source.biz || 'global'}:${source.id}`; }
function conversationState(source) {
  const key=conversationKey(source);
  if (!conversationStates.has(key)) conversationStates.set(key,{text:'',notes:[],participants:[],loaded:false,cursor:null,before:null});
  return conversationStates.get(key);
}
function conversationAge(at) {
  if (typeof agpadInboxAge === 'function') return agpadInboxAge(at);
  const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(at))/1000));
  const [n,unit]=seconds<60?[seconds,'second']:seconds<3600?[Math.floor(seconds/60),'minute']:seconds<86400?[Math.floor(seconds/3600),'hour']:[Math.floor(seconds/86400),'day'];
  return `${n} ${unit}${n===1?'':'s'} ago`;
}
async function conversationCall(source,operation,args,options={}) {
  const path=source.biz?`/biz/${encodeURIComponent(source.biz)}`:'/root';
  return coreData(await post(`${path}/operations/${operation}`,{...args,...(!source.biz?{scope:'global'}:{}),...options}));
}
function conversationMount(host,source,adapter={}) {
  if (!host) return;
  const key=conversationKey(source),s=conversationState(source);
  let expanded=false;
  if (host.dataset.conversation===key) { host._source=source; return host._conversation; }
  host.dataset.conversation=key;host._source=source;
  host.innerHTML='<button type="button" class="quiet conversation-toggle" aria-expanded="false">Comment</button><section class="conversation-panel" hidden><div class="conversation-participants" aria-label="Conversation participants"></div><button type="button" class="quiet conversation-older" hidden>Older messages</button><div class="conversation-feed" role="log" aria-label="Conversation"></div><label class="conversation-compose">Message<textarea class="inp" rows="3" maxlength="20000"></textarea></label><button type="button" class="btn conversation-send">Send</button><p class="ferr" role="alert" hidden></p></section>';
  const toggle=host.querySelector('.conversation-toggle'),panel=host.querySelector('.conversation-panel'),feed=host.querySelector('.conversation-feed'),input=host.querySelector('textarea'),send=host.querySelector('.conversation-send'),older=host.querySelector('.conversation-older'),error=host.querySelector('[role="alert"]');
  input.value=s.text;input.oninput=()=>{s.text=input.value;adapter.onInput?.();};
  const current=()=>host.isConnected && host.dataset.conversation===key;
  function paint() {
    const top=feed.scrollTop;
    const people=new Map();
    for (const n of [...(s.participants || []),...s.notes]) if(n.entry_kind!=='system') people.set(n.created_by,n);
    host.querySelector('.conversation-participants').innerHTML=[...people.values()].map(person=>{
      const name=person.created_by_name || person.created_by || 'Unknown';
      const glyph=typeof agentGlyph==='function' && person.presentation?agentGlyph(person.presentation,20):esc(name.slice(0,2));
      return `<span title="${esc(name)}" aria-label="${esc(name)}">${glyph}</span>`;
    }).join(' ') || 'No messages yet';
    const groups=[];
    for (const note of s.notes) {
      const last=groups.at(-1);
      if(note.entry_kind!=='system' && last && last[0].entry_kind!=='system' && last[0].created_by===note.created_by) last.push(note);
      else groups.push([note]);
    }
    feed.innerHTML=groups.map(group=>{
      const first=group[0],last=group.at(-1),time=`<time datetime="${esc(last.created_at)}" title="${esc(last.created_at)}">${esc(conversationAge(last.created_at))}</time>`;
      if(first.entry_kind==='system') return `<div class="conversation-system">${esc(first.body)}${time}</div>`;
      const owner=first.is_owner ?? (first.author_kind==='human');
      const name=first.created_by_name || first.created_by || 'Unknown';
      const avatar=typeof agentGlyph==='function' && first.presentation?agentGlyph(first.presentation,24):esc(name.slice(0,2));
      return `<div class="conversation-group ${owner?'conversation-owner':'conversation-agent'}">${!owner?`<span class="conversation-avatar" aria-hidden="true">${avatar}</span><small>${esc(name)}</small>`:''}<div>${group.map(note=>`<div class="conversation-bubble">${note.title && note.title!=='Message'?`<strong>${esc(note.title)}</strong>`:''}<div data-conversation-body="${esc(note.id)}">${esc(note.body || '')}</div>${(note.refs||[]).map((r,i)=>`<button type="button" class="quiet" data-note-ref="${esc(note.id)}:${i}" ${r.unavailable?'disabled':''}>${esc(r.label || r.type || 'Reference')}</button>`).join('')}</div>`).join('')}${time}</div></div>`;
    }).join('');
    if(typeof StoryNotePane==='function') for(const note of s.notes) {
      if(note.statuses_json) {const body=[...feed.querySelectorAll('[data-conversation-body]')].find(el=>el.dataset.conversationBody===note.id);if(body)new StoryNotePane(body,{body:note.body,statuses:note.statuses_json},{readonly:true});}
    }
    feed.querySelectorAll('[data-note-ref]').forEach(button=>button.onclick=()=>{
      const [id,index]=button.dataset.noteRef.split(':');const note=s.notes.find(n=>n.id===id);
      if(note)taskProgressOpenReference(source.biz,note.refs[Number(index)]).catch(e=>showError(e));
    });
    older.hidden=!s.cursor && !s.before;feed.scrollTop=top;
  }
  function showError(e) { error.hidden=false;error.textContent=e.message || String(e); }
  async function load(more=false) {
    if(s.loading)return;s.loading=true;error.hidden=true;
    try {
      let data;
      const pagination=more?{...(s.cursor?{cursor:s.cursor}:{}),...(s.before?{before:s.before}:{})}:{};
      if(adapter.read) data=await adapter.read(pagination);
      else if(source.type==='story') {
        const result=await conversationCall(source,'stories_timeline',{story_id:source.id,notes_only:true,limit:20,...pagination});
        data={notes:result.events.slice().reverse().map(n=>({...n,created_by:n.actor_id,created_by_name:n.actor?.name,author_kind:n.actor?.kind})),next_cursor:result.next_cursor,participants:result.participants};
      } else data=await conversationCall(source,source.type==='task'?'tasks_thread_get':'signoffs_notes_list',{[source.type+'_id']:source.id,limit:20,...pagination});
      s.participants=data.participants || s.participants;
      const merged=new Map((more?[...data.notes,...s.notes]:[...s.notes,...data.notes]).map(n=>[n.id,n]));
      s.notes=[...merged.values()].sort((a,b)=>a.sequence!=null && b.sequence!=null?a.sequence-b.sequence:(a.created_at||'').localeCompare(b.created_at||'') || a.id.localeCompare(b.id));
      if(more || !s.loaded){s.cursor=data.next_cursor;s.before=data.next_before;}
      s.loaded=true;if(current())paint();
      s.rejectionRequiresMessage=data.rejection_requires_message;
    } catch(e){if(current())showError(e);} finally{s.loading=false;}
  }
  async function submit() {
    if(s.sending || !s.text.trim())return;
    s.sending=true;send.disabled=true;input.disabled=true;error.hidden=true;
    try {
      if(!s.submission) {
        let revision=host._source.revision;
        if(source.type==='task' || source.type==='story') {
          const data=await conversationCall(source,source.type==='task'?'tasks_get':'stories_get',{[source.type+'_id']:source.id});revision=data[source.type].revision;
        }
        s.submission={text:s.text,idempotency_key:newRequestId(),revision};
      }
      const request=s.submission;
      if(adapter.send) await adapter.send(request);
      else await conversationCall(source,{task:'tasks_comment',story:'stories_note_add',signoff:'signoffs_comment'}[source.type],
        {[source.type+'_id']:source.id,...(source.type==='story'?{body:request.text}:{text:request.text})},
        {idempotency_key:request.idempotency_key,...(request.revision?{expected_revision:request.revision}:{})});
      s.text='';s.submission=null;if(current()){input.value='';send.textContent='Send';}await load();
    } catch(e){if(current()){showError(e);if(['revision_conflict','stale_signoff','not_found','forbidden','policy_denied'].includes(e.code)){s.submission=null;send.textContent='Send';error.textContent+=' Review the current source before sending again.';}else send.textContent='Retry Send';}} finally{s.sending=false;if(current()){send.disabled=false;input.disabled=!!s.submission;}}
  }
  function expand(value) {expanded=value;panel.hidden=!value;toggle.setAttribute('aria-expanded',String(value));if(value) {if(s.loaded)paint();load();}}
  toggle.onclick=()=>expand(!expanded);older.onclick=()=>load(true);send.onclick=submit;
  host._conversation={refresh:()=>expanded?load():null,isExpanded:()=>expanded,expand:()=>expand(true),state:s};
  expand(false);return host._conversation;
}
