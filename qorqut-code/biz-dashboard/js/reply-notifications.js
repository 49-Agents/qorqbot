// Owner reply notifications and shared unread markers, independent of the current page.
const replyNotifications=(()=>{
  let loaded=false,pending=false,items=[],seen=new Set(),timer;
  try{seen=new Set(JSON.parse(sessionStorage.getItem('reply-notifications:seen') || '[]'));}catch(_){}
  const key=s=>`${s?.type}:${s?.business_id || 'global'}:${s?.id}`;
  const unread=source=>loaded?items.some(item=>item.kind==='thread' && item.unread && key(item.source)===key(source)):null;
  async function open(item){
    if(item.kind==='global'){
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
      },onClose:()=>{conversation?.dispose();void poll();}});
  }
  async function poll(){
    if(pending)return;
    pending=true;
    try{
      const data=coreData(await api('/root/reply-notifications'));
      const first=!loaded;items=data.items || [];loaded=true;
      for(const item of items){
        if(seen.has(item.id))continue;
        seen.add(item.id);
        // Establish a baseline on first load; existing unread markers still appear.
        if(!first)notify({text:item.agent_name+' replied',sub:item.text,onClick:()=>open(item),ms:8000});
      }
      seen=new Set([...seen].slice(-1000));
      try{sessionStorage.setItem('reply-notifications:seen',JSON.stringify([...seen]));}catch(_){}
      document.dispatchEvent(new Event('reply-notifications-changed'));
    }catch(_){/* Retain the last known unread state; retry without toast spam. */}
    finally{pending=false;clearTimeout(timer);timer=setTimeout(poll,document.hidden?30000:10000);}
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)void poll();});
  return {start:poll,refresh:poll,unread,open};
})();
