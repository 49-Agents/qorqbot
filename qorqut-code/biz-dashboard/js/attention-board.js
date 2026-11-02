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
  body.innerHTML = '<section class="attention-board"><header><div class="global-eyebrow">Attention board</div><div data-attention-focus></div><p data-attention-error role="alert" hidden></p></header><div data-attention-list></div><aside data-attention-record hidden></aside></section>';
  const board = body.firstElementChild, dock = board.querySelector('[data-attention-record]');
  const workspace = recordWorkspaceMount(dock, {entry:'focus', paired:false, visible:()=>agpadActive(p) && p.view === 'attentionBoard', onClose:()=>{dock.hidden=true;}});
  p.attentionBoard = {board, workspace, next:0, busy:false, data:null};
  const unsubscribe=taskStateSubscribe((scope,id,snapshot)=>{
    if (!snapshot.pending && p.attentionBoard === boardState && boardState.data?.items.some(item => item.task_ref.id === id && attentionBoardScope(item.task_ref,item.task) === scope)) {
      clearTimeout(boardState.repaint); boardState.repaint=setTimeout(()=>{if(p.attentionBoard === boardState) attentionBoardPaint(p);},0);
    }
  });
  const boardState=p.attentionBoard;
  p.workspaceDispose = () => {unsubscribe(); clearTimeout(boardState.repaint); workspace.dispose(); p.attentionBoard = null;};
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
function attentionBoardOpen(p, source) {
  const b=p.attentionBoard, record=rwRecordFromItem({source});
  if (!record) return;
  if (record.scopeMissing) { toast('This project is unavailable.'); return; }
  const dock=b.board.querySelector('[data-attention-record]'); dock.hidden=false;
  b.workspace.open(record,{root:true,tab:record.can_thread?'chat':'dashboard'});
}
function attentionBoardPaint(p) {
  const b=p.attentionBoard, data=b.data, board=b.board, header=board.querySelector('[data-attention-focus]'), list=board.querySelector('[data-attention-list]');
  board.querySelector('[data-attention-error]').hidden=true;
  const attention=data.attention;
  header.replaceChildren();
  if (attention.state !== 'focused' || !attention.focus) header.textContent='Unfocused';
  else {
    const line=document.createElement('div'); line.append('Focused on: ');
    if (attention.focus.type === 'workflow') {
      line.append(document.createTextNode(attention.focus.label || attention.focus.id));
      header.append(line);
      if (attention.task) {
        const work=data.items.find(item=>item.task_ref.id === attention.task.id && item.task_ref.business_id === attention.task.business_id);
        const workLine=document.createElement('div'), link=document.createElement('button'); link.type='button'; link.className='quiet';
        link.textContent=work?.task?.title || attention.task.id;
        link.onclick=()=>attentionBoardOpen(p,{...attention.task, business_slug:work ? attentionBoardScope(work.task_ref,work.task) : null, label:work?.task?.title || 'Task', task_type:work?.task?.task_type, revision:work?.task?.revision});
        workLine.append('Work task: ',link); header.append(workLine);
      }
    } else {
      const link=document.createElement('button'); link.type='button'; link.className='quiet'; link.textContent=attention.focus.label || attention.focus.id;
      link.onclick=()=>attentionBoardOpen(p,attention.focus); line.append(link); header.append(line);
    }
    const intent=document.createElement('p'); intent.textContent='Intent: '+attention.intent; header.append(intent);
  }
  list.replaceChildren();
  for (const status of ATTENTION_GROUPS) {
    const items=data.items.filter(item=>(attentionBoardTask(item.task_ref,item.task).status || 'open')===status);
    if (!items.length) continue;
    const group=document.createElement('section'); group.className='attention-group';
    const title=document.createElement('h3'); title.textContent=T_LABEL[status]; group.append(title);
    for (const item of items) {
      const ref=item.task_ref, scope=attentionBoardScope(ref,item.task), unavailable=ref.business_id != null && !scope, task=attentionBoardTask(ref,item.task);
      const row=document.createElement('div'); row.className='trow attention-task';
      const ball=document.createElement('button'); ball.type='button'; ball.className='ball agfocus-task-state'; ball.innerHTML='<span class="core"></span>'; row.append(ball);
      if (!unavailable) taskStateBind(ball,scope,task,()=>{}); else {ball.disabled=true; ball.setAttribute('aria-label','Task project unavailable');} ball.onclick=()=>{if (ref.business_id != null && !scope) return toast('This project is unavailable.'); taskStateSet(scope,task,tNext(task.status));};
      ball.oncontextmenu=event=>{event.preventDefault(); if(ref.business_id == null || scope) taskBoardStateMenu(ball,scope,task);};
      const name=document.createElement('button'); name.type='button'; name.className='quiet attention-task-name'; name.textContent=task.title || 'Task'; name.onclick=()=>attentionBoardOpen(p,{...ref,business_slug:scope,label:task.title,state:task.status,task_type:task.task_type,revision:task.revision}); row.append(name);
      const actions=document.createElement('div'); actions.className='attention-task-actions'; row.append(actions);
      const stateMenu=document.createElement('button'); stateMenu.type='button'; stateMenu.className='quiet'; stateMenu.textContent='State'; stateMenu.setAttribute('aria-label','Change state of '+(task.title || 'task')); stateMenu.disabled=unavailable; stateMenu.onclick=()=>{if(!unavailable) taskBoardStateMenu(stateMenu,scope,task);}; actions.append(stateMenu);
      if (ref.business_id == null || scope) taskWorkerMount(actions,scope,task,p.agentId);
      const remove=document.createElement('button'); remove.type='button'; remove.className='quiet'; remove.textContent='Remove'; remove.setAttribute('aria-label','Remove '+(task.title || 'task')+' from Attention board');
      remove.onclick=async()=>{remove.disabled=true; try { coreData(await post('/root/operations/attention_remove',{scope:'global',membership_id:item.membership_id,expected_membership_revision:item.membership_revision,idempotency_key:newRequestId()})); await attentionBoardRefresh(p); } catch(error) {toast(error.message); remove.disabled=false;}};
      actions.append(remove); group.append(row);
    }
    list.append(group);
  }
  if (!data.items.length) list.textContent='No tasks on Attention board.';
}
