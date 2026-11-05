// Synthetic, in-memory CRM Operations for the CRM browser tests. It follows CONTRACT.md (table_kind/crm_config, membership
// entries, Table threads, recorded activity) with exact Table revisions, idempotency replay and scoped identities. Nothing
// here is real data: every name, address and URL is made up, and no request leaves the test origin.
const crypto = require('node:crypto');

const SCOPES = {global: {business_id: null, slug: null}, alpha: {business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', slug: 'alpha'}};
const FIELD_PATHS = ['title', 'task_type', 'status', 'overview', 'customer_kind', 'customer_state', 'customer_profile.company',
  'customer_profile.contact_name', 'customer_profile.email', 'customer_profile.phone', 'customer_profile.website', 'customer_profile.notes', 'tags'];
const uuid = () => crypto.randomUUID();
const scopeKey = businessId => businessId == null ? 'global' : 'alpha';

function createCrmApi() {
  const api = {tasks: new Map(), tables: new Map(), entries: new Map(), threads: new Map(), follows: new Map(), activity: new Map(),
    log: [], receipts: new Map(), actor: {id: 'owner', installationOwner: true}, flags: {conflictNext: false, failThreadOnce: false, hidden: new Set(), loseEntryUpdate: false}, seq: 0, clock: 0};
  const now = () => new Date(Date.UTC(2026, 9, 28, 12, 0, 0) + (++api.clock) * 1000).toISOString();
  const taskKey = (businessId, id) => `${scopeKey(businessId)}:${id}`;
  api.addTask = task => {
    const full = {id: task.id, title: task.title, task_type: 'task', status: 'open', revision: 1, archived: false, tags: [], overview: '', customer_kind: null,
      customer_state: '', customer_profile: {}, fields: {}, assignee_ids: [], due: null, business_id: null, created_at: now(), updated_at: now(), ...task};
    api.tasks.set(taskKey(full.business_id, full.id), full);
    return full;
  };
  api.task = (businessId, id) => api.tasks.get(taskKey(businessId, id));
  const read = (task, path) => {
    if (path.startsWith('fields.')) return task.fields?.[path.slice(7)] ?? null;
    let value = task;
    for (const part of path.split('.')) value = value == null ? null : value[part];
    return value ?? null;
  };
  api.addTable = (scope, table) => {
    const full = {id: table.id || uuid(), title: table.title, description: '', business_id: SCOPES[scope].business_id, table_kind: 'general', crm_config: null,
      columns: [{id: 'name', name: 'Name', type: 'text'}], revision: 1, archived: false, row_count: 0, append_only: false, tags: {topics: []}, readonly_columns: [], identity_columns: [], ...table};
    api.tables.set(full.id, full); api.entries.set(full.id, api.entries.get(full.id) || []);
    if (!api.threads.has(full.id)) api.threads.set(full.id, []);
    return full;
  };
  api.addEntry = (tableId, record, extra = {}) => {
    const list = api.entries.get(tableId), table = api.tables.get(tableId);
    const entry = {id: uuid(), table_id: tableId, revision: 1, position: list.length, stage_id: null, cells: {}, removed: false,
      record_task_id: record.id, record_business_id: record.business_id ?? null, created_at: now(), updated_at: now(), ...extra};
    list.push(entry); table.row_count = list.filter(item => !item.removed).length;
    return entry;
  };
  const tableDto = table => ({...table, row_count: (api.entries.get(table.id) || []).filter(entry => !entry.removed).length});
  const taskDto = task => ({...task, customer_profile: {...task.customer_profile}, tags: [...task.tags]});
  const entryDto = (table, entry) => {
    const task = api.task(entry.record_business_id, entry.record_task_id);
    if (!task || api.flags.hidden.has(taskKey(entry.record_business_id, entry.record_task_id)))
      return {id: entry.id, revision: entry.revision, position: entry.position, unavailable: true, removed: entry.removed};
    const cells = {};
    for (const column of table.columns) cells[column.id] = column.record_field ? read(task, column.record_field) : (entry.cells[column.id] ?? null);
    return {id: entry.id, table_id: table.id, business_id: table.business_id, revision: entry.revision, position: entry.position, stage_id: entry.stage_id,
      cells, local_cells: {...entry.cells}, removed: entry.removed, created_at: entry.created_at, updated_at: entry.updated_at,
      record: taskDto(task), record_ref: {type: 'task', id: task.id, business_id: task.business_id ?? null}};
  };
  const error = (status, code, message = code) => ({status, body: {status: 'error', error: {code, message}}});
  const ok = data => ({status: 200, body: {status: 'ok', data}});
  const pageOf = (items, args, revision) => {
    const limit = Math.min(Number(args.limit) || 50, 100);
    let offset = 0;
    if (args.cursor) {
      const [tag, rev, at] = String(args.cursor).split(':');
      if (tag !== 'c' || Number(rev) !== revision) return {fail: error(409, 'revision_conflict', 'cursor is bound to another table revision')};
      offset = Number(at);
    }
    const slice = items.slice(offset, offset + limit);
    return {items: slice, next_cursor: offset + limit < items.length ? `c:${revision}:${offset + limit}` : null};
  };
  const bump = table => { table.revision += 1; return table; };
  const stageIds = table => new Set((table.crm_config?.stages || []).map(stage => stage.id));
  const system = (table, body, change) => {
    api.threads.get(table.id).push({id: uuid(), kind: 'system', author_id: null, body, created_at: now(), change});
  };

  // handle(scope, op, args) -> {status, body}; the caller routes /api/root|biz/<slug>/operations/<op>.
  api.handle = (scope, op, args) => {
    api.log.push({scope, op, args});
    const businessId = SCOPES[scope].business_id;
    const receiptKey = args.idempotency_key && ['crm_entries_add', 'crm_entries_update', 'crm_entries_remove', 'tables_create', 'tables_update', 'tables_comment', 'crm_activity_record'].includes(op) ? `${scope}:${op}:${args.idempotency_key}` : null;
    if (receiptKey && api.receipts.has(receiptKey)) return api.receipts.get(receiptKey);
    const done = result => { if (receiptKey && result.status === 200) api.receipts.set(receiptKey, result); return result; };
    const table = args.table_id ? api.tables.get(args.table_id) : null;
    if (args.table_id && (!table || table.business_id !== businessId)) return error(404, 'not_found', 'Table not found');
    const stale = () => args.expected_revision != null && (args.expected_revision !== table.revision || api.flags.conflictNext) ? (api.flags.conflictNext = false, error(409, 'revision_conflict', 'stale')) : null;
    switch (op) {
      case 'tables_list': {
        const items = [...api.tables.values()].filter(item => item.business_id === businessId && (args.include_archived || !item.archived)
          && (!args.table_kind || item.table_kind === args.table_kind) && (!args.query || item.title.toLowerCase().includes(String(args.query).toLowerCase())));
        const page = pageOf(items.map(tableDto), args, 0);
        return page.fail || ok({items: page.items, next_cursor: page.next_cursor});
      }
      case 'tables_get': return ok({table: tableDto(table)});
      case 'tables_create': {
        const crm = args.table_kind === 'crm';
        const created = api.addTable(scope, {title: args.title, description: args.description || '', table_kind: crm ? 'crm' : 'general', tags: args.tags || {topics: []},
          columns: args.columns, crm_config: crm ? (args.crm_config || {stages: []}) : null});
        return done(ok({table: tableDto(created)}));
      }
      case 'tables_update': {
        const failure = stale(); if (failure) return failure;
        const changes = args.changes || {};
        for (const key of ['title', 'description', 'columns', 'tags', 'archived', 'crm_config']) if (key in changes) table[key] = changes[key];
        bump(table); return done(ok({table: tableDto(table)}));
      }
      case 'tables_rows_list': {
        if (table.table_kind === 'crm') return api.handle(scope, 'crm_entries_list', args);
        return ok({items: [], next_cursor: null, table_revision: table.revision});
      }
      case 'tables_rows_write': {
        if (table.table_kind === 'crm') return error(400, 'validation_error', 'Use crm_entries_* for a CRM');
        return done(ok({table: tableDto(bump(table))}));
      }
      case 'crm_entries_list': {
        if (table.table_kind !== 'crm') return error(400, 'validation_error', 'not a crm');
        let items = api.entries.get(table.id).filter(entry => args.include_removed || !entry.removed);
        items = items.map(entry => ({entry, dto: entryDto(table, entry)}));
        if (!args.include_archived) items = items.filter(({dto}) => dto.unavailable || !dto.record.archived);
        if (args.stage_id) items = items.filter(({entry}) => entry.stage_id === args.stage_id);
        if (args.query) { const q = String(args.query).toLowerCase(); items = items.filter(({dto}) => !dto.unavailable && JSON.stringify([dto.record.title, dto.cells]).toLowerCase().includes(q)); }
        if (args.order_by) {
          const column = table.columns.find(candidate => candidate.id === args.order_by);
          if (!column || column.record_field || !['text', 'number', 'boolean', 'date'].includes(column.type)) return error(400, 'validation_error', 'unsupported CRM sort column');
          const direction = args.descending ? -1 : 1;
          items.sort((a, b) => {
            const left = a.dto.cells[args.order_by], right = b.dto.cells[args.order_by];
            if (left == null || right == null) return left == null && right == null ? a.entry.position - b.entry.position : left == null ? 1 : -1;
            const compared = typeof left === 'number' && typeof right === 'number' ? left - right : String(left).localeCompare(String(right), undefined, {numeric: column.type === 'number'});
            return compared * direction || a.entry.position - b.entry.position || a.entry.id.localeCompare(b.entry.id);
          });
        } else items.sort((a, b) => a.entry.position - b.entry.position || a.entry.id.localeCompare(b.entry.id));
        const page = pageOf(items.map(({dto}) => dto), args, table.revision);
        return page.fail || ok({items: page.items, next_cursor: page.next_cursor, table_revision: table.revision});
      }
      case 'crm_entries_add': {
        const failure = stale(); if (failure) return failure;
        if (table.archived) return error(400, 'table_archived', 'archived');
        if (args.stage_id && !stageIds(table).has(args.stage_id)) return error(400, 'validation_error', 'unknown stage');
        const list = api.entries.get(table.id);
        let createdRecord = false, record = args.record;
        if (args.create) {
          createdRecord = true;
          const task = api.addTask({id: 't-' + (++api.seq), business_id: businessId, ...args.create});
          record = {id: task.id, business_id: businessId};
        } else if (!api.task(record.business_id, record.id)) return error(404, 'not_found', 'record');
        let entry = list.find(item => item.record_task_id === record.id && item.record_business_id === (record.business_id ?? null));
        if (entry && !entry.removed) return done(ok({table: tableDto(table), entry: entryDto(table, entry), created_record: false}));
        if (entry) { entry.removed = false; entry.revision += 1; if (args.stage_id) entry.stage_id = args.stage_id; }
        else entry = api.addEntry(table.id, record, {stage_id: args.stage_id || null, cells: args.cells || {}});
        bump(table);
        system(table, 'An entry was ' + (entry.revision > 1 ? 'restored' : 'added') + '.', {kind: 'crm_membership', entry_id: entry.id, record: {type: 'task', id: record.id, business_id: record.business_id ?? null}, operation: entry.revision > 1 ? 'restored' : 'added', changed_fields: []});
        return done(ok({table: tableDto(table), entry: entryDto(table, entry), created_record: createdRecord}));
      }
      case 'crm_entries_update': {
        const failure = stale(); if (failure) return failure;
        if (api.flags.loseEntryUpdate) { api.flags.loseEntryUpdate = false; return error(503, 'temporarily_unavailable', 'lost'); }
        const entry = api.entries.get(table.id).find(item => item.id === args.entry_id);
        if (!entry || entry.removed) return error(404, 'not_found', 'entry');
        const changes = args.changes || {}, previousStage = entry.stage_id;
        if (changes.cells) for (const [key, value] of Object.entries(changes.cells)) {
          const column = table.columns.find(candidate => candidate.id === key);
          if (!column || column.record_field) return error(400, 'validation_error', 'unknown or projected cell ' + key);
          if (value === null) delete entry.cells[key]; else entry.cells[key] = value;
        }
        if ('stage_id' in changes) {
          if (changes.stage_id !== null && !stageIds(table).has(changes.stage_id)) return error(400, 'validation_error', 'unknown stage');
          entry.stage_id = changes.stage_id;
        }
        entry.revision += 1; bump(table);
        system(table, 'An entry was updated.', {kind: 'crm_membership', entry_id: entry.id, record: {type: 'task', id: entry.record_task_id, business_id: entry.record_business_id}, operation: 'updated', changed_fields: Object.keys(changes.cells || {}), ...('stage_id' in changes ? {stage: {from: previousStage, to: changes.stage_id}} : {})});
        return done(ok({table: tableDto(table), entry: entryDto(table, entry)}));
      }
      case 'crm_entries_remove': {
        const failure = stale(); if (failure) return failure;
        const entry = api.entries.get(table.id).find(item => item.id === args.entry_id);
        if (!entry) return error(404, 'not_found', 'entry');
        entry.removed = true; entry.revision += 1; bump(table);
        system(table, 'An entry was removed.', {kind: 'crm_membership', entry_id: entry.id, record: {type: 'task', id: entry.record_task_id, business_id: entry.record_business_id}, operation: 'removed', changed_fields: []});
        return done(ok({table: tableDto(table), entry_id: entry.id, removed: true}));
      }
      case 'tables_thread_get': {
        if (api.flags.failThreadOnce) { api.flags.failThreadOnce = false; return error(503, 'temporarily_unavailable'); }
        const messages = api.threads.get(table.id);
        const page = pageOf([...messages].reverse(), args, 0);
        return ok({thread_id: 'thread-' + table.id, messages: page.items.reverse(), next_cursor: page.next_cursor, following: api.follows.get(table.id) ?? true});
      }
      case 'tables_comment': {
        // The installation owner's side conversation stays in the private channel (team_channel.py: only enumerated project
        // Task discussion operations are open to that actor, and tables_comment is not one of them).
        if (api.actor.installationOwner) return error(403, 'channel_required', 'Use the private channel');
        const message = {id: uuid(), kind: 'comment', author_id: 'owner', author_name: 'Owner One', body: args.text, created_at: now()};
        api.threads.get(table.id).push(message);
        return done(ok({table: tableDto(table), thread_id: 'thread-' + table.id, message}));
      }
      case 'tables_follow': api.follows.set(table.id, args.following); return ok({following: args.following});
      case 'tasks_list': {
        let items = [...api.tasks.values()].filter(task => task.business_id === businessId && !api.flags.hidden.has(taskKey(businessId, task.id)) && (!args.parent_id || task.parent_id === args.parent_id));
        if (args.search) items = items.filter(task => task.title.toLowerCase().includes(String(args.search).toLowerCase()));
        const page = pageOf(items.map(taskDto), args, 0);
        return ok({tasks: page.items, next_cursor: page.next_cursor});
      }
      case 'tasks_get': {
        const task = api.task(businessId, args.task_id);
        return task && !api.flags.hidden.has(taskKey(businessId, args.task_id)) ? ok({task: taskDto(task)}) : error(404, 'not_found', 'Task not found');
      }
      case 'tasks_update': {
        const task = api.task(businessId, args.task_id);
        if (!task || api.flags.hidden.has(taskKey(businessId, args.task_id))) return error(404, 'not_found', 'Task not found');
        if (args.expected_revision != null && args.expected_revision !== task.revision) return error(409, 'revision_conflict', 'stale task');
        Object.assign(task, args.changes || {}); task.revision += 1; task.updated_at = now();
        return ok({task: taskDto(task)});
      }
      case 'tasks_config_get': return ok({config: {states: [{name: 'open'}, {name: 'done'}]}, customer_states: ['Lead', 'Active'], revision: 1});
      case 'assignees_list': return ok({assignees: [{id: 'owner', kind: 'human', display_name: 'Owner One', is_owner: true}]});
      case 'crm_activity_list': {
        const task = api.task(businessId, args.task_id);
        if (!task || api.flags.hidden.has(taskKey(businessId, args.task_id))) return error(404, 'not_found', 'Task not found');
        const all = [...(api.activity.get(taskKey(businessId, args.task_id)) || [])].sort((a, b) => b.occurred_at.localeCompare(a.occurred_at) || b.id.localeCompare(a.id));
        const page = pageOf(all, args, 0);
        return ok({record_ref: {type: 'task', id: task.id, business_id: businessId}, items: page.items, next_cursor: page.next_cursor, as_of: now()});
      }
      case 'crm_activity_record': {
        const key = taskKey(businessId, args.task_id);
        // Recording requires the actor's Task-comment authority: a member with that authority, or the installation owner on a
        // project Task only (the enumerated project Task discussion operations); never on a Global Task.
        const authority = api.actor.installationOwner ? businessId != null : api.actor.commentAuthority !== false;
        if (!authority) return error(403, api.actor.installationOwner ? 'channel_required' : 'forbidden', 'No Task comment authority');
        if (typeof args.body !== 'string' || args.body.length < 1) return error(400, 'validation_error', 'body is required');
        if (!args.summary || !args.summary.trim()) return error(400, 'validation_error', 'summary is required');
        const list = api.activity.get(key) || []; api.activity.set(key, list);
        const existing = list.find(item => item._event_key === args.event_key);
        const normalized = JSON.stringify([args.kind, args.summary, args.body ?? '', args.occurred_at, Object.fromEntries(Object.entries(args.source || {}).sort())]);
        if (existing) return existing._payload === normalized ? ok({event: existing, duplicate: true}) : error(409, 'event_conflict', 'Different content for this event key');
        const event = {id: `recorded:${scope}:${uuid()}`, _event_key: args.event_key, kind: args.kind, occurred_at: args.occurred_at, recorded_at: now(), actor: {id: 'owner', display_name: 'Owner One'},
          _payload: normalized, summary: args.summary, body: args.body, source: {type: args.source.kind, id: args.source.message_id || args.source.thread_id, label: args.source.title, url: args.source.url}, origin: 'recorded'};
        list.push(event);
        return done(ok({event, duplicate: false}));
      }
      case 'focus_present': return null;
      default: return null;
    }
  };
  return api;
}

module.exports = {createCrmApi, SCOPES, scopeKey};
