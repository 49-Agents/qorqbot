import type { CurrentOwner, NativeCollectionView, NativeSetupView, RuntimeView } from './wire';

const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const fields = (v: Record<string, unknown>, names: string) => {
  const expected = names.split(' ');
  return Object.keys(v).length === expected.length && expected.every(key => Object.hasOwn(v, key));
};
const uuid = (v: unknown) => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
const counter = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
const positive = (v: unknown) => counter(v) && v > 0;
const oneOf = (v: unknown, values: string) => typeof v === 'string' && values.split(' ').includes(v);
function timestamp(v: unknown): v is string {
  if (typeof v !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(v) || v.startsWith('0000')) return false;
  const millis = Date.parse(v);
  // Date.parse normalizes impossible dates such as February 30; core rejects them.
  return Number.isFinite(millis) && new Date(millis).toISOString().slice(0, 19) === v.slice(0, 19);
}
const nullableTime = (v: unknown) => v === null || timestamp(v);
function owner(v: unknown): v is CurrentOwner {
  return record(v) && fields(v, 'owner_id run_id run_generation conversation_id terminal_id recovery_epoch environment_generation boot_id provider cli_version native_session_id connected_at')
    && ['owner_id', 'run_id', 'conversation_id', 'terminal_id', 'recovery_epoch', 'boot_id', 'native_session_id'].every(key => uuid(v[key]))
    && positive(v.run_generation) && positive(v.environment_generation) && oneOf(v.provider, 'claude codex')
    && typeof v.cli_version === 'string' && timestamp(v.connected_at);
}
function setup(v: unknown): v is NativeSetupView {
  return record(v) && fields(v, 'state observed_at reason') && nullableTime(v.observed_at)
    && oneOf(v.state, 'disabled waiting ready blocked unknown')
    && typeof v.reason === 'string' && Object.hasOwn(setupReasons, v.reason);
}
function collection(v: unknown): v is NativeCollectionView {
  return record(v) && fields(v, 'state observed_at reason source_count pending_archive_bytes pending_parse_bytes')
    && nullableTime(v.observed_at) && oneOf(v.state, 'disabled waiting collecting blocked gap unknown')
    && typeof v.reason === 'string' && Object.hasOwn(collectionReasons, v.reason)
    && counter(v.source_count) && counter(v.pending_archive_bytes) && counter(v.pending_parse_bytes);
}
export function runtimeView(v: unknown): v is RuntimeView {
  return record(v) && fields(v, 'agent_id status current_owner observed_at input_ready native_setup native_capture')
    && uuid(v.agent_id) && oneOf(v.status, 'disconnected connected unknown') && v.input_ready === false
    && nullableTime(v.observed_at) && (v.current_owner === null || owner(v.current_owner))
    && (v.native_setup === null || setup(v.native_setup)) && (v.native_capture === null || collection(v.native_capture));
}

export const sessionStatus: Record<RuntimeView['status'], string> = {
  disconnected: 'No session is connected.', connected: 'Session connected.', unknown: 'Session connection unknown.',
};
export const nativeStates: Record<NativeSetupView['state'] | NativeCollectionView['state'], string> = {
  disabled: 'Disabled', waiting: 'Waiting', ready: 'Ready', collecting: 'Collecting', blocked: 'Blocked', gap: 'Gap', unknown: 'Unknown',
};
export const setupReasons: Record<NativeSetupView['reason'], string> = {
  not_configured: 'Not configured', unsupported_target: 'Unsupported for this agent', waiting_terminal: 'Waiting for Terminal',
  configuration_installed: 'Configuration installed', setup_unavailable: 'Setup unavailable',
  recovery_review: 'Needs recovery review', observation_stale: 'Observation is stale',
};
export const collectionReasons: Record<NativeCollectionView['reason'], string> = {
  not_configured: 'Not configured', unsupported_target: 'Unsupported for this agent', waiting_session: 'Waiting for a session',
  waiting_source: 'Waiting for a source', active: 'Collection active', collection_blocked: 'Collection blocked',
  capture_gap: 'Archival gap reported', parser_gap: 'Parsing gap reported', recovery_review: 'Needs recovery review', observation_stale: 'Observation is stale',
};
