// A side chat is an ordinary private Signoff thread. Only its owner can close it.
let sideChatDraft={text:'',title:''},sideChatSubmission=null;

function sideChatCreate(){
  return window.focusExpansion.open({title:'New side chat',mount(host,session){
    let busy=false;
    host.innerHTML='<form class="side-chat-create"><input class="inp" name="title" maxlength="200" placeholder="Title (optional)" aria-label="Title (optional)"><textarea class="inp" name="text" rows="4" maxlength="20000" placeholder="Ask Ordubashy…" aria-label="First message" required></textarea><p class="ferr" role="alert" hidden></p><button class="primary" type="submit">Send</button></form>';
    const box=host.querySelector('form'),error=box.querySelector('.ferr');
    box.elements.text.value=sideChatDraft.text;box.elements.title.value=sideChatDraft.title;
    box.oninput=()=>{sideChatDraft={text:box.elements.text.value,title:box.elements.title.value};};
    box.onsubmit=event=>{event.preventDefault();void submit();};
    // Drafts survive leaving; an in-flight create must settle before switching.
    session.setBeforeLeave(()=>{
      if(busy){error.textContent='Wait for the message to finish sending.';error.hidden=false;return false;}
      return true;
    });
    function paint(){
      if(!session.isCurrent())return;
      for(const input of [box.elements.text,box.elements.title])input.disabled=busy || !!sideChatSubmission;
      const button=box.querySelector('button');button.disabled=busy;button.textContent=busy?'Sending…':sideChatSubmission?'Retry send':'Send';
    }
    async function submit(){
      if(!session.isCurrent() || busy || !sideChatDraft.text.trim())return;
      if(!sideChatSubmission)sideChatSubmission={...sideChatDraft,scope:'global',idempotency_key:newRequestId()};
      busy=true;paint();error.hidden=true;
      try{
        const data=coreData(await post('/root/operations/side_chats_create',sideChatSubmission));
        const s=data.signoff;
        if(!s?.id)throw Object.assign(Error('Could not confirm the new side chat. Retry send.'),{code:'invalid_response'});
        sideChatDraft={text:'',title:''};sideChatSubmission=null;
        busy=false;void attentionQueue.refresh();
        if(session.isCurrent())await sideChatOpen({id:s.id,title:s.title,source:{type:'signoff',id:s.id,business_id:s.review_business_id}});
      }catch(e){
        if(e.code && e.code!=='invalid_response' && !(e.httpStatus>=500))sideChatSubmission=null;
        if(session.isCurrent()){error.textContent=e.message;error.hidden=false;}
      }finally{busy=false;paint();}
    }
    paint();box.elements.text.focus();
  }});
}

function sideChatOpen(item){
  const source=item.source || {type:'signoff',id:item.id,business_id:item.review_business_id ?? null};
  const biz=item.business_slug || (state.bizs || []).find(b=>b.id===source.business_id)?.slug || null;
  const closeKey=newRequestId();
  return window.focusExpansion.open({title:item.title || 'Side chat',mount(el,session){
    let closing=false;
    el.innerHTML='<div class="side-chat-thread"></div><div class="side-chat-footer"><p class="ferr" role="alert" hidden></p><button type="button" class="quiet" data-side-chat-close>Close side chat</button></div>';
    const host=el.querySelector('.side-chat-thread'),button=el.querySelector('[data-side-chat-close]'),error=el.querySelector('.ferr');
    const visible=()=>session.isCurrent() && host.isConnected && !document.hidden && !!host.getClientRects().length && !host.closest('[inert],[aria-hidden="true"],[hidden]');
    const view=conversationMount(host,{...source,biz},{privateSource:source,privateGlobal:true,inlineComposer:true,dismissOnOutside:false,
      placeholder:'Message Ordubashy…',ariaLabel:'Side chat message',sourceTitle:item.title || 'Side chat',
      isVisible:visible,onRead:()=>void attentionQueue.refresh(),onUnread:()=>void attentionQueue.refresh(),
      onSent:()=>void attentionQueue.refresh()});
    const canLeave=()=>{
      if(closing || !view.canLeave()){
        error.textContent=closing?'Wait for the side chat to finish closing.':view.state.sending?'Wait for the message to finish sending.':'Save or discard the question edits before leaving.';
        error.hidden=false;return false;
      }
      return true;
    };
    session.setBeforeLeave(canLeave);
    view.expand();
    const refresh=()=>{if(visible())void view.refresh({acknowledge:true});};
    const timer=setInterval(refresh,10000);window.addEventListener('focus',refresh);
    document.addEventListener('visibilitychange',refresh);
    button.onclick=()=>void closeChat();
    async function closeChat(){
      if(!session.isCurrent() || !canLeave())return;
      closing=true;button.disabled=true;error.hidden=true;
      try{
        if(source.business_id && !biz)throw new Error('The project is unavailable. Reload the queue and try again.');
        await post(`${biz?'/biz/'+encodeURIComponent(biz):'/root'}/operations/side_chats_close`,{scope:biz?'business':'global',signoff_id:source.id,idempotency_key:closeKey});
        closing=false;void attentionQueue.refresh();
        if(session.isCurrent())await session.close();
      }catch(e){if(session.isCurrent()){error.textContent=e.message;error.hidden=false;}}
      finally{closing=false;button.disabled=false;}
    }
    return ()=>{clearInterval(timer);window.removeEventListener('focus',refresh);document.removeEventListener('visibilitychange',refresh);view.dispose();};
  }});
}
