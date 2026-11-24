// Owner restore of archived projects (Charts in the shared drawer). Restore returns the
// project and its resources; no agent is restored or started.
const projectRestoreKeys = new Map();   // project id -> idempotency key kept until success

function projectRestoreRows(data) {
  const items = Array.isArray(data?.items) ? data.items : Array.isArray(data) ? data : null;
  if (!items) throw new Error('Archived projects are unavailable.');
  return items.map(item => ({...item, id: item.project_id || item.id})).filter(item => item.id);
}

async function projectRestoreMount(host, restored) {
  if (!host) return;
  host.hidden = true;
  if (state.session?.is_owner === false) return;
  let rows;
  try {
    const response = await api('/root/projects/archived');
    // A core without the archived listing offers no restore here.
    if (response?.coreError?.code === 'not_found' || response?.httpStatus === 404) return;
    rows = projectRestoreRows(coreData(response));
  } catch (error) {
    if (!host.isConnected) return;
    host.hidden = false;
    host.innerHTML = `<h2>Archived projects</h2><p role="alert">${esc(uiFailure(error, {load: true}))}</p>`;
    return;
  }
  if (!host.isConnected || !rows.length) return;
  host.hidden = false;
  host.innerHTML = `<h2>Archived projects</h2><ul class="global-archived-list">${rows.map(row => `<li data-archived-project="${esc(row.id)}">
    <span class="global-archived-name"><strong>${esc(row.name || row.slug)}</strong><span class="muted small">${esc(row.slug || '')}${row.archived_at ? ' · archived ' + esc(String(row.archived_at).slice(0, 10)) : ''}</span></span>
    <button class="btn" type="button" data-restore-project="${esc(row.id)}">Restore</button></li>`).join('')}</ul>`;
  host.querySelectorAll('[data-restore-project]').forEach(button => {
    button.onclick = () => projectRestoreConfirm(rows.find(row => row.id === button.dataset.restoreProject), host, restored);
  });
}

function projectRestoreConfirm(row, host, restored) {
  if (!row) return;
  const name = row.name || row.slug;
  let busy = false;
  modal({
    title: `Restore ${name}?`, width: 460,
    body: `<p>${esc(name)} returns to the project menu with its resources.</p>
      <p class="muted">Its saved work is preserved. No agent is restored and no session starts.</p>
      <p class="ferr" id="project-restore-error" role="alert" hidden></p>`,
    actions: [
      { label: 'Cancel', onClick: close => close() },
      { label: 'Restore', primary: true, onClick: async (close, el) => {
        if (busy) return;
        busy = true;
        const error = el.querySelector('#project-restore-error'), button = el.querySelector('.mfoot .primary');
        if (button) button.disabled = true;
        error.hidden = true;
        if (!projectRestoreKeys.has(row.id)) projectRestoreKeys.set(row.id, newRequestId());
        try {
          const data = coreData(await post('/root/project-restore', {project_id: row.id, expected_revision: row.revision,
            idempotency_key: projectRestoreKeys.get(row.id)}));
          projectRestoreKeys.delete(row.id);
          close();
          toast(`${name} restored`);
          await loadProject({preserveNavigation: true});
          if (restored) restored();
          else if (host.isConnected) projectRestoreMount(host);
        } catch (failure) {
          // A changed project needs a fresh read and a new attempt.
          if (failure.code === 'revision_conflict' || failure.code === 'project_not_archived') projectRestoreKeys.delete(row.id);
          error.hidden = false;
          error.textContent = uiFailure(failure);
        } finally {
          busy = false;
          if (button) button.disabled = false;
        }
      } },
    ],
  });
}
