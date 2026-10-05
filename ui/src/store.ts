import { ApiError, operation, request } from './api';
import type { Agent, AgentPage, Config, ConversationPage, Entry, EventPage, Publication, Schema, SendResult, Session } from './wire';

export type Pending = {
  operation: 'messages_send' | 'agents_update' | 'agents_create'; args: Record<string, unknown>;
  actor: string; agentId?: string; draftVersion?: number;
  state: 'sending' | 'uncertain' | 'failed' | 'conflict'; error?: string;
};
export type Chat = {
  items: Entry[]; epoch: string; hasOlder: boolean; loading: boolean; earlier: boolean;
  publication: Publication | null; error: string; refreshSeq: number; change: 'initial' | 'append' | 'prepend' | 'refresh';
};
export type Draft = { text: string; version: number };
export type Scroll = { top: number; bottom: boolean };
const failure = (e: unknown) => e instanceof ApiError ? e : new ApiError('unavailable', 'Request failed.');
const newChat = (): Chat => ({ items: [], epoch: '', hasOlder: false, loading: false, earlier: false, publication: null, error: '', refreshSeq: 0, change: 'initial' });

export class Store {
  phase: 'checking' | 'login' | 'ready' | 'error' = 'checking';
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
  pending = new Map<string, Pending>();
  notice = '';
  private listeners = new Set<() => void>();
  private generation = 0;
  private selectionVersion = 0;
  private reads = new Set<AbortController>();
  private conversationRead?: AbortController;
  private timer?: number;
  private pollBusy = false;
  private pollFailures = 0;
  private eventSeq = 0;
  private hint = '';
  private logoutFailed = false;

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
    this.selectionVersion++;
    this.agents = [];
    this.chats.clear();
    this.scrolls.clear();
    this.config = null;
    this.supported.clear();
    this.epoch = '';
    this.eventSeq = 0;
    this.directoryLoading = false;
    this.notice = '';
  }
  private expire() {
    if (this.phase === 'login') return;
    this.generation++;
    this.resetServer();
    for (const mutation of this.pending.values()) {
      if (mutation.state === 'sending') {
        mutation.state = 'uncertain';
        mutation.error = 'The session ended before confirmation. Retry the original request after signing in.';
      }
    }
    this.phase = 'login';
    this.connection = 'Signed out';
    this.error = 'Your session expired. Unsent drafts remain on this page.';
    this.emit();
  }
  async open() {
    const gen = ++this.generation;
    this.resetServer();
    this.phase = 'checking'; this.error = ''; this.emit();
    const controller = this.controller();
    try {
      const identity = await request<Session>('/api/v1/session', { signal: controller.signal });
      if (gen === this.generation) await this.connected(identity, gen);
    } catch (e) {
      if (gen !== this.generation) return;
      const err = failure(e);
      this.phase = err.status === 401 ? 'login' : 'error';
      this.error = err.status === 401 ? '' : err.message;
      this.emit();
    } finally { this.reads.delete(controller); }
  }
  async login(token: string) {
    const gen = ++this.generation;
    this.phase = 'checking'; this.error = ''; this.emit();
    try {
      const identity = await request<Session>('/api/v1/session', { method: 'POST', body: JSON.stringify({ token }) });
      token = '';
      if (gen === this.generation) await this.connected(identity, gen);
    } catch (e) {
      token = '';
      if (gen !== this.generation) return;
      const err = failure(e);
      this.phase = err.code === 'unsupported_schema' ? 'error' : 'login';
      this.error = err.status === 401 ? 'The console token was not accepted.' : err.message;
      this.emit();
    }
  }
  async logout() {
    ++this.generation;
    this.resetServer();
    this.pending.clear(); this.drafts.clear(); this.actor = ''; this.selected = '';
    this.phase = 'checking'; this.error = ''; this.emit();
    try {
      await request('/api/v1/session', { method: 'DELETE' });
      this.logoutFailed = false;
    } catch {
      this.logoutFailed = true;
      this.error = 'Local data cleared, but sign-out was not confirmed. Retry sign-out.';
    }
    this.phase = 'login'; this.emit();
  }
  needsLogoutRetry = () => this.logoutFailed;
  private async connected(identity: Session, gen: number) {
    if (identity.api_version !== 1) throw new ApiError('unsupported_schema', 'Incompatible API version. This interface requires version 1.');
    if (identity.authenticated !== true || typeof identity.actor_id !== 'string') throw new ApiError('invalid_response', 'The server did not confirm a session identity.');
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
    } else if (!this.chats.has(this.selected)) void this.refreshConversation('initial');
    this.emit();
  }
  select(id: string) {
    if (id && !this.agents.some(a => a.id === id)) return;
    this.conversationRead?.abort();
    this.selectionVersion++;
    this.selected = id;
    this.hint = id;
    try { localStorage.setItem('qorqut.selected', id); } catch { /* optional */ }
    this.emit();
    if (id) void this.refreshConversation(this.chats.has(id) ? 'refresh' : 'initial');
  }
  private async recover(gen: number) {
    if (gen !== this.generation) return;
    const nextGeneration = ++this.generation;
    const selection = this.selected;
    this.resetServer();
    for (const mutation of this.pending.values()) {
      if (mutation.state === 'sending') {
        mutation.state = 'uncertain';
        mutation.error = 'Server recovery interrupted confirmation. The original request may have been saved.';
      }
    }
    this.selected = selection;
    this.connection = 'Refreshing after server recovery';
    this.emit();
    // Fresh discovery invalidates every server-derived row and cursor; drafts stay in memory.
    await this.connected({ authenticated: true, actor_id: this.actor, api_version: 1 }, nextGeneration);
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
        this.epoch = page.recovery_epoch; this.eventSeq = page.next_after_seq;
        if (page.items.length) {
          if (page.items.some(e => e.kind.startsWith('config.')) && this.has('config_get')) {
            const config = await operation<Config>('config_get', {}, controller.signal);
            if (gen !== this.generation || controller.signal.aborted) return;
            if (config.recovery_epoch !== this.epoch) throw new ApiError('target_changed', 'Server recovery epoch changed.');
            this.config = config;
          }
          await this.loadDirectory(controller.signal, gen);
        }
      }
      if (gen !== this.generation) return;
      await this.refreshConversation('refresh');
      if (gen !== this.generation) return;
      if (this.chats.get(this.selected)?.error) {
        this.connection = 'Offline'; this.pollFailures++;
      } else {
        this.connection = 'Connected'; this.error = ''; this.pollFailures = 0;
      }
    } catch (e) {
      if (gen !== this.generation || controller.signal.aborted) return;
      const err = failure(e);
      if (err.status === 401) this.expire();
      else if (err.code === 'target_changed') {
        try { await this.recover(gen); } catch (recoveryError) {
          const err = failure(recoveryError);
          if (err.status === 401) this.expire();
          else { this.connection = 'Offline'; this.error = err.message; this.pollFailures++; }
        }
      } else { this.connection = 'Offline'; this.error = err.message; this.pollFailures++; }
    } finally {
      this.reads.delete(controller); this.pollBusy = false; this.emit();
      this.schedule(Math.min(30000, 2000 * 2 ** this.pollFailures));
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
    chat.loading = mode === 'initial'; chat.earlier = mode === 'earlier'; chat.error = ''; this.emit();
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
      for (const entry of page.items) rows.set(entry.entry_id, entry);
      chat.items = [...rows.values()].sort((a, b) => a.conversation_seq - b.conversation_seq);
      chat.epoch = page.recovery_epoch; chat.publication = page.publication; chat.change = change;
    };
    try {
      const first = chat.items[0]?.conversation_seq;
      const last = chat.items.at(-1)?.conversation_seq;
      if (mode === 'initial') {
        const page = await read({}); if (!page) return;
        chat.items = []; merge(page, 'initial'); chat.hasOlder = page.has_older;
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
      if (err.status === 401) this.expire();
      else if (err.code === 'target_changed') {
        // Let the one poll loop own recovery so concurrent reads cannot restart it twice.
        this.eventSeq = 0; this.schedule(0); chat.error = 'Server recovery detected. Refreshing conversation.';
        if (!this.has('events_list')) {
          try { await this.recover(gen); }
          catch (recoveryError) {
            // Recovery itself can fail during discovery/config reads. Keep it a visible, retryable connection error.
            if (this.generation !== gen + 1) return;
            const refreshError = failure(recoveryError);
            if (refreshError.status === 401) this.expire();
            else { this.phase = 'error'; this.connection = 'Offline'; this.error = refreshError.message; }
          }
        }
      } else { chat.error = err.message; this.connection = 'Offline'; }
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
    if (!draft.text || new TextEncoder().encode(draft.text).length > 65536) return;
    const pending: Pending = {
      operation: 'messages_send', args: { recipient_agent_id: id, body: draft.text, idempotency_key: crypto.randomUUID() },
      actor: this.actor, agentId: id, draftVersion: draft.version, state: 'sending',
    };
    this.pending.set(`send:${id}`, pending);
    await this.mutate(`send:${id}`);
  }
  async create(args: { display_name: string; icon_key: string; cli: { provider: string; model: string }; start: boolean }) {
    if (this.pending.has('create') || !this.has('agents_create')) return;
    this.pending.set('create', { operation: 'agents_create', args: { ...args, idempotency_key: crypto.randomUUID() }, actor: this.actor, state: 'sending' });
    await this.mutate('create');
  }
  async update(id: string, fields: { display_name?: string; icon_key?: string }) {
    const agent = this.agents.find(a => a.id === id);
    if (!agent || !this.has('agents_update') || this.pending.has(`edit:${id}`)) return;
    this.pending.set(`edit:${id}`, {
      operation: 'agents_update', args: { agent_id: id, expected_revision: agent.revision, ...fields, idempotency_key: crypto.randomUUID() },
      actor: this.actor, agentId: id, state: 'sending',
    });
    await this.mutate(`edit:${id}`);
  }
  discard(key: string) {
    const pending = this.pending.get(key);
    if (pending && (pending.state === 'failed' || pending.state === 'conflict')) { this.pending.delete(key); this.emit(); }
  }
  async retry(key: string) {
    const pending = this.pending.get(key);
    if (!pending || pending.state === 'sending' || pending.actor !== this.actor || !this.has(pending.operation)) return;
    // Revision conflicts are confirmed refusals. Reapplying is a new explicit mutation against the reloaded revision.
    if (pending.state === 'conflict') {
      const agent = this.agents.find(a => a.id === pending.agentId);
      if (!agent) return;
      pending.args = { ...pending.args, expected_revision: agent.revision, idempotency_key: crypto.randomUUID() };
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
    pending.state = 'sending'; pending.error = ''; this.emit();
    try {
      // Mutation is not tied to selected-agent read cancellation. It may commit after navigation.
      const data = await operation<SendResult | Agent | { agent: Agent }>(pending.operation, pending.args, undefined, true);
      if (gen !== this.generation || this.pending.get(key) !== pending) return;
      if (pending.operation === 'messages_send') {
        const id = pending.agentId!;
        if (this.draft(id).version === pending.draftVersion) this.drafts.set(id, { text: '', version: this.draft(id).version + 1 });
        this.pending.delete(key);
        if (this.selected === id) void this.refreshConversation('refresh');
      } else {
        const agent = pending.operation === 'agents_create' ? (data as { agent: Agent }).agent : data as Agent;
        this.upsert(agent); this.pending.delete(key);
        if (pending.operation === 'agents_create') {
          this.notice = `${agent.display_name} registered — ${agent.lifecycle}.`;
          this.select(agent.id);
        }
      }
    } catch (e) {
      if (gen !== this.generation || this.pending.get(key) !== pending) return;
      const err = failure(e);
      pending.state = err.ambiguous ? 'uncertain' : err.code === 'revision_conflict' ? 'conflict' : 'failed';
      pending.error = err.message;
      if (err.status === 401) this.expire();
      else if (err.code === 'revision_conflict' && pending.agentId) {
        const controller = this.controller();
        try {
          if (this.has('agents_get')) {
            const agent = await operation<Agent>('agents_get', { agent_id: pending.agentId }, controller.signal);
            if (gen === this.generation) this.upsert(agent);
          } else await this.loadDirectory(controller.signal, gen);
        } catch (refreshError) {
          if (gen === this.generation) {
            if (failure(refreshError).status === 401) this.expire();
            pending.error += ' Current name could not be refreshed; reconnect before reapplying.';
            pending.state = 'failed';
          }
        } finally { this.reads.delete(controller); }
      }
    } finally { this.emit(); }
  }
}
