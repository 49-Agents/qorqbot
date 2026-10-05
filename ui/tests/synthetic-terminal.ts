// Protocol fixture only: no shell, recorder, control service or native provider.
import { randomUUID } from 'node:crypto';
import type { Page, WebSocketRoute } from '@playwright/test';
import type { TerminalView } from '../src/wire';
import { SyntheticAPI, beta } from './synthetic-api';

type Control = { type: 'control'; action: 'take' | 'release'; expected_epoch: number };
export type Frame = { type: 'input'; epoch: number; sequence: number; data: string }
  | { type: 'resize'; epoch: number; sequence: number; columns: number; rows: number };
export class SyntheticTerminal {
  connections: { route: WebSocketRoute; viewer: string; messages: (Control | Frame)[]; closed: boolean }[] = [];
  autoControl = true;
  autoReceipt = true;
  holdNextInput = false;
  hello: ((route: WebSocketRoute, viewer: string) => void) | null = null;
  constructor(readonly api: SyntheticAPI, readonly id = beta) {
    api.operations.push('terminal_get');
    api.terminals.set(id, { agent_id: id, recovery_epoch: api.epoch, terminal_id: randomUUID(),
      observed_at: '2026-10-08T12:00:00Z', status: 'available', reason: 'running',
      control: { mode: 'unclaimed', epoch: 0, viewer_id: null, last_sequence: 0 },
      recording: { state: 'healthy', local_captured_offset: 120, host_archived_offset: 0 } });
  }
  get view() { return this.api.terminals.get(this.id)!; }
  set view(view: TerminalView) { this.api.terminals.set(this.id, view); }
  get current() { return this.connections.at(-1)!; }
  get frames() { return this.current.messages.filter((m): m is Frame => m.type !== 'control'); }
  get inputs() { return this.frames.filter(m => m.type === 'input'); }
  async route(page: Page) {
    await page.routeWebSocket('**/api/v1/terminal/*', route => {
      const connection = { route, viewer: randomUUID(), messages: [] as (Control | Frame)[], closed: false };
      this.connections.push(connection);
      route.onClose(() => { connection.closed = true; });
      route.onMessage(raw => {
        const message: Control | Frame = JSON.parse(raw.toString()); connection.messages.push(message);
        if (message.type === 'control') {
          if (!this.autoControl) return;
          const c = this.view.control!;
          if (message.expected_epoch !== c.epoch) {
            route.send(JSON.stringify({ type: 'error', code: 'revision_conflict', message: 'Synthetic stale control', related_type: 'control', sequence: null })); return;
          }
          this.view = { ...this.view, control: { mode: message.action === 'take' ? 'human' : 'unclaimed',
            epoch: c.epoch + 1, viewer_id: message.action === 'take' ? connection.viewer : null, last_sequence: 0 } };
          route.send(JSON.stringify({ type: 'control', control: this.view.control }));
        } else {
          this.view = { ...this.view, control: { ...this.view.control!, last_sequence: message.sequence } };
          if (message.type === 'input' && this.holdNextInput) { this.holdNextInput = false; this.autoReceipt = false; }
          if (this.autoReceipt) this.receipt(message, 'submitted', connection);
        }
      });
      if (this.hello) this.hello(route, connection.viewer);
      else route.send(JSON.stringify({ type: 'hello', protocol: 1, viewer_id: connection.viewer, terminal: this.view }));
    });
  }
  send(value: object) { this.current.route.send(JSON.stringify(value)); }
  state(view = this.view) { this.send({ type: 'state', terminal: view }); }
  output(bytes: Buffer | string) { this.current.route.send(typeof bytes === 'string' ? Buffer.from(bytes) : bytes); }
  receipt(frame: Frame, outcome: 'submitted' | 'uncertain' = 'submitted', connection = this.current) {
    connection.route.send(JSON.stringify({ type: 'frame', kind: frame.type, viewer_id: connection.viewer,
      epoch: frame.epoch, sequence: frame.sequence, outcome, bytes_written: frame.type === 'input' ? Buffer.from(frame.data, 'base64').length : 0 }));
  }
}
