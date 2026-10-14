import { render, type ComponentChildren } from 'preact';
import { useEffect, useLayoutEffect, useReducer, useRef, useState } from 'preact/hooks';
import { Store, correctable, locked, meaningful, nameProblem, stripName, type Pending } from './store';
import { Icon, IconPicker } from './icons';
import { ViewIcon } from './view-icons';
import type { Agent, Entry } from './wire';
import { TerminalPane, TerminalSummary } from './terminal';
import { collectionReasons, nativeStates, sessionStatus, setupReasons } from './runtime-view';
import { fileReferenceEntry } from './file-reference';
import { FileReferenceCard } from './file-reference-card';
import { inboxReferenceEntry } from './inbox-reference';
import { browserRequestEntry } from './browser-view';
import { BrowserRequestCard, BrowserWorkspace, ComputerBrowser } from './browser';
import { MutationNotice as PendingNotice } from './mutation-notice';
import { NativeAttentionNotice } from './native-attention';
import './style.css';

const store = new Store();
function useStore() { const [, redraw] = useReducer(n => n + 1, 0); useEffect(() => store.subscribe(() => redraw(null)), []); return store; }
function useMedia(query: string) {
  const [matches, set] = useState(() => matchMedia(query).matches);
  useEffect(() => { const media = matchMedia(query); const update = () => set(media.matches); media.addEventListener('change', update); return () => media.removeEventListener('change', update); }, [query]);
  return matches;
}
const time = (value: string | null) => value ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Unknown';
const relativeFormatter = new Intl.RelativeTimeFormat('en', { numeric: 'always' });
function relativeTime(value: string, now: number) {
  const minutes = Math.max(0, Math.floor((now - Date.parse(value)) / 60000));
  if (!Number.isFinite(minutes)) return 'Unknown date';
  if (minutes < 1) return 'just now';
  if (minutes < 60) return relativeFormatter.format(-minutes, 'minute');
  if (minutes < 1440) return relativeFormatter.format(-Math.floor(minutes / 60), 'hour');
  return relativeFormatter.format(-Math.floor(minutes / 1440), 'day');
}
function Dialog({ title, children, onClose, drawer = '' }: { title: string; children: ComponentChildren; onClose: () => void; drawer?: string }) {
  const ref = useRef<HTMLDialogElement>(null);
  const close = useRef(onClose); close.current = onClose;
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => { ref.current?.close(); previous?.focus(); };
  }, []);
  return <dialog ref={ref} className={`dialog ${drawer}`} aria-label={title} onCancel={e => { e.preventDefault(); close.current(); }} onKeyDown={e => {
    if (e.key !== 'Tab') return;
    const focusable = [...e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]')].filter(el => el.tabIndex >= 0 && el.checkVisibility());
    const first = focusable[0], last = focusable.at(-1);
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
  }} onClick={e => {
    if (ref.current && e.target === ref.current) {
      const box = ref.current.getBoundingClientRect();
      if (e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom) close.current();
    }
  }}>
    <div className="dialog-title"><h2>{title}</h2><button type="button" aria-label={`Close ${title}`} onClick={onClose}>×</button></div>
    {children}
  </dialog>;
}
function MutationNotice(props: { item?: Pending; requestKey: string }) { return <PendingNotice store={store} {...props} />; }
function ConnectionPanel() {
  return <main className="connection-page"><div className="connection-panel">
    <div className="brand"><span className="brand-mark">q</span><span>Qorqbot</span></div>
    <h1>Your agent workspace</h1>
    {store.error && <p className="error" role="alert">{store.error}</p>}
    {store.phase === 'checking' ? <p role="status">Connecting…</p> : <>
      <button onClick={() => void store.open()}>Retry connection</button>
      <p className="quiet">Unsent drafts remain on this page.</p>
    </>}
  </div></main>;
}
function Creation({ close }: { close: () => void }) {
  const pending = store.pending.get('create');
  const [name, setName] = useState((pending?.args.display_name as string) || '');
  const [icon, setIcon] = useState((pending?.args.icon_key as string) || 'circle');
  const [attempted, setAttempted] = useState(false);
  const problem = attempted || name ? nameProblem(name) : '';
  const lastState = useRef(pending?.state);
  useEffect(() => {
    if (lastState.current === 'sending' && !pending) close();
    lastState.current = pending?.state;
  }, [pending, pending?.state]);
  return <Dialog title="Create agent" onClose={close}>
    <form className="creation" onSubmit={e => { e.preventDefault(); setAttempted(true); if (!nameProblem(name)) void store.create({ display_name: name, icon_key: icon }); }}>
      <label>Display name<input value={name} onInput={e => setName(e.currentTarget.value)} autoFocus disabled={locked(pending)} aria-invalid={!!problem} aria-describedby={problem ? 'name-problem' : undefined} />
        {problem && <span className="field-error" id="name-problem" role="alert">{problem}</span>}</label>
      <IconPicker value={icon} onChange={setIcon} keys={store.config!.icon_keys} disabled={locked(pending)} />
      <p className="quiet">Creates a local folder and terminal. Start and configure your CLI in Terminal.</p>
      <MutationNotice item={pending} requestKey="create" />
      <div className="form-actions"><button type="button" onClick={close}>Close</button><button className="primary" type="submit" disabled={locked(pending)}>Create agent</button></div>
    </form>
  </Dialog>;
}
function RenameInput({ agent, done, trigger }: { agent: Agent; done: () => void; trigger: string }) {
  const failed = store.pending.get(`edit:${agent.id}`);
  // A definitive failure keeps the attempted text so it can be corrected in place.
  const [name, set] = useState(correctable(failed) && typeof failed!.args.display_name === 'string' ? failed!.args.display_name : agent.display_name);
  const [problem, setProblem] = useState('');
  const ref = useRef<HTMLInputElement>(null);
  const original = useRef({ id: agent.id, name: agent.display_name, selected: store.selected, epoch: store.epoch, actor: store.actor });
  const finished = useRef(false);
  useLayoutEffect(() => () => { finished.current = true; }, []);
  useEffect(() => { ref.current?.focus(); ref.current?.select(); }, []);
  const finish = (save: boolean, restore: boolean) => {
    if (finished.current) return;
    const bad = save ? nameProblem(name) : '';
    if (bad) { setProblem(bad); return; }
    finished.current = true; done();
    // Only keyboard exits return focus; a blur must not steal it from the next target.
    if (restore) {
      // The trigger is disabled while the save is in flight; wait for it unless focus has moved on.
      const refocus = (tries: number) => {
        if (document.activeElement && document.activeElement !== document.body) return;
        const control = document.querySelector<HTMLButtonElement>(`[data-rename-trigger="${trigger}"]`);
        if (control && !control.disabled) control.focus(); else if (tries) setTimeout(() => refocus(tries - 1), 50);
      };
      setTimeout(() => refocus(20));
    }
    const start = original.current, value = stripName(name);
    if (save && value !== start.name && store.phase === 'ready' && store.selected === start.selected && store.epoch === start.epoch && store.actor === start.actor) {
      void store.update(start.id, { display_name: value });
    }
  };
  const errorId = `rename-problem-${trigger}`;
  return <span className="rename-field"><input ref={ref} aria-label={`Rename ${agent.display_name}`} value={name} className="rename-input" disabled={locked(failed)} aria-invalid={!!problem} aria-describedby={problem ? errorId : undefined}
    onInput={e => { set(e.currentTarget.value); setProblem(''); }} onBlur={() => finish(true, false)} onKeyDown={e => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Escape') { e.preventDefault(); finish(false, true); }
      if (e.key === 'Enter') { e.preventDefault(); finish(true, true); }
    }} />
    {problem && <span className="field-error" id={errorId} role="alert">{problem}</span>}</span>;
}
function RenameIcon() {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" /></svg>;
}
function Navigation({ select, create, renameId, setRename }: { select: (id: string) => void; create: () => void; renameId: string; setRename: (id: string) => void }) {
  return <nav className="navigation" aria-label="Agents">
    <div className="section-label">AGENTS <span>{store.agents.length}</span></div>
    <div className="agent-list">
      {store.directoryLoading && <p role="status">Loading agents…</p>}
      {!store.has('agents_list') && <p className="quiet">Agent directory unavailable on this server.</p>}
      {store.has('agents_list') && !store.directoryLoading && !store.agents.length && <p className="quiet">No agents registered.</p>}
      {store.agents.map(agent => <div className={`agent-row ${store.selected === agent.id ? 'selected' : ''}`} key={agent.id}>
        {renameId === agent.id ? <div className="rename-row"><Icon name={agent.icon_key} /><RenameInput key={agent.id} agent={agent} trigger={`side:${agent.id}`} done={() => setRename('')} /></div> : <>
          <button className="agent-select" aria-pressed={store.selected === agent.id} onClick={() => select(agent.id)}>
            <span className="agent-icon"><Icon name={agent.icon_key} /></span><span className="agent-labels"><strong>{agent.display_name}</strong>
              <span className="agent-status">{agent.status.text}</span>
              {agent.needs_user_input && <span className="agent-attention" title="Needs your attention" aria-label="Needs your attention">●</span>}
              {agent.needs_user_input && <span className="agent-attention-label">Needs input</span>}
              {store.agents.filter(a => a.display_name === agent.display_name).length > 1 && <span className="disambiguation">{agent.id}</span>}
            </span>
          </button>
          {store.has('agents_update') && <button className="row-rename" type="button" data-rename-trigger={`side:${agent.id}`} aria-label={`Rename ${agent.display_name}`} title="Rename" disabled={locked(store.pending.get(`edit:${agent.id}`))}
            onClick={e => { e.stopPropagation(); setRename(agent.id); }}><RenameIcon /></button>}
        </>}
        {store.selected !== agent.id && <MutationNotice item={store.pending.get(`edit:${agent.id}`)} requestKey={`edit:${agent.id}`} />}
      </div>)}
    </div>
    <div className="nav-bottom"><span className="quiet connection">{store.connection}</span>
      {store.has('agents_create') && store.config && <button className="new-agent" aria-label="Create agent" title="Create agent" onClick={create}>＋</button>}
    </div>
  </nav>;
}
const deliveryNames: Record<string, string> = { pending: 'Queued', deferred: 'Queued', dispatching: 'Sending to Terminal', submitted: 'Submitted', uncertain: 'Delivery uncertain', failed: 'Delivery failed', cancelled: 'Delivery cancelled' };
function deliveryLabel(delivery: NonNullable<Entry['delivery']>) {
  const reason = delivery.reason && !['pending', 'dispatching'].includes(delivery.state) ? ` · ${delivery.reason.replaceAll('_', ' ')}` : '';
  return `${deliveryNames[delivery.state] || `Delivery: ${delivery.state}`}${reason}`;
}
function EntryRow({ entry }: { entry: Entry }) {
  const [hoverTime, setHoverTime] = useState(Date.now);
  const [copyState, setCopyState] = useState('');
  const inbox = inboxReferenceEntry(entry);
  const nativeInput = entry.kind === 'message' && entry.source?.type === 'native' && entry.role === 'user' && entry.sender_actor_id == null;
  const sender = nativeInput ? 'Terminal input' : store.agents.find(a => a.actor_id === entry.sender_actor_id)?.display_name || (entry.role === 'user' ? 'Owner' : 'Agent');
  const reference = fileReferenceEntry(entry) ? entry.source.reference : null;
  const browser = browserRequestEntry(entry) && entry.agent_id === store.selected && entry.conversation_id === store.agent()?.conversation_id;
  const message = entry.kind === 'message' && (entry.source?.type === 'message' || entry.source?.type === 'native');
  const copy = async () => {
    try { await navigator.clipboard.writeText(entry.text); setCopyState('Copied'); setTimeout(() => setCopyState(state => state === 'Copied' ? '' : state), 2500); }
    catch { setCopyState('Could not copy. Select the text to copy it.'); }
  };
  return <article className={`entry ${entry.kind === 'browser_request' ? 'browser-entry' : inbox ? 'inbox-reference-entry' : entry.role === 'user' ? 'owner-entry' : ''}`} data-entry-id={entry.entry_id}
    onMouseEnter={() => { setHoverTime(Date.now()); setCopyState(''); }} onFocusCapture={() => setHoverTime(Date.now())}>
    <header className="message-meta"><strong>{sender}</strong><time dateTime={entry.occurred_at} title={time(entry.occurred_at)}>{relativeTime(entry.occurred_at, hoverTime)}</time></header>
    <div className="message-bubble">
    {browser ? <BrowserRequestCard request={entry.browser_request!} /> : entry.kind === 'browser_request' ? <div className="browser-card"><p className="message-body">{entry.text}</p><p className="quiet">Browser request unavailable. Its details could not be verified.</p></div> : inbox ? <details className="inbox-reference">
      <summary>Inbox reference</summary>
      <p className="message-body">{entry.text}</p>
      <p className="inbox-reference-id">Message ID: {entry.inbox_reference!.message_id}</p>
    </details> : reference ? <FileReferenceCard reference={reference} /> : message ? <p className="message-body">{entry.text}</p> : <p className="unsupported">Unsupported content: {entry.kind}</p>}
    <div className="delivery">
      {entry.handling && <span title={`Handled by ${entry.handling.handled_by}`}>Handled · {time(entry.handling.handled_at)}</span>}
      {entry.delivery && (entry.handling ? <details className="delivery-history"><summary>Delivery history</summary><span>{deliveryLabel(entry.delivery)}</span></details> : <span>{deliveryLabel(entry.delivery)}</span>)}
    </div>
    </div>
    {message && <div className="message-actions"><button type="button" className="copy-message" aria-label="Copy message" title={copyState || 'Copy message'} onClick={() => void copy()}><ViewIcon name={copyState === 'Copied' ? 'check' : 'copy'} /></button></div>}
    {message && <span className="copy-feedback" role="status">{copyState}</span>}
  </article>;
}
function Chat({ agent }: { agent: Agent }) {
  const chat = store.chat(agent.id);
  const draft = store.draft(agent.id);
  const pending = store.pending.get(`send:${agent.id}`);
  const bytes = new TextEncoder().encode(draft.text).length;
  const ref = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const previous = useRef<{ id: string; first?: string; last?: string; height: number }>({ id: '', height: 0 });
  const [newMessages, setNewMessages] = useState(false);
  const composing = useRef(false);
  const fitComposer = () => {
    const input = composerRef.current; if (!input) return;
    input.style.height = '0px'; input.style.height = `${input.scrollHeight}px`;
  };
  useLayoutEffect(fitComposer, [agent.id, draft.text]);
  useLayoutEffect(() => {
    const input = composerRef.current; if (!input) return;
    let width = input.clientWidth;
    const observer = new ResizeObserver(() => { if (input.clientWidth !== width) { width = input.clientWidth; fitComposer(); } });
    observer.observe(input); return () => observer.disconnect();
  }, [agent.id]);
  useLayoutEffect(() => {
    const box = ref.current; if (!box) return;
    const prior = previous.current;
    const first = chat?.items[0]?.entry_id, last = chat?.items.at(-1)?.entry_id;
    const saved = store.scrolls.get(agent.id);
    if (prior.id !== agent.id) {
      box.scrollTop = saved ? saved.top : box.scrollHeight; setNewMessages(false);
    } else if (first && prior.first && first !== prior.first && chat?.change === 'prepend') {
      box.scrollTop += box.scrollHeight - prior.height;
    } else if (last !== prior.last) {
      if (!saved || saved.bottom) box.scrollTop = box.scrollHeight;
      else setNewMessages(true);
    }
    previous.current = { id: agent.id, first, last, height: box.scrollHeight };
    store.scrolls.set(agent.id, { top: box.scrollTop, bottom: box.scrollHeight - box.scrollTop - box.clientHeight < 64 });
  }, [agent.id, chat?.items]);
  const sendable = meaningful(draft.text) && bytes <= 65536 && !pending;
  return <>
    {store.has('messages_send') && <a className="skip-link" href="#message" onClick={e => { e.preventDefault(); composerRef.current?.focus(); }}>Skip to message</a>}
    <div className="history" ref={ref} aria-label={`Conversation with ${agent.display_name}`} onScroll={e => {
      const box = e.currentTarget;
      const bottom = box.scrollHeight - box.scrollTop - box.clientHeight < 64;
      store.scrolls.set(agent.id, { top: box.scrollTop, bottom });
      if (bottom) setNewMessages(false);
    }}>
      <div className="history-content">
        {chat?.hasOlder && <button className="earlier" onClick={() => void store.refreshConversation('earlier')} disabled={chat.earlier}>{chat.earlier ? 'Loading earlier…' : 'Load earlier'}</button>}
        {chat?.earlierError && <div className="error earlier-error" role="alert">{chat.earlierError} <button onClick={() => void store.refreshConversation('earlier')}>Retry earlier messages</button></div>}
        {!store.has('conversation_list') ? <p className="empty">Conversation history unavailable on this server.</p> : chat?.loading ? <p className="empty" role="status">Loading conversation…</p> : chat?.loaded && !chat.error && !chat.items.length && <div className="empty"><Icon name={agent.icon_key} size={32} /><h2>A conversation with {agent.display_name}</h2><p>No messages saved yet.</p></div>}
        {chat?.items.map(entry => <EntryRow key={entry.entry_id} entry={entry} />)}
        {chat?.error && <div className="error" role="alert">{chat.error} <button onClick={() => void store.refreshConversation(chat.epoch ? 'refresh' : 'initial')}>Retry history</button></div>}
      </div>
    </div>
    {newMessages && <button className="new-messages" onClick={() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; setNewMessages(false); }}>New messages ↓</button>}
    <div className="composer-wrap">
      <MutationNotice item={pending} requestKey={`send:${agent.id}`} />
      <NativeAttentionNotice agent={agent} observation={store.runtimes.get(agent.id)} readUnavailable={store.connection === 'Offline'} openTerminal={() => store.setView(agent.id, 'terminal')} />
      {store.has('messages_send') ? <form className="composer" onSubmit={e => { e.preventDefault(); if (sendable) void store.send(agent.id); }}>
        <label className="sr-only" htmlFor="message">Message {agent.display_name}</label>
        <textarea id="message" ref={composerRef} placeholder={`Message ${agent.display_name}`} value={draft.text} rows={1} onInput={e => store.setDraft(agent.id, e.currentTarget.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={e => {
          if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !composing.current && e.keyCode !== 229) { e.preventDefault(); if (sendable) void store.send(agent.id); }
        }} />
        <button className="primary send-message" type="submit" aria-label="Send" title="Send" disabled={!sendable}><ViewIcon name="send" /></button>
      </form> : <p className="quiet">Sending messages is unavailable on this server.</p>}
      {bytes > 65536 && <p className="composer-limit error" role="alert">{bytes.toLocaleString()} / 65,536 bytes — message is too long</p>}
    </div>
  </>;
}
function ObservationTime({ value }: { value: string | null }) {
  return value ? <time dateTime={value} title={value}>{time(value)}</time> : <>No observation time available.</>;
}
function RuntimeSummary({ id }: { id: string }) {
  const supported = store.has('runtime_get');
  const observation = store.runtimes.get(id);
  const view = observation?.view, owner = view?.current_owner;
  const setup = view?.native_setup, capture = view?.native_capture;
  return <>
    <section aria-label="Session"><h3>Session</h3>
      {!supported ? <p className="quiet">Session information is unavailable on this server.</p> : <>
        {view ? <>
          <p>Last reported{observation?.error ? ' (stale)' : ''}: {sessionStatus[view.status]}</p>
          <p className="quiet">Observed: <ObservationTime value={view.observed_at} /></p>
          {owner && <>
            <dl><dt>Reported provider</dt><dd>{owner.provider === 'claude' ? 'Claude' : 'Codex'}</dd><dt>CLI version</dt><dd>{owner.cli_version}</dd><dt>Connected at</dt><dd><ObservationTime value={owner.connected_at} /></dd></dl>
            {view.status === 'unknown' && <p className="quiet runtime-note">The last-reported session identity is retained while its connection is unknown.</p>}
            <details className="runtime-details"><summary>Session details</summary><dl className="identity-details"><dt>Native session ID</dt><dd>{owner.native_session_id}</dd><dt>Run ID</dt><dd>{owner.run_id}</dd><dt>Run generation</dt><dd>{owner.run_generation}</dd></dl></details>
          </>}
          {view.status === 'disconnected' && <p className="quiet">Start and manage the CLI manually in Terminal.</p>}
        </> : !observation?.error && !observation?.loading && <p className="quiet">No session observation available.</p>}
        {observation?.error && <p role="alert">{observation.error}</p>}
        {observation?.loading && <p className="quiet" role="status">{view ? 'Refreshing last-reported session observation…' : 'Loading session observation…'}</p>}
        {observation?.error && <button type="button" onClick={() => void store.refreshRuntime(true)}>Retry session information</button>}
      </>}
    </section>
    <section aria-label="Native history"><h3>Native history</h3>
      {!supported ? <p className="quiet">Native history information is unavailable on this server.</p> : view ? <>
        <p className="quiet">Last-reported observations{observation?.error ? ' · stale' : ''}{observation?.loading ? ' · refreshing…' : ''}</p>
        {setup ? <dl><dt>Setup</dt><dd>{nativeStates[setup.state]} · {setupReasons[setup.reason]}</dd><dt>Setup observed</dt><dd><ObservationTime value={setup.observed_at} /></dd></dl> : <p className="quiet">No setup observation available.</p>}
        {capture ? <dl><dt>Collection</dt><dd>{nativeStates[capture.state]} · {collectionReasons[capture.reason]}</dd><dt>Collection observed</dt><dd><ObservationTime value={capture.observed_at} /></dd><dt>Sources</dt><dd>{capture.source_count.toLocaleString()}</dd><dt>Bytes awaiting archival</dt><dd>{capture.pending_archive_bytes.toLocaleString()}</dd><dt>Bytes awaiting parsing</dt><dd>{capture.pending_parse_bytes.toLocaleString()}</dd></dl> : <p className="quiet">No collection observation available.</p>}
      </> : <p className="quiet">{observation?.loading ? 'Loading native history observations…' : 'No native history observation available.'}</p>}
      <p className="quiet runtime-note">Collected native history and Terminal recording are separate. These observations do not confirm Chat delivery or complete history coverage.</p>
    </section>
  </>;
}
function Inspector({ agent, changeIcon }: { agent: Agent; changeIcon: () => void }) {
  const chat = store.chat(agent.id), publication = chat?.publication;
  const [renaming, setRenaming] = useState(false);
  const tabs = ['Details', 'Library', 'Computer'] as const;
  const [tab, setTab] = useState<typeof tabs[number]>('Details');
  const pending = store.pending.get(`edit:${agent.id}`), editable = store.has('agents_update');
  const files = [...new Map((chat?.items || []).filter(fileReferenceEntry).map(entry => [entry.source.reference.reference_id, entry.source.reference])).values()];
  return <aside className="inspector" aria-label="Agent details">
    <div className="inspector-identity">
      {editable ? <button className="identity-icon" aria-label="Change icon" title="Change icon" onClick={changeIcon} disabled={locked(pending)}><Icon name={agent.icon_key} size={56} /></button> : <Icon name={agent.icon_key} size={56} />}
      <h2>{renaming ? <RenameInput key={agent.id} agent={agent} trigger={`inspector:${agent.id}`} done={() => setRenaming(false)} /> : editable ? <button className="identity-name" data-rename-trigger={`inspector:${agent.id}`} aria-label={`Rename ${agent.display_name}`} title="Rename" onClick={() => setRenaming(true)} disabled={locked(pending)}>{agent.display_name}</button> : agent.display_name}</h2>
    </div>
    <div className="inspector-tabs" role="tablist" aria-label="Agent details tabs">{tabs.map(name => <button key={name} type="button" role="tab" id={`tab-${agent.id}-${name}`} aria-controls={`panel-${agent.id}`} aria-selected={tab === name} tabIndex={tab === name ? 0 : -1} onClick={() => setTab(name)} onKeyDown={e => {
      const index = tabs.indexOf(name);
      const next = e.key === 'ArrowRight' ? (index + 1) % tabs.length : e.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : -1;
      if (next < 0) return;
      e.preventDefault(); setTab(tabs[next]);
      e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('button')[next].focus();
    }}>{name}</button>)}</div>
    <MutationNotice item={pending} requestKey={`edit:${agent.id}`} />
    <div className="inspector-tabpanel" role="tabpanel" id={`panel-${agent.id}`} aria-labelledby={`tab-${agent.id}-${tab}`}>
      {tab === 'Details' ? <>
        <section className="current-work"><h3>Current work</h3><p>{agent.status.text}</p>
          {(agent.activity !== 'unknown' || agent.needs_user_input) && <dl>{agent.activity !== 'unknown' && <><dt>Activity</dt><dd>{agent.activity}</dd></>}{agent.needs_user_input && <><dt>Attention</dt><dd>Needs your input</dd></>}</dl>}
        </section>
        <details className="more-details"><summary>More details</summary>
          <dl><dt>Agent type</dt><dd>{agent.kind === 'host' ? 'Local agent' : 'Legacy VM agent'} · {agent.lifecycle}</dd><dt>Status reviewed</dt><dd>{time(agent.status.reviewed_at)}</dd><dt>Activity observed</dt><dd>{time(agent.activity_observed_at)}</dd><dt>Last contact</dt><dd>{time(agent.last_contact_at)}</dd></dl>
          <RuntimeSummary id={agent.id} />
          <section><h3>Transcript publication</h3>{publication ? <><p>{publication.state === 'current' ? 'Current' : publication.state === 'blocked' ? 'Blocked' : 'Pending'} · {publication.pending_entries} pending entries</p>{publication.error && <p className="error">{publication.error.code}: {publication.error.message}</p>}<p className="quiet">Last saved publication observation.</p></> : <p className="quiet">No publication observation available.</p>}</section>
          <details><summary>Identity & workspace</summary><dl className="identity-details"><dt>Agent ID</dt><dd>{agent.id}</dd><dt>Conversation ID</dt><dd>{agent.conversation_id}</dd><dt>Workspace</dt><dd>{agent.workspace_path}</dd><dt>Workspace initialized</dt><dd>{agent.workspace_initialized_at ? time(agent.workspace_initialized_at) : 'Not yet'}</dd><dt>Metadata revision</dt><dd>{agent.revision}</dd></dl></details>
        </details>
      </> : tab === 'Library' ? <div className="shared-files">
        <h3>Files shared in chat</h3>
        {files.map(reference => <FileReferenceCard key={reference.reference_id} reference={reference} />)}
        {!store.has('conversation_list') ? <p className="quiet">Shared files are unavailable on this server.</p> : chat?.loading ? <p className="quiet" role="status">Loading shared files…</p> : chat?.loaded && !files.length && <p className="quiet">No files shared in the loaded chat pages.</p>}
        {chat?.error && <div className="error" role="alert">{chat.error} <button onClick={() => void store.refreshConversation(chat.epoch ? 'refresh' : 'initial')}>Retry shared files</button></div>}
        {chat?.earlierError && <div className="error" role="alert">{chat.earlierError} <button onClick={() => void store.refreshConversation('earlier')}>Retry earlier shared files</button></div>}
        {chat?.hasOlder && <button onClick={() => void store.refreshConversation('earlier')} disabled={chat.earlier}>{chat.earlier ? 'Loading earlier shared files…' : 'Load earlier shared files'}</button>}
      </div> : <>
        <ComputerBrowser agent={agent} />
        <section aria-label="Terminal status"><h3>Terminal</h3><TerminalSummary store={store} id={agent.id} /></section>
      </>}
    </div>
  </aside>;
}
function App() {
  useStore();
  const mobile = useMedia('(max-width: 700px)'), overlayInspector = useMedia('(max-width: 1100px)');
  const [navOpen, setNavOpen] = useState(false), [inspectOpen, setInspectOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false), [iconAgent, setIconAgent] = useState('');
  const [icon, setIcon] = useState('circle'), [renameId, setRename] = useState('');
  const agent = store.agent();
  useEffect(() => { void store.open(); document.addEventListener('visibilitychange', store.visibility); return () => document.removeEventListener('visibilitychange', store.visibility); }, []);
  useEffect(() => { if (store.phase !== 'ready') { setNavOpen(false); setInspectOpen(false); setCreateOpen(false); setIconAgent(''); setRename(''); } }, [store.phase]);
  const select = (id: string) => { store.select(id); setNavOpen(false); setRename(''); };
  const navigation = <Navigation select={select} create={() => { setNavOpen(false); setCreateOpen(true); }} renameId={renameId} setRename={setRename} />;
  const inspector = agent && <Inspector key={agent.id} agent={agent} changeIcon={() => { setIconAgent(agent.id); setIcon(agent.icon_key); }} />;
  if (store.phase !== 'ready') return <ConnectionPanel />;
  return <BrowserWorkspace store={store}><div className={`workspace ${!overlayInspector && store.inspector && agent ? 'with-inspector' : ''}`}>
    {!mobile && navigation}
    <main className="conversation">
      <div className="workspace-controls">
        {mobile && <button aria-label="Open agents" title="Agents" aria-expanded={navOpen} onClick={() => setNavOpen(true)}><ViewIcon name="agents" /></button>}
        {agent && <div className="view-switch" role="group" aria-label="Agent view"><button aria-label="Chat" title="Chat" aria-pressed={store.view(agent.id) === 'chat'} onClick={() => store.setView(agent.id, 'chat')}><ViewIcon name="chat" /></button><button aria-label="Terminal" title="Terminal" aria-pressed={store.view(agent.id) === 'terminal'} onClick={() => store.setView(agent.id, 'terminal')}><ViewIcon name="terminal" /></button></div>}
        {agent && <button aria-label="Details" title="Details" aria-expanded={overlayInspector ? inspectOpen : store.inspector} onClick={() => overlayInspector ? setInspectOpen(true) : store.toggleInspector()}><ViewIcon name="details" /></button>}
      </div>
      {store.error && <div className="connection-error error" role="alert">{store.error}</div>}
      {store.notice && <div className="notice" role="status">{store.notice}<button aria-label="Dismiss notice" onClick={() => { store.notice = ''; store.emit(); }}>×</button></div>}
      {agent && !(overlayInspector ? inspectOpen : store.inspector) && <MutationNotice item={store.pending.get(`edit:${agent.id}`)} requestKey={`edit:${agent.id}`} />}
      {agent ? store.view(agent.id) === 'terminal' ? <TerminalPane key={agent.id} store={store} id={agent.id} /> : <Chat agent={agent} /> : <div className="empty"><h2>Select an agent</h2><p>Conversations stay with each agent.</p></div>}
    </main>
    {!overlayInspector && store.inspector && inspector}
    {mobile && navOpen && <Dialog title="Agents" drawer="drawer left-drawer" onClose={() => setNavOpen(false)}>{navigation}</Dialog>}
    {overlayInspector && inspectOpen && inspector && <Dialog title="Inspector" drawer="drawer right-drawer" onClose={() => setInspectOpen(false)}>{inspector}</Dialog>}
    {createOpen && store.config && <Creation close={() => setCreateOpen(false)} />}
    {iconAgent && store.config && <Dialog title="Change icon" onClose={() => setIconAgent('')}><IconPicker value={icon} onChange={setIcon} keys={store.config.icon_keys} /><div className="form-actions"><button onClick={() => setIconAgent('')}>Cancel</button><button className="primary" onClick={() => { const id = iconAgent; setIconAgent(''); void store.update(id, { icon_key: icon }); }}>Save icon</button></div></Dialog>}
  </div></BrowserWorkspace>;
}

render(<App />, document.getElementById('app')!);
