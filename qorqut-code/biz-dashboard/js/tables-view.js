// Read-only owner table page for /tables/view?table_id=<uuid>[&business_id=<uuid>]. It runs alone (no application shell) and
// reads through the same owner operations as the Tables screen: tables_get, then 50-row tables_rows_list pages.
(() => {
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const PAGE = 50;
  const $ = id => document.getElementById(id);
  const status = $('tv-status'), wrap = $('tv-wrap'), foot = $('tv-foot'), more = $('tv-more'), count = $('tv-count');
  const fail = text => { status.textContent = text; status.hidden = false; wrap.hidden = true; foot.hidden = true; };

  const query = new URLSearchParams(location.search), keys = [...query.keys()];
  const tableId = query.get('table_id'), businessId = query.get('business_id');
  if (new Set(keys).size !== keys.length || keys.some(key => key !== 'table_id' && key !== 'business_id')
    || !UUID.test(tableId || '') || (businessId !== null && !UUID.test(businessId))) return fail('This table link is not valid.');

  const call = async (path, body) => {
    let response;
    try { response = await fetch('/api' + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }
    catch (_) { throw new Error('The table could not be reached.'); }
    let payload = null;
    try { payload = await response.json(); } catch (_) { /* reported below */ }
    if (response.status === 404) throw new Error('This table was not found.');
    if (response.status === 403) throw new Error('Only the owner can open this table.');
    if (!response.ok || !payload || payload.status !== 'ok' || !payload.data) throw new Error('The table could not be loaded.');
    return payload.data;
  };
  // Business operations route by slug; Global omits business_id and uses the root scope. Never /biz/null.
  const scope = async () => {
    if (businessId === null) return { path: '/root/operations/', extra: { scope: 'global' } };
    let response;
    try { response = await fetch('/api/biz'); } catch (_) { throw new Error('The table could not be reached.'); }
    const list = response.ok ? await response.json() : null;
    const business = Array.isArray(list) ? list.find(entry => entry && entry.id === businessId) : null;
    if (!business || typeof business.slug !== 'string' || !business.slug) throw new Error('This table was not found.');
    return { path: `/biz/${encodeURIComponent(business.slug)}/operations/`, extra: {} };
  };
  // A CRM row: the canonical record link needs an explicit valid scope (Global or a Project UUID) and is built only by
  // openTaskHref/safeLinkUrl; an unavailable entry is plain text with no link and no values.
  const crmRow = (tr, row) => {
    const td = () => tr.appendChild(document.createElement('td'));
    if (row.unavailable || !row.record || !row.record_ref) {
      const first = td(); first.textContent = 'Unavailable record';
      for (let i = 0; i < tableColumns + 2; i++) td();
      return;
    }
    const ref = row.record_ref, project = ref.business_id == null ? null : ref.business_id;
    const href = typeof ref.id === 'string' && (project === null || UUID.test(String(project))) ? safeLinkUrl(openTaskHref(ref.id, project)) : null;
    const name = cell(row.record.title || row.record.name || ref.id);
    const first = td();
    if (href) { const a = document.createElement('a'); a.href = href; a.textContent = name; first.appendChild(a); } else first.textContent = name;
    for (const column of tableColumnsList) {
      const value = row.cells && Object.hasOwn(row.cells, column.id) ? row.cells[column.id] : null;
      td().textContent = cell(value);
    }
    td().textContent = stagesMap.get(row.stage_id) || '';
    td().textContent = cell(row.record.status);
  };
  let tableColumnsList = [], tableColumns = 0, stagesMap = new Map();
  const cell = value => value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);

  (async () => {
    try {
      const { path, extra } = await scope();
      const table = (await call(path + 'tables_get', { ...extra, table_id: tableId })).table;
      if (!table || !Array.isArray(table.columns)) throw new Error('The table could not be loaded.');
      document.title = table.title;
      $('tv-title').textContent = table.title;
      $('tv-note').textContent = table.archived ? 'Archived · read only' : 'Read only';
      wrap.querySelector('thead').innerHTML = '';
      const head = document.createElement('tr');
      const crm = table.table_kind === 'crm';
      tableColumnsList = table.columns; tableColumns = table.columns.length;
      stagesMap = new Map(((table.crm_config && table.crm_config.stages) || []).map(stage => [stage.id, stage.name]));
      const labels = crm ? ['Record', ...table.columns.map(column => column.name), 'Stage', 'Task state'] : table.columns.map(column => column.name);
      for (const label of labels) { const th = document.createElement('th'); th.scope = 'col'; th.textContent = label; head.appendChild(th); }
      wrap.querySelector('thead').appendChild(head);
      const body = wrap.querySelector('tbody');
      let cursor = null, total = 0, loading = false;
      const load = async () => {
        if (loading) return;
        loading = true; more.disabled = true;
        try {
          const page = await call(path + (crm ? 'crm_entries_list' : 'tables_rows_list'), { ...extra, table_id: tableId, limit: PAGE, ...(cursor ? { cursor } : {}) });
          for (const row of page.items) {
            const tr = document.createElement('tr');
            if (crm) { crmRow(tr, row); body.appendChild(tr); continue; }
            for (const column of table.columns) { const td = document.createElement('td'); td.textContent = cell(row.cells && Object.hasOwn(row.cells, column.id) ? row.cells[column.id] : null); tr.appendChild(td); }
            body.appendChild(tr);
          }
          total += page.items.length;
          cursor = page.next_cursor || null;
          status.hidden = total > 0;
          status.textContent = total > 0 ? '' : 'This table has no rows.';
          wrap.hidden = total === 0; foot.hidden = total === 0;
          count.textContent = total === 1 ? '1 row' : `${total} rows`;
          more.hidden = !cursor;
        } catch (error) {
          if (total === 0) fail(error.message); else { count.textContent = error.message; }
        } finally { loading = false; more.disabled = false; }
      };
      more.onclick = load;
      await load();
    } catch (error) { fail(error.message); }
  })();
})();
