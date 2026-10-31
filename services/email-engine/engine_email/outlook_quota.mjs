// Recent sending metadata is needed before the first reservation, including
// sends before the private collector's body-capture cutover. Never request MIME.
const MAX_CALLS = 24;
const PAGE_SIZE = 500;

export class OutlookQuotaError extends Error {
  constructor(code) {
    super(code);
    this.name = 'OutlookQuotaError';
    this.code = code;
  }
}
const fail = (code = 'outlook_quota_schema') => { throw new OutlookQuotaError(code); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const identifier = value => typeof value === 'string' && value.length > 0
  && value.length <= 4096 && !/[\r\n]/.test(value);
const address = value => typeof value === 'string' && value.length <= 320
  && /^[^\s@]+@[^\s@]+$/.test(value.trim()) ? value.trim().toLowerCase() : null;

function date(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return null;
  const [, year, month, day, hour, minute, second] = match.map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (month < 1 || month > 12 || day < 1 || day > days[month - 1]
    || hour > 23 || minute > 59 || second > 59) return null;
  const result = Date.parse(value);
  return Number.isFinite(result) ? result : null;
}

function internetId(value) {
  if (!identifier(value)) return false;
  let content = value.trim();
  if (content.startsWith('<') && content.endsWith('>')) content = content.slice(1, -1);
  return /^[^<>\s@]+@[^<>\s@]+$/.test(content);
}

function rootFolder(result) {
  const messages = result?.Body?.ResponseMessages?.Items;
  if (!Array.isArray(messages) || messages.length !== 1 || !object(messages[0])) fail();
  const message = messages[0];
  if ((message.ResponseCode && message.ResponseCode !== 'NoError')
    || (message.ResponseClass && message.ResponseClass !== 'Success')) fail('outlook_quota_provider_error');
  const root = message.RootFolder;
  if (!object(root) || !Array.isArray(root.Items) || root.Items.length > PAGE_SIZE
    || !Number.isSafeInteger(root.TotalItemsInView) || root.TotalItemsInView < 0
    || (root.IncludesLastItemInRange !== undefined && typeof root.IncludesLastItemInRange !== 'boolean')) fail();
  return root;
}

/** Return a complete recent-Sent metadata snapshot or throw; never return partial quota evidence. */
export async function quota({call, config, since}) {
  const account = object(config) ? address(config.address) : null;
  const cutoff = date(since);
  if (typeof call !== 'function' || !account || cutoff === null) fail('outlook_quota_config');
  let total;
  let offset = 0;
  const seen = new Set();
  const items = [];
  for (let calls = 0; calls < MAX_CALLS; calls += 1) {
    const root = rootFolder(await call('FindItem', {
      Traversal: 'Shallow',
      ItemShape: {__type: 'ItemResponseShape:#Exchange', BaseShape: 'IdOnly',
        AdditionalProperties: ['InternetMessageId', 'DateTimeSent', 'From', 'IsDraft']
          .map(FieldURI => ({__type: 'PropertyUri:#Exchange', FieldURI}))},
      ParentFolderIds: [{__type: 'DistinguishedFolderId:#Exchange', Id: 'sentitems'}],
      Paging: {__type: 'IndexedPageView:#Exchange', BasePoint: 'Beginning', Offset: offset, MaxEntriesReturned: PAGE_SIZE},
    }));
    if (total !== undefined && total !== root.TotalItemsInView) fail('outlook_quota_changed');
    total = root.TotalItemsInView;
    for (const item of root.Items) {
      if (!object(item) || !identifier(item.ItemId?.Id) || typeof item.IsDraft !== 'boolean'
        || ['MimeContent', 'Body', 'UniqueBody', 'NormalizedBody'].some(key => key in item)) fail();
      if (seen.has(item.ItemId.Id)) fail('outlook_quota_changed');
      seen.add(item.ItemId.Id);
      if (seen.size > total) fail('outlook_quota_changed');
      if (item.IsDraft) continue;
      const sentAt = date(item.DateTimeSent);
      if (sentAt === null) fail();
      // Older Exchange items may expose a legacy directory address instead of
      // SMTP. Their proven timestamp already excludes them from this window.
      if (sentAt < cutoff) continue;
      const sender = address(item.From?.Mailbox?.EmailAddress);
      if (!sender || !internetId(item.InternetMessageId)) fail();
      if (sender === account) items.push(item);
    }
    if (root.IncludesLastItemInRange === true) {
      if (seen.size !== total) fail('outlook_quota_changed');
      return {account_address: config.address, since, items, complete: true};
    }
    if (!Number.isSafeInteger(root.IndexedPagingOffset) || root.IndexedPagingOffset <= offset) fail('outlook_quota_changed');
    offset = root.IndexedPagingOffset;
  }
  fail('outlook_quota_limit');
}
