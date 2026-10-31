// Owner-only inspection of the same scoped handoff agents read. Drafts stay in RAM.
const workingMemoryDrafts = new Map();
let workingMemoryGeneration = 0;
window.addEventListener('beforeunload', event => {
  if ([...workingMemoryDrafts.values()].some(d => d.dirty || d.pending || d.saving)) {
    event.preventDefault(); event.returnValue = '';
  }
});

async function workingMemoryMount(host, agentId, business, {globalAgent = false} = {}) {
  const generation = ++workingMemoryGeneration;
  const active = () => generation === workingMemoryGeneration && host.isConnected;
  if (!agentId) { host.textContent = 'Registered agent identity unavailable.'; return; }
  const key = JSON.stringify([agentId, business || null]);
  const endpoint = business ? `/biz/${encodeURIComponent(business)}/operations/` : '/root/operations/';
  const call = (operation, args = {}) => post(endpoint + 'working_memory_' + operation, {agent_id: agentId, ...args}).then(coreData);
  let draft = workingMemoryDrafts.get(key);
  host.innerHTML = '<p role="status">Loading working memory…</p>';
  let data;
  try { data = await call('get', {history: true}); }
  catch (error) {
    if (!active()) return;
    // Keep a checked head available without exposing drifted/revoked prose.
    try { data = await call('get', {metadata_only: true, history: true}); data.unavailable = error.message; }
    catch (metadataError) { host.innerHTML = `<p role="alert">${esc(metadataError.message)}</p>`; return; }
  }
  if (!active()) return;
  const latest = data.document;
  if (!draft || !draft.dirty && !draft.pending && !draft.saving) {
    draft = {head: latest, body: latest?.body || '', sources: latest?.sources || [], dirty: false, pending: null, saving: false, stale: false};
    workingMemoryDrafts.set(key, draft);
  }
  const stale = Boolean(draft.head && latest && (draft.head.revision !== latest.revision || draft.head.content_hash !== latest.content_hash));
  host.innerHTML = `<section class="wm-editor" aria-label="Working memory editor">
    <h3>Working memory</h3><p>Agent-authored interpretation, not verified business truth. Private to this agent and the owner; no inbox acknowledgement.</p>
    <div class="wm-toolbar">${globalAgent ? `<label>Scope<select class="wm-scope"><option value="">Global</option>${(state.bizs || []).map(b => `<option value="${esc(b.slug)}">${esc(b.name || b.slug)}</option>`).join('')}</select></label>` : `<p>Business: ${esc(business)}</p>`}
    <p class="wm-revision">${latest ? `Revision ${esc(latest.revision)} · ${esc(latest.updated_at || 'retained metadata')}` : 'No handoff saved in this scope. The agent can establish one after reviewing its legacy plan.'}</p>
    </div>
    ${data.unavailable ? `<p role="alert">Body unavailable: ${esc(data.unavailable)}. Inspect history or use the checked owner-repair workflow.</p>` : ''}
    ${stale ? '<p role="alert">A newer version exists. Your draft is preserved; copy useful edits before discarding and reloading.</p>' : ''}
    ${latest ? `<label>Handoff<textarea class="wm-body" rows="15" maxlength="4000">${esc(draft.body)}</textarea></label>
      <p class="wm-count"></p><p class="wm-evidence">${esc(draft.sources.length)} evidence links retained. Correction does not mark sources reviewed.</p>
      <div class="wm-actions"><button class="wm-save primary">Save correction</button><button class="wm-reload">Discard draft and reload</button></div>` : ''}
    <p class="wm-status" role="status"></p><p class="wm-error" role="alert"></p>
    <details class="wm-history"><summary>Version history</summary><div class="wm-versions"></div><button class="wm-more" hidden>Older versions</button><pre class="wm-preview"></pre><button class="wm-restore" hidden>Use this version as a draft</button></details>
    ${latest ? '<button class="wm-legacy">Inspect original imported plan</button><pre class="wm-legacy-body"></pre>' : ''}
  </section>`;
  const scope = host.querySelector('.wm-scope');
  if (scope) {
    scope.value = business || '';
    scope.onchange = () => workingMemoryMount(host, agentId, scope.value || null, {globalAgent: true});
  }
  const body = host.querySelector('.wm-body'), saveButton = host.querySelector('.wm-save');
  const sync = () => {
    if (!active()) return;
    const locked = draft.saving || Boolean(draft.pending);
    if (body) body.disabled = locked || Boolean(data.unavailable);
    if (saveButton) {
      saveButton.disabled = draft.saving || !draft.pending && (stale || draft.stale || !draft.dirty || Boolean(data.unavailable));
      saveButton.textContent = draft.pending && !draft.saving ? 'Retry same save' : 'Save correction';
      host.querySelector('.wm-reload').disabled = locked;
      host.querySelector('.wm-count').textContent = `${draft.body.length} / 4000 characters`;
    }
    host.querySelector('.wm-status').textContent = draft.saving ? 'Saving…' : draft.pending ? 'Save unconfirmed. Retry the same request.' : draft.dirty ? 'Unsaved draft retained in this page.' : '';
    host.querySelector('.wm-error').textContent = draft.error || '';
    const restore = host.querySelector('.wm-restore');
    restore.disabled = locked || stale || draft.stale || Boolean(data.unavailable);
  };
  draft.notify = sync;
  draft.refresh = () => { if (active()) workingMemoryMount(host, agentId, business, {globalAgent}); };
  if (body) body.oninput = () => { draft.body = body.value; draft.dirty = true; sync(); };
  if (saveButton) {
    host.querySelector('.wm-reload').onclick = () => { workingMemoryDrafts.delete(key); workingMemoryMount(host, agentId, business, {globalAgent}); };
    saveButton.onclick = async () => {
      if (saveButton.disabled || draft.saving) return;
      draft.pending ||= {body: draft.body, sources: draft.sources, expected_revision: draft.head.revision,
                         expected_content_hash: draft.head.content_hash, idempotency_key: newRequestId()};
      draft.saving = true; draft.error = ''; sync();
      try {
        await call('update', draft.pending);
        workingMemoryDrafts.delete(key);
        draft.pending = null; draft.dirty = false; draft.saving = false;
        draft.refresh();
      } catch (error) {
        if (error.code && error.code !== 'invalid_response') draft.pending = null;
        draft.error = error.message;
        if (error.code === 'revision_conflict') { draft.stale = true; draft.error += ' Discard and reload the current version after copying your edits.'; }
      } finally { draft.saving = false; draft.notify(); }
    };
  }
  let cursor = data.history_before, preview = null, historyRequest = 0;
  const versions = host.querySelector('.wm-versions'), more = host.querySelector('.wm-more');
  const appendVersions = items => {
    for (const item of items || []) {
      const button = document.createElement('button'); button.type = 'button';
      button.textContent = `Version ${item.version} · ${item.created_at}`;
      button.onclick = async () => {
        const request = ++historyRequest;
        try {
          const result = await call('get', {version: item.version});
          if (!active() || request !== historyRequest) return;
          preview = result.document;
          host.querySelector('.wm-preview').textContent = preview.body;
          host.querySelector('.wm-restore').hidden = false;
          sync();
        } catch (error) { if (active()) { draft.error = error.message; sync(); } }
      };
      versions.appendChild(button);
    }
    more.hidden = !cursor;
  };
  appendVersions(data.history);
  more.onclick = async () => {
    more.disabled = true;
    try {
      const page = await call('get', {metadata_only: true, history: true, history_before: cursor});
      if (!active()) return;
      cursor = page.history_before; appendVersions(page.history);
    } catch (error) { if (active()) { draft.error = error.message; sync(); } }
    finally { if (active()) more.disabled = false; }
  };
  host.querySelector('.wm-restore').onclick = () => {
    if (!preview || !body || draft.pending || draft.saving || stale || data.unavailable) return;
    draft.body = preview.body; draft.sources = preview.sources; draft.dirty = true;
    body.value = draft.body;
    host.querySelector('.wm-evidence').textContent = `${draft.sources.length} historical evidence links retained; saving rechecks current access.`;
    sync();
  };
  const legacy = host.querySelector('.wm-legacy');
  if (legacy) legacy.onclick = async () => {
    try {
      const result = await call('get', {legacy_plan: true});
      if (active()) host.querySelector('.wm-legacy-body').textContent = result.legacy_plan?.body || 'No legacy plan was imported.';
    } catch (error) { if (active()) { draft.error = error.message; sync(); } }
  };
  sync();
}
