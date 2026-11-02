// Shared Worker presentation and editor. Task writes use the existing scoped Tasks operation.
function taskWorkerMount(host, scope, task, agentId) {
  let button = host.querySelector('[data-task-workers]');
  if (!button) { button = document.createElement('button'); button.type = 'button'; button.className = 'quiet task-workers'; button.dataset.taskWorkers = ''; host.prepend(button); }
  const current = taskStateObserve(scope, task);
  const workers = [...(current.workers || [])].sort((a,b) => (a.is_owner ? 2 : a.id === agentId ? 1 : 0) - (b.is_owner ? 2 : b.id === agentId ? 1 : 0));
  button.innerHTML = (workers.map(w => identityAvatar(w.kind === 'human' ? 'person' : 'agent', w.display_name, globalAgentProfile(w.id) || w, w.display_name, true)).join('') || '<span class="muted">+</span>') + (current.user_needed ? '<span class="task-user-needed">User needed</span>' : '');
  button.setAttribute('aria-label', 'Workers' + (workers.length ? ': ' + workers.map(w=>w.display_name).join(', ') : ': none') + (current.user_needed ? ' · User needed' : ''));
  button.title = current.user_needed ? 'Workers · User needed' : 'Workers';
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

const ATTENTION_GROUPS = ['blocked','working','open','waiting','done','cancelled'];
function taskBoardStateMenu(button, scope, task) {
  dropdown(button, T_STATES.map(s => ({value:s,label:T_LABEL[s],html:`<span class="towner">${tDotHtml(s)}<span class="tn">${T_LABEL[s]}</span></span>`})), value => {if(value !== task.status) taskStateSet(scope,task,value);}, task.status);
}
function attentionBoardScope(ref, task) {
  if (ref.business_id == null) return null;
  return task.business_slug || (state.bizs || []).find(b => (b.business_id || b.id) === ref.business_id)?.slug || null;
}
function attentionBoardTask(ref, task) {
  const scope=attentionBoardScope(ref,task);
  return ref.business_id != null && !scope ? task : taskStateObserve(scope,task);
}
function attentionBoardMount(p) {
  const body = $('#agpad-body', p.el);
  body.innerHTML = '<section class="agfocus" data-attention-board><header><div class="agfocus-heading"><div class="global-eyebrow">Attention board</div></div><div class="agfocus-framing" data-attention-focus></div><p class="agfocus-error" data-attention-error role="alert" hidden></p></header><div class="agfocus-layout"><div data-attention-list></div><aside class="agfocus-dock" data-attention-record hidden><div class="agfocus-dock-content"></div></aside></div></section>';
  const board = body.firstElementChild, dock = board.querySelector('[data-attention-record]');
  const mountWorkspace = () => recordWorkspaceMount(dock.firstElementChild, {entry:'focus', paired:false, visible:()=>agpadActive(p) && p.view === 'attentionBoard', onClose:()=>{
    dock.hidden=true;
    board.querySelector('.agfocus-layout').classList.remove('agfocus-paired-workspace');
    board.querySelector('header').hidden=false;
    board.querySelector(`[data-attention-member="${p.attentionBoard?.selected || ''}"]`)?.focus({preventScroll:true});
  }});
  p.attentionBoard = {board, workspace:mountWorkspace(), mountWorkspace, next:0, busy:false, data:null};
  const unsubscribe=taskStateSubscribe((scope,id,snapshot)=>{
    if (!snapshot.pending && p.attentionBoard === boardState && boardState.data?.items.some(item => item.task_ref.id === id && attentionBoardScope(item.task_ref,item.task) === scope)) {
      clearTimeout(boardState.repaint); boardState.repaint=setTimeout(()=>{if(p.attentionBoard === boardState) attentionBoardPaint(p);},0);
    }
  });
  const boardState=p.attentionBoard;
  p.workspaceDispose = () => {unsubscribe(); clearTimeout(boardState.repaint); boardState.workspace.dispose(); p.attentionBoard = null;};
  attentionBoardTick(p);
}
function attentionBoardTick(p) {
  const board = p.attentionBoard;
  if (!board || board.busy || Date.now() < board.next || !agpadActive(p)) return;
  void attentionBoardRefresh(p);
}
async function attentionBoardRefresh(p) {
  const board = p.attentionBoard;
  if (!board || board.busy) return;
  board.busy = true;
  try {
    let cursor = null, data, items = [], seen = new Set();
    do {
      const page = coreData(await post('/root/operations/attention_get', {scope:'global', agent_id:p.agentId, limit:100, ...(cursor ? {cursor} : {})}));
      if (!data) data = page;
      items.push(...page.items); cursor = page.next_cursor;
      if (cursor && seen.has(cursor)) throw Error('Attention pagination did not advance.');
      if (cursor) seen.add(cursor);
    } while (cursor);
    if (p.attentionBoard !== board) return;
    board.data = {...data, items}; attentionBoardPaint(p);
  } catch (error) {
    if (p.attentionBoard === board) { const el = board.board.querySelector('[data-attention-error]'); el.hidden=false; el.textContent='Could not load Attention board. ' + error.message; }
  } finally { board.busy=false; board.next=Date.now()+12000; }
}
async function attentionBoardOpen(p, source) {
  const b=p.attentionBoard, record=rwRecordFromItem({source});
  if (!record) return;
  if (record.scopeMissing) { toast('This project is unavailable.'); return; }
  if(!b.workspace.alive?.())b.workspace=b.mountWorkspace();
  const dock=b.board.querySelector('[data-attention-record]');
  dock.hidden=false;
  b.board.querySelector('header').hidden=true;
  b.board.querySelector('.agfocus-layout').classList.add('agfocus-paired-workspace');
  try { await b.workspace.open(record,{root:true,tab:record.can_thread?'chat':'dashboard'}); }
  catch(error) {toast(error.message);}
}
// Attention owns membership and intent; the row structure, markers and scope badges are shared.
function attentionReferenceRow(p, source, task = null) {
  const row=document.createElement('article'); row.className='agfocus-card agfocus-collapsed';
  row.innerHTML=agfocusRowHtml();
  const label=source.label || task?.title || 'Record';
  row.querySelector('.ttl').textContent=label;
  agfocusContext(row,{source,resolved_source:source});
  const marker=row.querySelector('.agfocus-marker');
  marker.innerHTML=task && !taskShowsState(task) ? tBall(task)
    : `<span class="task-type-marker">${source.type === 'table' ? RW_TABLE_ICON : rwTypeIcon(rwRecordFromItem({source}) || {source})}</span>`;
  const flag=row.querySelector('.agfocus-flag');
  const stateLabel=task ? customerStateText(task) : '';
  flag.textContent=stateLabel; flag.hidden=!stateLabel;
  row.tabIndex=0; row.setAttribute('role','group'); row.setAttribute('aria-label',label);
  const open=()=>{p.attentionBoard.selected=row.dataset.attentionMember;void attentionBoardOpen(p,source);};
  row.onclick=event=>{if(!event.target.closest('button,a,input,textarea,select'))open();};
  row.onkeydown=event=>{if(event.target===row && ['Enter',' '].includes(event.key)){event.preventDefault();open();}};
  return row;
}
function attentionBoardPaint(p) {
  const b=p.attentionBoard, data=b.data, board=b.board, header=board.querySelector('[data-attention-focus]'), list=board.querySelector('[data-attention-list]');
  board.querySelector('[data-attention-error]').hidden=true;
  const attention=data.attention;
  header.replaceChildren();
  const current=document.createElement('section');header.append(current);
  if (attention.state !== 'focused' || !attention.focus) {const text=document.createElement('p');text.textContent='Unfocused';current.append(text);}
  else {
    const label=document.createElement('p'); label.textContent='Focused on';current.append(label);
    const focus=attention.focus;
    const workFor=ref=>data.items.find(item=>item.task_ref.id===ref.id && item.task_ref.business_id===ref.business_id);
    const reference=ref=>{
      const work=workFor(ref),task=work ? attentionBoardTask(work.task_ref,work.task) : null;
      const rows=document.createElement('div');rows.className='agfocus-items';
      rows.append(attentionReferenceRow(p,{...ref,business_slug:work ? attentionBoardScope(work.task_ref,work.task) : ref.business_slug,label:ref.label || task?.title || 'Task',task_type:task?.task_type || ref.task_type,revision:task?.revision},task));
      current.append(rows);
    };
    if (focus.type === 'workflow') {
      const text=document.createElement('p');text.textContent=focus.label || focus.id;current.append(text);
      if(attention.task)reference(attention.task);
    } else reference(focus);
    const intent=document.createElement('p');intent.textContent='Intent: '+attention.intent;current.append(intent);
  }
  list.replaceChildren();
  for (const status of ATTENTION_GROUPS) {
    const items=data.items.filter(item=>(attentionBoardTask(item.task_ref,item.task).status || 'open')===status);
    if (!items.length) continue;
    const group=document.createElement('section'); group.dataset.attentionStatus=status;
    const title=document.createElement('h2'); title.textContent=T_LABEL[status]; group.append(title);
    const rows=document.createElement('div');rows.className='agfocus-items';group.append(rows);
    for (const item of items) {
      const ref=item.task_ref, scope=attentionBoardScope(ref,item.task), unavailable=ref.business_id != null && !scope, task=attentionBoardTask(ref,item.task);
      const row=attentionReferenceRow(p,{...ref,business_slug:scope,label:task.title,state:task.status,task_type:task.task_type,revision:task.revision},task);
      row.dataset.attentionMember=item.membership_id;
      if(taskShowsState(task)) {
        const marker=row.querySelector('.agfocus-marker');
        marker.innerHTML='<button type="button" class="ball agfocus-task-state"><span class="core"></span></button>';
        const ball=marker.firstElementChild;
        if (!unavailable) taskStateBind(ball,scope,task,()=>{}); else {ball.disabled=true; ball.setAttribute('aria-label','Task project unavailable');}
        ball.onclick=event=>{event.stopPropagation();if(!unavailable)taskStateSet(scope,task,tNext(task.status));};
        ball.oncontextmenu=event=>{event.preventDefault();event.stopPropagation();if(!unavailable)taskBoardStateMenu(ball,scope,task);};
      }
      const actions=row.querySelector('.agfocus-actions');
      if (!unavailable && (task.workers?.length || task.user_needed)) taskWorkerMount(actions,scope,task,p.agentId);
      const remove=document.createElement('button'); remove.type='button'; remove.className='quiet'; remove.innerHTML=SVG_X; remove.setAttribute('aria-label','Remove '+(task.title || 'task')+' from Attention board');
      remove.onclick=async()=>{remove.disabled=true; try { coreData(await post('/root/operations/attention_remove',{scope:'global',membership_id:item.membership_id,expected_membership_revision:item.membership_revision,idempotency_key:newRequestId()})); await attentionBoardRefresh(p); } catch(error) {toast(error.message); remove.disabled=false;}};
      actions.append(remove); rows.append(row);
    }
    list.append(group);
  }
  if (!data.items.length) {const empty=document.createElement('p');empty.className='attention-empty';empty.textContent='No tasks on Attention board.';list.append(empty);}
}
