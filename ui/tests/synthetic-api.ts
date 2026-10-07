// Isolated test-only API v1 fixture. Never imported by the application or included in dist.
import { createServer, type Server, type ServerResponse } from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Agent, Config, Entry, FileReference, Publication, RuntimeView, TerminalView } from '../src/wire';

export const owner = '00000000-0000-4000-8000-000000000001';
export const alpha = '10000000-0000-4000-8000-000000000001';
export const beta = '20000000-0000-4000-8000-000000000001';
const now = '2026-10-08T12:00:00.000000Z';
const keys = ['circle','square','triangle','diamond','star','compass','globe','leaf','feather','book','notebook','flask','microscope','telescope','code','terminal','wrench','gear','lightbulb','anchor'];
const defaults = { cli: { provider: 'codex' as const, model: 'synthetic-model' }, resources: { cpu: 2, memory_mib: 2048, disk_gib: 20 } };
export function makeAgent(id: string, name: string, kind: 'host' | 'vm' = 'vm'): Agent {
  return {
    id, actor_id: randomUUID(), conversation_id: randomUUID(), display_name: name, icon_key: kind === 'host' ? 'terminal' : 'compass',
    kind, workspace_path: `agents/${id}/workspace`, cli: { ...defaults.cli }, resources: { ...defaults.resources },
    revision: 1, lifecycle_revision: 1, lifecycle: 'provisioning', start_requested: kind !== 'host',
    created_by: owner, created_at: now, updated_at: now, workspace_initialized_at: null, projection_revision: 0,
    status: { text: 'Initializing.', revision: 1, updated_at: now, reviewed_at: now, actor_id: owner },
    activity: 'unknown', activity_observed_at: null, last_contact_at: null, current_run: null,
    capture_health: 'unknown', needs_user_input: false,
  };
}
type Args = Record<string, any>;
type Call = { name: string; args: Args };
type Gate = { name: string; matches: (args: Args) => boolean; wait: Promise<void>; release: () => void; seen: Promise<void>; mark: () => void };
export class SyntheticAPI {
  server!: Server;
  url = '';
  epoch = randomUUID();
  version = 1;
  actor = owner;
  agents = [makeAgent(alpha, 'Atlas'), makeAgent(beta, 'onbash', 'host')];
  entries = new Map<string, Entry[]>([[alpha, []], [beta, []]]);
  calls: Call[] = [];
  events: { event_seq: number; event_id: string; kind: string; agent_id: string | null; actor_id: string; operation_id: string | null; occurred_at: string; recorded_at: string; origin_installation_id: string; origin_seq: number; payload_version: number; payload: object }[] = [];
  operations = ['config_get', 'events_list', 'agents_list', 'agents_get', 'agents_create', 'agents_update', 'conversation_list', 'messages_send'];
  publication: Publication = { pending_entries: 0, state: 'current', error: null };
  terminals = new Map<string, TerminalView>();
  runtimes = new Map<string, RuntimeView>();
  receipts = new Map<string, object>();
  messages: object[] = [];
  dropSend = false;
  conflictUpdate = false;
  refuseSend = false;
  directoryPageSize = 1;
  private gates: Gate[] = [];
  private installId = randomUUID();
  hold(name: string, matches: (args: Args) => boolean = () => true) {
    let release!: () => void, mark!: () => void;
    const gate: Gate = { name, matches, wait: new Promise(r => { release = r; }), release: () => release(), seen: new Promise(r => { mark = r; }), mark: () => mark() };
    this.gates.push(gate);
    return gate;
  }
  event(kind: string, agentId: string | null = null) {
    const seq = this.events.length + 1;
    this.events.push({ event_seq: seq, event_id: randomUUID(), kind, agent_id: agentId, actor_id: owner, operation_id: null, occurred_at: now, recorded_at: now, origin_installation_id: this.installId, origin_seq: seq, payload_version: 1, payload: {} });
  }
  add(id: string, text: string, options: Partial<Entry> = {}) {
    const rows = this.entries.get(id)!;
    const agent = this.agents.find(a => a.id === id)!;
    const messageId = randomUUID();
    const row: Entry = {
      entry_id: randomUUID(), agent_id: id, conversation_id: agent.conversation_id, conversation_seq: (rows.at(-1)?.conversation_seq || 0) + 1,
      kind: 'message', role: 'user', direction: 'incoming', text, sender_actor_id: owner, occurred_at: now, recorded_at: now,
      source: { type: 'message', message_id: messageId, run_id: null, provider: null, source_id: null, source_epoch: null, offset: null, length: null },
      delivery: { message_id: messageId, recipient_agent_id: id, state: 'pending', reason: 'runtime_unavailable', updated_at: now }, handling: null,
      ...options,
    };
    rows.push(row);
    if (row.source.type === 'message') this.event('message.created', id);
    return row;
  }
  addNative(id: string, text: string, role: 'user' | 'agent', provider: 'claude' | 'codex') {
    const agent = this.agents.find(a => a.id === id)!;
    const nativeId = randomUUID();
    const raw = Buffer.from(JSON.stringify({ id: nativeId, role, text }) + '\n');
    return this.add(id, text, {
      role, direction: role === 'user' ? 'incoming' : 'outgoing', sender_actor_id: role === 'user' ? null : agent.actor_id,
      occurred_at: '2026-10-07T12:00:00.000000Z',
      source: { type: 'native', provider, run_id: randomUUID(), source_id: randomUUID(), source_epoch: randomUUID(),
        offset: 0, length: raw.length, sha256: createHash('sha256').update(raw).digest('hex'), parser_version: 'native-v1',
        native_id: nativeId, part_index: 0, timestamp_origin: 'native' },
      delivery: null, handling: null,
    });
  }
  addFileReference(id: string, options: Partial<FileReference> = {}) {
    const agent = this.agents.find(a => a.id === id)!;
    const reference: FileReference = {
      reference_id: randomUUID(), agent_id: id, conversation_id: agent.conversation_id, actor_id: owner,
      installation_id: this.installId, recovery_epoch: this.epoch, state: 'referenced', location: 'workspace',
      path: 'files/report.txt', requested_path: './files//report.txt', label: 'Report', note: null,
      size_bytes: 1234567, mtime_ns: '1791460800123456789', observed_at: now, recorded_at: now, sha256: null,
      ...options,
    };
    const entry = this.add(id, reference.label, {
      kind: 'file_reference', role: reference.actor_id === owner ? 'user' : 'agent', direction: 'outgoing',
      sender_actor_id: reference.actor_id, occurred_at: reference.observed_at, recorded_at: reference.recorded_at,
      source: { type: 'file_reference', reference }, delivery: null, handling: null,
    });
    this.event('file.referenced', id);
    return entry;
  }
  recover() { this.epoch = randomUUID(); this.entries.set(alpha, []); this.entries.set(beta, []); this.events = []; }
  async start() {
    this.server = createServer(async (req, res) => {
      try {
        const path = new URL(req.url!, 'http://localhost').pathname;
        const json = (status: number, data: object) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(data)); };
        if (!path.startsWith('/api/')) {
          if (path !== '/' && !/^\/assets\/[\w.-]+$/.test(path)) { res.writeHead(404); res.end(); return; }
          const file = path === '/' ? 'index.html' : path.slice(1);
          const bytes = await readFile(resolve('dist', file));
          res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'", 'X-Content-Type-Options': 'nosniff' }); res.end(bytes); return;
        }
        const chunks: Buffer[] = []; for await (const part of req) chunks.push(Buffer.from(part));
        const raw = Buffer.concat(chunks).toString('utf8');
        const args = raw ? JSON.parse(raw) : {};
        if (path === '/api/v1/session') {
          if (req.method !== 'GET') { json(405, { error: 'method_not_allowed' }); return; }
          json(200, { actor_id: this.actor, api_version: 1 }); return;
        }
        if (path === '/api/v1/schema') {
          json(200, { api_version: this.version, result_schema: { type: 'object' }, operations: this.operations.map(name => ({ name, mutation: ['agents_create', 'agents_update', 'messages_send'].includes(name), input_schema: { type: 'object' }, output_data_schema: { type: 'object' } })) }); return;
        }
        const name = path.split('/').at(-1)!;
        this.calls.push({ name, args: structuredClone(args) });
        const gate = this.gates.find(g => g.name === name && g.matches(args));
        if (gate) { this.gates = this.gates.filter(g => g !== gate); gate.mark(); await gate.wait; }
        this.dispatch(name, args, res, json);
      } catch { if (!res.headersSent) res.writeHead(500); res.end(); }
    });
    await new Promise<void>(r => this.server.listen(0, '127.0.0.1', r));
    this.url = `http://127.0.0.1:${(this.server.address() as { port: number }).port}`;
    return this;
  }
  private dispatch(name: string, args: Args, res: ServerResponse, json: (status: number, data: object) => void) {
    const error = (code: string, message: string, status = 409) => json(status, { ok: false, operation_id: null, outcome: null, data: null, event_seq: null, error: { code, message, retryable: false } });
    const success = (data: object, accepted = false) => {
      const result = { ok: true, operation_id: args.idempotency_key ? randomUUID() : null, outcome: accepted ? 'accepted' : 'completed', data, event_seq: args.idempotency_key ? this.events.length : null, error: null };
      if (args.idempotency_key) this.receipts.set(`${name}:${args.idempotency_key}`, result);
      return result;
    };
    if (args.recovery_epoch && args.recovery_epoch !== this.epoch) { error('target_changed', 'Cursor belongs to another recovery epoch'); return; }
    if (args.idempotency_key && this.receipts.has(`${name}:${args.idempotency_key}`)) { json(200, this.receipts.get(`${name}:${args.idempotency_key}`)!); return; }
    if (name === 'config_get') {
      const config: Config = { installation_id: this.installId, recovery_epoch: this.epoch, revision: 1, defaults, icon_keys: keys, api_versions: [1] };
      json(200, success({ ...config, policy: 'allow_all', event_payload_versions: [1], runner_protocol_versions: [], browser_protocol_versions: [] }));
    } else if (name === 'agents_list') {
      const sorted = this.agents.slice().sort((a, b) => a.id.localeCompare(b.id));
      const candidates = sorted.filter(a => !args.after_id || a.id > args.after_id);
      const items = candidates.slice(0, Math.min(args.limit || 100, this.directoryPageSize));
      json(200, success({ items, next_after_id: items.at(-1)?.id || args.after_id || null, has_more: candidates.length > items.length, recovery_epoch: this.epoch, onbash_agent_id: beta }));
    } else if (name === 'agents_get') {
      const agent = this.agents.find(a => a.id === args.agent_id);
      agent ? json(200, success(agent)) : error('not_found', 'Agent not found', 404);
    } else if (name === 'agents_create') {
      if (!args.display_name.trim() || [...args.display_name.trim()].length > 120) { error('invalid_argument', 'Display names require 1–120 characters after trimming', 400); return; }
      const agent = makeAgent(randomUUID(), args.display_name.trim());
      Object.assign(agent, { icon_key: args.icon_key });
      this.agents.push(agent); this.entries.set(agent.id, []); this.event('agent.created', agent.id);
      json(200, success({ agent, provisioning_job_id: randomUUID() }, true));
    } else if (name === 'agents_update') {
      const agent = this.agents.find(a => a.id === args.agent_id)!;
      if (this.conflictUpdate) { this.conflictUpdate = false; agent.revision++; agent.display_name = 'Changed elsewhere'; }
      if (agent.revision !== args.expected_revision) { error('revision_conflict', 'Agent presentation revision changed'); return; }
      if (args.display_name !== undefined && (!args.display_name.trim() || [...args.display_name.trim()].length > 120)) { error('invalid_argument', 'Display names require 1–120 characters after trimming', 400); return; }
      if (args.display_name !== undefined) agent.display_name = args.display_name.trim();
      if (args.icon_key !== undefined) agent.icon_key = args.icon_key;
      agent.revision++; this.event('agent.presentation_changed', agent.id); json(200, success(agent, true));
    } else if (name === 'events_list') {
      const candidates = this.events.filter(e => e.event_seq > (args.after_seq || 0));
      const items = candidates.slice(0, args.limit || 100);
      json(200, success({ items, next_after_seq: items.at(-1)?.event_seq || args.after_seq || 0, has_more: candidates.length > items.length, recovery_epoch: this.epoch }));
    } else if (name === 'conversation_list') {
      if ((args.after_seq !== undefined || args.before_seq !== undefined) && !args.recovery_epoch) { error('invalid_argument', 'Cursor requires recovery epoch', 400); return; }
      const all = this.entries.get(args.agent_id)!;
      const size = args.limit || 50;
      const items = args.after_seq !== undefined ? all.filter(e => e.conversation_seq > args.after_seq).slice(0, size) : args.before_seq !== undefined ? all.filter(e => e.conversation_seq < args.before_seq).slice(-size) : all.slice(-size);
      const first = items[0]?.conversation_seq ?? null, last = items.at(-1)?.conversation_seq ?? null;
      json(200, success({ agent_id: args.agent_id, conversation_id: this.agents.find(a => a.id === args.agent_id)!.conversation_id, recovery_epoch: this.epoch,
        items, first_seq: first, last_seq: last,
        has_older: first !== null ? all.some(e => e.conversation_seq < first) : !!all.length && args.after_seq !== undefined,
        has_newer: last !== null ? all.some(e => e.conversation_seq > last) : !!all.length && args.before_seq !== undefined,
        publication: this.publication }));
    } else if (name === 'runtime_get') {
      json(200, success(this.runtimes.get(args.agent_id) || { agent_id: args.agent_id, status: 'disconnected',
        current_owner: null, observed_at: null, input_ready: false, native_setup: null, native_capture: null }));
    } else if (name === 'terminal_get') {
      json(200, success(this.terminals.get(args.agent_id) || { agent_id: args.agent_id, recovery_epoch: this.epoch,
        terminal_id: null, observed_at: null, status: 'disabled', reason: 'not_configured', control: null, recording: null, dimensions: null }));
    } else if (name === 'messages_send') {
      if (this.refuseSend) { this.refuseSend = false; json(503, { error: { code: 'unavailable', message: 'Transport unavailable' } }); return; }
      if (!args.body || Buffer.byteLength(args.body) > 65536) { error('invalid_argument', 'Message exceeds UTF-8 limit', 400); return; }
      const entry = this.add(args.recipient_agent_id, args.body);
      if (entry.source.type !== 'message') throw new Error('Synthetic send requires a message source');
      const message = { message_id: entry.source.message_id, message_seq: this.messages.length + 1, thread_id: randomUUID(), reply_to_message_id: null, sender_actor_id: owner, sender_agent_id: null, recipient_agent_id: args.recipient_agent_id, body: args.body, occurred_at: now, recorded_at: now };
      this.messages.push(message);
      const result = success({ message, delivery: entry.delivery, entries: [{ entry_id: entry.entry_id, agent_id: entry.agent_id, conversation_id: entry.conversation_id, conversation_seq: entry.conversation_seq }] }, true);
      if (this.dropSend) {
        this.dropSend = false;
        // Send partial response bytes before disconnecting so Chrome cannot transparently retry a connection that failed before any headers.
        res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '100000' });
        res.write('{"ok":');
        setTimeout(() => res.destroy(), 20);
      } else json(200, result);
    } else error('not_found', 'Operation is not implemented', 404);
  }
  async close() {
    for (const gate of this.gates) gate.release();
    this.server.closeAllConnections();
    await new Promise<void>(r => this.server.close(() => r()));
  }
}
