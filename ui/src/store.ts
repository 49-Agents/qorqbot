import { ApiError, operation, request } from './api';
import type { Agent, AgentPage, BrowserView, Config, ConversationPage, Entry, EventPage, InputRequestView, Publication, RuntimeView, Schema, SendResult, WorkspaceIdentity, TerminalView } from './wire';
import { mergeTerminal, terminalView, uuid } from './terminal-protocol';
import { runtimeView } from './runtime-view';
import { browserRequestEntry, browserView, inputRequestView, sameRequest, streamReady, unresolved } from './browser-view';

export type BrowserAction = 'input_requests_complete' | 'input_requests_cancel' | 'input_requests_resume';
export type Pending = {
  operation: 'messages_send' | 'agents_update' | 'agents_create' | 'input_requests_create' | BrowserAction; args: Record<string, unknown>;
  actor: string; agentId?: string; draftVersion?: number;
  unresolvedAmbiguity: boolean;
  state: 'sending' | 'uncertain' | 'failed' | 'conflict'; error?: string; retryable?: boolean;
};
export type Chat = {
  items: Entry[]; epoch: string; hasOlder: boolean; loading: boolean; earlier: boolean;
  publication: Publication | null; error: string; earlierError: string; loaded: boolean; refreshSeq: number; change: 'initial' | 'append' | 'prepend' | 'refresh';
};
export type Draft = { text: string; version: number };
export type Scroll = { top: number; bottom: boolean };
export type TerminalObservation = { view: TerminalView | null; loading: boolean; error: string };
export type RuntimeObservation = { view: RuntimeView | null; loading: boolean; error: string };
export type BrowserObservation = { view: BrowserView | null; loading: boolean; error: string };
const failure = (e: unknown) => e instanceof ApiError ? e : new ApiError('unavailable', 'Request failed.');
const transientRead = (e: unknown) => e instanceof ApiError && (e.code === 'unavailable' || e.code === 'connection_lost'
  || (e.code === 'transport_refused' && e.status >= 500 && e.status < 600));
// One content gate for every send path; valid bodies keep their exact bytes.
export const meaningful = (text: string) => text.trim().length > 0;
// Names use Python str.strip() whitespace (not JS trim(): U+001C-001F and U+0085 count, U+FEFF does not),
// then must be 1-120 Unicode code points of UTF-8-encodable text, as the server counts them.
const nameSpace = '[\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]+';
const nameEdges = new RegExp(`^${nameSpace}|${nameSpace}$`, 'g');
export const stripName = (raw: string) => raw.replace(nameEdges, '');
export function nameProblem(raw: string) {
  const value = stripName(raw), length = [...value].length;
  if (!length || length > 120) return 'Name must be 1–120 characters.';
  try { encodeURIComponent(value); } catch { return 'Name contains text that cannot be saved.'; }
  return '';
}
// A definitive failure took no effect, so an explicit correction may use a fresh request.
export const correctable = (item?: Pending) => !!item && item.state === 'failed' && !item.unresolvedAmbiguity;
export const locked = (item?: Pending) => !!item && !correctable(item);
const newChat = (): Chat => ({ items: [], epoch: '', hasOlder: false, loading: false, earlier: false, publication: null, error: '', earlierError: '', loaded: false, refreshSeq: 0, change: 'initial' });

export class Store {
  phase: 'checking' | 'ready' | 'error' = 'checking';
  error = '';
  connection = 'Connecting';
  actor = '';
  config: Config | null = null;
  supported = new Set<string>();
  agents: Agent[] = [];
  directoryLoading = false;
  selected = '';
  inspector = true;
  epoch = '';
  drafts = new Map<string, Draft>();
  chats = new Map<string, Chat>();
  scrolls = new Map<string, Scroll>();
  views = new Map<string, 'chat' | 'terminal'>();
  terminals = new Map<string, TerminalObservation>();
  runtimes = new Map<string, RuntimeObservation>();
  browsers = new Map<string, BrowserObservation>();
  browserRequests = new Map<string, InputRequestView>();
  browserNotes = new Map<string, string>();
  pending = new Map<string, Pending>();
  notice = '';
  private listeners = new Set<() => void>();
  private generation = 0;
  private selectionVersion = 0;
  private reads = new Set<AbortController>();
  private conversationRead?: AbortController;
  private terminalRead?: AbortController;
  private runtimeRead?: AbortController;
  private browserRead?: AbortController;
  private timer?: number;
  private pollBusy = false;
  private pollFailures = 0;
  private eventSeq = 0;
  private hint = '';

  constructor() {
    try {
      this.hint = localStorage.getItem('qorqut.selected') || '';
      this.inspector = localStorage.getItem('qorqut.inspector') !== 'closed';
    } catch { /* Preferences are optional. */ }
  }
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  emit = () => this.listeners.forEach(fn => fn());
  has = (name: string) => this.supported.has(name);
  agent = () => this.agents.find(a => a.id === this.selected);
  draft = (id: string) => this.drafts.get(id) || { text: '', version: 0 };
  chat = (id: string) => this.chats.get(id);
  view = (id: string) => this.views.get(id) || 'chat';
  setView(id: string, view: 'chat' | 'terminal') { this.views.set(id, view); this.emit(); }
  setDraft(id: string, text: string) {
    this.drafts.set(id, { text, version: this.draft(id).version + 1 });
    this.emit();
  }
  toggleInspector() {
    this.inspector = !this.inspector;
    try { localStorage.setItem('qorqut.inspector', this.inspector ? 'open' : 'closed'); } catch { /* optional */ }
    this.emit();
  }
  private controller() {
    const controller = new AbortController();
    this.reads.add(controller);
    return controller;
  }
  private resetServer() {
    clearTimeout(this.timer);
    for (const read of this.reads) read.abort();
    this.reads.clear();
    this.conversationRead?.abort();
    this.terminalRead?.abort();
    this.runtimeRead?.abort();
    this.browserRead?.abort();
    this.selectionVersion++;
    this.agents = [];
    this.chats.clear();
    this.scrolls.clear();
    this.terminals.clear();
    this.runtimes.clear();
    this.browsers.clear();
    this.browserRequests.clear();
    this.actor = '';
    this.config = null;
    this.supported.clear();
    this.epoch = '';
    this.eventSeq = 0;
    this.pollFailures = 0;
    this.directoryLoading = false;
    this.notice = '';
    for (const mutation of this.pending.values()) {
      if (mutation.state === 'sending') {
        mutation.unresolvedAmbiguity = true;
        mutation.state = 'uncertain';
        mutation.error = 'The connection changed before confirmation. The original request may have been saved.';
      }
    }
  }
  private disconnect(error: ApiError) {
    this.generation++;
    this.resetServer();
    this.phase = 'error';
    this.connection = 'Offline';
    this.error = error.message;
    this.emit();
  }
  async open() {
    const gen = ++this.generation;
    this.resetServer();
    this.phase = 'checking'; this.connection = 'Connecting'; this.error = ''; this.emit();
    const controller = this.controller();
    try {
      const identity = await request<WorkspaceIdentity>('/api/v1/session', { signal: controller.signal });
      if (gen === this.generation) await this.connected(identity, gen);
    } catch (e) {
      if (gen !== this.generation) return;
      this.disconnect(failure(e));
    } finally { this.reads.delete(controller); }
  }
  private async connected(identity: WorkspaceIdentity, gen: number) {
    if (identity?.api_version !== 1) throw new ApiError('unsupported_schema', 'Incompatible API version. This interface requires version 1.');
    if (!uuid(identity.actor_id)) throw new ApiError('invalid_response', 'The server did not provide a valid workspace identity.');
    this.actor = identity.actor_id;
    const controller = this.controller();
    try {
      const schema = await request<Schema>('/api/v1/schema', { signal: controller.signal });
      if (gen !== this.generation) return;
      if (schema.api_version !== 1 || !Array.isArray(schema.operations)) {
        throw new ApiError('unsupported_schema', 'Incompatible API version. This interface requires version 1.');
      }
      this.supported = new Set(schema.operations.map(op => op.name));
      if (this.has('config_get')) {
        const config = await operation<Config>('config_get', {}, controller.signal);
        if (gen !== this.generation) return;
        this.config = config;
        this.epoch = config.recovery_epoch;
      }
      this.phase = 'ready'; this.connection = 'Connected'; this.error = '';
      await this.loadDirectory(controller.signal, gen);
      if (gen !== this.generation) return;
      this.emit();
      this.schedule(0);
    } finally { this.reads.delete(controller); }
  }
  private async loadDirectory(signal: AbortSignal, gen: number) {
    if (!this.has('agents_list')) return;
    this.directoryLoading = this.agents.length === 0; this.emit();
    const agents: Agent[] = [];
    let cursor: string | null = null;
    let epoch = this.epoch;
    do {
      const page: AgentPage = await operation('agents_list', {
        limit: 100, ...(cursor ? { after_id: cursor, recovery_epoch: epoch } : {}),
      }, signal);
      if (signal.aborted || gen !== this.generation) return;
      if (epoch && page.recovery_epoch !== epoch) throw new ApiError('target_changed', 'Server recovery epoch changed.');
      epoch = page.recovery_epoch;
      agents.push(...page.items);
      if (!page.has_more) break;
      if (!page.next_after_id || page.next_after_id === cursor) throw new ApiError('invalid_response', 'Invalid directory cursor.');
      cursor = page.next_after_id;
    } while (!signal.aborted);
    if (gen !== this.generation || signal.aborted) return;
    this.epoch = epoch;
    // A slower directory response must not undo a confirmed local rename.
    this.agents = agents.map(a => {
      const prior = this.agents.find(p => p.id === a.id);
      return prior && prior.revision > a.revision ? { ...a, display_name: prior.display_name, icon_key: prior.icon_key, revision: prior.revision } : a;
    });
    this.directoryLoading = false;
    if (!this.agents.some(a => a.id === this.selected)) {
      this.select(this.agents.find(a => a.id === this.hint)?.id || this.agents[0]?.id || '');
    } else {
      if (!this.chats.has(this.selected)) void this.refreshConversation('initial');
      if (!this.terminals.has(this.selected)) void this.refreshTerminal();
      if (!this.runtimes.has(this.selected)) void this.refreshRuntime();
      if (!this.browsers.has(this.selected)) void this.refreshBrowser();
    }
    this.emit();
  }
  select(id: string) {
    if (id && !this.agents.some(a => a.id === id)) return;
    this.conversationRead?.abort();
    this.terminalRead?.abort();
    this.runtimeRead?.abort();
    this.browserRead?.abort();
    this.selectionVersion++;
    if (id !== this.selected) {
      this.browsers.delete(id);
      this.runtimes.delete(id);
    }
    this.selected = id;
    this.hint = id;
    try { localStorage.setItem('qorqut.selected', id); } catch { /* optional */ }
    this.emit();
    if (id) {
      void this.refreshConversation(this.chats.has(id) ? 'refresh' : 'initial');
      void this.refreshTerminal();
      void this.refreshRuntime();
      void this.refreshBrowser();
    }
  }
  private async recover(gen: number) {
    if (gen !== this.generation) return;
    // Rediscover the workspace identity as well as all server rows and cursors.
    await this.open();
  }
  private schedule(delay = 2000) {
    clearTimeout(this.timer);
    if (this.phase === 'ready') this.timer = window.setTimeout(() => void this.poll(), delay);
  }
  visibility = () => { if (!document.hidden) this.schedule(0); };
  async poll() {
    if (this.pollBusy || this.phase !== 'ready') { this.schedule(); return; }
    if (document.hidden) { this.schedule(); return; }
    this.pollBusy = true;
    const gen = this.generation;
    const controller = this.controller();
    try {
      if (this.has('events_list')) {
        const page = await operation<EventPage>('events_list', {
          after_seq: this.eventSeq, limit: 100, ...(this.epoch ? { recovery_epoch: this.epoch } : {}),
        }, controller.signal);
        if (gen !== this.generation || controller.signal.aborted) return;
        if (this.epoch && page.recovery_epoch !== this.epoch) throw new ApiError('target_changed', 'Server recovery epoch changed.');
        this.epoch = page.recovery_epoch;
        if (page.items.length) {
          if (page.items.some(e => e.kind.startsWith('config.')) && this.has('config_get')) {
            const config = await operation<Config>('config_get', {}, controller.signal);
            if (gen !== this.generation || controller.signal.aborted) return;
            if (config.recovery_epoch !== this.epoch) throw new ApiError('target_changed', 'Server recovery epoch changed.');
            this.config = config;
          }
          await this.loadDirectory(controller.signal, gen);
        }
        if (gen !== this.generation || controller.signal.aborted) return;
        // A failed invalidation read must leave the event available for the next bounded poll.
        this.eventSeq = page.next_after_seq;
      }
      if (gen !== this.generation || controller.signal.aborted) return;
      await Promise.all([this.refreshConversation('refresh'), this.refreshTerminal(), this.refreshRuntime(), this.refreshBrowser()]);
      if (gen !== this.generation || controller.signal.aborted) return;
      if (this.chats.get(this.selected)?.error) {
        this.connection = 'Offline'; this.pollFailures++;
      } else {
        this.connection = 'Connected'; this.error = ''; this.pollFailures = 0;
      }
    } catch (e) {
      if (gen !== this.generation || controller.signal.aborted) return;
      const err = failure(e);
      if (err.status === 401) this.disconnect(err);
      else if (err.code === 'target_changed') await this.recover(gen);
      else { this.connection = 'Offline'; this.error = err.message; this.pollFailures++; }
    } finally {
      this.reads.delete(controller); this.pollBusy = false; this.emit();
      this.schedule(Math.min(30000, 2000 * 2 ** this.pollFailures));
    }
  }
  acceptTerminal(view: TerminalView, fromRead = false) {
    if (this.phase !== 'ready' || view.agent_id !== this.selected || view.recovery_epoch !== this.epoch) return;
    const prior = this.terminals.get(view.agent_id);
    // Socket observations remain valid, but cannot certify a failed HTTP read.
    this.terminals.set(view.agent_id, { view: mergeTerminal(prior?.view, view),
      loading: !fromRead && !!prior?.loading, error: fromRead ? '' : prior?.error || '' });
    this.emit();
  }
  browserSupported = () => ['browser_get', 'input_requests_complete', 'input_requests_cancel', 'input_requests_resume'].every(this.has);
  browserReady(request: InputRequestView) {
    const observation = this.browsers.get(request.agent_id);
    return this.phase === 'ready' && this.selected === request.agent_id && this.browserSupported()
      && !observation?.error && !this.pending.has(`browser:${request.request_id}`)
      && streamReady(observation?.view, request, this.epoch);
  }
  private acceptBrowserRequest(view: InputRequestView) {
    const agent = this.agents.find(a => a.id === view.agent_id);
    const prior = this.browserRequests.get(view.request_id);
    if (!agent || view.conversation_id !== agent.conversation_id || (prior && !sameRequest(prior, view))) {
      throw new ApiError('invalid_response', 'Browser request could not be verified.');
    }
    // A late poll or an exact-key receipt cannot undo a newer request revision.
    if (!prior || view.revision >= prior.revision) this.browserRequests.set(view.request_id, view);
  }
  async refreshBrowser(force = false) {
    const id = this.selected;
    if (!id || !this.has('browser_get') || this.phase !== 'ready') return;
    if (this.browserRead && !this.browserRead.signal.aborted) {
      if (!force) return;
      this.browserRead.abort();
    }
    const controller = this.controller(); this.browserRead = controller;
    const gen = this.generation, selection = this.selectionVersion;
    const prior = this.browsers.get(id);
    this.browsers.set(id, { view: prior?.view || null, loading: true, error: prior?.error || '' }); this.emit();
    const valid = () => gen === this.generation && selection === this.selectionVersion && id === this.selected && !controller.signal.aborted;
    try {
      const view = await operation<unknown>('browser_get', { agent_id: id }, controller.signal);
      if (!valid()) return;
      if (!browserView(view) || view.agent_id !== id) throw new ApiError('invalid_response', 'Browser observation could not be verified.');
      if (view.recovery_epoch !== this.epoch) throw new ApiError('target_changed', 'Server recovery epoch changed.');
      if (view.active_request) this.acceptBrowserRequest(view.active_request);
      this.browsers.set(id, { view, loading: false, error: '' });
    } catch (e) {
      if (!valid()) return;
      const err = failure(e);
      this.browsers.set(id, { view: transientRead(e) ? prior?.view || null : null,
        loading: false, error: 'Browser information unavailable. Retry to refresh.' });
      if (err.status === 401) this.disconnect(err);
      else if (err.code === 'target_changed') await this.recover(gen);
    } finally {
      this.reads.delete(controller);
      if (this.browserRead === controller) this.browserRead = undefined;
      this.emit();
    }
  }
  async refreshRuntime(force = false) {
    const id = this.selected;
    if (!id || !this.has('runtime_get') || this.phase !== 'ready') return;
    if (this.runtimeRead && !this.runtimeRead.signal.aborted) {
      if (!force) return;
      this.runtimeRead.abort();
    }
    const controller = this.controller(); this.runtimeRead = controller;
    const gen = this.generation, selection = this.selectionVersion;
    const prior = this.runtimes.get(id);
    this.runtimes.set(id, { view: prior?.view || null, loading: true, error: prior?.error || '' }); this.emit();
    const valid = () => gen === this.generation && selection === this.selectionVersion && id === this.selected && !controller.signal.aborted;
    try {
      const view = await operation<unknown>('runtime_get', { agent_id: id }, controller.signal);
      if (!valid()) return;
      if (!runtimeView(view) || view.agent_id !== id || (view.current_owner && view.current_owner.conversation_id !== this.agent()?.conversation_id)) {
        throw new ApiError('invalid_response', 'Session observation could not be verified.');
      }
      if (view.current_owner && view.current_owner.recovery_epoch !== this.epoch) throw new ApiError('target_changed', 'Server recovery epoch changed.');
      this.runtimes.set(id, { view, loading: false, error: '' });
    } catch (e) {
      if (!valid()) return;
      const err = failure(e);
      this.runtimes.set(id, { view: transientRead(e) ? prior?.view || null : null,
        loading: false, error: 'Session information unavailable. Retry to refresh.' });
      if (err.status === 401) this.disconnect(err);
      else if (err.code === 'target_changed') await this.recover(gen);
    } finally {
      this.reads.delete(controller);
      if (this.runtimeRead === controller) this.runtimeRead = undefined;
      this.emit();
    }
  }
  async refreshTerminal(force = false) {
    const id = this.selected;
    if (!id || !this.has('terminal_get') || this.phase !== 'ready') return;
    if (this.terminalRead && !this.terminalRead.signal.aborted) {
      if (!force) return;
      this.terminalRead.abort();
    }
    const controller = this.controller(); this.terminalRead = controller;
    const gen = this.generation, selection = this.selectionVersion;
    const prior = this.terminals.get(id);
    this.terminals.set(id, { view: prior?.view || null, loading: true, error: prior?.error || '' }); this.emit();
    const valid = () => gen === this.generation && selection === this.selectionVersion && id === this.selected && !controller.signal.aborted;
    try {
      const view = await operation<unknown>('terminal_get', { agent_id: id }, controller.signal);
      if (!valid()) return;
      if (!terminalView(view) || view.agent_id !== id) throw new ApiError('invalid_response', 'Terminal observation could not be verified.');
      if (view.recovery_epoch !== this.epoch) throw new ApiError('target_changed', 'Server recovery epoch changed.');
      this.acceptTerminal(view, true);
    } catch (e) {
      if (!valid()) return;
      const err = failure(e);
      this.terminals.set(id, { view: transientRead(e) ? this.terminals.get(id)?.view || null : null,
        loading: false, error: 'Terminal observation unavailable. Retry to refresh.' });
      if (err.status === 401) this.disconnect(err);
      else if (err.code === 'target_changed') await this.recover(gen);
    } finally {
      this.reads.delete(controller);
      if (this.terminalRead === controller) this.terminalRead = undefined;
      this.emit();
    }
  }
  async refreshConversation(mode: 'initial' | 'refresh' | 'earlier') {
    const id = this.selected;
    if (!id || !this.has('conversation_list') || this.phase !== 'ready') return;
    if (this.conversationRead && !this.conversationRead.signal.aborted) {
      if (mode === 'refresh') return;
      this.conversationRead.abort();
    }
    const controller = this.controller(); this.conversationRead = controller;
    const gen = this.generation, selection = this.selectionVersion;
    const chat = this.chats.get(id) || newChat(); this.chats.set(id, chat);
    if (!chat.epoch) mode = 'initial';
    chat.loading = mode === 'initial'; chat.earlier = mode === 'earlier';
    // Earlier-page failures are a separate channel so polling can neither clear them nor move their cursor.
    if (mode === 'earlier') chat.earlierError = ''; else chat.error = '';
    this.emit();
    const valid = () => gen === this.generation && selection === this.selectionVersion && id === this.selected && !controller.signal.aborted;
    const read = async (cursor: object) => {
      const page = await operation<ConversationPage>('conversation_list', { agent_id: id, limit: 50, ...cursor }, controller.signal);
      if (!valid()) return null;
      if (page.agent_id !== id || page.conversation_id !== this.agent()?.conversation_id) throw new ApiError('invalid_response', 'Conversation identity did not match.');
      if (this.epoch && page.recovery_epoch !== this.epoch) throw new ApiError('target_changed', 'Server recovery epoch changed.');
      return page;
    };
    const merge = (page: ConversationPage, change: Chat['change']) => {
      const rows = new Map(chat.items.map(entry => [entry.entry_id, entry]));
      for (const entry of page.items) {
        if (browserRequestEntry(entry) && entry.agent_id === id && entry.conversation_id === this.agent()?.conversation_id) {
          try { this.acceptBrowserRequest(entry.browser_request); }
          catch { rows.set(entry.entry_id, { ...entry, browser_request: null }); continue; }
        }
        rows.set(entry.entry_id, entry);
      }
      chat.items = [...rows.values()].sort((a, b) => a.conversation_seq - b.conversation_seq);
      chat.epoch = page.recovery_epoch; chat.publication = page.publication; chat.change = change;
    };
    try {
      const first = chat.items[0]?.conversation_seq;
      const last = chat.items.at(-1)?.conversation_seq;
      if (mode === 'initial') {
        const page = await read({}); if (!page) return;
        chat.items = []; merge(page, 'initial'); chat.hasOlder = page.has_older; chat.loaded = true;
      } else if (mode === 'earlier') {
        if (first === undefined) return;
        const page = await read({ before_seq: first, recovery_epoch: chat.epoch }); if (!page) return;
        merge(page, 'prepend'); chat.hasOlder = page.has_older;
      } else {
        const page = await read(last === undefined ? {} : { after_seq: last, recovery_epoch: chat.epoch });
        if (!page) return;
        merge(page, page.items.length ? 'append' : 'refresh');
        if (last === undefined) chat.hasOlder = page.has_older;
        this.emit();
        // Refresh one bounded page of loaded mutable views on every pass, including quiet polls.
        if (first !== undefined && last !== undefined) {
          const cursor = Math.max(first - 1, chat.refreshSeq);
          const views = await read({ after_seq: cursor, recovery_epoch: chat.epoch }); if (!views) return;
          const existing = new Set(chat.items.map(e => e.entry_id));
          merge({ ...views, items: views.items.filter(e => existing.has(e.entry_id)) }, 'refresh');
          chat.refreshSeq = views.last_seq !== null && views.last_seq < last ? views.last_seq : first - 1;
        }
      }
    } catch (e) {
      if (!valid()) return;
      const err = failure(e);
      if (err.status === 401) this.disconnect(err);
      else if (err.code === 'target_changed') {
        // Let the one poll loop own recovery so concurrent reads cannot restart it twice.
        this.eventSeq = 0; this.schedule(0); chat.error = 'Server recovery detected. Refreshing conversation.';
        if (!this.has('events_list')) await this.recover(gen);
      } else {
        if (mode === 'earlier') chat.earlierError = err.message; else chat.error = err.message;
        this.connection = 'Offline';
      }
    } finally {
      this.reads.delete(controller);
      if (this.conversationRead === controller) {
        chat.loading = false; chat.earlier = false;
        this.conversationRead = undefined;
      }
      this.emit();
    }
  }
  async send(id: string) {
    if (!this.has('messages_send') || this.pending.has(`send:${id}`)) return;
    const draft = this.draft(id);
    if (!meaningful(draft.text) || new TextEncoder().encode(draft.text).length > 65536) return;
    const pending: Pending = {
      operation: 'messages_send', args: { recipient_agent_id: id, body: draft.text, idempotency_key: crypto.randomUUID() },
      actor: this.actor, agentId: id, draftVersion: draft.version, unresolvedAmbiguity: false, state: 'sending',
    };
    this.pending.set(`send:${id}`, pending);
    await this.mutate(`send:${id}`);
  }
  async create(args: { display_name: string; icon_key: string }) {
    if (locked(this.pending.get('create')) || !this.has('agents_create') || nameProblem(args.display_name)) return;
    this.pending.set('create', { operation: 'agents_create', args: { ...args, display_name: stripName(args.display_name), idempotency_key: crypto.randomUUID() }, actor: this.actor, unresolvedAmbiguity: false, state: 'sending' });
    await this.mutate('create');
  }
  async update(id: string, fields: { display_name?: string; icon_key?: string }) {
    const agent = this.agents.find(a => a.id === id);
    if (!agent || !this.has('agents_update') || locked(this.pending.get(`edit:${id}`))
      || (fields.display_name !== undefined && nameProblem(fields.display_name))) return;
    this.pending.set(`edit:${id}`, {
      operation: 'agents_update', args: { agent_id: id, expected_revision: agent.revision, ...fields, idempotency_key: crypto.randomUUID() },
      actor: this.actor, agentId: id, unresolvedAmbiguity: false, state: 'sending',
    });
    await this.mutate(`edit:${id}`);
  }
  async createBrowser(id: string) {
    const key = `browser-create:${id}`, observation = this.browsers.get(id);
    if (this.selected !== id || !this.browserSupported() || !this.has('input_requests_create') || this.pending.has(key)
      || observation?.error || observation?.view?.surface?.status !== 'available' || observation.view.active_request) return;
    this.pending.set(key, { operation: 'input_requests_create', args: { target_agent_id: id, prompt: 'Manual browser control', observed_tab: null,
      idempotency_key: crypto.randomUUID() }, actor: this.actor, agentId: id, unresolvedAmbiguity: false, state: 'sending' });
    return this.mutate(key);
  }
  async browserAction(request: InputRequestView, action: BrowserAction) {
    const current = this.browserRequests.get(request.request_id), key = `browser:${request.request_id}`;
    if (!current || this.selected !== current.agent_id || !this.browserSupported() || !this.has(action) || !unresolved(current) || this.pending.has(key)) return;
    const note = this.browserNotes.get(current.request_id) || '';
    if ([...note].length > 4096) return;
    this.pending.set(key, { operation: action, args: { request_id: current.request_id, expected_revision: current.revision,
      idempotency_key: crypto.randomUUID(), ...(action === 'input_requests_resume' ? {} : { note: note || null }) },
      actor: this.actor, agentId: current.agent_id, unresolvedAmbiguity: false, state: 'sending' });
    return this.mutate(key);
  }
  discard(key: string) {
    const pending = this.pending.get(key);
    if (pending && !pending.unresolvedAmbiguity && (pending.state === 'failed' || pending.state === 'conflict')) { this.pending.delete(key); this.emit(); }
  }
  async retry(key: string) {
    const pending = this.pending.get(key);
    if (!pending || pending.state === 'sending' || pending.actor !== this.actor || !this.has(pending.operation)) return;
    // A definitive nonretryable failure is corrected and resubmitted, never replayed.
    if (correctable(pending) && pending.retryable === false) return;
    // Reapplying is a new mutation only if no prior attempt remains unresolved.
    if (!pending.unresolvedAmbiguity && pending.state === 'conflict') {
      const target = pending.operation.startsWith('input_requests_')
        ? this.browserRequests.get(pending.args.request_id as string) : this.agents.find(a => a.id === pending.agentId);
      if (!target) return;
      pending.args = { ...pending.args, expected_revision: target.revision, idempotency_key: crypto.randomUUID() };
    }
    await this.mutate(key);
  }
  private upsert(agent: Agent) {
    const old = this.agents.find(a => a.id === agent.id);
    if (old && old.revision > agent.revision) return;
    this.agents = [...this.agents.filter(a => a.id !== agent.id), agent].sort((a, b) => a.id.localeCompare(b.id));
  }
  private async mutate(key: string) {
    const pending = this.pending.get(key); if (!pending) return;
    const gen = this.generation;
    pending.state = 'sending'; pending.error = ''; pending.retryable = undefined; this.emit();
    try {
      // Mutation is not tied to selected-agent read cancellation. It may commit after navigation.
      const data = await operation<SendResult | Agent | { agent: Agent } | InputRequestView>(pending.operation, pending.args, undefined, true);
      if (gen !== this.generation || this.pending.get(key) !== pending) return;
      if (pending.operation.startsWith('input_requests_')) {
        if (!inputRequestView(data) || data.agent_id !== pending.agentId
          || (pending.args.request_id && data.request_id !== pending.args.request_id)
          || (pending.operation === 'input_requests_create' && (data.prompt !== pending.args.prompt || data.requester_actor_id !== pending.actor))) {
          throw new ApiError('invalid_response', 'Browser confirmation could not be verified. The request may have been saved.', true);
        }
        try { this.acceptBrowserRequest(data); }
        catch { throw new ApiError('invalid_response', 'Browser confirmation could not be verified. The request may have been saved.', true); }
        this.pending.delete(key);
        if (this.selected === pending.agentId) {
          void this.refreshBrowser(true);
          void this.refreshConversation('refresh');
        }
        return data;
      } else if (pending.operation === 'messages_send') {
        const id = pending.agentId!;
        if (this.draft(id).version === pending.draftVersion) this.drafts.set(id, { text: '', version: this.draft(id).version + 1 });
        this.pending.delete(key);
        if (this.selected === id) void this.refreshConversation('refresh');
      } else {
        const agent = pending.operation === 'agents_create' ? (data as { agent: Agent }).agent : data as Agent;
        this.upsert(agent); this.pending.delete(key);
        if (pending.operation === 'agents_create') {
          this.notice = `${agent.display_name} created.`;
          this.select(agent.id);
        }
      }
    } catch (e) {
      if (gen !== this.generation || this.pending.get(key) !== pending) return;
      const err = failure(e);
      // A refused retry cannot settle an earlier attempt whose confirmation was lost.
      pending.unresolvedAmbiguity ||= err.ambiguous; pending.retryable = err.retryable;
      pending.state = pending.unresolvedAmbiguity ? 'uncertain' : err.code === 'revision_conflict' ? 'conflict' : 'failed';
      pending.error = pending.unresolvedAmbiguity && err.status === 401
        ? 'The retry was refused before confirmation. The original request may have been saved.'
        : err.message;
      if (err.status === 401) this.disconnect(err);
      else if (pending.operation.startsWith('input_requests_')) {
        if (this.selected === pending.agentId) void this.refreshBrowser(true);
      }
      else if (err.code === 'revision_conflict' && pending.agentId) {
        const controller = this.controller();
        try {
          if (this.has('agents_get')) {
            const agent = await operation<Agent>('agents_get', { agent_id: pending.agentId }, controller.signal);
            if (gen === this.generation && this.pending.get(key) === pending) this.upsert(agent);
          } else await this.loadDirectory(controller.signal, gen);
        } catch (refreshError) {
          if (gen === this.generation && this.pending.get(key) === pending) {
            if (failure(refreshError).status === 401) this.disconnect(failure(refreshError));
            pending.error += pending.unresolvedAmbiguity
              ? ' Current name could not be refreshed; reconnect before retrying the original request.'
              : ' Current name could not be refreshed; reconnect before reapplying.';
            pending.state = pending.unresolvedAmbiguity ? 'uncertain' : 'failed';
          }
        } finally { this.reads.delete(controller); }
      }
    } finally { this.emit(); }
  }
}
