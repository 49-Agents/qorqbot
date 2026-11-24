// Owner-only read views. Decisions remain in the existing Signoff controller.
T_VIEW_LABEL.changes = 'Changes';
T_VIEW_ICON.changes = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>';

function overseerMount(p) {
  const host = p.el.querySelector('#agpad-body');
  if (!host) return;
  const root = document.createElement('section'); root.className = 'overseer'; host.append(root);
  let disposed = false, generation = 0, signoff = null, thread = null, selected = null;
  const read = async path => coreData(await api('/overseer' + path));
  const e = value => esc(value == null ? '' : String(value));
  const json = value => e(JSON.stringify(value, null, 2));
  const pageNote = page => `<p class="overseer-notice">${e(page?.availability || 'unavailable')}${page?.complete === false ? ' · Incomplete' : ''}</p>${(page?.gaps || []).map(g => `<p class="overseer-notice">${e(g.code)} ${e(g.detail || '')}</p>`).join('')}`;
  const discussion = page => `${pageNote(page)}${(page?.items || []).map(x => `<p><strong>${e(x.actor?.label || 'Unknown')}</strong> · ${e(x.kind)} · ${e(x.at)}\n${e(x.availability === 'available' ? x.text : 'Unavailable')}</p>`).join('')}`;
  const stopSignoff = () => { signoff?.dispose(); signoff = null; thread?.dispose?.(); thread = null; };
  // The two workflows first (delta proposals, the observations they come from, the runs), then the earlier review views.
  const TABS = [['proposals', 'Delta proposals'], ['clusters', 'Observations'], ['runs', 'Runs'], ['batches', 'Review batches'], ['deltas', 'Deltas'], ['queue', 'Accepted queue'], ['applications', 'Application history']];
  const openTab = tab => tab === 'proposals' ? proposals() : tab === 'clusters' ? clusters() : tab === 'runs' ? runs() : list(tab);
  function shell(tab, title) {
    stopSignoff(); root.innerHTML = `<nav aria-label="Changes">${TABS.map(([id, label]) => `<button data-tab="${id}" aria-pressed="${tab === id}">${label}</button>`).join('')}</nav><div class="overseer-content"><h2>${e(title)}</h2><p>Loading…</p></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => openTab(b.dataset.tab));
    return root.querySelector('.overseer-content');
  }
  function run(tab, title, loader, paint) {
    const stamp = ++generation, box = shell(tab, title);
    Promise.resolve().then(loader).then(data => { if (!disposed && stamp === generation) paint(box, data); }).catch(error => { if (!disposed && stamp === generation) box.innerHTML = `<h2>${e(title)}</h2><p role="alert">${e(error.message || 'Unavailable')}</p>`; });
  }
  const button = (label, action) => { const b = document.createElement('button'); b.type = 'button'; b.className = 'overseer-item'; b.textContent = label; b.onclick = action; return b; };
  function paged(box, path, draw, first) {
    const area = document.createElement('div'); box.append(area);
    let page = first;
    const append = data => {
      page = data; area.insertAdjacentHTML('beforeend', pageNote(data));
      (data.items || []).forEach(item => draw(area, item));
      area.querySelector('[data-more]')?.remove();
      if (data.next_cursor) { const more = button('Load more', async () => { more.disabled = true; try { append(await read(path + (path.includes('?') ? '&' : '?') + 'cursor=' + encodeURIComponent(data.next_cursor))); } catch (err) { more.disabled = false; more.after(document.createTextNode(err.message)); } }); more.dataset.more = 'true'; area.append(more); }
    };
    if (first) append(first); else read(path).then(append).catch(err => { area.textContent = err.message; });
  }
  function list(kind) {
    selected = {kind};
    const path = kind === 'queue' ? '/applications?queue=unimplemented' : '/' + kind;
    const title = {batches:'Review batches', deltas:'Deltas', queue:'Accepted queue', applications:'Application history'}[kind];
    run(kind, title, () => read(path), (box, page) => {
      box.innerHTML = `<h2>${e(title)}</h2>`;
      if (kind === 'deltas' || kind === 'queue' || kind === 'applications') {
        const search = document.createElement('input'); search.type = 'search'; search.placeholder = 'Filter loaded items'; search.setAttribute('aria-label', 'Filter loaded items');
        search.oninput = () => box.querySelectorAll('.overseer-item').forEach(b => { if (!b.dataset.more) b.hidden = !b.textContent.toLowerCase().includes(search.value.toLowerCase()); }); box.append(search);
      }
      if (!page.items?.length) box.insertAdjacentHTML('beforeend', '<p>No items available.</p>');
      paged(box, path, (area, item) => {
        if (kind === 'batches') area.append(button(`${item.created_at} · ${item.item_count} changes`, () => batch(item.id)));
        else if (kind === 'deltas') area.append(button(`${item.summary} · ${item.decision} · revision ${item.revision}`, () => delta(item.id)));
        else area.append(button(`${item.summary} · ${item.phase} · checks ${item.checks?.state || 'unknown'}`, () => application(item.id)));
      }, page);
    });
  }
  function batch(id) {
    selected = {kind:'batch', id};
    run('batches', 'Review batch', () => read('/batches/' + encodeURIComponent(id)), (box, data) => {
      box.innerHTML = `<h2>Review batch</h2><p>${e(data.batch.created_at)}</p><div class="overseer-list"></div><h3>Discussion</h3>`;
      const items = box.querySelector('.overseer-list');
      (data.items || []).forEach(item => {
        const text = item.detail ? `${item.detail.summary} · revision ${item.revision} · current decision ${item.detail.decision}\n${item.ranking_reason}` : `Unavailable change · revision ${item.revision}\n${item.ranking_reason}`;
        const b = button(text, () => delta(item.delta_id, item.revision)); b.disabled = !item.detail || item.availability !== 'available'; items.append(b);
      });
      paged(box, `/batches/${encodeURIComponent(id)}/discussion`, (area, item) => area.insertAdjacentHTML('beforeend', discussion({availability:'available',items:[item]})), data.discussion);
    });
  }
  function signoffMount(box, ref, readonly) {
    if (!ref?.signoff_id) return;
    const project = ref.review_scope?.kind === 'project' ? (state.projects || []).find(b => (b.id || b.project_id) === ref.review_scope.project_id) : null;
    if (ref.review_scope?.kind === 'project' && !project?.slug) { box.insertAdjacentHTML('beforeend', '<p>Signoff workspace unavailable.</p>'); return; }
    const node = document.createElement('div'); box.append(node);
    signoff = sgCreateView({active: () => !disposed && node.isConnected, body: () => node, inline: true, execution: true, waitingOnly: false, global: !project,
      workspace: () => project?.slug || 'scope:global', item: () => ({id:ref.signoff_id,projectId:project ? ref.review_scope.project_id : null}), expectedBinding: ref, readOnly: readonly});
    signoff.paint();
  }
  function delta(id, revision) {
    selected = {kind:'delta', id, revision};
    const base = '/deltas/' + encodeURIComponent(id), path = base + (revision ? '?revision=' + encodeURIComponent(revision) : '');
    run('deltas', 'Change', () => read(path), (box, d) => {
      const v = d.delta, s = d.support || {};
      box.innerHTML = `<h2>${e(v.summary)}</h2><p>Displayed revision ${e(d.revision)}${d.historical ? ' · Historical' : ''} · Current revision ${e(d.current_revision)}</p><p>Current decision: ${e(d.decision)} · Accepted revision: ${e(d.accepted_revision ?? 'none')}</p><p>${e(v.change_direction)}</p><p>Scope: ${e(v.scope?.description)}</p><details><summary>Scope and targets</summary><pre>${json(v.scope)}\n${json(v.target_refs || [])}</pre></details><p>Ranking: ${e(v.ranking_reason)}</p><p>Support: ${e(s.proposal_count)} proposals · ${e(s.event_count)} events · ${e(s.episode_count)} episodes</p><p>Uncertainties: ${e((v.uncertainties || []).join('; ') || 'None recorded')}</p><div data-current></div><h3>Proposals and evidence</h3><div data-proposals></div><div data-evidence></div><h3>Decision history</h3><div data-decisions></div><h3>Versions</h3><div data-versions></div><h3>Discussion</h3><div data-discussion></div><div data-application></div><h3>Owner input</h3><div data-signoff></div>`;
      if (d.historical) box.querySelector('[data-current]').append(button('Open current change', () => delta(id)));
      for (const proposal of d.proposals || []) box.querySelector('[data-proposals]').insertAdjacentHTML('beforeend', `<details><summary>${e(proposal.summary)} · ${e(proposal.origin_type)}</summary><p>Observed: ${e(proposal.observed_fact)}</p><p>Interpretation: ${e(proposal.interpretation)}</p><p>Scope: ${e(proposal.scope?.description)}</p></details>`);
      for (const item of d.evidence || []) {
        const area = box.querySelector('[data-evidence]'), block = document.createElement('details');
        block.innerHTML = `<summary>${e(item.role)} · ${e(item.ref?.kind)} · ${e(item.availability)}</summary><p>${e(item.availability === 'available' ? item.excerpt || '' : 'Evidence unavailable')}</p><div></div>`; area.append(block);
        if (item.availability === 'available') block.ontoggle = async () => {
          if (!block.open || block.dataset.loaded || block.dataset.loading) return;
          block.dataset.loading = '1';
          const label = state => `${item.role || ''} · ${item.ref?.kind || ''} · ${state}`;
          try {
            const content = await read(`${base}/evidence/${encodeURIComponent(item.proposal_id)}/${encodeURIComponent(item.source_index)}?revision=${encodeURIComponent(d.revision)}`);
            block.querySelector('p').textContent = content.availability === 'available' ? '' : 'Evidence unavailable';
            block.querySelector('summary').textContent = label(content.availability);
            block.querySelector('div').innerHTML = content.availability === 'available' ? `<pre>${json(content.content)}</pre>${pageNote(content)}` : `<p>Evidence unavailable</p>${pageNote(content)}`;
            if (content.availability === 'available') block.dataset.loaded = '1';
          } catch (err) {
            block.querySelector('summary').textContent = label('unavailable');
            block.querySelector('p').textContent = 'Evidence unavailable';
            block.querySelector('div').textContent = `${err.message || 'Source read failed'}. Close and reopen to retry.`;
          } finally { delete block.dataset.loading; }
        };
      }
      const decisionArea = box.querySelector('[data-decisions]');
      paged(decisionArea, base + '/decisions', (area, x) => area.insertAdjacentHTML('beforeend', `<p>${e(x.purpose)} · ${e(x.at)} · targets ${json(x.targets)}\nOwner: ${e(x.notes)}\nChoices: ${e((x.selected_choice_ids || []).join(', '))}\nRequest: ${json(x.request_ref)}\nResponse: ${json(x.response_ref)}\nInterpretation: ${json(x.interpretation)}\nResult: ${json(x.result)}</p>`), d.decision_history);
      paged(box.querySelector('[data-versions]'), base + '/revisions', (area, x) => area.append(button(`Revision ${x.revision} · ${x.summary}`, () => delta(id, x.revision))), d.versions);
      paged(box.querySelector('[data-discussion]'), base + '/discussion', (area, x) => area.insertAdjacentHTML('beforeend', discussion({availability:'available', items:[x]})), d.discussion);
      if (d.application) box.querySelector('[data-application]').append(button(`Application · ${d.application.phase} · checks ${d.application.checks?.state || 'unknown'}`, () => application(d.application.id)));
      signoffMount(box.querySelector('[data-signoff]'), d.review_input, d.historical || d.review_input?.target_revision !== d.revision || !d.available_actions?.includes('open_signoff'));
    });
  }
  function application(id, planRevision) {
    selected = {kind:'application', id, planRevision};
    const base = '/applications/' + encodeURIComponent(id), path = base + (planRevision ? '?plan_revision=' + encodeURIComponent(planRevision) : '');
    run('applications', 'Application', () => read(path), (box, d) => {
      const a = d.application;
      box.innerHTML = `<h2>${e(a.summary)}</h2><p>Phase: ${e(a.phase)} · Accepted revision: ${e(a.accepted_revision)}</p><p>Merge: ${a.merge ? e(a.merge.merged_commit) : 'Not merged'} · Checks: ${e(a.checks?.state || 'unknown')} · Refresh: ${e(a.refresh?.state || 'unknown')}</p><pre>${json(a.checks?.receipts || [])}</pre><p>External tasks: ${json(a.external_tasks || [])}</p><p>Displayed plan: ${e(d.displayed_plan_revision ?? 'none')}${d.historical ? ' · Historical' : ''} · Current plan: ${e(d.current_plan_revision ?? 'none')}</p><pre>${json(d.plan)}</pre><div data-current></div><h3>Plan history</h3><div data-plans></div><h3>Events</h3><div data-events></div><h3>Discussion</h3><div data-discussion></div><h3>Owner input</h3><div data-signoff></div>`;
      box.querySelector('[data-current]').append(button('Open change', () => delta(a.delta_id)));
      if (d.historical) box.querySelector('[data-current]').append(button('Open current plan', () => application(id)));
      paged(box.querySelector('[data-plans]'), base + '/plans', (area, x) => area.append(button(`Plan ${x.revision} · ${x.intended_outcome}`, () => application(id,x.revision))), d.plan_history);
      paged(box.querySelector('[data-events]'), base + '/events', (area, x) => area.insertAdjacentHTML('beforeend', `<p>${e(x.at)} · ${e(x.kind)} · ${x.availability === 'available' ? json(x.data) : 'Unavailable'}</p>`), d.events);
      paged(box.querySelector('[data-discussion]'), base + '/discussion', (area, x) => area.insertAdjacentHTML('beforeend', discussion({availability:'available',items:[x]})), d.discussion);
      const input = d.waiting_input;
      const targetMismatch = input?.purpose === 'plan'
        ? !d.plan || d.plan.revision !== d.displayed_plan_revision || d.current_plan_revision !== d.displayed_plan_revision || a.plan_revision !== d.displayed_plan_revision || input.application_id !== a.id || input.application_revision !== a.revision || input.plan_revision !== d.displayed_plan_revision || input.target_revision !== d.plan.delta_revision
        : input?.target_revision != null && input.target_revision !== a.accepted_revision;
      signoffMount(box.querySelector('[data-signoff]'), input, d.historical || !d.available_actions?.includes('open_signoff') || (input?.application_id != null && input.application_id !== a.id) || targetMismatch || (input?.application_revision != null && input.application_revision !== a.revision) || (input?.plan_revision != null && input.plan_revision !== d.displayed_plan_revision));
    });
  }
  // ----- the experience extraction and the review and application: observations in ranked clusters, delta proposals, runs
  const op = async (name, body) => coreData(await post('/root/operations/' + name, {scope: 'global', ...(body || {})}));
  const when = value => value ? String(value).slice(0, 16).replace('T', ' ') + ' UTC' : '';
  const count = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const STATES = {waiting_for_approval: 'Waiting for approval', approved: 'Approved', rejected: 'Rejected'};
  const KINDS = {owner_complaint: 'The owner said', agent_failure: 'An agent did', tool_problem: 'A tool failed', tool_pattern: 'Repeated manual work', other: 'Other'};
  const choices = (box, label, options, current, pick) => {
    const bar = document.createElement('nav'); bar.setAttribute('aria-label', label); box.append(bar);
    for (const [id, text] of options) { const b = document.createElement('button'); b.type = 'button'; b.textContent = text; b.setAttribute('aria-pressed', String(current === id)); b.onclick = () => pick(id); bar.append(b); }
  };
  function more(box, first, key, next, draw) {
    const area = document.createElement('div'); area.className = 'overseer-list'; box.append(area);
    const append = data => {
      (data[key] || []).forEach(item => draw(area, item));
      area.querySelector('[data-more]')?.remove();
      if (data.next_cursor) { const b = button('Load more', async () => { b.disabled = true; try { append(await next(data.next_cursor)); } catch (err) { b.disabled = false; b.after(document.createTextNode(err.message)); } }); b.dataset.more = 'true'; area.append(b); }
    };
    append(first);
  }
  const proposalLine = x => `Delta proposal ${x.number}: ${x.title} · ${STATES[x.state] || x.state}`;
  const observationHtml = o => `<div class="overseer-item"><strong>${e(o.subject)}</strong> · ${e(KINDS[o.kind] || o.kind)} · filed ${e(when(o.filed_at))}\n${e(o.text)}${(o.evidence || []).map(x => `\n<small>${e(x.ref)}${x.quote ? ': “' + e(x.quote) + '”' : ''}</small>`).join('')}${(o.delta_proposals || []).length ? `\n<small>Addressed by ${o.delta_proposals.map(x => 'delta proposal ' + e(x.number) + ' (' + e(STATES[x.state] || x.state) + ')').join(', ')}</small>` : ''}</div>`;
  function proposals(filter) {
    selected = {kind: 'proposals', filter};
    const args = cursor => ({...(filter ? {state: filter} : {}), ...(cursor ? {cursor} : {})});
    run('proposals', 'Delta proposals', () => op('agentic_delta_proposals_list', args()), (box, first) => {
      const counts = first.counts || {}, total = Object.values(counts).reduce((a, b) => a + b, 0);
      box.innerHTML = '<h2>Delta proposals</h2><p class="overseer-notice">What the Learning Applier proposes to change in the agentic layer, from the observations of its daily experience extraction. You approve or reject each one in its approval request.</p>';
      choices(box, 'State', [['', `All ${total}`], ...Object.entries(STATES).map(([id, label]) => [id, `${label} ${counts[id] ?? 0}`])], filter || '', id => proposals(id || undefined));
      if (!first.proposals?.length) box.insertAdjacentHTML('beforeend', '<p>No delta proposals here.</p>');
      more(box, first, 'proposals', cursor => op('agentic_delta_proposals_list', args(cursor)), (area, x) => area.append(button(
        `${proposalLine(x)}\naddresses ${count(x.addresses.observations, 'observation')} and ${count(x.addresses.clusters, 'cluster')} · filed ${when(x.created_at)}${x.needs_revision ? ' · the Learning Applier has to revise it and ask again' : ''}`, () => proposal(x.id))));
    });
  }
  function proposal(id) {
    selected = {kind: 'proposal', id};
    run('proposals', 'Delta proposal', () => op('agentic_delta_proposal_get', {proposal_id: id}), (box, data) => {
      const x = data.proposal, request = x.approval_request || {};
      box.innerHTML = `<h2>Delta proposal ${e(x.number)}: ${e(x.title)}</h2><p><strong>${e(STATES[x.state] || x.state)}</strong> · filed ${e(when(x.created_at))}${x.decided_at ? ' · decided ' + e(when(x.decided_at)) : ''}${x.needs_revision ? '\nIts approval request was answered in words without a choice, or was withdrawn. The Learning Applier has to revise the proposal and ask again.' : ''}</p><div data-request></div><h3>Thread</h3><p class="overseer-notice">Talk about this proposal here. Ordubashy answers in the thread, and the Learning Applier reads what was said when it revises the proposal.</p><div data-thread></div><details${request.signoff_id ? '' : ' open'}><summary>Problem and proposed change, as recorded</summary><h3>Problem</h3><p>${e(x.problem)}</p><h3>Proposed change</h3><p>${e(x.change)}</p></details><h3>Addresses</h3><div class="overseer-list" data-addresses></div><h3>What was said</h3><div data-said></div>`;
      // The approval request and its thread are shown here, so the owner decides and talks without leaving the proposal.
      if (request.signoff_id) {
        const node = document.createElement('div'); box.querySelector('[data-request]').append(node);
        signoff = sgCreateView({active: () => !disposed && node.isConnected, body: () => node, inline: true, execution: true, waitingOnly: false, global: true,
          workspace: () => 'scope:global', item: () => ({id: request.signoff_id, projectId: null}),
          onResolved: () => { if (!disposed && selected?.kind === 'proposal' && selected.id === id) proposal(id); }});
        signoff.paint();
        if (typeof conversationMount === 'function') {
          thread = conversationMount(box.querySelector('[data-thread]'), {type: 'signoff', id: request.signoff_id, project: null, project_id: null}, {dismissOnOutside: false});
          thread?.expand?.();
        }
      } else box.querySelector('[data-thread]').textContent = 'This proposal has no approval request.';
      const addresses = box.querySelector('[data-addresses]');
      for (const c of x.clusters || []) addresses.append(button(`Cluster: ${c.label}\n${count(c.observations, 'observation')}, ${c.unaddressed} not addressed yet`, () => cluster(c.id)));
      for (const o of x.observations || []) addresses.insertAdjacentHTML('beforeend', observationHtml(o));
      if (!addresses.children.length) addresses.textContent = 'Nothing is linked.';
      const said = box.querySelector('[data-said]');
      for (const r of x.owner || []) {
        const lines = [];
        if (r.decision) lines.push(`<p><strong>Decision</strong> · ${e(when(r.decision.at))}\n${e((r.decision.choices || []).join(', ') || 'no choice, words only')}${r.decision.note ? '\n' + e(r.decision.note) : ''}</p>`);
        const noted = new Set(r.decision?.note ? [r.decision.note] : []);
        for (const note of r.notes || []) if (!noted.has(note.body)) lines.push(`<p><strong>${e(note.by || 'Note')}</strong> · ${e(when(note.created_at))}\n${e(note.body)}</p>`);
        for (const message of r.thread || []) lines.push(`<p><strong>${message.by === 'owner' ? 'You' : 'Ordubashy'}</strong> in the thread · ${e(when(message.created_at))}\n${e(message.body)}</p>`);
        if (lines.length) said.insertAdjacentHTML('beforeend', lines.join(''));
      }
      if (!said.children.length) said.innerHTML = '<p class="overseer-notice">Nothing yet.</p>';
    });
  }
  function clusters(order) {
    selected = {kind: 'clusters', order};
    const args = cursor => ({order: order || 'count', ...(cursor ? {cursor} : {})});
    run('clusters', 'Observations', () => op('experience_clusters_list', args()), (box, first) => {
      box.innerHTML = `<h2>Observations</h2><p class="overseer-notice">Problems the Learning Applier saw in its daily experience extraction, filed under the cluster that states the same problem. ${e(count(first.observations_total ?? 0, 'observation'))} in ${e(count(first.clusters_total ?? 0, 'cluster'))}.</p>`;
      choices(box, 'Order', [['count', 'Most filed first'], ['unaddressed', 'Most not addressed first'], ['recent', 'Latest first']], order || 'count', id => clusters(id));
      if (!first.clusters?.length) box.insertAdjacentHTML('beforeend', '<p>No observations have been filed yet.</p>');
      more(box, first, 'clusters', cursor => op('experience_clusters_list', args(cursor)), (area, c) => area.append(button(
        `${c.rank}. ${c.label}\n${count(c.observations, 'observation')} · ${c.observations_last_30_days} in the last 30 days · ${c.unaddressed} not addressed yet${(c.delta_proposals || []).length ? ' · ' + c.delta_proposals.map(x => 'delta proposal ' + x.number + ' (' + (STATES[x.state] || x.state).toLowerCase() + ')').join(', ') : ''}`, () => cluster(c.id))));
    });
  }
  function cluster(id) {
    selected = {kind: 'cluster', id};
    run('clusters', 'Cluster', () => op('experience_cluster_get', {cluster_id: id}), (box, first) => {
      const c = first.cluster;
      box.innerHTML = `<h2>${e(c.label)}</h2><p>${e(c.description || '')}</p><p>${e(count(c.observations, 'observation'))} · ${e(c.observations_last_30_days)} in the last 30 days · ${e(c.unaddressed)} not addressed yet · first filed ${e(when(c.first_filed_at))}, last ${e(when(c.last_filed_at))}</p><div class="overseer-list" data-proposals></div><h3>Observations</h3>`;
      for (const x of c.delta_proposals || []) box.querySelector('[data-proposals]').append(button(proposalLine(x), () => proposal(x.id)));
      more(box, first, 'observations', cursor => op('experience_cluster_get', {cluster_id: id, cursor}), (area, o) => area.insertAdjacentHTML('beforeend', observationHtml(o)));
    });
  }
  function runs() {
    selected = {kind: 'runs'};
    run('runs', 'Runs', () => op('experience_run_get'), (box, data) => {
      const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'], RUN_STATES = {queued: 'waiting', running: 'running', done: 'finished', abandoned: 'given up'};
      // The two usual shapes of a schedule in words; any other expression is shown as written.
      const at = cron => { const m = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-7])$/.exec(cron || ''); if (!m) return cron; const time = m[2].padStart(2, '0') + ':' + m[1].padStart(2, '0'); return m[3] === '*' ? `every day at ${time}` : `every ${DAYS[m[3] % 7]} at ${time}`; };
      const line = r => `${e(r.workflow)} · ${e(RUN_STATES[r.state] || r.state)} · due ${e(when(r.scheduled_for))}${r.started_at ? ' · started ' + e(when(r.started_at)) : ''}${r.finished_at ? ' · ended ' + e(when(r.finished_at)) : ''}${r.result?.summary ? '\n' + e(r.result.summary) : ''}${r.result?.reason ? '\n' + e(r.result.reason) : ''}`;
      const schedule = Object.values(data.schedule || {}).map(x => `${e(x.workflow)} ${e(at(x.cron))}`).join(' · ') || 'Both workflows are switched off.';
      box.innerHTML = `<h2>Runs</h2><p class="overseer-notice">Schedule (${e(data.time_zone)}): ${schedule}\nEach run is a fresh conversation of the Learning Applier; one runs at a time.</p><nav aria-label="Run now"></nav><p role="status" data-note></p><h3>Running</h3><div data-running></div><h3>Waiting</h3><div data-waiting></div><h3>Finished</h3><div data-finished></div>`;
      const bar = box.querySelector('nav'), note = box.querySelector('[data-note]');
      for (const [kind, label] of [['extraction', 'Run the experience extraction now'], ['review', 'Run the review and application now']]) {
        const b = document.createElement('button'); b.type = 'button'; b.textContent = label; bar.append(b);
        b.onclick = async () => { b.disabled = true; try { await op('experience_run_request', {kind, idempotency_key: newRequestId()}); if (!disposed && selected?.kind === 'runs') runs(); } catch (err) { b.disabled = false; note.textContent = err.message || 'The run could not be queued.'; } };
      }
      box.querySelector('[data-running]').innerHTML = data.running ? `<p>${line(data.running)}\nFiled so far: ${e(Object.entries(data.running.filed_so_far || {}).map(([k, v]) => v + ' ' + k.replaceAll('_', ' ')).join(', '))}</p>` : '<p class="overseer-notice">Nothing is running.</p>';
      box.querySelector('[data-waiting]').innerHTML = (data.waiting || []).map(r => `<p>${line(r)}</p>`).join('') || '<p class="overseer-notice">Nothing is waiting.</p>';
      box.querySelector('[data-finished]').innerHTML = (data.finished || []).map(r => `<p>${line(r)}</p>`).join('') || '<p class="overseer-notice">No run has finished yet.</p>';
    });
  }
  p.workspaceDispose = () => { disposed = true; generation++; stopSignoff(); };
  p.overseerOpenDelta = id => delta(id);
  proposals();
}
window.overseerOpenDelta = async id => {
  try {
    await testingModeReady();
    if (!testingModeEnabled()) { if (typeof toast === 'function') toast('Overseer is available in testing mode.'); return; }
    let p = typeof _agpad !== 'undefined' && _agpad;
    if (!p || p.workspaceRole !== 'learning-applier') {
      const overview = coreData(await api('/overseer/overview'));
      if (!overview.overseer?.agent_id || typeof globalAgentsNavigate !== 'function') throw Error('Overseer is unavailable.');
      let opened = await globalAgentsNavigate(overview.overseer.agent_id, {view:'changes'});
      if (!opened && typeof selectGlobal === 'function') { await selectGlobal('agents'); opened = await globalAgentsNavigate(overview.overseer.agent_id, {view:'changes'}); }
      if (!opened) throw Error('Overseer is unavailable.');
      p = typeof _agpad !== 'undefined' && _agpad;
    }
    if (!p || p.workspaceRole !== 'learning-applier') throw Error('Overseer is unavailable.');
    if (p.view !== 'changes') await agpadSetView(p, 'changes');
    p.overseerOpenDelta?.(id);
  } catch (error) { if (typeof toast === 'function') toast(error.message || 'Change unavailable.'); }
};
