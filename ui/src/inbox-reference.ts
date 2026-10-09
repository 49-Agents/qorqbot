import type { Entry, InboxReference } from './wire';

const uuid = (value: unknown) => typeof value === 'string' && value.length === 36 && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);

export function inboxReferenceEntry(entry: Entry): entry is Entry & { inbox_reference: InboxReference } {
  const reference: unknown = entry.inbox_reference;
  if (entry.kind !== 'message' || entry.role !== 'user' || entry.source?.type !== 'native'
    || !reference || typeof reference !== 'object' || Array.isArray(reference)) return false;
  // Core owns association matching; only validate the presentation's wire shape here.
  const fields = reference as Record<string, unknown>;
  return uuid(fields.message_id) && uuid(fields.message_entry_id);
}
