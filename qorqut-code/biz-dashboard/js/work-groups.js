// Work groups are ordinary Tasks: children use parent_id, every other related item uses a `concerns`
// relationship. This file holds the shared helpers the Task, Context, Signoff and upload views use to read and
// choose them.
const WORK_ITEM_TYPES = {tasks:'task',data_tables:'table',agent_profiles:'agent',action_logs:'action',signoffs:'signoff',
  email_conversations:'material',context_entities:'entity',documents:'material',notes:'material',uploads:'material'};
const workDate = value => sjDateTime(value);
const workStateLabel = task => TASK_SPECIAL_TYPES.includes(task.task_type)
  ? [TASK_TYPE_LABELS[task.task_type], customerStateText(task)].filter(Boolean).join(' · ') : T_LABEL[task.status] || task.status;
const workBall = task => taskTypeIcon(task.task_type) || `<span class="ball ${esc(task.status)}" aria-hidden="true"><span class="core"></span></span>`;

// The root route already supplies Global context. `scope` is sent only by a caller whose operation declares it
// (the Global relationship and item reads do); Task, Action and tag operations never receive an injected scope.
async function workCall(biz, operation, args = {}, options = {}) {
  const result = await post(biz ? `/biz/${encodeURIComponent(biz)}/operations/${operation}` : `/root/operations/${operation}`, {...args,...options,
    idempotency_key:options.idempotency_key || newRequestId()});
  if (result?.status === 'signoff_required') throw new Error('This change is awaiting signoff. Your draft is retained.');
  return coreData(result);
}

// The shared item a resource is known as in relationships and Context review.
const workItem = (type, id) => typeof type === 'object' ? type : {type: WORK_ITEM_TYPES[type] || type, id};

async function workRelatedChips(host, biz, type, id, beforeOpen) {
  if(!host)return;
  host.classList.add('work-associations');
  try {
    return await contextRelatedChips(host, biz, workItem(type, id), beforeOpen);
  }catch(error){
    if(!host.isConnected)return;
    host.textContent="Couldn't load. ";
    const retry=document.createElement('button');retry.className='quiet';retry.textContent='Retry';
    retry.onclick=()=>workRelatedChips(host,biz,type,id,beforeOpen);host.appendChild(retry);
  }
}

// Mail is handled in chat and the browser. A stored email reference stays readable as a label but is never fetched.
const HISTORICAL_EMAIL_TEXT = 'This is a historical email reference. Mail is no longer shown in the dashboard, so the conversation cannot be opened here.';
function historicalEmailMount(host) {
  host.replaceChildren();
  const note = document.createElement('p'); note.className = 'muted'; note.setAttribute('role', 'status'); note.textContent = HISTORICAL_EMAIL_TEXT;
  host.append(note);
}
function historicalEmailOpen(title) {
  modal({title: esc(title || 'Historical email reference'), stack: true, width: 520, body: '<div class="historical-email"></div>', onOpen: el => historicalEmailMount(el.querySelector('.historical-email'))});
}

async function workOpenLink(view, link) {
  if(link.resource_type==='data_tables')return tablesOpen(view.biz,link.resource_id);
  if(link.resource_type==='documents' && (link.current || link.snapshot_json)?.kind==='data_upload')return tablesOpen(view.biz,link.resource_id,'uploads');
  if(link.resource_type==='email_conversations' || link.resource_type==='email_messages')return historicalEmailOpen((link.current || link.snapshot_json)?.label);
  if(link.resource_type==='tasks'){if(view.biz)return tOpenTask(link.resource_id);return modal({title:'Global task',body:'<div class=global-work-task></div>',onOpen:el=>globalTasksMount(el.querySelector('.global-work-task'),{initialTaskId:link.resource_id})});}
  const current=link.current || link.snapshot_json;
  if(link.resource_type==='entity'){
    if(/^https?:\/\//.test(current.url || ''))window.open(current.url,'_blank','noopener,noreferrer');return;
  }
  if(link.resource_type==='agent_profiles')return agentProfileOpen(link.resource_id);
  if(link.resource_type==='documents' && current.kind==='business_owned_note') {
    if(!await ntFlush())return;
    openTasks(true);await tSetView('businessNotes');await ntFetch();await ntSelect(link.resource_id);return;
  }
  if(link.resource_type==='documents' && state.scope==='global' && ['business_note','agent_note','journal'].includes(current.kind)) {
    if(!await ntFlush())return;
    openTasks(true);await tSetView('notes');await ntFetch();await ntSelect(link.resource_id);return;
  }
  if(link.resource_type==='documents' && ['context_upload','context_dump'].includes(current.kind)) {if(state.scope==='global'){location.hash=`/global/context/library/${encodeURIComponent(link.resource_id)}`;return;}state.ctx=`_source/${link.resource_id}`;goTab('context');return;}
  if(link.resource_type==='documents' && current.path?.startsWith('context/')) {state.ctx=current.path.slice(8);goTab('context');return;}
  try {
    const mapping={documents:[current.kind==='asset'?'assets_get':'notes_get',current.kind==='asset'?'asset_id':'document_id'],signoffs:['signoffs_get','signoff_id'],action_logs:['actions_get','action_id']};
    const [op,key]=mapping[link.resource_type] || [];
    if(!op)return;
    let data;
    if(link.resource_type==='documents')data=await workCall(view.biz,'context_get',{document_id:link.resource_id}).catch(()=>workCall(view.biz,op,{[key]:link.resource_id}));
    else data=await workCall(view.biz,op,{[key]:link.resource_id});
    const item=data.document || data.action || data.signoff || data.asset;
    if(link.resource_type==='signoffs'){
      modal({title:esc(item.title),body:`<p>${esc(item.state)}</p><div class="work-source">${esc(item.preview?.rationale || 'The prepared proposal is available in Signoffs.')}</div>`,
        actions:item.state==='pending'?[{label:'Open Signoffs',primary:true,onClick:close=>{close();openTasks(true);tSetView('signoffs');}}]:[]});return;
    }
    if(current.kind==='asset'){
      const url=`/api/biz/${encodeURIComponent(view.biz)}/asset/${item.path.split('/').map(encodeURIComponent).join('/')}`;
      const media=item.metadata?.media_type || '';
      const preview=/^image\/(png|jpeg|webp|gif|avif)$/.test(media)?`<img class="work-asset-preview" src="${esc(url)}" alt="${esc(current.label)}">`:
        /^video\/(mp4|webm|ogg)$/.test(media)?`<video class="work-asset-preview" src="${esc(url)}" controls></video>`:'';
      modal({title:esc(current.label),body:`${preview}<p>${esc(item.metadata?.notes || '')}</p><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open file</a><div class="work-asset-associations"></div>`,
        onOpen:el=>workRelatedChips(el.querySelector('.work-asset-associations'),view.biz,'uploads',link.resource_id)});return;
    }
    const content=item.body || item.detail || item.summary || item.text || [item.payload?.subject || item.payload?.title,item.payload?.body,item.state].filter(Boolean).join('\n\n') || 'Open this item in its workspace for more details.';
    modal({title:esc(current.label),body:`<div class="work-source">${esc(content)}</div><div class="work-source-associations"></div>`,onOpen:el=>workRelatedChips(el.querySelector(".work-source-associations"),view.biz,link.resource_type,link.resource_id,()=>_modal?.close())});
  }catch(error){view.error(error);}
}

// An unsaved creation form keeps these choices locally. The creation operation writes the source and
// every selected `concerns` relationship together, or writes neither.
function workChooseTasks(biz,selected,onChange) {
  const chosen=new Map((selected || []).map(task=>[task.id,task]));
  modal({stack:true,title:'Choose tasks',body:'<input class="inp" aria-label="Find tasks" placeholder="Find tasks"><div class="work-association-list"></div><div class="ferr" role="alert" hidden></div>',
    actions:[{label:'Use selected tasks',primary:true,onClick:close=>{onChange([...chosen.values()]);close();}}],onOpen:el=>{
      let generation=0,cursor=null;
      const input=el.querySelector('input'),list=el.querySelector('.work-association-list'),error=el.querySelector('.ferr');
      const load=async(more=false)=>{
        const own=++generation;
        try{
          const data=await workCall(biz,'tasks_list',{search:input.value,include_closed:true,snooze_filter:'all',limit:50,...(more&&cursor?{cursor}:{})});
          if(!el.isConnected || (biz ? state.biz!==biz : state.scope!=="global") || own!==generation)return;
          if(!more) list.replaceChildren();
          list.querySelector('.work-association-more')?.remove();
          for(const task of data.tasks){
            const row=document.createElement('label');row.className='work-association-choice';
            row.innerHTML=`<input type="checkbox" data-task-id="${esc(task.id)}"${chosen.has(task.id)?' checked':''}>${workBall(task)}<span>${esc(task.title)}<small>${esc(workStateLabel(task))}</small></span>`;
            row.querySelector('input').onchange=e=>{if(e.target.checked)chosen.set(task.id,task);else chosen.delete(task.id);};list.appendChild(row);
          }
          cursor=data.next_cursor;
          if(cursor){const button=document.createElement('button');button.className='quiet work-association-more';button.textContent='More tasks';button.onclick=()=>load(true);list.appendChild(button);}
          if(!data.tasks.length&&!more)list.innerHTML='<p class="muted">No matching tasks.</p>';
        }catch(e){error.hidden=false;error.textContent=uiFailure(e,{load:true});}
      };
      input.oninput=()=>load();load();
    }});
}

// Task/Story/Customer/Email links are scoped references, independent of plain labels and parents.
async function taskLinkOpen(link) {
  if(link.business_id){
    const slug=link.business_slug || state.bizs.find(b=>(b.id || b.business_id)===link.business_id)?.slug;
    if(!slug)throw new Error('This task is unavailable.');
    await selectBiz(slug);
    if(state.biz===slug && state.scope!=='global')await tOpenTask(link.task_id);
  } else {
    modal({stack:true,title:'Task',body:'<div class="linked-global-task"></div>',onOpen:el=>globalTasksMount(el.querySelector('.linked-global-task'),{initialTaskId:link.task_id})});
  }
}

async function taskLinksMount(host,biz,task,{open=taskLinkOpen}={}) {
  if(!host)return;
  const token={};host._taskLinks=token;
  host.classList.add('task-links');
  let current=task,items=[],cursor=null;
  const present=()=>{
    if(host._taskLinks!==token || !host.isConnected)return;
    host.replaceChildren();
    for(const link of items){
      const wrap=document.createElement('span');wrap.className='task-link-pair';
      const chip=document.createElement('button');chip.type='button';chip.className='chip task-link-chip';
      chip.innerHTML=taskTypeIcon(link.task_type)+esc(link.title)+customerStateChip(link);
      chip.title=link.title+' · '+(link.business_name || 'Global');
      chip.onclick=()=>Promise.resolve(open(link)).catch(e=>toast(uiFailure(e,{load:true})));
      const remove=document.createElement('button');remove.type='button';remove.className='quiet task-link-remove';remove.textContent='×';remove.setAttribute('aria-label','Unlink '+link.title);
      remove.onclick=async()=>{
        remove.disabled=true;
        try{
          const origin=link.link_source;
          const originBiz=origin.business_id ? state.bizs.find(b=>(b.id || b.business_id)===origin.business_id)?.slug : null;
          if(origin.business_id && !originBiz)throw new Error('This task is unavailable.');
          const incoming=origin.task_id!==current.id || origin.business_id!==current.business_id;
          const target=incoming?{task_id:current.id,business_id:current.business_id}:link;
          await workCall(originBiz,'tasks_update',{task_id:origin.task_id,changes:{related_task:{task_id:target.task_id,business_id:target.business_id,linked:false}}},{expected_revision:origin.revision});
          await load();
        }catch(error){toast(uiFailure(error));remove.disabled=false;}
      };
      wrap.append(chip,remove);host.append(wrap);
    }
    const add=document.createElement('button');add.type='button';add.className='quiet';add.textContent='Link task';
    add.onclick=()=>taskLinkChoose(current,async target=>{
      const result=await workCall(biz,'tasks_update',{task_id:current.id,changes:{related_task:{...target,linked:true}}},{expected_revision:current.revision});
      current=result.task;await load();
    });host.append(add);
    if(cursor){const more=document.createElement('button');more.type='button';more.className='quiet';more.textContent='More links';more.onclick=()=>load(true).catch(e=>toast(uiFailure(e,{load:true})));host.append(more);}
  };
  const load=async(more=false)=>{
    const data=await workCall(biz,'tasks_get',{task_id:task.id,...(more&&cursor?{related_cursor:cursor}:{})});
    if(host._taskLinks!==token || !host.isConnected)return;
    current=data.task;items=more?[...items,...(current.related_tasks || [])]:(current.related_tasks || []);cursor=current.related_tasks_next_cursor;
    present();
  };
  // Reuse a detail response when available; mutation responses may omit links.
  if(Array.isArray(task.related_tasks)){items=task.related_tasks;cursor=task.related_tasks_next_cursor;present();}
  else try{await load();}catch(error){if(host.isConnected){host.textContent='';const retry=document.createElement('button');retry.className='quiet';retry.textContent='Retry links';retry.onclick=()=>taskLinksMount(host,biz,task,{open});host.append(retry);}}
}

function taskLinkChoose(source,onChoose) {
  modal({stack:true,title:'Link a Task, Story or Customer',body:`<select class="inp" aria-label="Task workspace"><option value="">Global</option>${(state.bizs || []).map(b=>`<option value="${esc(b.slug)}" data-id="${esc(b.id || b.business_id)}">${esc(b.name || b.display_name || b.slug)}</option>`).join('')}</select><input class="inp" aria-label="Find linked task" placeholder="Find a Task, Story or Customer"><div class="task-link-options"></div><p class="ferr" role="alert" hidden></p>`,onOpen:el=>{
    const select=el.querySelector('select'),search=el.querySelector('input'),list=el.querySelector('.task-link-options'),error=el.querySelector('.ferr');
    select.value=state.bizs.find(b=>(b.id || b.business_id)===source.business_id)?.slug || '';
    let generation=0,cursor=null,busy=false;
    const load=async(more=false)=>{
      const own=++generation,biz=select.value || null;
      error.hidden=true;
      try{
        const data=await workCall(biz,'tasks_list',{search:search.value,include_archived:true,include_closed:true,snooze_filter:'all',limit:50,...(more&&cursor?{cursor}:{})});
        if(!el.isConnected || own!==generation)return;
        if(!more)list.replaceChildren();
        list.querySelector('[data-more]')?.remove();
        for(const task of data.tasks){
          if(task.id===source.id && task.business_id===source.business_id)continue;
          const button=document.createElement('button');button.type='button';button.className='task-link-option';
          button.innerHTML=taskTypeIcon(task.task_type)+esc(task.title)+customerStateChip(task);
          button.onclick=async()=>{if(busy)return;busy=true;button.disabled=true;try{await onChoose({task_id:task.id,business_id:task.business_id});el.querySelector('.mx')?.click();}catch(e){error.textContent=uiFailure(e);error.hidden=false;}finally{busy=false;button.disabled=false;}};
          list.append(button);
        }
        cursor=data.next_cursor;
        if(cursor){const more=document.createElement('button');more.type='button';more.className='quiet';more.dataset.more='';more.textContent='More';more.onclick=()=>load(true);list.append(more);}
        if(!list.children.length)list.textContent='No matching tasks.';
      }catch(e){if(own===generation){error.textContent=uiFailure(e,{load:true});error.hidden=false;}}
    };
    select.onchange=()=>load();search.oninput=()=>load();load();search.focus();
  }});
}
