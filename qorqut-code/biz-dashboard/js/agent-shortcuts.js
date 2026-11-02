// Navigation chords never intercept an editor, composer, terminal or open dialog.
(() => {
  let tabHeld=false, chordAllowed=false, navigating=false;
  const typing=target=>target instanceof Element && !!target.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"]),.xterm,.termmode,[role="dialog"]');
  const reset=()=>{tabHeld=false;chordAllowed=false;};
  async function jump(role) {
    if(navigating)return;
    navigating=true;
    try {
      const agents=await loadGlobalAgentProfiles();
      const agent=agents.find(agent=>agent.role?.id===role && globalRosterAgent(agent));
      if(!agent){toast('This agent is not available.');return;}
      ls('ag:scope:global',agent.agent_id);
      await selectGlobal('agents');
    } catch(error){toast(uiFailure(error, {load: true}));}
    finally {navigating=false;}
  }
  document.addEventListener('keydown',event=>{
    if(event.isComposing || event.ctrlKey || event.metaKey || event.altKey)return;
    if(event.key==='Tab') {
      if(event.repeat)return;
      const blocked=typing(event.target) || !!document.querySelector('.overlay');
      if(event.shiftKey && !blocked && state.scope==='global' && state.globalTab==='agents') {
        // Global agents, then the owner's workspace Terminal, in roster order.
        const rows=[...document.querySelectorAll('.global-agents [data-global-agent]')]
          .filter(node=>!node.disabled && node.getClientRects().length);
        if(rows.length){event.preventDefault();event.stopImmediatePropagation();const index=rows.findIndex(row=>row.classList.contains('on'));const next=rows[(index+1)%rows.length];next.click();next.focus({preventScroll:true});reset();return;}
      }
      tabHeld=true;chordAllowed=!blocked && !event.shiftKey;return;
    }
    if(tabHeld && chordAllowed && !event.shiftKey && event.key.toLowerCase()==='o') {
      event.preventDefault();event.stopImmediatePropagation();reset();void jump('ordubashy');
    }
  },true);
  document.addEventListener('keyup',event=>{if(event.key==='Tab')reset();},true);
  window.addEventListener('blur',reset);
})();
