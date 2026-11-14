// A side chat is an ordinary private Signoff thread. Only its owner can rename or close it.
let sideChatDraft={text:'',title:''},sideChatSubmission=null;

// Side chats open in the Chat area on the left, beside Focus, when that Chat belongs to the Focus pane's agent.
// Elsewhere (an embedded pane) they keep the Focus surface.
async function sideChatSurface(spec){
  const pane=typeof _agpad==='undefined'?null:_agpad;
  const left=pane && typeof agentWorkspacePairedCurrent==='function' && agentWorkspacePairedCurrent(window)
    && typeof globalAgentViewActive!=='undefined' && typeof globalAgentViewActive?.openSide==='function'?globalAgentViewActive:null;
  if(!left)return window.focusExpansion.open(spec);
  if(pane.surface){await agpadSurfaceClose(pane);if(pane.surface)return null;}
  // A record open on the right owns the Chat area; closing it hands the area back first.
  if(pane.focusWorkspace?.isOpen?.()){await pane.focusWorkspace.close();if(pane.focusWorkspace?.isOpen?.())return null;}
  return left.openSide(spec);
}

function sideChatCreate(){
  return sideChatSurface({title:'New side chat',mount(host,session){
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
  return sideChatSurface({title:item.title || 'Side chat',mount(el,session){
    let closing=false;
    let title=item.title || 'Side chat',renamePending=null,renameSubmission=null;
    el.innerHTML='<p class="ferr side-chat-title-error" role="alert" hidden></p><div class="side-chat-thread"></div><div class="side-chat-footer"><p class="ferr" role="alert" hidden></p><button type="button" class="quiet" data-side-chat-close>Close side chat</button></div>';
    const host=el.querySelector('.side-chat-thread'),button=el.querySelector('[data-side-chat-close]'),error=el.querySelector('.side-chat-footer .ferr');
    const titleHost=session.titleHost || el.closest('.agfocus-surface').querySelector('.agfocus-surface-title'),titleError=el.querySelector('.side-chat-title-error');
    if(session.titleHost)session.addHeaderAction(button);
    const titleButton=document.createElement('button'),titleInput=document.createElement('input');
    titleButton.type='button';titleButton.className='side-chat-title-button';titleButton.title='Rename side chat';titleButton.textContent=title;
    titleInput.type='text';titleInput.className='inp';titleInput.maxLength=200;titleInput.hidden=true;
    titleInput.setAttribute('aria-label','Side chat name');titleInput.title='Enter to save, Escape to cancel';
    titleHost.classList.add('side-chat-title');titleHost.replaceChildren(titleButton,titleInput);
    titleButton.onclick=()=>{
      if(!session.isCurrent() || closing || renamePending)return;
      titleInput.value=title;titleButton.hidden=true;titleInput.hidden=false;titleError.hidden=true;
      titleInput.focus();titleInput.select();
    };
    titleInput.onkeydown=event=>{
      if(event.isComposing)return;
      if(event.key==='Enter'){event.preventDefault();void saveTitle();}
      else if(event.key==='Escape'){event.preventDefault();event.stopPropagation();if(!renamePending)finishTitleEdit(true);}
    };
    titleInput.onblur=()=>{void saveTitle();};
    const visible=()=>session.isCurrent() && host.isConnected && !document.hidden && !!host.getClientRects().length && !host.closest('[inert],[aria-hidden="true"],[hidden]');
    const view=conversationMount(host,{...source,biz},{privateSource:source,privateGlobal:true,inlineComposer:true,dismissOnOutside:false,
      placeholder:'Message Ordubashy…',ariaLabel:'Side chat message',sourceTitle:item.title || 'Side chat',attachments:true,
      isVisible:visible,onRead:()=>void attentionQueue.refresh(),onUnread:()=>void attentionQueue.refresh(),
      onSent:()=>void attentionQueue.refresh(),
      // A referenced side chat opens as that side chat; Tasks and other signoffs open as they do elsewhere.
      openReference:ref=>{const chat=ref.type==='signoff'?attentionQueue.sideChat(ref.id):null;return chat?sideChatOpen(chat):taskProgressOpenReference(ref.business_slug ?? biz,ref);}});
    const canLeave=()=>{
      if(closing || !view.canLeave()){
        error.textContent=closing?'Wait for the side chat to finish closing.':view.state.sending?'Wait for the message to finish sending.':'Save or discard the question edits before leaving.';
        error.hidden=false;return false;
      }
      return true;
    };
    session.setBeforeLeave(async()=>canLeave() && await saveTitle());
    view.expand();
    const refresh=()=>{if(visible())void view.refresh({acknowledge:true});};
    const timer=setInterval(refresh,10000);window.addEventListener('focus',refresh);
    document.addEventListener('visibilitychange',refresh);
    button.onclick=()=>void closeChat();
    function finishTitleEdit(focus=false){
      if(!session.isCurrent())return;
      titleInput.hidden=true;titleInput.value=title;titleButton.hidden=false;titleButton.textContent=title;titleError.hidden=true;
      session.setTitle(title);titleHost.replaceChildren(titleButton,titleInput);
      if(focus)titleButton.focus();
    }
    async function saveTitle(){
      if(renamePending)return renamePending;
      if(!session.isCurrent())return false;
      if(titleInput.hidden)return true;
      const nextTitle=titleInput.value.trim();
      if(!nextTitle){titleError.textContent='Enter a side chat name.';titleError.hidden=false;return false;}
      if(nextTitle===title){finishTitleEdit(document.activeElement===titleInput);return true;}
      if(!renameSubmission || renameSubmission.title!==nextTitle)renameSubmission={scope:biz?'business':'global',signoff_id:source.id,title:nextTitle,idempotency_key:newRequestId()};
      titleInput.readOnly=true;titleInput.setAttribute('aria-busy','true');titleError.hidden=true;
      renamePending=Promise.resolve().then(async()=>{
        try{
          if(source.business_id && !biz)throw new Error('The project is unavailable. Reload the queue and try again.');
          const data=coreData(await post(`${biz?'/biz/'+encodeURIComponent(biz):'/root'}/operations/side_chats_rename`,renameSubmission));
          if(typeof data.signoff?.title!=='string' || !data.signoff.title.trim())throw new Error('Could not confirm the new name. Retry saving.');
          title=data.signoff.title;item.title=title;renameSubmission=null;
          finishTitleEdit(document.activeElement===titleInput);void attentionQueue.refresh();
          return true;
        }catch(e){
          if(session.isCurrent()){titleError.textContent=e.message;titleError.hidden=false;}
          return false;
        }finally{titleInput.readOnly=false;titleInput.removeAttribute('aria-busy');renamePending=null;}
      });
      return renamePending;
    }
    async function closeChat(){
      if(!session.isCurrent() || !canLeave())return;
      if(!await saveTitle() || !session.isCurrent() || !canLeave())return;
      closing=true;button.disabled=true;error.hidden=true;
      try{
        if(source.business_id && !biz)throw new Error('The project is unavailable. Reload the queue and try again.');
        await post(`${biz?'/biz/'+encodeURIComponent(biz):'/root'}/operations/side_chats_close`,{scope:biz?'business':'global',signoff_id:source.id,idempotency_key:closeKey});
        closing=false;void attentionQueue.refresh();
        if(session.isCurrent())await session.close();
      }catch(e){if(session.isCurrent()){error.textContent=e.message;error.hidden=false;}}
      finally{closing=false;button.disabled=false;}
    }
    return ()=>{clearInterval(timer);window.removeEventListener('focus',refresh);document.removeEventListener('visibilitychange',refresh);titleHost.classList.remove('side-chat-title');titleHost.textContent=title;view.dispose();};
  }});
}
