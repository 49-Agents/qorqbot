// Shared Worker presentation and editor. Task writes use the existing scoped Tasks operation.
function taskWorkerMount(host, scope, task, agentId, {editable=true}={}) {
  let button = host.querySelector('[data-task-workers]');
  const tag=editable?'BUTTON':'SPAN';
  if(button && button.tagName!==tag){button.remove();button=null;}
  if (!button) { button = document.createElement(tag.toLowerCase()); if(editable)button.type = 'button'; button.className = 'quiet task-workers'; button.dataset.taskWorkers = ''; host.prepend(button); }
  const current = taskStateObserve(scope, task);
  const workers = [...(current.workers || [])].sort((a,b) => (a.is_owner ? 2 : a.id === agentId ? 1 : 0) - (b.is_owner ? 2 : b.id === agentId ? 1 : 0));
  button.innerHTML = (workers.map(w => identityAvatar(w.kind === 'human' ? 'person' : 'agent', w.display_name, globalAgentProfile(w.id) || w, w.display_name, true)).join('') || (editable?'<span class="muted">+</span>':'')) + (current.user_needed ? '<span class="task-user-needed">Owner assigned</span>' : '');
  button.setAttribute('aria-label', 'Workers' + (workers.length ? ': ' + workers.map(w=>w.display_name).join(', ') : ': none') + (current.user_needed ? ' · Owner assigned' : ''));
  button.title = current.user_needed ? 'Workers · Owner assigned' : 'Workers';
  button.hidden=!editable && !workers.length;
  if(!editable){button.setAttribute('role','img');return;}
  button.onpointerdown = event => event.stopPropagation();
  button.onclick = async event => {
    event.stopPropagation();
    button.disabled = true;
    try {
      const {workers: candidates} = await workCall(scope, 'workers_list');
      if (!button.isConnected) return;
      const snapshot = taskStateSnapshot(scope, task.id);
      if (snapshot.pending) throw Error('Wait for the task state to finish saving.');
      const latest = snapshot.task || task;
      const available = (candidates || []).filter(w => w.kind === 'human' || !/^(agent:)?overseer$/i.test(w.id || '') && w.role_id !== 'overseer');
      const pickerRevision = latest.revision;
      if (!pickerRevision) throw Error('The task revision is unavailable.');
      const selected = new Set(latest.worker_ids || []);
      const popup = tControlPopover(button, `<div class="task-worker-picker"><strong>Workers</strong>${available.map((w,i)=>`<label><input type="checkbox" data-worker-index="${i}" ${selected.has(w.id)?'checked':''}> ${identityAvatar(w.kind === 'human' ? 'person' : 'agent', w.display_name, globalAgentProfile(w.id) || w, w.display_name)} ${esc(w.display_name)}</label>`).join('')}<p role="alert" hidden></p><button type="button" class="quiet" data-worker-save>Save</button></div>`, 'Edit workers');
      if (!popup) return;
      popup.el.querySelector('[data-worker-save]').onclick = async () => {
        const save = popup.el.querySelector('[data-worker-save]'); save.disabled = true;
        try {
          const now = taskStateSnapshot(scope, task.id);
          if (now.pending || now.task?.revision !== pickerRevision) throw Error('Task changed while this picker was open. Close it and reopen to review the current workers.');
          const ids = [...popup.el.querySelectorAll('[data-worker-index]:checked')].map(input => available[Number(input.dataset.workerIndex)].id);
          const response = await post(_taskStatePath(scope) + '/tasks_update', {task_id:task.id, changes:{worker_ids:ids}, expected_revision:pickerRevision, idempotency_key:newRequestId()});
          if (response?.status === 'accepted' || response?.status === 'signoff_required') throw Error('Submitted for confirmation; workers have not changed.');
          const result = coreData(response);
          if (!result.task) throw Error('The server did not return the updated task.');
          Object.assign(task, taskStateObserve(scope, result.task));
          popup.close(); taskWorkerMount(host, scope, task, agentId);
        } catch (error) { const alert = popup.el.querySelector('[role=alert]'); alert.hidden = false; alert.textContent = error.message; save.disabled = false; }
      };
    } catch (error) { toast(error.message); } finally { button.disabled = false; }
  };
}

const ATTENTION_GROUPS = ['working','needs_input','open','blocked','waiting','done','cancelled'];
function taskBoardStateMenu(button, scope, task) {
  dropdown(button, T_STATES.filter(s => s !== 'needs_input').map(s => ({value:s,label:T_LABEL[s],html:`<span class="towner">${tDotHtml(s)}<span class="tn">${T_LABEL[s]}</span></span>`})), value => {if(value !== (task.work_status ?? task.status)) taskStateSet(scope,task,value);}, task.work_status ?? task.status);
}
function attentionBoardScope(ref, task) {
  if (ref.business_id == null) return null;
  return task.business_slug || attentionBoardRefScope(ref);
}
// The project scope of a reference whose Task record is not loaded yet. A business reference never falls back to Global.
function attentionBoardRefScope(ref) {
  if (ref?.business_id == null) return null;
  return ref.business_slug || (state.bizs || []).find(b => (b.business_id || b.id) === ref.business_id)?.slug || null;
}
function attentionBoardTask(ref, task) {
  const scope=attentionBoardScope(ref,task);
  return ref.business_id != null && !scope ? task : taskStateObserve(scope,task);
}
function attentionBoardMount(p) {
  const body = $('#agpad-body', p.el);
  body.innerHTML = '<section class="agfocus" data-attention-board><header><div class="agfocus-heading"><div class="global-eyebrow">Todo</div></div></header><div class="agfocus-layout"><div class="attention-content"><section data-attention-agents aria-label="Agents with todos"></section><div data-attention-focus></div><p class="agfocus-error" data-attention-error role="alert" hidden></p><div data-attention-list></div></div><aside class="agfocus-dock" data-attention-record hidden><div class="agfocus-dock-content"></div></aside></div></section>';
  const board = body.firstElementChild, dock = board.querySelector('[data-attention-record]');
  const mountWorkspace = () => recordWorkspaceMount(dock.firstElementChild, {entry:'focus', paired:false, visible:()=>agpadActive(p) && p.view === 'attentionBoard', onClose:()=>{
    dock.hidden=true;
    board.querySelector('.agfocus-layout').classList.remove('agfocus-thread-open');
    agfocusOverlayClose(p);
    board.querySelector(`[data-attention-member="${p.attentionBoard?.selected || ''}"]`)?.focus({preventScroll:true});
  }});
  const cache = attentionBoardCache(p);
  p.attentionBoard = {board, workspace:mountWorkspace(), mountWorkspace, data:cache.data};
  const unsubscribe=taskStateSubscribe((scope,id,snapshot)=>{
    if (!snapshot.pending && p.attentionBoard === boardState && boardState.data?.items.some(item => item.task_ref.id === id && attentionBoardScope(item.task_ref,item.task) === scope)) {
      clearTimeout(boardState.repaint); boardState.repaint=setTimeout(()=>{if(p.attentionBoard === boardState) attentionBoardPaint(p);},0);
    }
  });
  const boardState=p.attentionBoard;
  const repliesChanged=()=>board.querySelectorAll('[data-attention-reference]').forEach(row=>{
    agfocusUnread(p,row,row._attentionReference);
  });
  document.addEventListener('reply-notifications-changed',repliesChanged);
  p.workspaceDispose = () => {
    const layout=board.querySelector('.agfocus-layout');
    if (layout) attentionBoardCache(p).scroll=layout.scrollTop;
    document.removeEventListener('reply-notifications-changed',repliesChanged);
    unsubscribe(); agfocusOverlayClose(p); clearTimeout(boardState.repaint); boardState.workspace.dispose(); p.attentionBoard = null;
  };
  // Paint the cached snapshot synchronously and restore the scroll position, then refresh only if it is stale.
  attentionBoardPaint(p);
  board.querySelector('.agfocus-layout').scrollTop=cache.scroll;
  if (cache.run || cache.partial || Date.now() - cache.at >= ATTENTION_POLL_MS) void attentionBoardRefresh(p);
  void attentionAgentsRefresh(p);
}
const ATTENTION_POLL_MS = 12000;
// Todo can select another agent without changing the pane's chat identity. Each board keeps its own snapshot.
function attentionBoardCacheFor(p, agentId) {
  p.attentionCaches ||= new Map();
  if (!p.attentionCaches.has(agentId)) p.attentionCaches.set(agentId, {agentId, data:null, at:0, retryAt:0, partial:false, error:null, run:null, scroll:0, version:0});
  return p.attentionCaches.get(agentId);
}
function attentionBoardCache(p) {
  return p.attentionCache = attentionBoardCacheFor(p, p.attentionAgentId || p.agentId);
}
async function attentionBoardSelectAgent(p, agentId) {
  const board=p.attentionBoard, previous=attentionBoardCache(p);
  if (!board || board.selecting || previous.agentId === agentId) return;
  board.selecting=true;
  try {
    if (board.workspace.isOpen()) {
      await board.workspace.close();
      if (board.workspace.isOpen()) return;
    }
    if (p.attentionBoard !== board || !attentionBoardLive(p, previous)) return;
    const layout=board.board.querySelector('.agfocus-layout');
    previous.scroll=layout.scrollTop;
    p.attentionAgentId=agentId;
    const cache=attentionBoardCache(p);
    board.data=cache.data; board.selected=null; board.focusRead=null;
    attentionBoardPaint(p);
    layout.scrollTop=cache.scroll;
    if (cache.run || cache.partial || Date.now()-cache.at >= ATTENTION_POLL_MS) void attentionBoardRefresh(p);
  } finally { board.selecting=false; }
}
function attentionBoardLive(p, cache) {
  return p.attentionCache === cache && !!p.attentionBoard && p.view === 'attentionBoard' && agpadActive(p);
}
function attentionBoardShowError(p, error) {
  const el = p.attentionBoard?.board.querySelector('[data-attention-error]');
  if (el) { el.hidden=false; el.textContent='Could not load Todo. ' + error.message; }
}
function attentionBoardTick(p) {
  void attentionAgentsRefresh(p);
  const cache = attentionBoardCache(p);
  if (!p.attentionBoard || cache.run || Date.now() < cache.retryAt || !agpadActive(p)) return;
  if (!cache.partial && Date.now() - cache.at < ATTENTION_POLL_MS) return;
  void attentionBoardRefresh(p);
}
// Rapid switches share one request. The first load paints each page as it arrives; a refresh keeps the complete old list
// until the full replacement is ready. Leaving the view stops at the next page boundary and leaves the snapshot stale.
function attentionBoardRefresh(p) {
  const cache = attentionBoardCache(p);
  if (cache.run) return cache.run.promise;
  if (!attentionBoardLive(p, cache)) return Promise.resolve();
  const run = {};
  cache.run = run;
  const progressive = !cache.data;
  const show = () => { if (cache.run === run && attentionBoardLive(p, cache)) { attentionAgentsObserve(p, cache); p.attentionBoard.data = cache.data; attentionBoardPaint(p); } };
  run.promise = (async () => {
    try {
      let cursor = null, data, items = [], seen = new Set();
      do {
        const page = coreData(await post('/root/operations/todo_get', {scope:'global', agent_id:cache.agentId, projection:'summary', limit:100, ...(cursor ? {cursor} : {})}));
        if (cache.run !== run || p.attentionCache !== cache) return;
        if (page.agent_id !== cache.agentId || !Array.isArray(page.items)) throw Error('The selected agent’s Todo is unavailable.');
        if (!data) data = page;
        items.push(...page.items); cursor = page.next_cursor;
        if (cursor && seen.has(cursor)) throw Error('Todo pagination did not advance.');
        if (cursor) seen.add(cursor);
        if (cursor && progressive) { cache.data = {...data, items:[...items]}; cache.partial = true; show(); }
        if (cursor && !attentionBoardLive(p, cache)) { cache.at = 0; return; }
      } while (cursor);
      cache.data = {...data, items}; cache.partial = false; cache.error = null; cache.at = Date.now(); cache.retryAt = Date.now() + ATTENTION_POLL_MS;
      show();
    } catch (error) {
      if (cache.run !== run || p.attentionCache !== cache) return;
      cache.at = 0; cache.retryAt = Date.now() + ATTENTION_POLL_MS; cache.error = error;
      if (accessLost(error) || error.code === 'not_found') attentionAgentsUnavailable(p, cache.agentId, error);
      else attentionAgentsObserve(p, cache);
      if (attentionBoardLive(p, cache)) { attentionAgentsPaint(p); attentionBoardShowError(p, error); }
    } finally { if (cache.run === run) cache.run = null; }
  })();
  return run.promise;
}
// A removed membership leaves the snapshot at once; any older in-flight request is discarded and replaced.
function attentionBoardForget(p, membershipId, cache = attentionBoardCache(p)) {
  if (cache.data) cache.data = {...cache.data, items:cache.data.items.filter(item => item.membership_id !== membershipId)};
  cache.run = null; cache.at = 0; cache.retryAt = 0; cache.version++;
  attentionAgentsObserve(p, cache);
  if (attentionBoardLive(p, cache)) {
    p.attentionBoard.data = cache.data; attentionBoardPaint(p);
    return attentionBoardRefresh(p);
  }
  attentionAgentsPaint(p);
}
async function attentionBoardOpen(p, source) {
  const b=p.attentionBoard, record=rwRecordFromItem({source});
  if (!record) return false;
  if (record.scopeMissing) { toast('This project is unavailable.'); return false; }
  if(!b.workspace.alive?.())b.workspace=b.mountWorkspace();
  const dock=b.board.querySelector('[data-attention-record]');
  dock.hidden=false;
  b.board.querySelector('.agfocus-layout').classList.add('agfocus-thread-open');
  agfocusOverlayOpen(p,b.board,dock,()=>{void p.attentionBoard?.workspace.close();});
  try { return await b.workspace.open(record,{root:true,tab:record.can_thread?'chat':'dashboard'}); }
  catch(error) {toast(error.message);return false;}
}
// One marker for the focused row and every membership: the existing Focus state ball for an ordinary Task, the
// existing type marker for a special Task or another source, and a normal inert ball until a canonical Task is known.
function attentionStateMarker(row, source, task, scope, unavailable = false) {
  const marker=row.querySelector('.agfocus-marker');
  if (task && !taskShowsState(task)) { marker.innerHTML=tBall(task); return null; }
  if (!task && source.type !== 'task') {
    marker.innerHTML=`<span class="task-type-marker">${source.type === 'table' ? RW_TABLE_ICON : rwTypeIcon(rwRecordFromItem({source}) || {source})}</span>`;
    return null;
  }
  marker.innerHTML='<button type="button" class="ball agfocus-task-state"><span class="core"></span></button>';
  const ball=marker.firstElementChild;
  if (!task || unavailable) {
    ball.disabled=true;
    ball.setAttribute('aria-label',unavailable ? 'Task project unavailable' : 'Task state unavailable');
    return ball;
  }
  taskStateBind(ball,scope,task,()=>{});
  ball.onclick=event=>{event.stopPropagation();taskStateSet(scope,task,tNext(task.work_status ?? task.status));};
  ball.oncontextmenu=event=>{event.preventDefault();event.stopPropagation();taskBoardStateMenu(ball,scope,task);};
  return ball;
}
// Attention owns membership and intent; the row structure, markers and scope badges are shared.
function attentionReferenceRow(p, source, task = null) {
  const row=document.createElement('article'); row.className='agfocus-card agfocus-collapsed';
  row.innerHTML=agfocusRowHtml();
  const label=source.label || task?.title || 'Record';
  row.querySelector('.ttl').textContent=label;
  agfocusContext(row,{source,resolved_source:source});
  const scope=attentionBoardRefScope(source);
  attentionStateMarker(row,source,task,scope,source.business_id != null && !scope);
  const flag=row.querySelector('.agfocus-flag');
  const stateLabel=task ? customerStateText(task) : '';
  flag.textContent=stateLabel; flag.hidden=!stateLabel;
  row.tabIndex=0; row.setAttribute('role','group');
  row.dataset.attentionReference='';
  row._attentionReference={source,label};
  agfocusUnread(p,row,row._attentionReference);
  const leading=row.querySelector('.agfocus-notification');
  leading.removeAttribute('aria-hidden');
  // Show the Task's shared assignees; board membership does not assign a worker.
  if (task && !(source.business_id != null && !scope)) {
    taskWorkerMount(leading,scope,task,p.attentionAgentId || p.agentId,{editable:false});
    leading.append(leading.querySelector('[data-task-workers]'));
  }
  const open=()=>{p.attentionBoard.selected=row.dataset.attentionMember;void attentionBoardOpen(p,source);};
  row.onclick=event=>{if(!event.target.closest('button,a,input,textarea,select'))open();};
  row.onkeydown=event=>{if(event.target===row && ['Enter',' '].includes(event.key)){event.preventDefault();open();}};
  return row;
}
// The focused Task is not necessarily on the current membership page: read its exact scoped record once per
// focus identity. A business reference whose project is unavailable is never read as Global.
function attentionFocusTask(p, ref, key) {
  const board=p.attentionBoard;
  if (!board || ref?.type !== 'task') return null;
  const scope=attentionBoardRefScope(ref);
  if (ref.business_id != null && !scope) return null;
  if (board.focusRead?.key === key) return board.focusRead;
  const read={key,scope,task:null,error:null};
  board.focusRead=read;
  read.promise=workCall(scope,'tasks_get',{task_id:ref.id}).then(data=>{
    const task=data?.task;
    if (!task || String(task.id) !== String(ref.id)) throw new Error('The focused task is unavailable.');
    read.task=task; return task;
  }).catch(error=>{read.error=error;return null;}).then(()=>{
    if (p.attentionBoard === board && board.focusRead === read && board.data) attentionBoardPaint(p);
  });
  return read;
}
function attentionFocusKey(data, ref) {
  return JSON.stringify([ref?.type, ref?.id, ref?.business_id ?? null,
    data?.attention?.focus_generation ?? data?.revision ?? ref?.revision ?? null]);
}
function attentionBoardPaint(p) {
  const b=p.attentionBoard, data=b.data, board=b.board, header=board.querySelector('[data-attention-focus]'), list=board.querySelector('[data-attention-list]');
  attentionAgentsPaint(p);
  board.querySelector('[data-attention-error]').hidden=true;
  header.replaceChildren();
  if (!data) {
    list.innerHTML='<p class="attention-empty">Loading Todo…</p>';
    if (p.attentionCache?.error) { list.replaceChildren(); attentionBoardShowError(p, p.attentionCache.error); }
    return;
  }
  const attention=data.attention;
  const current=document.createElement('section');header.append(current);
  if (attention.state !== 'focused' || !attention.focus) current.remove();
  else if (attention.focus.unavailable) {const text=document.createElement('p');text.textContent='Focus unavailable';current.append(text);}
  else {
    const focus=attention.focus;
    const workFor=ref=>data.items.find(item=>item.task_ref.id===ref.id && item.task_ref.business_id===ref.business_id);
    const reference=ref=>{
      const work=workFor(ref);
      let scope=attentionBoardRefScope(ref), task=null;
      if (work) { task=attentionBoardTask(work.task_ref,work.task); scope=attentionBoardScope(work.task_ref,work.task); }
      else if (ref.type === 'task') {
        const read=attentionFocusTask(p,ref,attentionFocusKey(data,ref));
        if (read?.task) { task=attentionBoardTask(ref,read.task); scope=read.scope; }
      }
      const rows=document.createElement('div');rows.className='agfocus-items';
      rows.append(attentionReferenceRow(p,{...ref,business_slug:scope,label:ref.label || task?.title || 'Task',task_type:task?.task_type || ref.task_type,revision:task?.revision},task));
      current.append(rows);
    };
    if (focus.type === 'workflow') {
      const text=document.createElement('p');text.textContent=focus.label || focus.id;current.append(text);
      if(attention.task)reference(attention.task);
    } else reference(focus);
    if(attention.intent){const intent=document.createElement('p');intent.className='attention-intent';intent.textContent=attention.intent;current.append(intent);}
  }
  list.replaceChildren();
  const toolbar=document.createElement('div');toolbar.className='attention-list-heading';list.append(toolbar);
  const name=document.createElement('h2');
  name.textContent=attentionAgentsName(p, p.attentionAgentId || p.agentId) + '’s Todo';toolbar.append(name);
  const mine=p.attentionBoard.filter==='owner';
  const filter=document.createElement('button');filter.type='button';filter.className='attention-owner-toggle';
  filter.setAttribute('aria-pressed',String(mine));
  filter.setAttribute('aria-label',mine?'Showing only my tasks. Show everyone\'s tasks':'Showing everyone\'s tasks. Show only my tasks');
  filter.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>';
  filter.onclick=()=>{p.attentionBoard.filter=mine?'all':'owner';attentionBoardPaint(p);filter.isConnected||board.querySelector('.attention-owner-toggle')?.focus({preventScroll:true});};toolbar.append(filter);
  const rows=document.createElement('div');rows.className='agfocus-items';
  const visible=data.items.filter(item=>!mine || (attentionBoardTask(item.task_ref,item.task).worker_ids || []).includes(data.owner_id));
  for (const status of ATTENTION_GROUPS) {
    const items=visible.filter(item=>(attentionBoardTask(item.task_ref,item.task).status || 'open')===status);
    for (const item of items) {
      const ref=item.task_ref, scope=attentionBoardScope(ref,item.task), task=attentionBoardTask(ref,item.task);
      const row=attentionReferenceRow(p,{...ref,business_slug:scope,label:task.title,state:task.status,task_type:task.task_type,revision:task.revision},task);
      row.dataset.attentionMember=item.membership_id;
      if(status==='done' && item===items[0] && rows.childElementCount)row.classList.add('attention-completed-start');
      const actions=row.querySelector('.agfocus-actions');
      const remove=document.createElement('button'); remove.type='button'; remove.className='quiet'; remove.innerHTML=SVG_X; remove.setAttribute('aria-label','Remove '+(task.title || 'task')+' from Todo');
      // Capture this board before awaiting: a later agent selection must not change the removal target or receipt.
      remove.onclick=async()=>{const cache=attentionBoardCache(p);remove.disabled=true; try { coreData(await post('/root/operations/todo_remove',{scope:'global',agent_id:cache.agentId,membership_id:item.membership_id,expected_membership_revision:item.membership_revision,idempotency_key:newRequestId()})); await attentionBoardForget(p,item.membership_id,cache); } catch(error) {toast(error.message); remove.disabled=false;}};
      actions.append(remove); rows.append(row);
    }
  }
  if (visible.length) list.append(rows);
  else {const empty=document.createElement('p');empty.className='attention-empty';empty.textContent=data.items.length?'No tasks for this filter.':'No tasks on Todo.';list.append(empty);}
  if (p.attentionCache?.error) attentionBoardShowError(p, p.attentionCache.error);
}
