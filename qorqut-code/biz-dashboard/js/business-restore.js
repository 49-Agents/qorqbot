// Owner restore of archived businesses in Global Overview. Restore returns the
// business with exactly one business agent; nothing else is started.
const businessRestoreKeys = new Map();   // business id -> idempotency key kept until success

function businessRestoreRows(data) {
  const items = Array.isArray(data?.items) ? data.items : Array.isArray(data) ? data : null;
  if (!items) throw new Error('Archived businesses are unavailable.');
  return items.map(item => ({...item, id: item.business_id || item.id})).filter(item => item.id);
}

async function businessRestoreMount(host) {
  if (!host) return;
  host.hidden = true;
  if (state.session?.is_owner === false) return;
  let rows;
  try {
    const response = await api('/root/businesses/archived');
    // A core without the archived listing offers no restore here.
    if (response?.coreError?.code === 'not_found' || response?.httpStatus === 404) return;
    rows = businessRestoreRows(coreData(response));
  } catch (error) {
    if (!host.isConnected) return;
    host.hidden = false;
    host.innerHTML = `<h2>Archived businesses</h2><p role="alert">${esc(error.message || 'Archived businesses are unavailable.')}</p>`;
    return;
  }
  if (!host.isConnected || !rows.length) return;
  host.hidden = false;
  host.innerHTML = `<h2>Archived businesses</h2><ul class="global-archived-list">${rows.map(row => `<li data-archived-business="${esc(row.id)}">
    <span class="global-archived-name"><strong>${esc(row.name || row.slug)}</strong><span class="muted small">${esc(row.slug || '')}${row.archived_at ? ' · archived ' + esc(String(row.archived_at).slice(0, 10)) : ''}</span></span>
    <button class="btn" type="button" data-restore-business="${esc(row.id)}">Restore</button></li>`).join('')}</ul>`;
  host.querySelectorAll('[data-restore-business]').forEach(button => {
    button.onclick = () => businessRestoreConfirm(rows.find(row => row.id === button.dataset.restoreBusiness), host);
  });
}

function businessRestoreConfirm(row, host) {
  if (!row) return;
  const name = row.name || row.slug;
  let busy = false;
  modal({
    title: `Restore ${name}?`, width: 460,
    body: `<p>${esc(name)} returns to the business menu with its one business agent, Onbashy.</p>
      <p class="muted">Retired agents stay inactive and no session starts. The agent folder is prepared from its saved profile.</p>
      <p class="ferr" id="business-restore-error" role="alert" hidden></p>`,
    actions: [
      { label: 'Cancel', onClick: close => close() },
      { label: 'Restore', primary: true, onClick: async (close, el) => {
        if (busy) return;
        busy = true;
        const error = el.querySelector('#business-restore-error'), button = el.querySelector('.mfoot .primary');
        if (button) button.disabled = true;
        error.hidden = true;
        if (!businessRestoreKeys.has(row.id)) businessRestoreKeys.set(row.id, newRequestId());
        try {
          const data = coreData(await post('/root/business-restore', {business_id: row.id, expected_revision: row.revision,
            idempotency_key: businessRestoreKeys.get(row.id)}));
          businessRestoreKeys.delete(row.id);
          close();
          toast(data.display_name ? `${name} restored with ${data.display_name}` : `${name} restored`);
          await loadBiz({preserveNavigation: true});
          if (state.scope === 'global' && state.globalTab === 'overview') renderTab();
          else if (host.isConnected) businessRestoreMount(host);
        } catch (failure) {
          // A changed business needs a fresh read and a new attempt.
          if (failure.code === 'revision_conflict' || failure.code === 'business_not_archived') businessRestoreKeys.delete(row.id);
          error.hidden = false;
          error.textContent = failure.message || 'Could not restore this business.';
        } finally {
          busy = false;
          if (button) button.disabled = false;
        }
      } },
    ],
  });
}
