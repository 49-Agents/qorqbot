import { render, type ComponentChildren } from 'preact';
import { useEffect, useLayoutEffect, useReducer, useRef, useState } from 'preact/hooks';
import { Store, type Pending } from './store';
import { Icon, IconPicker } from './icons';
import type { Agent, Entry } from './wire';
import { TerminalPane, TerminalSummary } from './terminal';
import { collectionReasons, nativeStates, sessionStatus, setupReasons } from './runtime-view';
import { fileReferenceEntry } from './file-reference';
import { FileReferenceCard } from './file-reference-card';
import './style.css';

const store = new Store();
function useStore() { const [, redraw] = useReducer(n => n + 1, 0); useEffect(() => store.subscribe(() => redraw(null)), []); return store; }
function useMedia(query: string) {
  const [matches, set] = useState(() => matchMedia(query).matches);
  useEffect(() => { const media = matchMedia(query); const update = () => set(media.matches); media.addEventListener('change', update); return () => media.removeEventListener('change', update); }, [query]);
  return matches;
}
const time = (value: string | null) => value ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Unknown';
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
    const focusable = [...e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]')].filter(el => el.getClientRects().length);
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
function MutationNotice({ item, requestKey }: { item?: Pending; requestKey: string }) {
  if (!item) return null;
  if (item.state === 'sending') return <p className="quiet" role="status">Saving request…</p>;
  return <div className="error mutation-error" role="alert">
    <p>{item.unresolvedAmbiguity ? 'Confirmation uncertain. ' : ''}{item.error}</p>
    {item.operation === 'agents_update' && <p>Attempted {item.args.display_name !== undefined ? `name: “${item.args.display_name}”` : `icon: ${item.args.icon_key}`}.</p>}
    {item.actor !== store.actor ? <p>Sign in as the original account to retry this request.</p> : <button type="button" onClick={() => void store.retry(requestKey)} disabled={!store.has(item.operation)}>
      {!item.unresolvedAmbiguity && item.state === 'conflict' ? 'Apply attempted change to current revision' : 'Retry original request'}
    </button>}
    {!item.unresolvedAmbiguity && (item.state === 'failed' || item.state === 'conflict') && <button type="button" onClick={() => store.discard(requestKey)}>Dismiss error</button>}
    {item.unresolvedAmbiguity && <p className="quiet">Retry keeps the exact request and idempotency key.</p>}
  </div>;
}
function SignIn() {
  const [token, setToken] = useState('');
  return <main className="sign-in"><div className="sign-in-panel">
    <div className="brand"><span className="brand-mark">q</span><span>Qorqut</span></div>
    <h1>Your agent workspace</h1><p className="quiet">Sign in with this installation’s console token.</p>
    {store.error && <p className="error" role="alert">{store.error}</p>}
    {store.phase === 'checking' ? <p role="status">Connecting…</p> : store.phase === 'error' ? <button onClick={() => void store.open()}>Retry connection</button> : <form onSubmit={e => {
      e.preventDefault(); const value = token; setToken(''); void store.login(value);
    }}>
      <label>Console token<input type="password" name="console-token" value={token} onInput={e => setToken(e.currentTarget.value)} autoComplete="off" autoCapitalize="off" spellcheck={false} required autoFocus /></label>
      <button className="primary" type="submit" disabled={!token}>Sign in</button>
    </form>}
    {store.needsLogoutRetry() && <button onClick={() => void store.logout()}>Retry sign-out</button>}
    <p className="footnote">The token is used once to establish a browser session.</p>
  </div></main>;
}
function Creation({ close }: { close: () => void }) {
  const pending = store.pending.get('create');
  const [name, setName] = useState((pending?.args.display_name as string) || '');
  const [icon, setIcon] = useState((pending?.args.icon_key as string) || 'circle');
  const lastState = useRef(pending?.state);
  useEffect(() => {
    if (lastState.current === 'sending' && !pending) close();
    lastState.current = pending?.state;
  }, [pending, pending?.state]);
  return <Dialog title="Create agent" onClose={close}>
    <form className="creation" onSubmit={e => { e.preventDefault(); void store.create({ display_name: name, icon_key: icon }); }}>
      <label>Display name<input value={name} onInput={e => setName(e.currentTarget.value)} required autoFocus disabled={!!pending} /></label>
      <IconPicker value={icon} onChange={setIcon} keys={store.config!.icon_keys} disabled={!!pending} />
      <p className="quiet">Registration starts provisioning only. Creating an agent does not start a CLI; you start and configure it yourself in Terminal. Runtime availability is reported separately.</p>
      <MutationNotice item={pending} requestKey="create" />
      <div className="form-actions"><button type="button" onClick={close}>Close</button><button className="primary" type="submit" disabled={!!pending || !name.trim()}>Create agent</button></div>
    </form>
  </Dialog>;
}
function RenameInput({ agent, done }: { agent: Agent; done: () => void }) {
  const [name, set] = useState(agent.display_name);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { ref.current?.focus(); ref.current?.select(); }, []);
  return <input ref={ref} aria-label={`Rename ${agent.display_name}`} value={name} className="rename-input" onInput={e => set(e.currentTarget.value)} onBlur={done} onKeyDown={e => {
    if (e.isComposing) return;
    if (e.key === 'Escape') { e.preventDefault(); done(); }
    if (e.key === 'Enter') { e.preventDefault(); done(); void store.update(agent.id, { display_name: name }); }
  }} />;
}
function Navigation({ select, create, renameId, setRename }: { select: (id: string) => void; create: () => void; renameId: string; setRename: (id: string) => void }) {
  return <nav className="navigation" aria-label="Agents">
    <div className="nav-top"><div className="brand"><span className="brand-mark">q</span><span>Qorqut</span></div>
      {store.has('agents_create') && store.config && <button className="new-agent" aria-label="Create agent" onClick={create}>＋</button>}</div>
    <div className="section-label">AGENTS <span>{store.agents.length}</span></div>
    <div className="agent-list">
      {store.directoryLoading && <p role="status">Loading agents…</p>}
      {!store.has('agents_list') && <p className="quiet">Agent directory unavailable on this server.</p>}
      {store.has('agents_list') && !store.directoryLoading && !store.agents.length && <p className="quiet">No agents registered.</p>}
      {store.agents.map(agent => <div className={`agent-row ${store.selected === agent.id ? 'selected' : ''}`} key={agent.id}>
        {renameId === agent.id ? <div className="rename-row"><Icon name={agent.icon_key} /><RenameInput agent={agent} done={() => setRename('')} /></div> : <button className="agent-select" aria-pressed={store.selected === agent.id} onClick={() => select(agent.id)} onDblClick={() => { if (store.has('agents_update')) setRename(agent.id); }}>
          <span className="agent-icon"><Icon name={agent.icon_key} /></span><span className="agent-labels"><strong>{agent.display_name}</strong>
            <span className="agent-status">{agent.status.text}</span><span className="agent-activity">{agent.needs_user_input ? 'Needs your attention · ' : ''}{agent.activity === 'unknown' ? 'Activity unknown' : agent.activity}</span>
            {store.agents.filter(a => a.display_name === agent.display_name).length > 1 && <span className="disambiguation">{agent.id}</span>}
          </span>
        </button>}
        {store.has('agents_update') && !renameId && !store.pending.has(`edit:${agent.id}`) && <button className="row-rename" aria-label={`Rename ${agent.display_name}`} title="Rename" onClick={() => setRename(agent.id)}>⋯</button>}
        {store.selected !== agent.id && <MutationNotice item={store.pending.get(`edit:${agent.id}`)} requestKey={`edit:${agent.id}`} />}
      </div>)}
    </div>
    <div className="nav-bottom"><span className="quiet connection">{store.connection}</span><button onClick={() => void store.logout()}>Sign out</button></div>
  </nav>;
}
function EntryRow({ entry }: { entry: Entry }) {
  const sender = store.agents.find(a => a.actor_id === entry.sender_actor_id)?.display_name || (entry.role === 'user' ? 'Owner' : 'Agent');
  const reference = fileReferenceEntry(entry) ? entry.source.reference : null;
  const message = entry.kind === 'message' && (entry.source?.type === 'message' || entry.source?.type === 'native');
  return <article className={`entry ${entry.role === 'user' ? 'owner-entry' : ''}`} data-entry-id={entry.entry_id}>
    <header><strong>{sender}</strong><time dateTime={entry.occurred_at} title={entry.occurred_at}>{reference && 'Observed '}{time(entry.occurred_at)}</time></header>
    {entry.recorded_at !== entry.occurred_at && <p className="recorded">Recorded <time dateTime={entry.recorded_at} title={entry.recorded_at}>{time(entry.recorded_at)}</time></p>}
    {reference ? <FileReferenceCard reference={reference} /> : message ? <p className="message-body">{entry.text}</p> : <p className="unsupported">Unsupported content: {entry.kind}</p>}
    <div className="delivery">
      {entry.delivery && <span>{({ pending: 'Stored · queued', deferred: 'Stored · deferred', dispatching: 'Stored · dispatching', submitted: 'Submitted', uncertain: 'Delivery uncertain', failed: 'Stored · delivery failed', cancelled: 'Stored · delivery cancelled' } as Record<string, string>)[entry.delivery.state] || `Delivery: ${entry.delivery.state}`}{entry.delivery.reason ? ` · ${entry.delivery.reason.replaceAll('_', ' ')}` : ''}</span>}
      {entry.handling && <span title={`Handled by ${entry.handling.handled_by}`}>Handled · {time(entry.handling.handled_at)}</span>}
    </div>
  </article>;
}
function Chat({ agent }: { agent: Agent }) {
  const chat = store.chat(agent.id);
  const draft = store.draft(agent.id);
  const pending = store.pending.get(`send:${agent.id}`);
  const bytes = new TextEncoder().encode(draft.text).length;
  const ref = useRef<HTMLDivElement>(null);
  const previous = useRef<{ id: string; first?: string; last?: string; height: number }>({ id: '', height: 0 });
  const [newMessages, setNewMessages] = useState(false);
  const composing = useRef(false);
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
  return <>
    <div className="history" ref={ref} aria-label={`Conversation with ${agent.display_name}`} onScroll={e => {
      const box = e.currentTarget;
      const bottom = box.scrollHeight - box.scrollTop - box.clientHeight < 64;
      store.scrolls.set(agent.id, { top: box.scrollTop, bottom });
      if (bottom) setNewMessages(false);
    }}>
      <div className="history-content">
        {chat?.hasOlder && <button className="earlier" onClick={() => void store.refreshConversation('earlier')} disabled={chat.earlier}>{chat.earlier ? 'Loading earlier…' : 'Load earlier'}</button>}
        {!store.has('conversation_list') ? <p className="empty">Conversation history unavailable on this server.</p> : chat?.loading ? <p className="empty" role="status">Loading conversation…</p> : !chat?.items.length && <div className="empty"><Icon name={agent.icon_key} size={32} /><h2>A conversation with {agent.display_name}</h2><p>No messages saved yet.</p></div>}
        {chat?.items.map(entry => <EntryRow key={entry.entry_id} entry={entry} />)}
        {chat?.error && <div className="error" role="alert">{chat.error} <button onClick={() => void store.refreshConversation(chat.epoch ? 'refresh' : 'initial')}>Retry history</button></div>}
      </div>
    </div>
    {newMessages && <button className="new-messages" onClick={() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; setNewMessages(false); }}>New messages ↓</button>}
    <div className="composer-wrap">
      <MutationNotice item={pending} requestKey={`send:${agent.id}`} />
      {store.has('messages_send') ? <form className="composer" onSubmit={e => { e.preventDefault(); void store.send(agent.id); }}>
        <label className="sr-only" htmlFor="message">Message {agent.display_name}</label>
        <textarea id="message" placeholder={`Message ${agent.display_name}`} value={draft.text} rows={3} onInput={e => store.setDraft(agent.id, e.currentTarget.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={e => {
          if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !composing.current && e.keyCode !== 229) { e.preventDefault(); if (!pending && bytes <= 65536) void store.send(agent.id); }
        }} />
        <div className="composer-footer"><span className={bytes > 65536 ? 'error' : 'quiet'}>{bytes > 65536 ? `${bytes.toLocaleString()} / 65,536 bytes — message is too long` : 'Enter to send · Shift + Enter for a new line'}</span><button className="primary" type="submit" disabled={!draft.text || bytes > 65536 || !!pending}>Send <span aria-hidden="true">↑</span></button></div>
      </form> : <p className="quiet">Sending messages is unavailable on this server.</p>}
      <p className="composer-note">Start and manage the CLI in Terminal. Messages are saved for delivery.</p>
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
          <p>Last reported: {sessionStatus[view.status]}</p>
          <p className="quiet">Observed: <ObservationTime value={view.observed_at} /></p>
          {owner && <>
            <dl><dt>Reported provider</dt><dd>{owner.provider === 'claude' ? 'Claude' : 'Codex'}</dd><dt>CLI version</dt><dd>{owner.cli_version}</dd><dt>Connected at</dt><dd><ObservationTime value={owner.connected_at} /></dd></dl>
            {view.status === 'unknown' && <p className="quiet runtime-note">The last-reported session identity is retained while its connection is unknown.</p>}
            <details className="runtime-details"><summary>Session details</summary><dl className="identity-details"><dt>Native session ID</dt><dd>{owner.native_session_id}</dd><dt>Run ID</dt><dd>{owner.run_id}</dd><dt>Run generation</dt><dd>{owner.run_generation}</dd></dl></details>
          </>}
          {view.status === 'disconnected' && <p className="quiet">Start and manage the CLI manually in Terminal.</p>}
        </> : observation?.error ? <p role="alert">{observation.error}</p> : !observation?.loading && <p className="quiet">No session observation available.</p>}
        {observation?.loading && <p className="quiet" role="status">{view ? 'Refreshing last-reported session observation…' : 'Loading session observation…'}</p>}
        {observation?.error && <button type="button" onClick={() => void store.refreshRuntime(true)}>Retry session information</button>}
      </>}
    </section>
    <section aria-label="Native history"><h3>Native history</h3>
      {!supported ? <p className="quiet">Native history information is unavailable on this server.</p> : view ? <>
        <p className="quiet">Last-reported observations{observation?.loading ? ' · refreshing…' : ''}</p>
        {setup ? <dl><dt>Setup</dt><dd>{nativeStates[setup.state]} · {setupReasons[setup.reason]}</dd><dt>Setup observed</dt><dd><ObservationTime value={setup.observed_at} /></dd></dl> : <p className="quiet">No setup observation available.</p>}
        {capture ? <dl><dt>Collection</dt><dd>{nativeStates[capture.state]} · {collectionReasons[capture.reason]}</dd><dt>Collection observed</dt><dd><ObservationTime value={capture.observed_at} /></dd><dt>Sources</dt><dd>{capture.source_count.toLocaleString()}</dd><dt>Bytes awaiting archival</dt><dd>{capture.pending_archive_bytes.toLocaleString()}</dd><dt>Bytes awaiting parsing</dt><dd>{capture.pending_parse_bytes.toLocaleString()}</dd></dl> : <p className="quiet">No collection observation available.</p>}
      </> : <p className="quiet">{observation?.loading ? 'Loading native history observations…' : 'No native history observation available.'}</p>}
      <p className="quiet runtime-note">Collected native history and Terminal recording are separate. These observations do not confirm Chat delivery or complete history coverage.</p>
    </section>
  </>;
}
function Inspector({ agent, rename, changeIcon }: { agent: Agent; rename: () => void; changeIcon: () => void }) {
  const publication = store.chat(agent.id)?.publication;
  return <aside className="inspector" aria-label="Agent details">
    <div className="inspector-identity"><Icon name={agent.icon_key} size={30} /><h2>{agent.display_name}</h2><p className="quiet">{agent.kind === 'host' ? 'Host agent' : 'VM agent'} · {agent.lifecycle}</p>
      {store.has('agents_update') && <div className="inline-actions"><button onClick={rename} disabled={store.pending.has(`edit:${agent.id}`)}>Rename</button><button onClick={changeIcon} disabled={store.pending.has(`edit:${agent.id}`)}>Change icon</button></div>}
    </div>
    <section><h3>Current work</h3><p>{agent.status.text}</p><dl><dt>Activity</dt><dd>{agent.activity}</dd><dt>Attention</dt><dd>{agent.needs_user_input ? 'Needs your input' : 'None reported'}</dd><dt>Status reviewed</dt><dd>{time(agent.status.reviewed_at)}</dd><dt>Activity observed</dt><dd>{time(agent.activity_observed_at)}</dd><dt>Last contact</dt><dd>{time(agent.last_contact_at)}</dd></dl></section>
    <RuntimeSummary id={agent.id} />
    <section><h3>Transcript publication</h3>{publication ? <><p>{publication.state === 'current' ? 'Current' : publication.state === 'blocked' ? 'Blocked' : 'Pending'} · {publication.pending_entries} pending entries</p>{publication.error && <p className="error">{publication.error.code}: {publication.error.message}</p>}<p className="quiet">Last saved publication observation.</p></> : <p className="quiet">No publication observation available.</p>}</section>
    <section><h3>Browser</h3><p className="quiet">Unavailable. Browser viewing and control are not connected.</p></section>
    <section><h3>{agent.kind === 'host' ? 'Desktop' : 'Machine'}</h3><p className="quiet">Unavailable. No desktop surface is connected.</p></section>
    <section><h3>Terminal</h3><TerminalSummary store={store} id={agent.id} /></section>
    <details><summary>Identity & workspace</summary><dl className="identity-details"><dt>Agent ID</dt><dd>{agent.id}</dd><dt>Conversation ID</dt><dd>{agent.conversation_id}</dd><dt>Workspace</dt><dd>{agent.workspace_path}</dd><dt>Workspace initialized</dt><dd>{agent.workspace_initialized_at ? time(agent.workspace_initialized_at) : 'Not yet'}</dd><dt>Metadata revision</dt><dd>{agent.revision}</dd></dl></details>
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
  const inspector = agent && <Inspector agent={agent} rename={() => { setInspectOpen(false); setRename(agent.id); if (mobile) setNavOpen(true); }} changeIcon={() => { setInspectOpen(false); setIconAgent(agent.id); setIcon(agent.icon_key); }} />;
  if (store.phase !== 'ready') return <SignIn />;
  return <div className={`workspace ${!overlayInspector && store.inspector && agent ? 'with-inspector' : ''}`}>
    {!mobile && navigation}
    <main className="conversation">
      <header className="conversation-header">
        {mobile && <button aria-label="Open agents" onClick={() => setNavOpen(true)}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" /></svg></button>}
        <div className="conversation-title">{agent && <Icon name={agent.icon_key} />}<div><h1>{agent?.display_name || 'Conversations'}</h1><p>{agent ? `${agent.lifecycle} · ${agent.activity === 'unknown' ? 'Activity unknown' : agent.activity}` : 'Your agent workspace'}</p></div></div>
        {agent && <div className="view-switch" role="group" aria-label="Agent view"><button aria-pressed={store.view(agent.id) === 'chat'} onClick={() => store.setView(agent.id, 'chat')}>Chat</button><button aria-pressed={store.view(agent.id) === 'terminal'} onClick={() => store.setView(agent.id, 'terminal')}>Terminal</button></div>}
        {agent && <button aria-label={overlayInspector ? 'Open inspector' : store.inspector ? 'Close inspector' : 'Open inspector'} aria-expanded={overlayInspector ? inspectOpen : store.inspector} onClick={() => overlayInspector ? setInspectOpen(true) : store.toggleInspector()}>Details</button>}
      </header>
      {store.error && <div className="connection-error error" role="alert">{store.error}</div>}
      {store.notice && <div className="notice" role="status">{store.notice}<button aria-label="Dismiss notice" onClick={() => { store.notice = ''; store.emit(); }}>×</button></div>}
      {agent && <MutationNotice item={store.pending.get(`edit:${agent.id}`)} requestKey={`edit:${agent.id}`} />}
      {agent ? store.view(agent.id) === 'terminal' ? <TerminalPane key={agent.id} store={store} id={agent.id} /> : <Chat agent={agent} /> : <div className="empty"><h2>Select an agent</h2><p>Conversations stay with each agent.</p></div>}
    </main>
    {!overlayInspector && store.inspector && inspector}
    {mobile && navOpen && <Dialog title="Agents" drawer="drawer left-drawer" onClose={() => setNavOpen(false)}>{navigation}</Dialog>}
    {overlayInspector && inspectOpen && inspector && <Dialog title="Inspector" drawer="drawer right-drawer" onClose={() => setInspectOpen(false)}>{inspector}</Dialog>}
    {createOpen && store.config && <Creation close={() => setCreateOpen(false)} />}
    {iconAgent && store.config && <Dialog title="Change icon" onClose={() => setIconAgent('')}><IconPicker value={icon} onChange={setIcon} keys={store.config.icon_keys} /><div className="form-actions"><button onClick={() => setIconAgent('')}>Cancel</button><button className="primary" onClick={() => { const id = iconAgent; setIconAgent(''); void store.update(id, { icon_key: icon }); }}>Save icon</button></div></Dialog>}
  </div>;
}

render(<App />, document.getElementById('app')!);
