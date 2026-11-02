/** Fixed Outlook operations through the owner's authenticated web session.
 * Credentials stay in this process/browser. They never appear in results or logs.
 * This uses Outlook's private web protocol, which can change independently of us.
 */
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';

const ORIGIN = 'https://outlook.cloud.microsoft';
const HOSTS = new Set(['outlook.cloud.microsoft', 'outlook.office.com', 'outlook.office365.com']);
const MAX_BYTES = 80 * 1024 * 1024;
const shape = {__type: 'ItemResponseShape:#Exchange', BaseShape: 'AllProperties', IncludeMimeContent: true, BodyType: 'Text'};
const folder = id => ({__type: 'TargetFolderId:#Exchange', BaseFolderId: {__type: 'DistinguishedFolderId:#Exchange', Id: id}});
export class BridgeError extends Error {
  constructor(code, uncertain = false) { super(code); this.code = code; this.uncertain = uncertain; }
}
const fail = (code, uncertain = false) => { throw new BridgeError(code, uncertain); };
function identifier(value) {
  if (typeof value !== 'string' || !value || value.length > 4096 || /[\r\n]/.test(value)) fail('outlook_identifier_invalid');
  return value;
}
function messages(result) {
  const values = result?.Body?.ResponseMessages?.Items;
  if (!Array.isArray(values) || !values.length) fail('outlook_response_invalid');
  return values;
}
export async function getItem(call, id) {
  const data = await call('GetItem', {ItemShape: shape, ItemIds: [{__type: 'ItemId:#Exchange', Id: identifier(id)}]});
  const items = messages(data).flatMap(value => value.Items || []);
  if (items.length !== 1 || items[0].ItemId?.Id !== id || !items[0].MimeContent?.Value) fail('outlook_message_incomplete');
  return items[0];
}
export async function findItem(call, internetId, selected = ['drafts', 'sentitems']) {
  identifier(internetId);
  const items = []; let calls = 0;
  for (const id of selected) {
    let offset = 0, total;
    const seen = new Set();
    // Outlook's web endpoint does not support an InternetMessageId restriction.
    // Scan only identifiers, never bodies; an incomplete scan is an explicit gap.
    for (;;) {
      if (++calls > 24) fail('outlook_lookup_limit');
      const result = await call('FindItem', {
        Traversal: 'Shallow', ItemShape: {__type: 'ItemResponseShape:#Exchange', BaseShape: 'IdOnly',
          AdditionalProperties: [{__type: 'PropertyUri:#Exchange', FieldURI: 'InternetMessageId'}]},
        ParentFolderIds: [folder(id).BaseFolderId],
        Paging: {__type: 'IndexedPageView:#Exchange', BasePoint: 'Beginning', Offset: offset, MaxEntriesReturned: 500},
      });
      const root = messages(result)[0].RootFolder;
      if (!root || !Array.isArray(root.Items) || !Number.isInteger(root.TotalItemsInView)
          || (total !== undefined && total !== root.TotalItemsInView)) fail('outlook_lookup_changed');
      total = root.TotalItemsInView;
      for (const item of root.Items) {
        const itemId = identifier(item.ItemId?.Id);
        if (seen.has(itemId)) fail('outlook_lookup_changed');
        seen.add(itemId);
        if (item.InternetMessageId === internetId) items.push(item);
      }
      if (items.length > 1) fail('outlook_lookup_ambiguous');
      if (root.IncludesLastItemInRange === true) {
        if (seen.size !== total) fail('outlook_lookup_changed');
        break;
      }
      if (!Number.isInteger(root.IndexedPagingOffset) || root.IndexedPagingOffset <= offset) fail('outlook_lookup_changed');
      offset = root.IndexedPagingOffset;
    }
  }
  if (items.length > 1) fail('outlook_lookup_ambiguous');
  if (!items.length) return null;
  if (calls >= 24) fail('outlook_lookup_limit');
  const raw = await getItem(call, items[0].ItemId?.Id);
  if (raw.InternetMessageId !== internetId) fail('outlook_lookup_changed');
  return raw;
}
export async function dispatch({call, config, command, args = {}}) {
  switch (command) {
    case 'status': return {available: true, account_address: config.address};
    case 'get': return getItem(call, args.id);
    case 'find': return findItem(call, args.internet_message_id, args.sent_only ? ['sentitems'] : undefined);
    case 'create': {
      if (typeof args.mime_base64 !== 'string' || args.mime_base64.length > 36 * 1024 * 1024) fail('outlook_mime_invalid');
      const result = await call('CreateItem', {
        MessageDisposition: 'SaveOnly', SendOnNotFoundError: false, SavedItemFolderId: folder('drafts'),
        Items: [{__type: 'Message:#Exchange', MimeContent: {CharacterSet: 'UTF-8', Value: args.mime_base64}}],
        ItemShape: shape,
      });
      const values = messages(result).flatMap(value => value.Items || []);
      if (values.length !== 1 || !values[0].ItemId?.Id) fail('outlook_draft_result_missing', true);
      return values[0].MimeContent?.Value ? values[0] : getItem(call, values[0].ItemId.Id);
    }
    case 'send': {
      const raw = await getItem(call, args.id);
      if (raw.IsDraft !== true || raw.InternetMessageId !== args.internet_message_id
          || raw.Subject !== args.subject
          || !args.change_key || raw.ItemId.ChangeKey !== args.change_key
          || createHash('sha256').update(Buffer.from(raw.MimeContent.Value, 'base64')).digest('hex') !== args.mime_sha256) {
        fail('outlook_send_draft_changed');
      }
      // OWA exposes its native UpdateItem send operation; standalone EWS SendItem
      // is not exposed by service.svc. Keep the verified MIME and change key intact.
      const response = await call('UpdateItem', {ItemChanges: [{__type: 'ItemChange:#Exchange', ItemId: raw.ItemId,
        Updates: [{__type: 'SetItemField:#Exchange', Path: {__type: 'PropertyUri:#Exchange', FieldURI: 'Subject'},
          Item: {__type: 'Message:#Exchange', Subject: raw.Subject}}]}],
        ConflictResolution: 'NeverOverwrite', MessageDisposition: 'SendAndSaveCopy',
        SendCalendarInvitationsOrCancellations: 'SendToNone', SuppressReadReceipts: true,
        SendOnNotFoundError: false, ItemShape: {__type: 'ItemResponseShape:#Exchange', BaseShape: 'IdOnly'}});
      return {accepted: true, provider_response: response};
    }
    case 'capture': {
      const {capture} = await import('./outlook_capture.mjs');
      return capture({call, config, cursor: args.cursor ?? null});
    }
    case 'retrieve': {
      const {retrieve} = await import('./outlook_capture.mjs');
      return retrieve({call, config, ids: args.ids});
    }
    case 'quota': {
      const {quota} = await import('./outlook_quota.mjs');
      return quota({call, config, since: args.since});
    }
    default: fail('outlook_command_invalid');
  }
}

/** The pinned browser client, resolved from beside this file. A missing or unusable install
 * is its own code: it happens before any browser or provider contact and is never the
 * transient "bridge unavailable" outage. The exception text is dropped (it names paths).
 */
export async function loadPlaywright() {
  try {
    return await import('playwright-core');
  } catch (error) {
    fail(error?.code === 'ERR_MODULE_NOT_FOUND' ? 'outlook_dependency_missing' : 'outlook_dependency_unusable');
  }
}

/** Offline readiness: the exact pinned playwright-core sits in this bridge's own
 * `node_modules` (the one that ships with an immutable release) and imports from there.
 * An install found only in a parent directory does not count. Opens no browser, session,
 * socket or provider request.
 */
export async function dependencyReadiness() {
  const here = dirname(fileURLToPath(import.meta.url));
  let pinned;
  try {
    pinned = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8')).dependencies?.['playwright-core'];
  } catch { /* reported below */ }
  if (typeof pinned !== 'string' || !/^\d+\.\d+\.\d+$/.test(pinned)) fail('outlook_dependency_manifest_invalid');
  let installed;
  try {
    installed = JSON.parse(readFileSync(join(here, 'node_modules', 'playwright-core', 'package.json'), 'utf8'));
  } catch { fail('outlook_dependency_missing'); }
  if (installed?.name !== 'playwright-core') fail('outlook_dependency_unusable');
  if (installed.version !== pinned) fail('outlook_dependency_version_mismatch');
  // The nearest node_modules wins, so this loads the adjacent package checked above.
  const {chromium} = await loadPlaywright();
  if (typeof chromium?.connectOverCDP !== 'function') fail('outlook_dependency_unusable');
  return {dependency: 'playwright-core', version: installed.version};
}

export async function session(config) {
  if (!config || !Number.isInteger(config.timeout_seconds) || config.timeout_seconds < 1 || config.timeout_seconds > 120) fail('outlook_timeout_invalid');
  const endpoint = new URL(config.cdp_url);
  if (!['http:', 'ws:'].includes(endpoint.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname)
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) fail('outlook_endpoint_invalid');
  if (typeof config.address !== 'string' || !/^[^\s@]+@[^\s@]+$/.test(config.address)) fail('outlook_account_invalid');
  const {chromium} = await loadPlaywright();
  const browser = await chromium.connectOverCDP(config.cdp_url, {timeout: config.timeout_seconds * 1000});
  const context = browser.contexts()[0];
  if (!context) fail('outlook_browser_context_missing');
  const name = 'qorqut-email:' + config.address.toLowerCase();
  let page;
  for (const candidate of context.pages()) {
    if (!HOSTS.has(new URL(candidate.url()).hostname)) continue;
    if (await candidate.evaluate(() => window.name).catch(() => '') === name) { page = candidate; break; }
  }
  page ??= await context.newPage();
  let headers;
  const listener = async request => {
    try {
      const url = new URL(request.url());
      if (url.protocol !== 'https:' || !HOSTS.has(url.hostname) || url.pathname !== '/owa/service.svc') return;
      const values = await request.allHeaders();
      if (values.authorization) {
        headers = {authorization: values.authorization, 'x-anchormailbox': values['x-anchormailbox'] || config.address};
      }
    } catch { /* A torn-down page is reported as unavailable below. */ }
  };
  page.on('request', listener);
  await page.goto(ORIGIN + '/mail/', {waitUntil: 'domcontentloaded', timeout: config.timeout_seconds * 1000});
  await page.evaluate(value => { window.name = value; }, name);
  await page.getByRole('treeitem', {name: config.address, exact: true}).waitFor({timeout: config.timeout_seconds * 1000});
  const until = Date.now() + Math.min(config.timeout_seconds * 1000, 20000);
  while (!headers && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 100));
  if (!headers) fail('outlook_session_unavailable');
  async function call(action, body) {
    const write = ['CreateItem', 'UpdateItem'].includes(action);
    if (!['GetItem', 'FindItem', 'CreateItem', 'UpdateItem', 'FindFolder', 'GetFolder', 'SyncFolderItems'].includes(action)) fail('outlook_action_invalid');
    let result;
    try {
      if (!HOSTS.has(new URL(page.url()).hostname)
          || await page.getByRole('treeitem', {name: config.address, exact: true}).count() !== 1) fail('outlook_account_changed');
      result = await page.evaluate(async ({action, body, headers, timeout, maximum}) => {
        if (location.protocol !== 'https:' || !['outlook.cloud.microsoft', 'outlook.office.com', 'outlook.office365.com'].includes(location.hostname)) {
          return {account_changed: true};
        }
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);
        try {
          const response = await fetch('/owa/service.svc?action=' + action + '&app=Mail', {
            method: 'POST', credentials: 'include', signal: controller.signal,
            headers: {...headers, 'content-type': 'application/json; charset=utf-8', action},
            body: JSON.stringify({__type: action + 'JsonRequest:#Exchange',
              Header: {__type: 'JsonRequestHeaders:#Exchange', RequestServerVersion: 'V2018_01_08'},
              Body: {__type: action + 'Request:#Exchange', ...body}}),
          });
          const reader = response.body.getReader();
          const decoder = new TextDecoder(); let content = '', size = 0;
          while (true) {
            const part = await reader.read(); if (part.done) break;
            size += part.value.byteLength;
            if (size > maximum) { await reader.cancel(); return {oversized: true}; }
            content += decoder.decode(part.value, {stream: true});
          }
          content += decoder.decode();
          return {status: response.status, data: JSON.parse(content)};
        } catch { return {connection_failed: true}; }
        finally { clearTimeout(timer); }
      }, {action, body, headers, timeout: config.timeout_seconds * 1000, maximum: MAX_BYTES});
    } catch { fail('outlook_connection_failed', write); }
    if (result.account_changed) fail('outlook_account_changed');
    if (result.connection_failed) fail('outlook_connection_failed', write);
    if (result.oversized) fail('outlook_response_too_large', write);
    if (result.status !== 200) fail(result.status === 401 || result.status === 403 ? 'outlook_session_unavailable' : 'outlook_request_failed', write);
    let responses;
    try { responses = messages(result.data); } catch { fail('outlook_response_invalid', write); }
    for (const response of responses) {
      if (response.ResponseCode !== 'NoError') {
        if (['ErrorInvalidSyncStateData', 'ErrorSyncStateNotFound', 'ErrorInvalidWatermark', 'ErrorSyncStateNotSupported'].includes(response.ResponseCode)) fail('outlook_sync_state_expired');
        if (response.ResponseCode === 'ErrorItemNotFound') fail('outlook_item_not_found');
        fail('outlook_provider_rejected', write);
      }
    }
    return result.data;
  }
  return {call, page, browser, dispose: () => { headers = undefined; page.off('request', listener); }};
}

let cliWrite = false;
async function main() {
  let input = '', size = 0;
  for await (const part of process.stdin) {
    size += part.length; if (size > MAX_BYTES) fail('outlook_input_too_large'); input += part.toString('utf8');
  }
  const request = JSON.parse(input);
  const opened = await session(request.config);
  cliWrite = ['create', 'send'].includes(request.command);
  try { return await dispatch({...opened, ...request}); }
  finally { opened.dispose(); }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  // Closing the CDP browser would affect other work. Exit only our client process.
  // `--check-dependencies` is the offline readiness check: no stdin, no config, no session.
  (process.argv[2] === '--check-dependencies' ? dependencyReadiness() : main()).then(result => {
    const output = JSON.stringify({ok: true, result});
    if (Buffer.byteLength(output) > MAX_BYTES) fail('outlook_output_too_large', cliWrite);
    process.stdout.write(output, () => process.exit(0));
  }).catch(error => {
    const code = error instanceof BridgeError || /^outlook_[a-z_]+$/.test(error?.code || '') ? error.code : 'outlook_bridge_unavailable';
    process.stdout.write(JSON.stringify({ok: false, error: code, uncertain: Boolean(error?.uncertain)}), () => process.exit(1));
  });
}
