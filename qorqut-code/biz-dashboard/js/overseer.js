// Owner-only read views. Decisions remain in the existing Signoff controller.
T_VIEW_LABEL.changes = 'Changes';
T_VIEW_ICON.changes = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>';

function overseerMount(p) {
  const host = p.el.querySelector('#agpad-body');
  if (!host) return;
  const root = document.createElement('section'); root.className = 'overseer'; host.append(root);
  let disposed = false, generation = 0, signoff = null, selected = null;
  const read = async path => coreData(await api('/overseer' + path));
  const e = value => esc(value == null ? '' : String(value));
  const json = value => e(JSON.stringify(value, null, 2));
  const pageNote = page => `<p class="overseer-notice">${e(page?.availability || 'unavailable')}${page?.complete === false ? ' · Incomplete' : ''}</p>${(page?.gaps || []).map(g => `<p class="overseer-notice">${e(g.code)} ${e(g.detail || '')}</p>`).join('')}`;
  const discussion = page => `${pageNote(page)}${(page?.items || []).map(x => `<p><strong>${e(x.actor?.label || 'Unknown')}</strong> · ${e(x.kind)} · ${e(x.at)}\n${e(x.availability === 'available' ? x.text : 'Unavailable')}</p>`).join('')}`;
  const stopSignoff = () => { signoff?.dispose(); signoff = null; };
  function shell(tab, title) {
    stopSignoff(); root.innerHTML = `<nav aria-label="Changes"><button data-tab="batches" aria-pressed="${tab === 'batches'}">Review batches</button><button data-tab="deltas" aria-pressed="${tab === 'deltas'}">Deltas</button><button data-tab="queue" aria-pressed="${tab === 'queue'}">Accepted queue</button><button data-tab="applications" aria-pressed="${tab === 'applications'}">Application history</button></nav><div class="overseer-content"><h2>${e(title)}</h2><p>Loading…</p></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => list(b.dataset.tab));
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
    const business = ref.review_scope?.kind === 'business' ? (state.bizs || []).find(b => (b.id || b.business_id) === ref.review_scope.business_id) : null;
    if (ref.review_scope?.kind === 'business' && !business?.slug) { box.insertAdjacentHTML('beforeend', '<p>Signoff workspace unavailable.</p>'); return; }
    const node = document.createElement('div'); box.append(node);
    signoff = sgCreateView({active: () => !disposed && node.isConnected, body: () => node, inline: true, execution: true, waitingOnly: false, global: !business,
      workspace: () => business?.slug || 'scope:global', item: () => ({id:ref.signoff_id,businessId:business ? ref.review_scope.business_id : null}), expectedBinding: ref, readOnly: readonly});
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
  p.workspaceDispose = () => { disposed = true; generation++; stopSignoff(); };
  p.overseerOpenDelta = id => delta(id);
  run('batches', 'Changes', () => read('/overview'), (box, data) => { if (data.latest_batch_id) batch(data.latest_batch_id); else { box.innerHTML = `<h2>Changes</h2><p>${data.availability === 'available' ? 'No review batches yet.' : 'Review batches unavailable.'}</p>${pageNote(data)}`; } });
}
window.overseerOpenDelta = async id => {
  try {
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
