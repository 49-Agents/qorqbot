// The browser bridge supplies the authenticated, fixed-action call function.
// This module has no browser, network, filesystem, or business-mailbox access.
const MAX_CALLS = 24;
const MAX_GET_ITEMS = 10;
const MAX_PAGE_BYTES = 50 * 1024 * 1024;
const MAX_MESSAGE_BYTES = 25 * 1024 * 1024;
const MAX_CURSOR_BYTES = 1_000_000;
const MAX_ITEMS = 500;
const PAGE_TIME_MS = 15_000;

export class OutlookCaptureError extends Error {
  constructor(code) {
    super(code);
    this.name = 'OutlookCaptureError';
    this.code = code;
  }
}

const fail = (code = 'outlook_capture_schema') => { throw new OutlookCaptureError(code); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const identifier = value => typeof value === 'string' && value.length > 0 && value.length <= 4096;
const syncState = value => typeof value === 'string' && value.length > 0 && value.length <= MAX_CURSOR_BYTES;
const instant = value => typeof value === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(value)
  && Number.isFinite(Date.parse(value));
const folderId = folder => folder?.FolderId?.Id;
const mailFolder = folder => typeof folder.FolderClass === 'string'
  && folder.FolderClass.startsWith('IPF.Note') && !/SearchFolder/i.test(folder.__type || '');

function response(raw, allowMissing = false) {
  const messages = raw?.Body?.ResponseMessages?.Items;
  if (!Array.isArray(messages) || messages.length !== 1 || !object(messages[0])) fail();
  const message = messages[0];
  if (allowMissing && message.ResponseCode === 'ErrorItemNotFound') return null;
  if (['ErrorInvalidSyncStateData', 'ErrorSyncStateNotFound', 'ErrorInvalidWatermark',
    'ErrorSyncStateNotSupported'].includes(message.ResponseCode)) fail('outlook_sync_state_expired');
  if ((message.ResponseCode && message.ResponseCode !== 'NoError')
    || (message.ResponseClass && message.ResponseClass !== 'Success')) fail('outlook_capture_provider_error');
  return message;
}

function foldersValid(folders) {
  return Array.isArray(folders) && folders.every(folder => object(folder) && identifier(folderId(folder)))
    && new Set(folders.map(folderId)).size === folders.length;
}

function changeId(change) {
  return change?.Item?.ItemId?.Id ?? change?.ItemId?.Id;
}

function changeValid(change) {
  return object(change) && ['Create', 'Update', 'Delete', 'ReadFlagChange'].includes(change.ChangeType)
    && identifier(changeId(change))
    // Sync uses IdOnly. MIME or a body in a cursor would put private content in
    // operational storage, outside the separately encrypted research archive.
    && !('MimeContent' in change) && !('Body' in change)
    && (!change.Item || (object(change.Item) && !('MimeContent' in change.Item) && !('Body' in change.Item)));
}

function readCursor(cursor, address, startAt) {
  if (cursor === null || cursor === undefined || cursor === '') {
    return {version: 1, address, start_at: startAt, sent_folder_id: null,
      folders: [], states: [], queue: null, pending: null};
  }
  if (typeof cursor !== 'string' || Buffer.byteLength(cursor) > MAX_CURSOR_BYTES) fail('outlook_cursor_invalid');
  let state;
  try { state = JSON.parse(cursor); } catch { fail('outlook_cursor_invalid'); }
  if (!object(state) || state.version !== 1) fail('outlook_cursor_invalid');
  if (state.address !== address) fail('outlook_account_mismatch');
  if (state.start_at !== startAt) fail('outlook_cutover_changed');
  if (!identifier(state.sent_folder_id) || !foldersValid(state.folders)
    || !Array.isArray(state.states)
    || !state.states.every(row => object(row) && identifier(row.id)
      && (row.sync_state === null || syncState(row.sync_state)) && typeof row.baseline === 'boolean'
      && (!row.baseline || row.sync_state !== null))
    || new Set(state.states.map(row => row.id)).size !== state.states.length) fail('outlook_cursor_invalid');
  const ids = new Set(state.states.map(row => row.id));
  if (state.queue !== null && (!Array.isArray(state.queue)
    || new Set(state.queue).size !== state.queue.length || !state.queue.every(id => ids.has(id)))) fail('outlook_cursor_invalid');
  if (state.pending !== null) {
    const p = state.pending;
    if (!object(p) || !state.queue?.length || p.folder_id !== state.queue[0]
      || !syncState(p.next_sync_state) || typeof p.caught_up !== 'boolean'
      || typeof p.established !== 'boolean' || !Array.isArray(p.changes)
      || p.changes.length > 512 || !p.changes.every(changeValid)
      || !Number.isInteger(p.offset) || p.offset < 0 || p.offset > p.changes.length
      || p.established !== state.states.find(row => row.id === p.folder_id)?.baseline) fail('outlook_cursor_invalid');
  }
  return state;
}

function mimeBytes(value) {
  if (typeof value !== 'string' || !value.length || value.length > Math.ceil(MAX_MESSAGE_BYTES / 3) * 4
    || value.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) {
    fail('outlook_complete_mime_unavailable');
  }
  const size = value.length / 4 * 3 - (value.endsWith('==') ? 2 : value.endsWith('=') ? 1 : 0);
  if (size > MAX_MESSAGE_BYTES) fail('outlook_complete_mime_unavailable');
  return size;
}

function binding(call, config) {
  if (typeof call !== 'function' || !object(config) || typeof config.address !== 'string'
    || !/^[^\s@]+@[^\s@]+$/.test(config.address.trim()) || !instant(config.start_at)) fail('outlook_capture_config');
  const address = config.address.trim().toLowerCase();
  const startAt = new Date(config.start_at).toISOString();
  const cutover = Date.parse(startAt);
  // A completed native baseline proves subsequent changes occurred after the
  // cutoff only when that cutoff has already elapsed.
  if (cutover > Date.now()) fail('outlook_capture_config');
  return {address, startAt, cutover};
}

const sentFolderRequest = () => ({
  FolderShape: {__type: 'FolderResponseShape:#Exchange', BaseShape: 'AllProperties'},
  FolderIds: [{__type: 'DistinguishedFolderId:#Exchange', Id: 'sentitems'}],
});

function sentFolder(result) {
  if (!Array.isArray(result.Folders) || result.Folders.length !== 1 || !identifier(folderId(result.Folders[0]))) fail();
  return folderId(result.Folders[0]);
}

const itemRequest = id => ({
  ItemShape: {__type: 'ItemResponseShape:#Exchange', BaseShape: 'AllProperties', IncludeMimeContent: true, BodyType: 'Text'},
  ItemIds: [{__type: 'ItemId:#Exchange', Id: id}],
});

function rawItem(result, id) {
  const raw = result.Items?.[0];
  if (!Array.isArray(result.Items) || result.Items.length !== 1 || !object(raw) || raw.ItemId?.Id !== id) fail();
  return raw;
}

function normalizedItem(raw, {id, sentFolderId, provenance, changedAt, sourceFolderId, isRead}) {
  for (const key of ['DateTimeSent', 'DateTimeReceived', 'DateTimeCreated', 'LastModifiedTime']) {
    if (raw[key] !== undefined && raw[key] !== null && !instant(raw[key])) fail();
  }
  const isSent = raw.IsDraft !== true && (sourceFolderId === sentFolderId
    || raw.ParentFolderId?.Id === sentFolderId || (raw.IsFromMe === true && instant(raw.DateTimeSent)));
  if (isSent && !instant(raw.DateTimeSent)) fail('outlook_cutover_evidence_missing');
  const {Value: mime, ...mimeMetadata} = raw.MimeContent;
  return {id, raw: {...raw, MimeContent: mimeMetadata,
    qorqut_change: {...provenance, mime_value_field: 'mime_base64'}}, mime_base64: mime,
    is_sent: isSent, is_read: (isRead ?? raw.IsRead) === true, sent_at: raw.DateTimeSent ?? null,
    received_at: raw.DateTimeReceived ?? null, created_at: raw.DateTimeCreated ?? null,
    changed_at: changedAt ?? raw.LastModifiedTime ?? null, after_cutover: true};
}

/** Collect a bounded page. Callers commit the returned cursor only after archive/import succeeds. */
export async function capture({call, config, cursor = null, clock = Date.now}) {
  if (typeof clock !== 'function') fail('outlook_capture_config');
  const startedAt = clock();
  if (!Number.isFinite(startedAt)) fail('outlook_capture_config');
  const elapsed = () => clock() - startedAt >= PAGE_TIME_MS;
  const {address, startAt, cutover} = binding(call, config);
  const state = readCursor(cursor, address, startAt);
  let calls = 0;
  let getItems = 0;
  let bytes = 0;
  const items = [];
  const request = async (action, body) => {
    if (calls >= MAX_CALLS) fail('outlook_capture_limit');
    calls += 1;
    return response(await call(action, body));
  };

  if (state.sent_folder_id === null) {
    state.sent_folder_id = sentFolder(await request('GetFolder', sentFolderRequest()));
  }

  if (state.queue === null) {
    // Publish only a complete catalog. Pathological catalog sizes produce an
    // explicit gap, never a partial snapshot that looks like folder deletion.
    const folders = [];
    let offset = 0;
    for (;;) {
      const result = await request('FindFolder', {
        Traversal: 'Deep',
        FolderShape: {__type: 'FolderResponseShape:#Exchange', BaseShape: 'AllProperties'},
        ParentFolderIds: [{__type: 'DistinguishedFolderId:#Exchange', Id: 'msgfolderroot'}],
        Paging: {__type: 'IndexedPageView:#Exchange', BasePoint: 'Beginning', Offset: offset, MaxEntriesReturned: 100},
      });
      const root = result.RootFolder;
      if (!object(root) || !foldersValid(root.Folders)
        || (root.IncludesLastItemInRange !== undefined && typeof root.IncludesLastItemInRange !== 'boolean')) fail();
      folders.push(...root.Folders);
      if (!foldersValid(folders)) fail('outlook_changes_gap');
      if (root.IncludesLastItemInRange === true) break;
      if (!Number.isInteger(root.IndexedPagingOffset) || root.IndexedPagingOffset <= offset) fail('outlook_changes_gap');
      offset = root.IndexedPagingOffset;
    }
    const old = new Map(state.states.map(row => [row.id, row]));
    state.folders = folders;
    state.states = folders.filter(mailFolder).map(folder => old.get(folderId(folder))
      ?? {id: folderId(folder), sync_state: null, baseline: false});
    state.queue = state.states.map(row => row.id);
  }

  const rows = new Map(state.states.map(row => [row.id, row]));
  page: while (state.queue.length) {
    const row = rows.get(state.queue[0]);
    if (state.pending === null) {
      if (calls >= MAX_CALLS || items.length >= MAX_ITEMS || elapsed()) break;
      const result = await request('SyncFolderItems', {
        ItemShape: {__type: 'ItemResponseShape:#Exchange', BaseShape: 'IdOnly',
          AdditionalProperties: [{__type: 'PropertyUri:#Exchange', FieldURI: 'ItemLastModifiedTime'}]},
        SyncFolderId: {__type: 'TargetFolderId:#Exchange', BaseFolderId: {__type: 'FolderId:#Exchange', Id: row.id}},
        MaxChangesReturned: 512, SyncScope: 'NormalItems',
        ...(row.sync_state === null ? {} : {SyncState: row.sync_state}),
      });
      const changes = result.Changes?.Changes;
      if (!syncState(result.SyncState) || !Array.isArray(changes) || changes.length > 512
        || !changes.every(changeValid)
        || (result.IncludesLastItemInRange !== undefined && typeof result.IncludesLastItemInRange !== 'boolean')) fail();
      if (result.SyncState === row.sync_state && result.IncludesLastItemInRange !== true) fail('outlook_changes_gap');
      state.pending = {folder_id: row.id, next_sync_state: result.SyncState,
        caught_up: result.IncludesLastItemInRange === true, established: row.baseline,
        changes, offset: 0};
    }

    const pending = state.pending;
    while (pending.offset < pending.changes.length) {
      if (items.length >= MAX_ITEMS) break page;
      const change = pending.changes[pending.offset];
      const id = changeId(change);
      const changedAt = change.Item?.LastModifiedTime ?? change.LastModifiedTime;
      if (changedAt !== undefined && !instant(changedAt)) fail();
      if (!pending.established) {
        // Initial native enumeration is not proof that a read/delete happened
        // after cutover. Old messages are examined as metadata only.
        if (['Delete', 'ReadFlagChange'].includes(change.ChangeType)) {
          pending.offset += 1;
          continue;
        }
        if (!changedAt) fail('outlook_cutover_evidence_missing');
        if (Date.parse(changedAt) < cutover) {
          pending.offset += 1;
          continue;
        }
      }
      const provenance = {change, folder_id: row.id, sync_state: row.sync_state,
        next_sync_state: pending.next_sync_state, established_sync: pending.established};
      if (change.ChangeType === 'Delete') {
        items.push({id, raw: {qorqut_change: provenance}, removed: true,
          is_sent: row.id === state.sent_folder_id, changed_at: changedAt ?? null,
          after_cutover: pending.established || Date.parse(changedAt) >= cutover});
        pending.offset += 1;
        continue;
      }
      if (calls >= MAX_CALLS || getItems >= MAX_GET_ITEMS || elapsed()) break page;
      getItems += 1;
      const raw = rawItem(await request('GetItem', itemRequest(id)), id);
      const size = mimeBytes(raw.MimeContent?.Value);
      // Leave this native change pending, without storing its MIME in the
      // cursor. The next page fetches it again before moving past it.
      if (bytes + size > MAX_PAGE_BYTES) break page;
      const isRead = change.ChangeType === 'ReadFlagChange' && typeof change.IsRead === 'boolean'
        ? change.IsRead : undefined;
      items.push(normalizedItem(raw, {id, sentFolderId: state.sent_folder_id,
        provenance, changedAt, sourceFolderId: row.id, isRead}));
      bytes += size;
      pending.offset += 1;
    }

    row.sync_state = pending.next_sync_state;
    row.baseline ||= pending.caught_up;
    state.queue.shift();
    if (!pending.caught_up) state.queue.push(row.id);
    state.pending = null;
  }

  const complete = state.queue.length === 0;
  if (complete) state.queue = null;
  const nextCursor = JSON.stringify(state);
  if (Buffer.byteLength(nextCursor) > MAX_CURSOR_BYTES) fail('outlook_capture_limit');
  if (nextCursor === cursor && !complete) fail('outlook_cursor_did_not_advance');
  return {account_address: config.address, start_at: config.start_at, items,
    cursor: nextCursor, complete, folders: state.folders};
}

/**
 * Retry only IDs from the trusted collector's durable, already-after-cutover
 * queue. This is not an agent-facing arbitrary mailbox read API. The queue is
 * the eligibility proof; rereading old timestamps does not erase that proof.
 * Two IDs per request guarantee the 50 MiB limit even for maximum-size MIME.
 */
export async function retrieve({call, config, ids}) {
  const {address, startAt} = binding(call, config);
  if (!Array.isArray(ids) || ids.length > MAX_GET_ITEMS || !ids.every(identifier)
    || new Set(ids).size !== ids.length) fail('outlook_capture_config');
  if (!ids.length) return [];
  const sentFolderId = sentFolder(response(await call('GetFolder', sentFolderRequest())));
  let bytes = 0;
  const items = [];
  for (const id of ids) {
    const provenance = {source: 'routing_retry', account_address: address, start_at: startAt,
      eligibility: 'durable_after_cutover_queue', native_id: id};
    let result;
    try { result = response(await call('GetItem', itemRequest(id)), true); }
    catch (error) {
      // The production bridge normalizes native response errors before they
      // reach us; direct injected calls may instead return the EWS error JSON.
      if (error?.code !== 'outlook_item_not_found') throw error;
      result = null;
    }
    if (result === null) {
      items.push({id, removed: true, after_cutover: true,
        raw: {qorqut_change: {...provenance, response_code: 'ErrorItemNotFound'}}});
      continue;
    }
    const raw = rawItem(result, id);
    bytes += mimeBytes(raw.MimeContent?.Value);
    if (bytes > MAX_PAGE_BYTES) fail('outlook_capture_limit');
    items.push(normalizedItem(raw, {id, sentFolderId, provenance}));
  }
  return items;
}
