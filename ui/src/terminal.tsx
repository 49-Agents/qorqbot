import { useLayoutEffect, useRef, useState } from 'preact/hooks';
import { TerminalClient, type TerminalState } from './terminal-client';
import type { Store } from './store';
import type { TerminalView } from './wire';
import '@xterm/xterm/css/xterm.css';

const labels: Record<TerminalView['status'], string> = {
  disabled: 'Terminal is disabled on this installation.', waiting_workspace: 'Terminal is waiting for the workspace.',
  unsupported_target: 'Terminal is not available for this agent yet.', stopped: 'Terminal is stopped.',
  unavailable: 'Terminal is unavailable.', available: 'Terminal available.',
};
export function TerminalSummary({ store, id }: { store: Store; id: string }) {
  const observation = store.terminals.get(id), view = observation?.view;
  if (!store.has('terminal_get')) return <p className="quiet">Terminal unavailable on this server.</p>;
  return <>
    <p>{view ? labels[view.status] : observation?.error || 'Reading terminal status…'}</p>
    {view?.recording && <p className="quiet">Local recording: {view.recording.state} · {view.recording.local_captured_offset.toLocaleString()} bytes captured · {view.recording.host_archived_offset.toLocaleString()} bytes archived by host.</p>}
    {view?.control?.mode === 'human' && <p className="quiet">Human control is held until explicitly released or taken over.</p>}
    {view?.recording && <p className="quiet">Terminal recording is separate from native history.</p>}
    {(!view || view.status !== 'available') && <button type="button" onClick={() => void store.refreshTerminal(true)} disabled={observation?.loading}>Retry terminal status</button>}
  </>;
}
const initial: TerminalState = { connection: 'connecting', owned: false, controlPending: false, warning: '' };
export function TerminalPane({ store, id }: { store: Store; id: string }) {
  const element = useRef<HTMLDivElement>(null), client = useRef<TerminalClient | null>(null);
  const mounted = useRef(true);
  const [state, setState] = useState<TerminalState>(initial);
  const [attempt, setAttempt] = useState(0);
  const observation = store.terminals.get(id), view = observation?.view;
  useLayoutEffect(() => {
    if (client.current) { client.current.observe(view || null); return; }
    if (view?.status !== 'available' || !element.current) return;
    client.current = new TerminalClient(element.current, view, {
      current: () => mounted.current && store.phase === 'ready' && store.selected === id && store.view(id) === 'terminal' && store.epoch === view.recovery_epoch,
      changed: next => { if (mounted.current) setState(next); },
      observation: next => store.acceptTerminal(next), refresh: () => { void store.refreshTerminal(true); },
    });
  }, [view, attempt]);
  useLayoutEffect(() => () => { mounted.current = false; client.current?.dispose(); client.current = null; }, []);
  const reconnect = async () => {
    await store.refreshTerminal(true);
    if (!mounted.current || store.phase !== 'ready') return;
    client.current?.dispose(); client.current = null; setState(initial); setAttempt(value => value + 1);
  };
  return <section className="terminal-pane" aria-label="Terminal view">
    {client.current || view?.status === 'available' ? <>
      <div className="terminal-toolbar">
        <span role="status">{state.connection === 'closed' ? 'Disconnected' : state.connection === 'connecting' ? 'Connecting…' : state.owned ? 'You have control' : 'View only'}</span>
        {state.connection === 'closed' ? <button onClick={() => void reconnect()} disabled={observation?.loading}>Reconnect terminal</button>
          : <button onClick={() => client.current?.control(state.owned ? 'release' : 'take')} disabled={state.connection !== 'connected' || state.controlPending || !view?.control}>{state.controlPending ? 'Confirming control…' : state.owned ? 'Release control' : 'Take control'}</button>}
      </div>
      {state.warning && <p className="terminal-warning" role="alert">{state.warning}</p>}
    </> : <div className="terminal-unavailable"><TerminalSummary store={store} id={id} /></div>}
    <div className="terminal-screen" ref={element} role="region" aria-label="Terminal display" tabIndex={0} hidden={!client.current && view?.status !== 'available'} />
    <p className="terminal-note">Start and configure your CLI here. Chat delivery is not connected yet. Input receipts confirm submission only.</p>
  </section>;
}
