// Shared presentation for an expandable review reference. No API calls or approval
// policy live here. The adapter supplies authorized records and revision-checked writes.
// Currently exercised by the isolated UI preview; no production adapter is installed.
let chatReviewSequence = 0;
function chatReviewReferenceMount(ref, {store, onOpen, pane = false}) {
  const id = ref.id, isSignoff = ref.type === 'signoff', el = document.createElement('section');
  el.className = 'chat-review-reference' + (pane ? ' chat-review-pane' : '');
  el.dataset.reviewId = id;
  const panelId = 'chat-review-' + (++chatReviewSequence);
  // Review content is visible in chat immediately, matching the existing inline Signoff editor.
  let expanded = true, editing = null, busy = false, error = '', disposed = false;
  const chevron = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg>';
  const fit = field => { field.style.height = 'auto'; field.style.height = field.scrollHeight + 'px'; };
  const editable = row => row.state === 'pending' && row.canEdit !== false;
  const value = row => editing?.fields || row.fields;
  function captureFocus() {
    const active = document.activeElement;
    return el.contains(active) && active.dataset.field ? {key: active.dataset.field, start: active.selectionStart, end: active.selectionEnd} : null;
  }
  function render() {
    if (disposed) return;
    const row = store.get(id), focus = captureFocus();
    if (!row) { el.textContent = 'Unavailable reference'; return; }
    const icon = ref.type === 'delta' ? aicon('brain', 'currentColor', 17)
      : row.kind === 'email' ? aicon('mail', 'currentColor', 17)
      : row.kind === 'message' ? RW_CHAT_ICON : T_VIEW_ICON.signoffs;
    const stateLabel = {pending:'Pending',approved:'Approved',accepted:'Accepted',rejected:'Rejected'}[row.state] || row.state;
    el.innerHTML = `<header class="crr-head"><button type="button" class="crr-title" data-open><span class="crr-icon">${icon}</span><span>${esc(row.title)}</span></button><span class="crr-status">${esc(stateLabel)}</span>${pane ? '' : `<button type="button" class="crr-expand" data-expand aria-label="${expanded ? 'Collapse' : 'Expand'} ${esc(row.title)}" aria-expanded="${expanded}" aria-controls="${panelId}">${chevron}</button>`}</header><div id="${panelId}" class="crr-body"${expanded ? '' : ' hidden'}></div>`;
    el.querySelector('[data-open]').onclick = () => onOpen?.(ref);
    el.querySelector('[data-expand]')?.addEventListener('click', () => {expanded = !expanded; render();});
    if (expanded) {
      const body = el.querySelector('.crr-body');
      body.innerHTML = `${row.context ? `<div class="crr-context">${esc(row.context)}</div>` : ''}<div class="crr-fields"></div><p class="crr-error" role="status"${error ? '' : ' hidden'}>${esc(error)}</p>${row.evidence ? `<details class="crr-evidence"><summary>Supporting evidence</summary><p>${esc(row.evidence)}</p></details>` : ''}<div class="crr-decisions"></div>${isSignoff ? '' : `<details class="crr-history"><summary>History · ${row.history.length}</summary><div></div></details>`}`;
      const fields = body.querySelector('.crr-fields');
      row.fieldLabels.forEach(({key,label}) => {
        const group = document.createElement('label'); group.className = 'crr-field';
        const caption = document.createElement('span'); caption.className = 'crr-label'; caption.textContent = label;
        const field = document.createElement('textarea'); field.className = 'crr-editable'; field.rows = 1; field.dataset.field = key; field.setAttribute('aria-label', label);
        field.value = value(row)[key]; field.readOnly = !editing || busy; field.disabled = !editable(row) && !editing;
        field.onfocus = () => {if (!editing && editable(row) && !busy) {editing = {revision:row.revision,fields:{...row.fields}};render();}};
        field.oninput = () => {if (editing) editing.fields[key] = field.value;fit(field);};
        group.append(caption,field);fields.append(group);
      });
      const decisions = body.querySelector('.crr-decisions');
      if (editing || row.state === 'pending') {
        const choices = editing ? [['save','Save'],['discard','Discard']]
          : ref.type === 'delta' ? [['accepted','Accept change'],['rejected','Reject']]
          : [['approved','Approve'],['rejected','Reject']];
        choices.forEach(([action,label])=>{
          const b=document.createElement('button');b.type='button';b.textContent=label;
          b.className='crr-decision';b.disabled=busy;
          b.onclick=()=>{
            if(action==='discard'){editing=null;error='';render();}
            else if(action==='save')run(async()=>{await store.save(id,editing.revision,{...editing.fields});editing=null;});
            else run(()=>store.decide(id,row.revision,action));
          };
          decisions.append(b);
        });
      }
      (isSignoff ? [] : row.history.slice().reverse()).forEach(item=>{const p=document.createElement('p');p.textContent=item;body.querySelector('.crr-history > div').append(p);});
      requestAnimationFrame(()=>{if(!disposed)el.querySelectorAll('textarea').forEach(fit);});
    }
    if(focus){const field=[...el.querySelectorAll('[data-field]')].find(n=>n.dataset.field===focus.key);field?.focus({preventScroll:true});field?.setSelectionRange(focus.start,focus.end);}
  }
  async function run(action){if(busy)return;busy=true;error='';render();try{await action();}catch(e){error=e.message || 'Could not save. Your draft is kept.';}finally{busy=false;render();}}
  const unsubscribe=store.subscribe(id,()=>render());
  render();
  return {el,update(){render();return true;},dispose(){disposed=true;unsubscribe();el.remove();}};
}
