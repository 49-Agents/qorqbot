import type { Entry, FileReferenceSource } from './wire';

const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const uuid = (v: unknown) => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
const fields = (v: Record<string, unknown>, names: string) => {
  const expected = names.split(' ');
  return Object.keys(v).length === expected.length && expected.every(key => Object.hasOwn(v, key));
};
function text(v: unknown, min: number, max: number, note = false): v is string {
  // D45 counts Unicode code points and rejects invalid UTF-8 (lone surrogates).
  return typeof v === 'string' && [...v].length >= min && [...v].length <= max
    && !/[\uD800-\uDFFF]/u.test(v)
    && !(note ? /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/u : /[\u0000-\u001f\u007f-\u009f]/u).test(v);
}
const path = (v: unknown) => text(v, 1, 4096) && !v.split('/').includes('..');
function timestamp(v: unknown): v is string {
  if (typeof v !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(v) || v.startsWith('0000')) return false;
  const millis = Date.parse(v);
  return Number.isFinite(millis) && new Date(millis).toISOString().slice(0, 19) === v.slice(0, 19);
}

export function fileReferenceEntry(entry: Entry): entry is Entry & { source: FileReferenceSource } {
  const source: unknown = entry.source;
  if (entry.kind !== 'file_reference' || !record(source) || source.type !== 'file_reference'
    || !fields(source, 'type reference') || !record(source.reference)) return false;
  const ref = source.reference;
  // Historical recovery_epoch is provenance, never compared with the current Store epoch.
  return fields(ref, 'reference_id agent_id conversation_id actor_id installation_id recovery_epoch state location path requested_path label note size_bytes mtime_ns observed_at recorded_at sha256')
    && ['reference_id', 'agent_id', 'conversation_id', 'actor_id', 'installation_id', 'recovery_epoch'].every(key => uuid(ref[key]))
    && ref.state === 'referenced' && (ref.location === 'workspace' || ref.location === 'host')
    && path(ref.path) && path(ref.requested_path)
    && text(ref.label, 1, 200) && /[^\p{White_Space}]/u.test(ref.label)
    && (ref.note === null || text(ref.note, 0, 4000, true))
    && typeof ref.size_bytes === 'number' && Number.isInteger(ref.size_bytes) && ref.size_bytes >= 0
    && typeof ref.mtime_ns === 'string' && /^-?[0-9]+$/.test(ref.mtime_ns) && ref.sha256 === null
    && timestamp(ref.observed_at) && timestamp(ref.recorded_at)
    && ref.agent_id === entry.agent_id && ref.conversation_id === entry.conversation_id
    && ref.actor_id === entry.sender_actor_id && ref.label === entry.text
    && ref.observed_at === entry.occurred_at && ref.recorded_at === entry.recorded_at
    && (entry.role === 'user' || entry.role === 'agent') && entry.direction === 'outgoing'
    && entry.delivery === null && entry.handling === null;
}
