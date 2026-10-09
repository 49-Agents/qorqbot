import type { BrowserBinding, BrowserSurface, BrowserView, Entry, InputRequestView } from './wire';

const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const fields = (v: Record<string, unknown>, names: string) => {
  const expected = names.split(' ');
  return Object.keys(v).length === expected.length && expected.every(key => Object.hasOwn(v, key));
};
const uuid = (v: unknown) => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
const counter = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
const oneOf = (v: unknown, values: string) => typeof v === 'string' && values.split(' ').includes(v);
const text = (v: unknown, max = 4096): v is string => typeof v === 'string' && [...v].length <= max && !/[\uD800-\uDFFF]/u.test(v);
function timestamp(v: unknown): v is string {
  if (typeof v !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(v) || v.startsWith('0000')) return false;
  return Number.isFinite(Date.parse(v)) && new Date(v).toISOString().slice(0, 19) === v.slice(0, 19);
}
export function browserBinding(v: unknown): v is BrowserBinding {
  if (!record(v) || !fields(v, 'identity control_epoch') || !counter(v.control_epoch) || !record(v.identity)) return false;
  const id = v.identity;
  return fields(id, 'agent_id recovery_epoch environment_generation boot_id browser_instance protocol')
    && ['agent_id', 'recovery_epoch', 'boot_id', 'browser_instance'].every(key => uuid(id[key]))
    && id.protocol === 1 && counter(id.environment_generation) && id.environment_generation > 0;
}
export const bindingKey = (v: BrowserBinding) => {
  const i = v.identity;
  return [i.agent_id, i.recovery_epoch, i.environment_generation, i.boot_id, i.browser_instance, i.protocol, v.control_epoch].join(':');
};
export const unresolved = (v: InputRequestView) => ['requested', 'ready', 'interrupted'].includes(v.state);
export function inputRequestView(v: unknown): v is InputRequestView {
  return record(v) && fields(v, 'request_id agent_id conversation_id surface_id requester_actor_id prompt state revision created_at updated_at note browser_binding control_pending error')
    && ['request_id', 'agent_id', 'conversation_id', 'surface_id', 'requester_actor_id'].every(key => uuid(v[key]))
    && text(v.prompt) && !!v.prompt.trim() && oneOf(v.state, 'requested ready completed cancelled interrupted')
    && counter(v.revision) && v.revision > 0 && timestamp(v.created_at) && timestamp(v.updated_at)
    && (v.note === null || text(v.note)) && typeof v.control_pending === 'boolean'
    && (v.error === null || (record(v.error) && fields(v.error, 'code message') && text(v.error.code) && !!v.error.code && text(v.error.message)))
    && (v.browser_binding === null || (browserBinding(v.browser_binding) && v.browser_binding.identity.agent_id === v.agent_id))
    && (v.state !== 'ready' || v.browser_binding !== null);
}
function surface(v: unknown): v is BrowserSurface {
  return record(v) && fields(v, 'surface_id agent_id kind status reason binding control')
    && uuid(v.surface_id) && uuid(v.agent_id) && v.kind === 'browser' && oneOf(v.status, 'available unavailable')
    && (v.reason === null || text(v.reason))
    && (v.binding === null || (browserBinding(v.binding) && v.binding.identity.agent_id === v.agent_id))
    && (v.control === null || (record(v.control) && fields(v.control, 'mode ready')
      && oneOf(v.control.mode, 'automation yielding human reconciling') && typeof v.control.ready === 'boolean'));
}
export function browserView(v: unknown): v is BrowserView {
  return record(v) && fields(v, 'agent_id recovery_epoch surface active_request') && uuid(v.agent_id) && uuid(v.recovery_epoch)
    && (v.surface === null || (surface(v.surface) && v.surface.agent_id === v.agent_id))
    && (v.active_request === null || (inputRequestView(v.active_request) && unresolved(v.active_request)
      && v.active_request.agent_id === v.agent_id && v.surface !== null && v.active_request.surface_id === (v.surface as BrowserSurface).surface_id));
}
export function browserRequestEntry(entry: Entry): entry is Entry & { browser_request: InputRequestView } {
  const source: unknown = entry.source, request: unknown = entry.browser_request;
  return entry.kind === 'browser_request' && record(source) && fields(source, 'type request_id') && source.type === 'browser_request'
    && inputRequestView(request) && request.request_id === source.request_id && request.agent_id === entry.agent_id
    && request.conversation_id === entry.conversation_id && request.prompt === entry.text && request.requester_actor_id === entry.sender_actor_id;
}
export function sameRequest(a: InputRequestView, b: InputRequestView) {
  return (['request_id', 'agent_id', 'conversation_id', 'surface_id', 'requester_actor_id', 'prompt', 'created_at'] as const).every(key => a[key] === b[key]);
}
export function streamReady(view: BrowserView | null | undefined, request: InputRequestView, epoch: string) {
  const surface = view?.surface, binding = request.browser_binding;
  return view?.recovery_epoch === epoch && view.agent_id === request.agent_id && surface?.surface_id === request.surface_id
    && surface.status === 'available' && surface.control?.mode === 'human' && surface.control.ready
    && view.active_request?.request_id === request.request_id && view.active_request.state === 'ready'
    && view.active_request.revision === request.revision && !view.active_request.control_pending
    && request.state === 'ready' && !request.control_pending && !!binding && !!surface.binding
    && binding.identity.recovery_epoch === epoch && bindingKey(binding) === bindingKey(surface.binding);
}
