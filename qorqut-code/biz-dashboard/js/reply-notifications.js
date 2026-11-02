// Owner reply notifications and shared unread markers, independent of the current page.
const replyNotifications=(()=>{
  let communicationCursor=null,seenCommunications=new Set(),loaded=false,inflight=null,trailing=false,items=[],signature='',seen=new Set(),timer;
  try{seen=new Set(JSON.parse(sessionStorage.getItem('reply-notifications:seen') || '[]'));}catch(_){}
  const key=s=>`${s?.type}:${s?.business_id || 'global'}:${s?.id}`;
  const unread=source=>loaded?items.some(item=>item.kind==='thread' && item.unread && key(item.source)===key(source)):null;
  // Main chats and task DMs share the same per-agent unread marker, for every scope.
  const unreadReply=item=>(item.chat===true || item.kind==='thread') && item.unread;
  const unreadAgent=agentId=>loaded?items.some(item=>unreadReply(item) && item.agent_id===agentId):null;
  const unreadChat=()=>loaded?items.some(unreadReply):null;
  const viewing=item=>item.chat===true && typeof globalChatReadable==='function' && globalChatReadable(item.agent_id);
  const eligible = id => primaryAgent(globalAgentProfile(id));
  function paintDots(){
    const dot=(host,on,label)=>{
      if(!host)return;
      let el=host.querySelector(':scope > .chat-unread-dot');
      if(!on){el?.remove();return;}
      if(!el){el=document.createElement('span');el.className='chat-unread-dot';host.appendChild(el);}
      el.setAttribute('role','img');el.setAttribute('aria-label',label);
    };
    document.querySelectorAll('[data-global-agent]').forEach(row=>{
      const id=row.dataset.globalAgent;
      dot(row.querySelector('.nm'),eligible(id) && !!unreadAgent(id),'Unread reply');
    });
  }
  async function open(item){
    if(item.kind==='global'){
      if(typeof globalAgentsNavigate==='function' && await globalAgentsNavigate(item.agent_id,{chat:true}))return;
      ls('ag:scope:global',item.agent_id);
      if(location.hash==='#/global/agents')await renderTab();else location.hash='/global/agents';
      return;
    }
    let conversation;
    modal({title:`${esc(item.agent_name)} · ${esc(item.source.type)} thread`,width:760,stack:true,body:'<div class="reply-notification-thread"></div>',
      onOpen:async el=>{
        const host=el.querySelector('.reply-notification-thread');
        conversation=conversationMount(host,{type:item.source.type,id:item.source.id,biz:item.biz},{dismissOnOutside:false});
        try{await conversation.openPeer(item.agent_id);}catch(error){toast(error.message);}
      },onClose:()=>{conversation?.dispose();void refresh();}});
  }
  // Order-independent comparison of every field, so a changed flag or preview counts as well as a new ID.
  const canonical=value=>JSON.stringify(value,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([x],[y])=>x<y?-1:x>y?1:0)):v);
  async function load(){
    try{
      const [response]=await Promise.all([api('/root/reply-notifications'+(communicationCursor===null?'':'?communications_after='+communicationCursor)),loadGlobalAgentProfiles()]);
      const data=coreData(response);
      const first=!loaded,next=data.items || [],nextSignature=canonical(next);
      const changed=first || nextSignature!==signature;
      items=next;signature=nextSignature;loaded=true;
      for(const item of items){
        if(seen.has(item.id))continue;
        seen.add(item.id);
        // Establish a baseline on first load; existing unread markers still appear.
        // The owner already looking at that chat does not need a toast for what is on screen.
        if(!first && eligible(item.agent_id) && !item.nav_only && !viewing(item))notify({replyAgentId:item.agent_id,text:item.agent_name,sub:item.text,onClick:()=>open(item),ms:6000});
      }
      const feed=data.communications;
      if(feed && Number.isSafeInteger(feed.next_cursor)){
        // The first response is the baseline: earlier traffic never replays. The cursor moves only after enqueueing.
        if(communicationCursor!==null){
          for(const c of feed.items || []){
            if(!(c.event_id>communicationCursor) || seenCommunications.has(c.event_id))continue;
            seenCommunications.add(c.event_id);
            notify({text:`${c.sender_name} \u2192 ${c.recipient_name}`,communication:c,ms:3000});
          }
          seenCommunications=new Set([...seenCommunications].slice(-1000));
        }
        communicationCursor=Math.max(communicationCursor ?? 0,feed.next_cursor);
        if(feed.has_more)trailing=true;
      }
      seen=new Set([...seen].slice(-1000));
      try{sessionStorage.setItem('reply-notifications:seen',JSON.stringify([...seen]));}catch(_){}
      if(changed){
        paintDots();
        document.dispatchEvent(new Event('reply-notifications-changed'));
      }
    }catch(_){/* Retain the last known unread state; retry without toast spam. */}
  }
  // One request at a time. An explicit refresh arriving in flight records a single trailing
  // read and resolves after it; timer ticks never do.
  function run(){
    inflight=(async()=>{
      try{do{trailing=false;await load();}while(trailing);}
      finally{inflight=null;clearTimeout(timer);timer=setTimeout(poll,document.hidden?30000:10000);}
    })();
    return inflight;
  }
  const poll=()=>inflight || run();
  function refresh(){
    if(inflight){trailing=true;return inflight;}
    return run();
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refresh();});
  return {start:poll,refresh,unread,unreadAgent,unreadChat,paint:paintDots,open};
})();
