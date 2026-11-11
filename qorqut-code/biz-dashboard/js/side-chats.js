// A side chat is an ordinary private Signoff thread. Only its owner can close it.
let sideChatDraft={text:'',title:''},sideChatSubmission=null;

function sideChatCreate(){
  let busy=false,box;
  const dialog=modal({title:'New side chat',width:560,stack:true,
    body:'<form class="side-chat-create"><input class="inp" name="title" maxlength="200" placeholder="Title (optional)" aria-label="Title (optional)"><textarea class="inp" name="text" rows="4" maxlength="20000" placeholder="Ask Ordubashy…" aria-label="First message" required></textarea><p class="ferr" role="alert" hidden></p><button class="primary" type="submit">Send</button></form>',
    isDirty:()=>!!sideChatDraft.text.trim(),
    onOpen:el=>{
      box=el.querySelector('form');
      box.elements.text.value=sideChatDraft.text;box.elements.title.value=sideChatDraft.title;
      box.oninput=()=>{sideChatDraft={text:box.elements.text.value,title:box.elements.title.value};};
      box.onsubmit=event=>{event.preventDefault();void submit();};
      paint();box.elements.text.focus();
    }});
  function paint(){
    if(!box?.isConnected)return;
    for(const input of [box.elements.text,box.elements.title])input.disabled=busy || !!sideChatSubmission;
    const button=box.querySelector('button');button.disabled=busy;button.textContent=busy?'Sending…':sideChatSubmission?'Retry send':'Send';
  }
  async function submit(){
    if(busy || !sideChatDraft.text.trim())return;
    if(!sideChatSubmission)sideChatSubmission={...sideChatDraft,scope:'global',idempotency_key:newRequestId()};
    busy=true;paint();box.querySelector('.ferr').hidden=true;
    try{
      const data=coreData(await post('/root/operations/side_chats_create',sideChatSubmission));
      sideChatDraft={text:'',title:''};sideChatSubmission=null;
      dialog.close();void attentionQueue.refresh();
      const s=data.signoff;
      sideChatOpen({id:s.id,title:s.title,source:{type:'signoff',id:s.id,business_id:s.review_business_id}});
    }catch(e){
      if(e.code && e.code!=='invalid_response' && !(e.httpStatus>=500))sideChatSubmission=null;
      if(box.isConnected){const error=box.querySelector('.ferr');error.textContent=e.message;error.hidden=false;}
    }finally{busy=false;paint();}
  }
}

function sideChatOpen(item){
  let view,host,timer,closing=false;
  const source=item.source || {type:'signoff',id:item.id,business_id:item.review_business_id ?? null};
  const biz=item.business_slug || (state.bizs || []).find(b=>b.id===source.business_id)?.slug || null;
  const closeKey=newRequestId();
  const visible=()=>host?.isConnected && !document.hidden && !!host.getClientRects().length && !host.closest('[inert],[aria-hidden="true"],[hidden]');
  const refresh=()=>{if(visible())void view?.refresh({acknowledge:true});};
  const dialog=modal({title:esc(item.title || 'Side chat'),width:680,stack:true,
    body:'<div class="side-chat-thread"></div><div class="side-chat-footer"><p class="ferr" role="alert" hidden></p><button type="button" class="quiet" data-side-chat-close>Close side chat</button></div>',
    isDirty:()=>view?.hasDraft() || false,
    onOpen:el=>{
      el.classList.add('side-chat-modal');
      el.querySelector('.mx').setAttribute('aria-label','Hide side chat');
      host=el.querySelector('.side-chat-thread');
      view=conversationMount(host,{...source,biz},{privateSource:source,privateGlobal:true,inlineComposer:true,dismissOnOutside:false,
        placeholder:'Message Ordubashy…',ariaLabel:'Side chat message',sourceTitle:item.title || 'Side chat',
        isVisible:visible,onRead:()=>void attentionQueue.refresh(),onUnread:()=>void attentionQueue.refresh(),
        onSent:()=>void attentionQueue.refresh()});
      view.expand();
      el.querySelector('[data-side-chat-close]').onclick=()=>void closeChat();
      timer=setInterval(refresh,10000);window.addEventListener('focus',refresh);
      document.addEventListener('visibilitychange',refresh);
    },onClose:()=>{clearInterval(timer);window.removeEventListener('focus',refresh);document.removeEventListener('visibilitychange',refresh);view?.dispose();}});
  async function closeChat(){
    if(closing)return;
    closing=true;
    const button=dialog.el.querySelector('[data-side-chat-close]'),error=dialog.el.querySelector('.side-chat-footer .ferr');
    button.disabled=true;error.hidden=true;
    try{
      if(source.business_id && !biz)throw new Error('The project is unavailable. Reload the queue and try again.');
      await post(`${biz?'/biz/'+encodeURIComponent(biz):'/root'}/operations/side_chats_close`,{scope:biz?'business':'global',signoff_id:source.id,idempotency_key:closeKey});
      dialog.close();void attentionQueue.refresh();
    }catch(e){error.textContent=e.message;error.hidden=false;}
    finally{closing=false;button.disabled=false;}
  }
}
