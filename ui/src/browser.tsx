import { createContext, type ComponentChildren } from 'preact';
import { useContext, useLayoutEffect, useRef, useState } from 'preact/hooks';
import type { Agent, InputRequestView } from './wire';
import type { Store } from './store';
import { bindingKey, unresolved } from './browser-view';
import { BrowserConnection } from './browser-client';
import { MutationNotice } from './mutation-notice';
import { ViewIcon } from './view-icons';

type Place = 'inline' | 'computer';
type Selection = { id: string; place: Place; attempt: number; expanded: boolean };
type Context = { store: Store; client: BrowserConnection; selection: Selection | null;
  open: (request: InputRequestView, place: Place, retry?: boolean) => void; expand: (expanded: boolean) => void };
const BrowserContext = createContext<Context>(null!);

export function BrowserWorkspace({ store, children }: { store: Store; children: ComponentChildren }) {
  const scope = `${store.actor}:${store.epoch}:${store.selected}:${store.view(store.selected)}`;
  const [saved, setSaved] = useState<(Selection & { scope: string }) | null>(null);
  const selection = saved?.scope === scope ? saved : null;
  const [, redraw] = useState(0);
  const [client] = useState(() => new BrowserConnection(() => redraw(n => n + 1)));
  const bound = useRef('');
  const request = selection && store.browserRequests.get(selection.id);
  const ready = !!request && store.browserReady(request);
  useLayoutEffect(() => () => client.dispose(), [client]);
  useLayoutEffect(() => { client.reset(); bound.current = ''; }, [scope]);
  useLayoutEffect(() => {
    const binding = ready && request?.browser_binding ? bindingKey(request.browser_binding) : '';
    if (!ready || (bound.current && binding !== bound.current)) client.stop();
    bound.current = binding;
    if (request && !unresolved(request)) setSaved(null);
  });
  const context: Context = { store, client, selection,
    open: (request, place, retry = false) => {
      if (scope !== `${store.actor}:${store.epoch}:${store.selected}:${store.view(store.selected)}`) return;
      setSaved(prior => {
        const same = prior?.scope === scope && prior.id === request.request_id;
        return { scope, id: request.request_id, place, attempt: (same ? prior.attempt : 0) + (retry ? 1 : 0),
          expanded: !!(retry && same && prior.expanded) };
      });
    },
    expand: expanded => setSaved(prior => prior && { ...prior, expanded, place: expanded ? prior.place : 'inline' }),
  };
  return <BrowserContext.Provider value={context}>{children}
    {selection?.expanded && request && unresolved(request) && <ExpandedBrowser request={request} />}
  </BrowserContext.Provider>;
}

function BrowserSlot({ request, place }: { request: InputRequestView; place: Place | 'expanded' }) {
  const { store, client, selection, open } = useContext(BrowserContext);
  const ref = useRef<HTMLDivElement>(null);
  const chosen = selection?.id === request.request_id && (selection.expanded ? place === 'expanded' : selection.place === place);
  const ready = store.browserReady(request);
  useLayoutEffect(() => {
    if (!selection && place === 'inline' && ready) open(request, place);
    if (chosen && ready && ref.current) client.show(ref.current, request.request_id, selection!.attempt);
  });
  useLayoutEffect(() => () => {
    // Layout transfers finish in this render before the microtask checks removal.
    queueMicrotask(() => { if (!client.container.isConnected) client.stop(); });
  }, [client]);
  const reconnect = async () => {
    await store.refreshBrowser(true);
    const current = store.browserRequests.get(request.request_id);
    if (current && store.browserReady(current)) open(current, place === 'expanded' ? selection!.place : place, true);
  };
  return <>
    {chosen && ready && <div className="browser-slot" ref={ref} aria-label="Interactive browser" />}
    {ready && (!chosen ? <button type="button" onClick={() => open(request, place as Place)}>Open here</button>
      : client.status === 'lost' ? <div className="browser-connection" role="status"><p>Browser disconnected. Your request is still open.</p><button type="button" onClick={() => void reconnect()}>Reconnect</button></div>
      : client.status !== 'live' && <p className="quiet" role="status">Connecting browser…</p>)}
  </>;
}

export function BrowserRequestCard({ request: initial, place = 'inline' }: { request: InputRequestView; place?: Place | 'expanded' }) {
  const { store, selection, client, open, expand } = useContext(BrowserContext);
  const request = store.browserRequests.get(initial.request_id) || initial;
  const pendingKey = `browser:${request.request_id}`, pending = store.pending.get(pendingKey);
  const observation = store.browsers.get(request.agent_id);
  const supported = store.browserSupported(), active = unresolved(request);
  const ready = store.browserReady(request);
  const selected = selection?.id === request.request_id && (selection.expanded ? place === 'expanded' : selection.place === place);
  const note = store.browserNotes.get(request.request_id) || '';
  const status = request.state === 'completed' ? 'Completed' : request.state === 'cancelled' ? 'Cancelled'
    : pending?.state === 'sending' ? 'Updating request' : pending?.unresolvedAmbiguity ? 'Confirmation uncertain'
    : request.state === 'interrupted' ? 'Interrupted' : request.state === 'requested' || request.control_pending ? 'Waiting for browser control'
    : ready ? 'Ready for your input' : 'Browser unavailable';
  return <div className="browser-card" data-browser-request={request.request_id}>
    <div className="browser-card-heading"><span>Browser</span>{(place === 'expanded' || ready && selected) && <button type="button" className="browser-expand" aria-label={place === 'expanded' ? 'Collapse browser' : 'Expand browser'} title={place === 'expanded' ? 'Collapse browser' : 'Expand browser'} onClick={() => expand(place !== 'expanded')}><ViewIcon name={place === 'expanded' ? 'collapse' : 'expand'} /></button>}</div>
    <p className="message-body browser-prompt">{request.prompt}</p>
    <p className="browser-status" role="status">{status}</p>
    {active && !supported ? <p className="quiet">Browser viewing and control are unavailable on this server.</p> : active && <>
      <BrowserSlot request={request} place={place} />
      {observation?.error && <div className="error" role="alert"><p>{observation.error}</p><button type="button" disabled={observation.loading} onClick={() => void store.refreshBrowser(true)}>Retry browser</button></div>}
      {!observation?.error && observation?.view?.surface?.status === 'unavailable' && <p className="quiet">Browser service unavailable.</p>}
      {request.error && <p className="error" role="alert">Browser control was interrupted. Resume to try again.</p>}
      {!pending && !request.control_pending && (request.state === 'interrupted' || (request.state === 'requested' && !!request.error) || (request.state === 'ready' && !ready && !observation?.error)) && <button type="button" onClick={() => {
        client.stop(); open(request, place === 'expanded' ? selection!.place : place, true);
        void store.browserAction(request, 'input_requests_resume');
      }}>Resume</button>}
      <label className="browser-note">Optional note<textarea rows={2} value={note} disabled={!!pending} onInput={e => { store.browserNotes.set(request.request_id, e.currentTarget.value); store.emit(); }} /></label>
      {[...note].length > 4096 && <p className="error">Keep the note to 4,096 characters.</p>}
      <div className="browser-actions"><button type="button" className="primary" disabled={!!pending || request.state !== 'ready' || request.control_pending || [...note].length > 4096} onClick={() => { client.stop(); void store.browserAction(request, 'input_requests_complete'); }}>Done</button>
        <button type="button" disabled={!!pending || [...note].length > 4096} onClick={() => { client.stop(); void store.browserAction(request, 'input_requests_cancel'); }}>Cancel</button></div>
    </>}
    {!active && request.note !== null && <p className="browser-saved-note">{request.note}</p>}
    {!active && request.control_pending && <p className="quiet">Returning browser control…</p>}
    <MutationNotice store={store} item={pending} requestKey={pendingKey} />
  </div>;
}

function ExpandedBrowser({ request }: { request: InputRequestView }) {
  const { expand } = useContext(BrowserContext);
  const ref = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => { ref.current?.close(); if (previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, []);
  return <dialog ref={ref} className="dialog browser-dialog" aria-label="Expanded browser" onCancel={e => { e.preventDefault(); expand(false); }}>
    <BrowserRequestCard request={request} place="expanded" />
  </dialog>;
}

export function ComputerBrowser({ agent }: { agent: Agent }) {
  const { store, open } = useContext(BrowserContext);
  const observation = store.browsers.get(agent.id), view = observation?.view;
  const active = view?.active_request && (store.browserRequests.get(view.active_request.request_id) || view.active_request);
  const request = active && unresolved(active) ? active : null;
  const pendingKey = `browser-create:${agent.id}`;
  return <section className="surface-card computer-browser" aria-label="Browser">
    {request ? <BrowserRequestCard request={request} place="computer" /> : <>
      <div className="surface-preview" aria-hidden="true"><ViewIcon name="computer" /></div><h3>Browser</h3>
      {!store.browserSupported() ? <p className="quiet">Unavailable. Browser viewing and control are not connected.</p>
        : observation?.loading && !view ? <p className="quiet" role="status">Checking browser…</p>
        : view?.surface?.status === 'available' ? <button type="button" disabled={!!observation?.error || !!view.active_request || store.pending.has(pendingKey) || !store.has('input_requests_create')} onClick={() => void store.createBrowser(agent.id).then(created => { if (created && store.selected === agent.id) open(created, 'computer'); })}>Open browser</button>
        : <p className="quiet">Unavailable. No browser service is connected.</p>}
    </>}
    {observation?.error && !request && <div className="error" role="alert"><p>{observation.error}</p><button type="button" disabled={observation.loading} onClick={() => void store.refreshBrowser(true)}>Retry browser</button></div>}
    <MutationNotice store={store} item={store.pending.get(pendingKey)} requestKey={pendingKey} />
  </section>;
}
