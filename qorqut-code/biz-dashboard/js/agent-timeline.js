// Ordubashy's completed-result journal, using the canonical action read model.
// The server filters by principal and occurrence date; recorded-only attribution
// stays explicit, and day/hour precision never becomes an invented timestamp.
T_VIEW_LABEL.timeline = 'Timeline';
T_VIEW_ICON.timeline = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M6 3v18M11 5h9M11 12h6M11 19h9"/><circle cx="6" cy="5" r="2"/><circle cx="6" cy="12" r="2"/><circle cx="6" cy="19" r="2"/></svg>';
const AGENT_TIMELINE_CHEVRON = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 4 4 4-4 4"/></svg>';

// Reconcile the mounted pane when Settings (or another tab) changes Testing mode.
if (typeof testingModeSubscribe === 'function') testingModeSubscribe(() => {
  if (_agpad?.workspaceRole === 'ordubashy') agpadWorkspaceViewsSync(_agpad);
});

function agentTimelineEvidence(key, value) {
  const href = safeLinkUrl(value);
  if (href) return `<a class="safe-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(value)}</a>`;
  const opens = ['task', 'signoff', 'document', 'upload'].includes(key);
  return `<button type="button" class="agent-timeline-reference" data-evidence-key="${esc(key)}" aria-label="${opens ? 'Open' : 'Copy'} ${esc(key)} reference">${esc(value)}</button>`;
}

// Evidence predates scoped references. Resolve its exact ID in the action's named
// projects and storage scope, rather than treating every recorded action as Global.
async function agentTimelineResolve(action, key, id) {
  const projects = action.businesses || [];
  const scopes = new Map([[null, null]]);
  for (const project of projects) {
    const known = (state.bizs || []).find(b => (b.id || b.business_id) === project.id);
    if (project.slug || known?.slug) scopes.set(project.id, project.slug || known.slug);
  }
  if (action.legacy?.business_id) {
    const known = (state.bizs || []).find(b => (b.id || b.business_id) === action.legacy.business_id);
    if (known?.slug) scopes.set(action.legacy.business_id, known.slug);
  }
  const matches = [];
  for (const [businessId, slug] of scopes) {
    try {
      let record;
      if (key === 'signoff') record = coreData(await api(`${slug ? `/biz/${encodeURIComponent(slug)}` : '/root'}/signoffs/${encodeURIComponent(id)}`)).signoff;
      else if (key === 'task') record = (await workCall(slug, 'tasks_get', {task_id:id})).task;
      else {
        for (const operation of ['notes_get', 'context_get', 'assets_get']) {
          try {
            const data = await workCall(slug, operation, {[operation === 'assets_get' ? 'asset_id' : 'document_id']:id});
            record = data.document || data.asset;
            break;
          } catch (error) {
            if (!['not_found', 'validation_error'].includes(error.code)) throw error;
          }
        }
      }
      if (record?.id === id) matches.push({record, businessId, slug});
    } catch (error) {
      if (!['not_found', 'validation_error'].includes(error.code)) throw error;
    }
  }
  if (matches.length !== 1) throw Error(matches.length ? 'This reference matches more than one project.' : 'This reference is no longer available.');
  return matches[0];
}

async function agentTimelineOpenEvidence(p, button) {
  const timeline = p.timeline, key = button.dataset.evidenceKey;
  const action = timeline?.rows?.find(row => row.action_id === button.closest('[data-action-id]')?.dataset.actionId);
  const value = action?.refs?.[key];
  if (!value || !agentTimelineCurrent(p, timeline)) return;
  const date = timeline.date;
  const current = () => agentTimelineCurrent(p, timeline) && timeline.date === date
    && timeline.rows?.some(row => row.action_id === action.action_id && row.refs?.[key] === value);
  button.disabled = true;
  try {
    if (!['task', 'signoff', 'document', 'upload'].includes(key)) {
      await navigator.clipboard.writeText(value); toast('Reference copied.'); return;
    }
    const {record, businessId, slug} = await agentTimelineResolve(action, key, value);
    if (!current()) return;
    if (key === 'task') await taskOpenReference({...record, business_id:businessId, business_slug:slug});
    else if (key === 'signoff') await sgAttentionNavigate(slug, value);
    else await workOpenLink({biz:slug, error:error=>toast(uiFailure(error, {load:true}))}, {
      resource_type:'documents', resource_id:value,
      current:{kind:record.kind, label:record.metadata?.title || record.title || 'Document'},
    });
  } catch (error) {
    if (current()) toast(error.message || uiFailure(error, {load:true}));
  } finally { button.disabled = false; }
}

function agentTimelineRow(action) {
  const refs = Object.entries(action.refs || {});
  const meta = action.attribution === 'recorded' ? [acPerson(action)] : [];
  const zone = action.occurred?.time_zone;
  if (zone && zone !== SJ_ZONE) meta.push(zone);
  const content = `<div class="agent-timeline-copy">
    <p class="agent-timeline-text">${esc(action.text)}</p>
    ${meta.length ? `<p class="agent-timeline-meta">${meta.map(esc).join(' · ')}</p>` : ''}</div>`;
  const precision = action.occurred?.precision;
  const instant = precision === 'instant' && action.occurred?.instant;
  return `<li class="agent-timeline-row" data-action-id="${esc(action.action_id || '')}">
    <time${instant ? ` datetime="${esc(instant)}"` : ''}>${esc(acOccurred(action))}</time>
    ${refs.length || action.detail ? `<details class="agent-timeline-entry">
      <summary>${content}<span class="agent-timeline-expand">${AGENT_TIMELINE_CHEVRON}</span></summary>
      <div class="agent-timeline-evidence" aria-label="Evidence">
        ${action.detail ? `<p>${renderSafeLinks(action.detail)}</p>` : ''}
        ${refs.length ? `<dl>${refs.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${agentTimelineEvidence(key, value)}</dd>`).join('')}</dl>` : ''}
      </div></details>` : content}
  </li>`;
}

function agentTimelineMount(p) {
  const body = $('#agpad-body', p.el);
  if (!body || !p.agentId || !agentPaneViewAllowed(p.workspaceRole, 'timeline')) return;
  body.innerHTML = `<section class="agent-timeline" aria-label="Ordubashy action timeline">
    <header class="agent-timeline-header">
      <h2 class="agent-timeline-heading">Timeline</h2>
      <div class="agent-timeline-nav" role="group" aria-label="Timeline date">
        <button type="button" class="quiet" data-day="previous" aria-label="Previous day" title="Previous day">${AGENT_TIMELINE_CHEVRON}</button>
        <input class="inp" type="date" aria-label="Action date" disabled>
        <button type="button" class="quiet" data-day="next" aria-label="Next day" title="Next day">${AGENT_TIMELINE_CHEVRON}</button>
        <button type="button" class="quiet" data-day="today" hidden>Today</button>
      </div>
    </header>
    <div class="agent-timeline-caption"><p role="status" aria-live="polite" data-status>Loading actions…</p><span data-zone></span></div>
    <div class="agent-timeline-error" hidden><p role="alert"></p><button type="button" class="quiet" data-retry>Retry</button></div>
    <div class="agent-timeline-empty" hidden><button type="button" class="quiet" data-latest>Latest recorded day ${AGENT_TIMELINE_CHEVRON}</button></div>
    <ol class="agent-timeline-list" aria-label="Recorded actions, newest first"></ol>
    <button type="button" class="quiet" data-more hidden>Load more</button>
  </section>`;
  const root = body.firstElementChild;
  const timeline = p.timeline = {root, date: p.timelineDate || '', ready: false, loading: false, seq: 0, next: 0, rows: null, cursor:null, cursors:new Set(), moreFailed:false, signature: '', error: ''};
  root.addEventListener('click', event => {
    const button = event.target.closest('[data-evidence-key]');
    if (button && !button.disabled) void agentTimelineOpenEvidence(p, button);
  });
  const input = root.querySelector('input');
  const choose = date => {
    if (!date || date > sjToday()) { input.value = timeline.date; return; }
    p.timelineDate = timeline.date = date;
    timeline.rows = null; timeline.cursor=null; timeline.cursors.clear(); timeline.signature = ''; timeline.error = '';
    timeline.seq++; timeline.loading = false;
    root.querySelector('ol').innerHTML = '';
    void agentTimelineRefresh(p);
  };
  input.onchange = () => choose(input.value);
  root.querySelectorAll('[data-day]').forEach(button => button.onclick = () => {
    if (!timeline.ready) return;
    choose(button.dataset.day === 'today' ? sjToday() : sjAddDays(timeline.date, button.dataset.day === 'previous' ? -1 : 1));
  });
  root.querySelector('[data-latest]').onclick = async () => {
    if (!timeline.ready || timeline.loading) return;
    const seq = ++timeline.seq;
    timeline.loading = true; timeline.error = ''; agentTimelinePaint(p);
    try {
      const data = await acOp('actions_list', {performer: p.agentId, limit: 1});
      if (!agentTimelineCurrent(p, timeline) || seq !== timeline.seq) return;
      timeline.loading = false;
      if (data.actions[0]?.occurred?.date) choose(data.actions[0].occurred.date);
      else { timeline.error = 'No dated actions have been recorded yet.'; agentTimelinePaint(p); }
    } catch (error) {
      if (!agentTimelineCurrent(p, timeline) || seq !== timeline.seq) return;
      timeline.loading = false; timeline.error = uiFailure(error, {load: true}); agentTimelinePaint(p);
    }
  };
  p.workspaceDispose = () => { timeline.seq++; if (p.timeline === timeline) p.timeline = null; };
  const initialize = async () => {
    const seq = ++timeline.seq;
    timeline.error = ''; timeline.loading = true; agentTimelinePaint(p);
    try {
      const config = await uiReads.read('actions-config',()=>acOp('actions_config_get', {}));
      if (!agentTimelineCurrent(p, timeline) || seq !== timeline.seq) return;
      sjConfigure(config);
      timeline.ready = true;
      timeline.date = timeline.date && timeline.date <= sjToday() ? timeline.date : sjToday();
      p.timelineDate = timeline.date;
      timeline.loading = false;
      void agentTimelineRefresh(p);
    } catch (error) {
      if (!agentTimelineCurrent(p, timeline) || seq !== timeline.seq) return;
      timeline.loading = false; timeline.error = uiFailure(error, {load: true}); agentTimelinePaint(p);
    }
  };
  root.querySelector('[data-more]').onclick = () => agentTimelineRefresh(p,true);
  root.querySelector('[data-retry]').onclick = () => timeline.ready ? agentTimelineRefresh(p,timeline.moreFailed) : initialize();
  void initialize();
}

function agentTimelineCurrent(p, timeline) {
  return p.timeline === timeline && p.view === 'timeline' && agentPaneViewAllowed(p.workspaceRole, 'timeline') && agpadActive(p, true);
}

function agentTimelinePaint(p) {
  const t = p.timeline;
  if (!t || !agentTimelineCurrent(p, t)) return;
  const input = t.root.querySelector('input');
  input.disabled = !t.ready; input.max = sjToday(); input.value = t.date;
  t.root.querySelector('[data-day="previous"]').disabled = !t.ready;
  t.root.querySelector('[data-day="next"]').disabled = !t.ready || t.date >= sjToday();
  t.root.querySelector('[data-day="today"]').hidden = !t.ready || t.date === sjToday();
  t.root.querySelector('[data-latest]').disabled = !t.ready || t.loading;
  t.root.querySelector('[data-zone]').textContent = t.ready ? SJ_ZONE : '';
  t.root.querySelector('[data-zone]').title = 'Times use the installation timezone. Newest first.';
  t.root.querySelector('[data-status]').textContent = t.rows === null
    ? (t.loading ? 'Loading actions…' : '')
    : t.rows.length ? `${t.rows.length} recorded action${t.rows.length === 1 ? '' : 's'}`
      : 'No actions recorded for this day.';
  t.root.querySelector('.agent-timeline-empty').hidden = t.rows === null || t.rows.length > 0 || !!t.error;
  t.root.querySelector('.agent-timeline-error').hidden = !t.error;
  t.root.querySelector('[role="alert"]').textContent = t.error;
  t.root.querySelector('[data-retry]').disabled = t.loading;
  const more=t.root.querySelector('[data-more]');more.hidden=!t.cursor;more.disabled=t.loading;more.textContent=t.loading?'Loading…':'Load more';
  if (t.rows !== null) {
    const signature = JSON.stringify(t.rows);
    if (signature !== t.signature) {
      // Keep evidence open when new activity arrives as well as on unchanged polls.
      const expanded = new Set([...t.root.querySelectorAll('details[open]')].map(detail => detail.closest('[data-action-id]').dataset.actionId).filter(Boolean));
      t.root.querySelector('ol').innerHTML = t.rows.map(agentTimelineRow).join('');
      t.root.querySelectorAll('details').forEach(detail => { detail.open = expanded.has(detail.closest('[data-action-id]').dataset.actionId); });
      t.signature = signature;
    }
  }
}

async function agentTimelineRefresh(p, more=false) {
  const t=p.timeline;
  if(!t?.ready || t.loading || !agentTimelineCurrent(p,t) || !agpadActive(p) || more && !t.cursor)return;
  const seq=++t.seq,date=t.date,cursor=more?t.cursor:null;
  const key='timeline:'+JSON.stringify([p.agentId,date]);
  const cached=uiReads.peek(key);
  if(!more && t.rows===null && cached){t.rows=cached.actions;t.cursor=cached.next_cursor;}
  t.loading=true;t.error='';t.moreFailed=more;agentTimelinePaint(p);
  try {
    const read=()=>acOp('actions_list',{performer:p.agentId,since:date,until:date,limit:50,...(cursor?{cursor}:{})});
    const first=more?await read():await uiReads.read(key,read);
    const data={...first,actions:[...first.actions]}, visited=new Set(cursor?[cursor]:[]);
    const depth=more?1:Math.max(1,t.cursors.size+1);
    let loaded=1;
    while(!more && data.next_cursor && loaded<depth){
      if(!agentTimelineCurrent(p,t)||seq!==t.seq)return;
      if(visited.has(data.next_cursor))throw Error('Action pagination did not advance.');
      visited.add(data.next_cursor);
      const page=await acOp('actions_list',{performer:p.agentId,since:date,until:date,limit:50,cursor:data.next_cursor});
      data.actions.push(...page.actions);data.next_cursor=page.next_cursor;loaded++;
    }
    if(!agentTimelineCurrent(p,t)||seq!==t.seq)return;
    if(!Array.isArray(data.actions))throw Error('Actions unavailable.');
    if(data.next_cursor && (data.next_cursor===cursor || visited.has(data.next_cursor) || more && t.cursors.has(data.next_cursor)))throw Error('Action pagination did not advance.');
    if(!more)t.cursors=visited;
    if(cursor)t.cursors.add(cursor);
    const rows=more?[...t.rows,...data.actions]:data.actions;
    t.rows=[...new Map(rows.map(row=>[row.action_id,row])).values()];t.cursor=data.next_cursor;
  } catch(error) {
    if(!agentTimelineCurrent(p,t)||seq!==t.seq)return;
    t.error=uiFailure(error,{load:true});
  } finally {
    if(agentTimelineCurrent(p,t)&&seq===t.seq){t.loading=false;t.next=Date.now()+AGPAD_POLL_MS;agentTimelinePaint(p);}
  }
}

function agentTimelineTick(p) {
  if (p.timeline?.ready && !p.timeline.loading && Date.now() >= p.timeline.next) void agentTimelineRefresh(p);
}
