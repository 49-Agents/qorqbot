// Business Tasks: every Task the selected business owns, whoever it is assigned to.
// Rows, status changes and detail reuse the drawer; this view owns paged list reads.
// The same page renders as the Tasks section of an agent pane's business view.
let btSearch = '', btVisibility = 'active', btSig = null, btPaintPending = false;
let btLoad = null, btQuery = null, btCursor = null, btCursors = new Set(), btError = null, btMoreFailed=false;
const btPage = () => document.querySelector('[data-business-task-page]');

function renderBusinessTasks(c) {
  btSig = null;
  c.innerHTML = `<section class="pane tasks business-tasks" data-business-task-page aria-label="Tasks">
    <header class="business-tasks-head"><h1>Tasks</h1></header>
    <div class="task-toolbar"><label class="tsearch">${SVG_TSEARCH}<input type="search" data-business-task-search aria-label="Search tasks" autocomplete="off" spellcheck="false" value="${esc(btSearch)}"></label><select data-business-task-visibility aria-label="Task visibility">${['active', 'snoozed', 'all'].map(v => `<option value="${v}" ${btVisibility === v ? 'selected' : ''}>${v[0].toUpperCase() + v.slice(1)}</option>`).join('')}</select><button class="btn" type="button" data-business-task-new>${SVG_TPLUS}New task</button></div>
    <div class="business-task-list" data-business-task-list></div></section>`;
  const page = btPage(), biz=state.biz, search = page.querySelector('[data-business-task-search]');
  let searchTimer;
  search.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { if(!page.isConnected || state.biz!==biz)return; btSearch = search.value; void businessTasksLoad(); }, 200); };
  page.querySelector('[data-business-task-visibility]').onchange = event => { btVisibility = event.target.value; void businessTasksLoad(); };
  page.querySelector('[data-business-task-new]').onclick = () => taskCreateDialog(state.biz, {onCreated:()=>{uiReads.invalidate('business-tasks:');btSig=null;refreshTasks();}});
  businessTasksPaint();
  refreshTasks();
}

function businessTasksKey() { return 'business-tasks:'+JSON.stringify([state.biz,btSearch.trim(),btVisibility]); }
function businessTasksLoad(more=false, current=()=>true) {
  const biz=state.biz,key=businessTasksKey(),cursor=more?btCursor:null;
  if(!biz || more && !cursor)return Promise.resolve();
  if(btLoad?.key===key && btLoad.current())return btLoad.promise;
  if(btLoad && !btLoad.current())uiReads.invalidate(key);
  const cached=uiReads.peek(key), same=(btQuery===null || btQuery===key) && state.tasksBiz===biz;
  if(!same) {
    state.tasks=cached?.tasks || [];state.tasksBiz=cached?biz:null;
    btCursor=cached?.next_cursor || null;btCursors=new Set();btQuery=key;
  }
  btQuery=key;
  const run={key,current};btLoad=run;btError=null;btMoreFailed=more;
  businessTasksPaint();
  run.promise=(async()=>{
    try {
      const params=new URLSearchParams({all:'1',paged:'1',limit:'50',search:btSearch.trim(),visibility:btVisibility});
      if(cursor)params.set('cursor',cursor);
      const read=()=>api(`/biz/${encodeURIComponent(biz)}/tasks?${params}`);
      const first=more?await read():await uiReads.read(key,read);
      const data={...first,tasks:[...first.tasks]}, visited=new Set(cursor?[cursor]:[]);
      const depth=more?1:Math.max(1,btCursors.size+1);
      let loaded=1;
      while(!more && data.next_cursor && loaded<depth){
        if(btLoad!==run || !current() || state.biz!==biz || businessTasksKey()!==key)return;
        if(visited.has(data.next_cursor))throw Error('Task pagination did not advance.');
        visited.add(data.next_cursor);params.set('cursor',data.next_cursor);
        const page=await read();data.tasks.push(...page.tasks);data.next_cursor=page.next_cursor;loaded++;
      }
      if(btLoad!==run || !current() || state.biz!==biz || businessTasksKey()!==key)return;
      if(!Array.isArray(data.tasks))throw Error('Invalid task page');
      if(data.next_cursor && (data.next_cursor===cursor || visited.has(data.next_cursor) || more && btCursors.has(data.next_cursor)))throw Error('Task pagination did not advance.');
      if(!more)btCursors=visited;
      if(cursor)btCursors.add(cursor);
      state.tasks=[...new Map((more?[...state.tasks,...data.tasks]:data.tasks).map(task=>[task.id,task])).values()];
      state.tasks.forEach(task=>Object.assign(task,taskStateObserve(biz,task)));
      state.tasksBiz=biz;state.tasksError=null;btCursor=data.next_cursor;
    } catch(error) {
      if(btLoad!==run || !current() || state.biz!==biz || businessTasksKey()!==key)return;
      btError=error;
      if(accessLost(error)){state.tasks=[];state.tasksBiz=null;btCursor=null;uiReads.invalidate(key,{drop:true});}
      state.tasksError=biz;
    } finally {
      if(btLoad===run){btLoad=null;businessTasksPaint();}
    }
  })();
  return run.promise;
}
onChange(['tasks','agents'],null,()=>uiReads.invalidate('business-tasks:'));

function businessTasksPaint() {
  const page = btPage(); if (!page || tEditingNow()) return;
  const list = page.querySelector('[data-business-task-list]'), biz = state.biz;
  const loaded = state.tasksBiz === biz && btQuery === businessTasksKey(), failed = !loaded && !!btError;
  const query = btSearch.trim().toLowerCase();
  const rows = (loaded ? state.tasks : []).filter(task => tSlug(task) === biz
    && (btVisibility === 'all' || !tSunk(task) && taskSnoozed(task) === (btVisibility === 'snoozed')))
    .sort((a, b) => Number(tSunk(a)) - Number(tSunk(b)) || String(a.due || '9999').localeCompare(b.due || '9999') || a.title.localeCompare(b.title) || tKey(a).localeCompare(tKey(b)));
  const sig = JSON.stringify([biz, loaded, failed, !!btLoad, btCursor, btError?.message, btVisibility, query, rows.map(task =>
    [tKey(task), task.status, tAssignees(task).join(','), task.title, task.due || '', (task.tags || []).join(','), task.created_by, task.created_by_name, tSunk(task), task.snoozed_until, (task.parent || {}).title])]);
  if (sig === btSig && list.childElementCount) return;
  btSig = sig;
  list.innerHTML = failed ? `<p class="personal-task-error" role="alert">Couldn't load tasks. <button class="ghost" type="button" data-business-task-retry>Retry</button></p>`
    : !loaded ? tEmptyHtml('Loading tasks…')
    : rows.length ? `<div class="personal-task-list">${rows.map(task => tRow(task)).join('')}</div>`
    : tEmptyHtml(query ? 'No matching tasks.' : btVisibility === 'snoozed' ? 'No snoozed tasks.' : 'No open tasks.');
  if(loaded && btError)list.insertAdjacentHTML('beforeend','<p role="alert">Could not refresh tasks. <button class="quiet" data-business-task-retry>Retry</button></p>');
  if(loaded && btCursor)list.insertAdjacentHTML('beforeend',`<button class="quiet" data-business-task-more ${btLoad?'disabled':''}>${btLoad?'Loading…':'Load more'}</button>`);
  list.querySelector('[data-business-task-more]')?.addEventListener('click',()=>businessTasksLoad(true));
  list.querySelector('[data-business-task-retry]')?.addEventListener('click', () => businessTasksLoad(btMoreFailed));
  tWireRows(list);
  list.querySelectorAll('.trow').forEach(row => {
    const task = taskById(row.dataset.key); if (!task) return;
    const ball = row.querySelector('.ball');
    if (ball) ball.onclick = event => { event.stopPropagation(); taskStateSet(tSlug(task), task, tNext(task.status)); };
    row.onclick = event => { if (!event.target.closest('button')) tDetail(task); };
  });
}

// A status change elsewhere (the detail, Focus, another row) repaints this page once.
taskStateSubscribe(scope => {
  if (!btPage() || scope !== state.biz || btPaintPending) return;
  btPaintPending = true;
  Promise.resolve().then(() => { btPaintPending = false; businessTasksPaint(); });
});
