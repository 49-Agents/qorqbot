import type { TerminalControl, TerminalDimensions, TerminalMessage, TerminalView } from './wire';

const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const fields = (v: Record<string, unknown>, names: string) => {
  const expected = names.split(' ');
  return Object.keys(v).length === expected.length && expected.every(key => Object.hasOwn(v, key));
};
export const uuid = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
const counter = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
const positive = (v: unknown) => counter(v) && v > 0;
const text = (v: unknown, limit: number) => typeof v === 'string' && v.length <= limit;
const oneOf = (v: unknown, values: string) => typeof v === 'string' && values.split(' ').includes(v);
function control(v: unknown): v is TerminalControl {
  return record(v) && fields(v, 'mode epoch viewer_id last_sequence') && counter(v.epoch) && counter(v.last_sequence)
    && ((v.mode === 'unclaimed' && v.viewer_id === null) || (v.mode === 'human' && uuid(v.viewer_id)));
}
const dimensions = (columns: unknown, rows: unknown) => counter(columns) && columns >= 20 && columns <= 500
  && counter(rows) && rows >= 5 && rows <= 250;
function terminalDimensions(v: unknown): v is TerminalDimensions {
  return record(v) && fields(v, 'columns rows') && dimensions(v.columns, v.rows);
}
export function terminalView(v: unknown): v is TerminalView {
  if (!record(v) || !fields(v, 'agent_id recovery_epoch terminal_id observed_at status reason control recording dimensions')) return false;
  if (!uuid(v.agent_id) || !uuid(v.recovery_epoch) || !(v.terminal_id === null || uuid(v.terminal_id))) return false;
  if (!(v.observed_at === null || (typeof v.observed_at === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(v.observed_at) && Number.isFinite(Date.parse(v.observed_at))))) return false;
  if (!oneOf(v.status, 'disabled waiting_workspace available stopped unavailable unsupported_target') || !text(v.reason, 100)) return false;
  if (!(v.dimensions === null || terminalDimensions(v.dimensions))) return false;
  if (v.status === 'available' && (v.terminal_id === null || v.dimensions === null)) return false;
  if (!(v.control === null || control(v.control))) return false;
  const r = v.recording;
  return r === null || (record(r) && fields(r, 'state local_captured_offset host_archived_offset')
    && oneOf(r.state, 'starting healthy blocked incomplete unknown') && counter(r.local_captured_offset) && counter(r.host_archived_offset));
}
export function terminalMessage(raw: string): TerminalMessage | null {
  if (new TextEncoder().encode(raw).length > 96 * 1024) return null;
  let v: unknown;
  try { v = JSON.parse(raw); } catch { return null; }
  if (!record(v)) return null;
  let valid = false;
  switch (v.type) {
    case 'hello': valid = fields(v, 'type protocol viewer_id terminal') && v.protocol === 2 && uuid(v.viewer_id) && terminalView(v.terminal); break;
    case 'size': valid = fields(v, 'type columns rows') && dimensions(v.columns, v.rows); break;
    case 'state': valid = fields(v, 'type terminal') && terminalView(v.terminal); break;
    case 'control': valid = fields(v, 'type control') && control(v.control); break;
    case 'frame': valid = fields(v, 'type kind viewer_id epoch sequence outcome bytes_written') && oneOf(v.kind, 'input resize')
      && uuid(v.viewer_id) && counter(v.epoch) && positive(v.sequence) && oneOf(v.outcome, 'submitted uncertain') && counter(v.bytes_written); break;
    case 'error': valid = fields(v, 'type code message related_type sequence') && text(v.code, 100) && text(v.message, 96 * 1024)
      && (v.related_type === null || text(v.related_type, 100)) && (v.sequence === null || positive(v.sequence)); break;
  }
  return valid ? v as TerminalMessage : null;
}
export const sameTerminal = (a: TerminalView, b: TerminalView) => a.agent_id === b.agent_id && a.recovery_epoch === b.recovery_epoch && a.terminal_id === b.terminal_id;
// Observations and receipts can be delivered after newer control replies.
export function mergeControl(prior: TerminalControl | null, next: TerminalControl | null): TerminalControl | null {
  if (!prior) return next;
  if (!next || next.epoch < prior.epoch) return prior;
  if (next.epoch > prior.epoch) return next;
  if (prior.viewer_id !== next.viewer_id || prior.mode !== next.mode) throw new Error('Inconsistent terminal control');
  return { ...next, last_sequence: Math.max(prior.last_sequence, next.last_sequence) };
}
export function mergeTerminal(prior: TerminalView | null | undefined, next: TerminalView): TerminalView {
  return prior && sameTerminal(prior, next) ? { ...next, control: mergeControl(prior.control, next.control) } : next;
}
