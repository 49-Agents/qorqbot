import type { ComponentChildren } from 'preact';
import { useId, useLayoutEffect, useRef, useState } from 'preact/hooks';
import type { Agent } from './wire';
import type { RuntimeObservation } from './store';

function ExpandablePrompt({ children }: { children: ComponentChildren }) {
  const [expanded, setExpanded] = useState(false), [long, setLong] = useState(false);
  const content = useRef<HTMLDivElement>(null), id = useId();
  useLayoutEffect(() => {
    const element = content.current;
    if (!element) return;
    const measure = () => setLong(element.scrollHeight > parseFloat(getComputedStyle(element).lineHeight) * 6 + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return <>
    <div id={id} className={`native-attention-prompt ${expanded ? 'expanded' : ''}`}><div ref={content}>{children}</div></div>
    {long && <button type="button" className="native-attention-expand" aria-expanded={expanded} aria-controls={id} onClick={() => setExpanded(!expanded)}>{expanded ? 'Show less' : 'Show full prompt'}</button>}
  </>;
}

export function NativeAttentionNotice({ agent, observation, readUnavailable = false, openTerminal }: {
  agent: Agent; observation?: RuntimeObservation; readUnavailable?: boolean; openTerminal: () => void;
}) {
  const view = observation?.view, attention = view?.attention;
  if (!observation?.error && !readUnavailable && view?.status === 'disconnected') return null;
  const unavailable = !!observation?.error || (!!attention && (readUnavailable || attention.state === 'unknown' || view?.status === 'unknown'));
  if (unavailable) return <section className="native-attention unavailable" aria-label="Native input observation">
    <p role="status">Native input observation unavailable. Check Terminal for any current request.</p>
    <button type="button" onClick={openTerminal}>Open Terminal</button>
  </section>;
  if (!view || attention?.state !== 'waiting' || view.status !== 'connected'
    || view.agent_id !== agent.id || view.current_owner?.conversation_id !== agent.conversation_id
    || attention.owner_id !== view.current_owner?.owner_id || attention.terminal_id !== view.current_owner?.terminal_id) return null;
  return <section className="native-attention" aria-label="Native input request">
    <div className="native-attention-heading"><div><strong>Needs your input</strong><p>{attention.kind === 'approval' ? 'Approval requested' : `Question from ${agent.display_name}`}</p></div>
      <button type="button" onClick={openTerminal}>Open Terminal</button></div>
    <ExpandablePrompt key={`${attention.owner_id}:${attention.terminal_id}:${attention.kind}:${attention.prompt}`}><p className="native-attention-text">{attention.prompt}</p></ExpandablePrompt>
    {attention.truncated && <p className="native-attention-note">Prompt truncated. See Terminal for the full request.</p>}
    <p className="native-attention-note">Last observed <time dateTime={attention.observed_at} title={attention.observed_at}>{new Date(attention.observed_at).toLocaleTimeString()}</time>{observation?.loading ? ' · Refreshing observation…' : ''}</p>
  </section>;
}
