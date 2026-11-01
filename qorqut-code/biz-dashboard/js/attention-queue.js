// Shared owner queue. Message read cursors and Signoff decisions remain independent.
const attentionQueue=(()=>{
  let items=[],loading=false,error='',timer,generation=0;
  const mounts=new Set(),lists=new Set(),acknowledged=new Set();
  const icon=item=>item.kind==='signoff'
    ? '<span class="attention-signoff" aria-hidden="true"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 5H5v16h14V5h-4M9 3h6v4H9zM8 14l3 3 5-6"/></svg></span>'
    : typeof identityAvatar==='function' ? identityAvatar('agent',item.agent_name,{agent_id:item.agent_id,...item.presentation}) : '<span aria-hidden="true">✉</span>';
  const label=item=>item.kind==='signoff'?'Review · '+item.title:item.agent_name+' · '+item.text;
  const button=item=>`<button type="button" class="attention-chip" data-attention-key="${esc(item.id)}" title="${esc(label(item))}" aria-label="${esc(label(item))}">${icon(item)}<span class="attention-label">${esc(item.kind==='signoff'?item.title:item.text)}</span></button>`;
  function paint(host){
    if(!host.isConnected){mounts.delete(host);return;}
    const visible=items.slice(0,3);
    host.innerHTML=`<div class="attention-stack">${visible.map(button).join('')}${items.length>3?`<button type="button" class="attention-more" data-attention-more aria-label="Show all ${items.length} attention items">+${items.length-3}</button>`:''}${!items.length?`<span class="attention-empty">${error?'Attention unavailable':loading?'Loading attention…':'Nothing new'}</span>`:''}</div>${error?'<button type="button" class="quiet attention-retry" data-attention-retry>Retry</button>':''}`;
  }
  const paintAll=()=>{mounts.forEach(paint);lists.forEach(host=>{if(!host.isConnected){lists.delete(host);return;}host.innerHTML=items.length?items.map(button).join(''):'<p class="muted">Nothing new</p>';});};
  async function refresh(){
    if(loading)return;
    loading=true;const request=++generation;
    try{const data=coreData(await api('/root/attention-queue'));if(request===generation){items=data.items || [];error='';}}
    catch(e){error=e.message;}
    finally{loading=false;paintAll();clearTimeout(timer);if(mounts.size)timer=setTimeout(refresh,10000);}
  }
  async function acknowledge(ids){
    const request=++generation;
    try{
      const data=coreData(await post('/root/attention-queue',{viewed:ids}));
      if(request===generation){items=data.items || [];paintAll();}
    }catch(e){ids.forEach(id=>acknowledged.delete(id));toast('Could not clear attention: '+e.message);}
  }
  function signoffViewed(s){
    if(document.hidden)return;
    const key=`signoff:${s.id}:${s.proposal_revision}:${s.payload_hash}`;
    if(acknowledged.has(key))return;
    acknowledged.add(key);void acknowledge([key]);
  }
  function viewboxViewed(item){
    const key=item.attention_id;
    if(document.hidden || !key || acknowledged.has(key))return;
    acknowledged.add(key);void acknowledge([key]);
  }
  async function open(item){
    if(item.kind==='viewbox'){await agviewboxNavigate(item);return;}
    if(item.kind!=='signoff'){
      // Native/private read cursors clear the queue only after the conversation actually loads.
      // Legacy Telegram/capture entries have no read cursor: show their exact text before clearing.
      if(item.kind==='global' && !item.chat){
        modal({title:esc(item.agent_name),body:`<p class="attention-message">${esc(item.body || item.text)}</p>`,
          actions:[{label:'Open chat',onClick:close=>{close();void replyNotifications.open(item);}}],onOpen:()=>acknowledge(item.viewed_ids)});
      }else await replyNotifications.open(item);
      return;
    }
    let view,host;
    modal({title:'Signoff',width:760,stack:true,body:'<div class="attention-signoff-view"></div>',onOpen:el=>{
      host=el.querySelector('.attention-signoff-view');
      view=sgCreateView({active:()=>host.isConnected,body:()=>host,workspace:()=>item.biz || 'scope:global',
        global:!item.business_id,category:item.category,item:()=>({id:item.signoff_id,businessId:item.business_id}),
        waitingOnly:false,execution:true});
      view.paint();
    },onClose:()=>{view?.dispose();void refresh();}});
  }
  function clicks(event){
    const target=event.target.closest('button');if(!target)return;
    if(target.hasAttribute('data-attention-retry')){void refresh();return;}
    if(target.hasAttribute('data-attention-more')){
      modal({title:'New attention',width:540,body:`<div class="attention-all">${items.map(button).join('')}</div>`,
        onOpen:el=>{const host=el.querySelector('.attention-all');lists.add(host);host.addEventListener('click',clicks);}});return;
    }
    const item=items.find(row=>row.id===target.dataset.attentionKey);
    if(item){
      if((item.kind==='viewbox' || item.kind==='global' && item.chat) && target.closest('.attention-all'))_modal?.close();
      void open(item).catch(e=>toast(e.message));
    }
  }
  document.addEventListener('reply-notifications-changed',()=>{if(mounts.size)void refresh();});
  window.addEventListener('focus',()=>{if(mounts.size)void refresh();});
  if(typeof onChange==='function')onChange(['signoffs'],null,()=>{if(mounts.size)void refresh();});
  return {signoffViewed,viewboxViewed,refresh,mount(host){mounts.add(host);host.addEventListener('click',clicks);paint(host);void refresh();
    return ()=>{mounts.delete(host);host.removeEventListener('click',clicks);if(!mounts.size)clearTimeout(timer);};}};
})();
