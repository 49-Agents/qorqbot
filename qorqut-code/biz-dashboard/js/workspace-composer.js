// Workspace-scoped terminal input; viewing the terminal is explicitly opt-in.
const workspaceComposer = {generation:0, drafts:new Map(), selections:new Map(), scope:null, dispose:null};
function workspaceComposerCloseTerminal() {
  workspaceComposer.dispose?.();workspaceComposer.dispose=null;
  const root=document.querySelector('#workspace-composer');if(!root)return;
  root.querySelector('.workspace-terminal').replaceChildren();
  root.querySelector('.workspace-terminal').hidden=true;
  root.querySelector('[data-terminal-resize]').hidden=true;
  const toggle=root.querySelector('[data-terminal]');toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-label','Show terminal');
}
async function workspaceComposerRefresh() {
  const w=workspaceComposer,key=state.scope==='global'?'global':state.biz;
  if(state.scope==='global' && state.globalTab==='agents'){
    ++w.generation;w.scope=null;
    workspaceComposerCloseTerminal();
    document.querySelector('#workspace-composer')?.remove();
    return;
  }
  if(!key)return;
  if(w.scope===key && document.querySelector('#workspace-composer'))return;
  const generation=++w.generation;w.scope=key;
  workspaceComposerCloseTerminal();
  document.querySelector('#workspace-composer')?.remove();
  const root=document.createElement('section');root.id='workspace-composer';root.setAttribute('aria-label','Message an agent');
  root.innerHTML='<div data-terminal-resize role="separator" aria-label="Terminal height" aria-orientation="horizontal" tabindex="0" hidden></div><div class="workspace-terminal" hidden></div><form><input class="inp" aria-label="Message agent" placeholder="Message an agent…" maxlength="16000" autocomplete="off"><button type="submit">Send</button><button type="button" data-recipient aria-label="Choose agent" disabled>…</button><button type="button" data-terminal aria-label="Show terminal" aria-expanded="false" disabled><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></svg></button></form><p role="status" class="workspace-composer-status"></p>';
  document.body.appendChild(root);
  const input=root.querySelector('input'),send=root.querySelector('[type=submit]'),picker=root.querySelector('[data-recipient]'),toggle=root.querySelector('[data-terminal]'),status=root.querySelector('[role=status]');
  send.disabled=true;
  const resize=root.querySelector('[data-terminal-resize]'),terminal=root.querySelector('.workspace-terminal');
  const setHeight=value=>{
    w.terminalHeight=Math.round(Math.max(120,Math.min(value,window.innerHeight-140)));
    terminal.style.height=w.terminalHeight+'px';
    resize.setAttribute('aria-valuenow',String(w.terminalHeight));
    resize.setAttribute('aria-valuemin','120');resize.setAttribute('aria-valuemax',String(Math.max(120,window.innerHeight-140)));
  };
  let drag;
  resize.onpointerdown=event=>{
    if(event.button!==0)return;
    event.preventDefault();drag={y:event.clientY,height:terminal.getBoundingClientRect().height};
    resize.setPointerCapture(event.pointerId);
  };
  resize.onpointermove=event=>{if(drag)setHeight(drag.height+drag.y-event.clientY);};
  resize.onpointerup=resize.onpointercancel=resize.onlostpointercapture=()=>{drag=null;};
  resize.onkeydown=event=>{
    if(!['ArrowUp','ArrowDown'].includes(event.key))return;
    event.preventDefault();setHeight(terminal.getBoundingClientRect().height+(event.key==='ArrowUp'?20:-20));
  };
  const current=()=>w.generation===generation && root.isConnected;
  let agents,selected,draft;
  const icon=agent=>identityAvatar('agent',agent.display_name || agent.name,{...agent,...agent.presentation,role:agent.role || {id:agent.role_id}});
  const choose=id=>{
    workspaceComposerCloseTerminal();selected=agents.find(a=>a.agent_id===id);if(!selected)return;
    w.selections.set(key,id);
    const draftKey=key+':'+id;
    draft=w.drafts.get(draftKey) || {text:'',sending:false};w.drafts.set(draftKey,draft);
    picker.innerHTML=icon(selected);picker.setAttribute('aria-label','Choose agent: '+(selected.display_name || selected.name));
    input.value=draft.text;input.readOnly=draft.sending;send.disabled=draft.sending;picker.disabled=draft.sending;toggle.disabled=false;status.textContent='';
  };
  w.sync=()=>{
    if(!current() || !draft)return;
    input.value=draft.text;input.readOnly=draft.sending;send.disabled=draft.sending;picker.disabled=draft.sending;
  };
  input.oninput=()=>{if(draft)draft.text=input.value;};
  input.onkeydown=event=>{
    if(event.key!=='Enter')return;
    if(event.isComposing || event.keyCode===229 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)event.preventDefault();
  };
  picker.onclick=()=>dropdown(picker,agents.map(agent=>({value:agent.agent_id,label:agent.display_name || agent.name,html:`<span class="workspace-agent-option">${icon(agent)}<span>${esc(agent.display_name || agent.name)}</span></span>`})),choose,selected.agent_id,{noCheck:true});
  toggle.onclick=()=>{
    if(w.dispose){workspaceComposerCloseTerminal();return;}
    const host=root.querySelector('.workspace-terminal');host.hidden=false;resize.hidden=false;
    setHeight(w.terminalHeight || (window.innerWidth<=600?200:240));
    const base=selected.business_slug?`/biz/${encodeURIComponent(selected.business_slug)}/terminal/${encodeURIComponent(selected.name)}`:`/root/terminal/${encodeURIComponent(selected.agent_id)}`;
    w.dispose=mountTerminal(selected.name,host,null,base,{});
    toggle.setAttribute('aria-expanded','true');toggle.setAttribute('aria-label','Hide terminal');
  };
  root.querySelector('form').onsubmit=async event=>{
    event.preventDefault();if(!selected || !draft || draft.sending || !draft.text.trim())return;
    const target=selected,saved=draft,text=saved.text;
    if(/[\x00-\x1f\x7f]/.test(text)){status.textContent='Remove control characters before sending.';return;}
    // Preserve an explicit focus handoff made while delivery is in flight. If
    // the owner clicks the desktop iframe or another editor, completion must
    // not pull focus back to this floating composer.
    const focusBeforeSend=document.activeElement;
    saved.sending=true;send.disabled=true;picker.disabled=true;input.readOnly=true;status.textContent='Sending…';
    try {
      const base=target.business_slug?`/biz/${encodeURIComponent(target.business_slug)}/terminal/${encodeURIComponent(target.name)}`:`/root/terminal/${encodeURIComponent(target.agent_id)}`;
      const write=async(action,body)=>{
        const result=await post(base+'/'+action,body,{signal:AbortSignal.timeout(15000)});
        if(!result?.ok || result.error)throw new Error(result.error || 'Terminal delivery unconfirmed.');
      };
      // Attach the existing terminal on explicit Send, without opening a viewer.
      await write('start',{});
      await write('input',{data:text});
      await new Promise(resolve=>setTimeout(resolve,200));
      await write('input',{data:'\r'});
      saved.text='';if(current()){input.value='';status.textContent='Sent';}
    } catch(error){if(current())status.textContent='Delivery unconfirmed. Show terminal before retrying. '+(error.message || '');}
    finally{saved.sending=false;w.sync?.();if(current() && document.activeElement===focusBeforeSend)input.focus();}
  };
  try {
    const [global,local]=await Promise.all([
      api('/root/agents').then(response=>coreData(response).agents),
      key==='global'?Promise.resolve([]):api(`/biz/${encodeURIComponent(key)}/agents?summary=1`)
    ]);
    if(!Array.isArray(global) || !Array.isArray(local))throw new Error('Could not load agents.');
    if(!current())return;
    agents=key==='global'?global:[...global.filter(a=>(a.role_id || a.role?.id)==='ordubashy'),...local.map(agent=>({...agent,business_slug:key}))];
    if(!Array.isArray(agents) || !agents.length)throw new Error('No agents available.');
    picker.disabled=false;
    choose(w.selections.get(key) && agents.some(a=>a.agent_id===w.selections.get(key))?w.selections.get(key):(agents.find(a=>(a.role_id || a.role?.id)==='ordubashy') || agents[0]).agent_id);
  } catch(error){if(current()){status.textContent=error.message || 'Could not load agents.';w.scope=null;}}
}
