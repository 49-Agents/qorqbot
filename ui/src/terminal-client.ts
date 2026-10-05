import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { mergeControl, sameTerminal, terminalMessage } from './terminal-protocol';
import type { TerminalControl, TerminalDimensions, TerminalFrame, TerminalView } from './wire';

const INPUT_LIMIT = 65536, OUTPUT_LIMIT = 1024 * 1024, DISPLAY_ENTRY_LIMIT = 1024;
const encoder = new TextEncoder();
type PendingFrame = { type: 'input'; bytes: Uint8Array } | ({ type: 'resize' } & TerminalDimensions);
type DisplayEntry = { type: 'bytes'; bytes: Uint8Array } | ({ type: 'size' } & TerminalDimensions);
export type TerminalState = { connection: 'connecting' | 'connected' | 'closed'; owned: boolean; controlPending: boolean; warning: string };

// One connection owns one emulator, viewer identity and non-replayable frame queue.
export class TerminalClient {
  readonly terminal: Terminal;
  private fit: FitAddon;
  private socket: WebSocket;
  private observer: ResizeObserver;
  private active = true;
  private viewer = '';
  private view: TerminalView;
  private acknowledgedEpoch: number | null = null;
  private pendingControl: { action: 'take' | 'release'; epoch: number } | null = null;
  private queue: PendingFrame[] = [];
  private queuedBytes = 0;
  private pendingFrame: { kind: 'input' | 'resize'; epoch: number; sequence: number; bytes: number } | null = null;
  private lastSequence = 0;
  private sentSize: TerminalDimensions | null = null;
  private outputBytes = 0;
  private displayQueue: DisplayEntry[] = [];
  private writing = false;
  private overflow = false;
  private warning = '';
  private compositionEpoch: number | null = null;
  private pasteOutput: string[] | null = null;
  private removeHumanInput = () => {};

  constructor(element: HTMLElement, view: TerminalView, private callbacks: {
    current: () => boolean; changed: (state: TerminalState) => void;
    observation: (view: TerminalView) => void; refresh: () => void;
  }) {
    this.view = view;
    this.terminal = new Terminal({
      disableStdin: true, scrollback: 2000, fontSize: 13, fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
      theme: { background: '#0b0b0b', foreground: '#eeeeee', cursor: '#eeeeee', selectionBackground: '#444444',
        black: '#111111', red: '#aaaaaa', green: '#bbbbbb', yellow: '#cccccc', blue: '#aaaaaa', magenta: '#bbbbbb', cyan: '#cccccc', white: '#eeeeee',
        brightBlack: '#777777', brightRed: '#dddddd', brightGreen: '#dddddd', brightYellow: '#eeeeee', brightBlue: '#dddddd', brightMagenta: '#dddddd', brightCyan: '#eeeeee', brightWhite: '#ffffff' },
    });
    this.fit = new FitAddon(); this.terminal.loadAddon(this.fit); this.terminal.open(element);
    this.terminal.textarea?.setAttribute('aria-label', 'Terminal input');
    const url = new URL(`/api/v1/terminal/${view.agent_id}`, location.origin);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.socket = new WebSocket(url); this.socket.binaryType = 'arraybuffer';
    this.socket.onmessage = event => {
      if (!this.valid()) return;
      try { this.receive(event.data); } catch { this.fail('Terminal response could not be verified.'); }
    };
    this.socket.onerror = () => { if (this.valid()) this.fail('Terminal connection lost.'); };
    this.socket.onclose = () => { if (this.valid()) this.fail('Terminal connection closed.'); };
    this.bindHumanInput();
    let { width, height } = element.getBoundingClientRect();
    this.observer = new ResizeObserver(() => {
      if (!this.valid()) return;
      const next = element.getBoundingClientRect();
      // Grid updates and scrollbars are display effects, not container resizes.
      if (next.width === width && next.height === height) return;
      width = next.width; height = next.height; this.fitViewport();
    });
    this.observer.observe(element, { box: 'border-box' }); this.emit();
  }
  private valid() {
    if (!this.active) return false;
    if (!this.callbacks.current()) { this.dispose(); return false; }
    return true;
  }
  private owned() {
    const c = this.view.control;
    return this.active && !!this.viewer && c?.mode === 'human' && c.viewer_id === this.viewer
      && c.epoch === this.acknowledgedEpoch && !this.pendingControl;
  }
  private clearHumanInput() { this.compositionEpoch = null; this.pasteOutput = null; }
  private bindHumanInput() {
    const element = this.terminal.element!, textarea = this.terminal.textarea!;
    // D38 amendment 198: onKey is synchronous inside xterm's keyboard handler.
    // Ordinary onData also contains parser/device replies and is never an input source.
    const key = this.terminal.onKey(({ key, domEvent }) => {
      if (domEvent.isComposing || this.compositionEpoch !== null || !this.valid() || !this.owned() || this.overflow) return;
      domEvent.preventDefault(); this.input(key);
    });
    const data = this.terminal.onData(value => { this.pasteOutput?.push(value); });
    const input = (event: Event) => {
      const e = event as InputEvent;
      if (e.target === textarea && e.inputType === 'insertText' && !e.isComposing && this.compositionEpoch === null && e.data) this.input(e.data);
    };
    const start = (event: Event) => {
      if (event.target === textarea) this.compositionEpoch = this.valid() && this.owned() && !this.overflow ? this.acknowledgedEpoch : null;
    };
    const end = (event: Event) => {
      if (event.target !== textarea) return;
      const epoch = this.compositionEpoch; this.compositionEpoch = null;
      if (epoch !== null && epoch === this.acknowledgedEpoch) this.input((event as CompositionEvent).data);
    };
    const paste = (event: Event) => {
      event.preventDefault(); event.stopImmediatePropagation();
      if (!this.valid() || !this.owned() || this.overflow) return;
      const text = (event as ClipboardEvent).clipboardData?.getData('text/plain');
      if (!text) return;
      if (encoder.encode(text).length > INPUT_LIMIT) { this.warn('Paste exceeds 64 KiB and was not sent.'); return; }
      const parts: string[] = []; this.pasteOutput = parts;
      try { this.terminal.paste(text); } finally { this.pasteOutput = null; }
      // Check the entire framed paste and queue capacity before accepting any bytes.
      this.input(parts.join(''));
    };
    element.addEventListener('input', input);
    element.addEventListener('compositionstart', start, true);
    element.addEventListener('compositionend', end, true);
    element.addEventListener('paste', paste, true);
    this.removeHumanInput = () => {
      key.dispose(); data.dispose();
      element.removeEventListener('input', input);
      element.removeEventListener('compositionstart', start, true);
      element.removeEventListener('compositionend', end, true);
      element.removeEventListener('paste', paste, true);
    };
  }
  private emit() {
    const owned = this.owned();
    this.terminal.options.disableStdin = !owned || this.overflow;
    if (!owned || this.overflow) this.clearHumanInput();
    this.callbacks.changed({ connection: !this.active ? 'closed' : this.viewer ? 'connected' : 'connecting', owned, controlPending: !!this.pendingControl, warning: this.warning });
  }
  private send(value: object) {
    if (!this.valid()) return false;
    try {
      if (this.socket.readyState !== WebSocket.OPEN) throw new Error('closed');
      this.socket.send(JSON.stringify(value)); return true;
    } catch { this.fail('Terminal connection lost.'); return false; }
  }
  private clearQueue() { this.queue = []; this.queuedBytes = 0; this.sentSize = null; this.clearHumanInput(); }
  private clearDisplay() { this.displayQueue = []; this.outputBytes = 0; this.writing = false; }
  private fail(reason: string) {
    if (!this.active) return;
    this.active = false; this.acknowledgedEpoch = null; this.pendingControl = null;
    this.clearQueue(); this.clearDisplay(); this.pendingFrame = null;
    this.warning = `${reason} Earlier input may have arrived. Unsent input was cleared. Reconnect to continue.`;
    this.socket.close(); this.emit();
  }
  observe(view: TerminalView | null) {
    if (!this.valid()) return;
    if (!view || !sameTerminal(this.view, view) || view.status !== 'available') {
      this.fail('Terminal availability or identity changed.'); return;
    }
    this.applyControl(view.control);
    this.view = { ...view, control: this.view.control };
    this.emit();
  }
  private applyControl(next: TerminalControl | null) {
    const previous = this.view.control;
    const control = mergeControl(previous, next);
    if (control && (!previous || control.epoch > previous.epoch)) {
      if (this.queue.length || this.pendingFrame) this.warning = 'Control changed. Earlier input may have arrived; unsent input was cleared.';
      this.acknowledgedEpoch = null; this.clearQueue();
    }
    this.view = { ...this.view, control };
    if (control?.epoch === this.acknowledgedEpoch) this.lastSequence = Math.max(this.lastSequence, control.last_sequence);
  }
  private publish() { this.callbacks.observation(this.view); this.emit(); }
  private display(entry: DisplayEntry) {
    const last = this.displayQueue.at(-1);
    if (entry.type === 'size' && last?.type === 'size') {
      this.displayQueue[this.displayQueue.length - 1] = entry;
    } else {
      const bytes = entry.type === 'bytes' ? entry.bytes.byteLength : 0;
      // Include the in-flight write in both bounds until its parser callback.
      if (this.outputBytes + bytes > OUTPUT_LIMIT || this.displayQueue.length >= DISPLAY_ENTRY_LIMIT) {
        this.fail('Live output exceeded the display limit. Recording continues independently.'); return;
      }
      this.outputBytes += bytes; this.displayQueue.push(entry);
    }
    this.drainDisplay();
  }
  private drainDisplay() {
    if (!this.valid() || this.writing) return;
    while (this.displayQueue.length) {
      const entry = this.displayQueue[0];
      if (entry.type === 'size') {
        this.displayQueue.shift(); this.terminal.resize(entry.columns, entry.rows);
      } else {
        this.writing = true;
        this.terminal.write(entry.bytes, () => {
          if (!this.valid()) return;
          this.outputBytes -= entry.bytes.byteLength;
          this.displayQueue.shift(); this.writing = false; this.drainDisplay();
        });
        return;
      }
    }
  }
  private receive(data: unknown) {
    if (data instanceof ArrayBuffer) {
      if (!this.viewer || data.byteLength > INPUT_LIMIT) { this.fail('Invalid terminal output frame.'); return; }
      this.display({ type: 'bytes', bytes: new Uint8Array(data) });
      return;
    }
    const message = typeof data === 'string' ? terminalMessage(data) : null;
    if (!message || (!this.viewer && message.type !== 'hello')) { this.fail('Terminal unavailable. Invalid terminal protocol response.'); return; }
    if (message.type === 'hello') {
      if (this.viewer || !sameTerminal(this.view, message.terminal) || message.terminal.status !== 'available') { this.fail('Terminal identity changed.'); return; }
      this.viewer = message.viewer_id; this.observe(message.terminal);
      if (this.active) { this.display({ type: 'size', ...message.terminal.dimensions! }); this.publish(); }
    } else if (message.type === 'size') {
      this.display(message);
    } else if (message.type === 'state') {
      this.observe(message.terminal); if (this.active) this.publish();
    } else if (message.type === 'control') {
      const pending = this.pendingControl;
      this.applyControl(message.control);
      this.pendingControl = null;
      if (pending && message.control.epoch === pending.epoch + 1 && message.control.epoch === this.view.control?.epoch) {
        if (pending.action === 'take' && message.control.mode === 'human' && message.control.viewer_id === this.viewer) {
          this.acknowledgedEpoch = message.control.epoch; this.lastSequence = this.view.control.last_sequence;
          this.overflow = false; this.warning = '';
          this.fitViewport();
        } else if (pending.action === 'release' && message.control.mode === 'unclaimed') {
          this.acknowledgedEpoch = null; this.warning = ''; this.overflow = false;
        } else { this.fail('Control reply did not match the request.'); return; }
      }
      this.publish();
    } else if (message.type === 'frame') this.receipt(message);
    else {
      if (message.related_type === 'control' && message.code === 'revision_conflict' && this.pendingControl) {
        this.pendingControl = null; this.acknowledgedEpoch = null; this.clearQueue();
        this.warning = 'Control changed. Refreshing the observation; take control again when ready.';
        this.callbacks.refresh(); this.emit();
      } else {
        this.fail('The server could not confirm the terminal request.');
        this.callbacks.refresh();
      }
    }
  }
  control(action: 'take' | 'release') {
    if (!this.valid() || !this.viewer || this.pendingControl || !this.view.control) return;
    if (action === 'release' && !this.owned()) return;
    this.clearQueue(); this.acknowledgedEpoch = null;
    this.pendingControl = { action, epoch: this.view.control.epoch };
    this.send({ type: 'control', action, expected_epoch: this.pendingControl.epoch }); this.emit();
  }
  input(data: string) {
    if (!this.valid() || !this.owned() || this.overflow || !data) return;
    const bytes = encoder.encode(data);
    if (bytes.length > INPUT_LIMIT) { this.warn('Input exceeds 64 KiB and was not sent.'); return; }
    if (this.queuedBytes + bytes.length > INPUT_LIMIT) {
      this.overflow = true; this.warn('Input queue is full. Additional input was not sent. Release and take control to resume.'); return;
    }
    this.queue.push({ type: 'input', bytes }); this.queuedBytes += bytes.length; this.flush();
  }
  private warn(message: string) { this.warning = message; this.emit(); }
  private fitViewport() {
    if (!this.owned()) return;
    const proposed = this.fit.proposeDimensions();
    if (!proposed || !Number.isFinite(proposed.cols) || !Number.isFinite(proposed.rows)) return;
    this.resize({ columns: Math.max(20, Math.min(500, Math.floor(proposed.cols))), rows: Math.max(5, Math.min(250, Math.floor(proposed.rows))) });
  }
  private resize(size: TerminalDimensions) {
    if (!this.owned()) return;
    const queued = this.queue.find(frame => frame.type === 'resize');
    if (queued?.type === 'resize' && size.columns === queued.columns && size.rows === queued.rows) return;
    this.queue = this.queue.filter(frame => frame.type !== 'resize');
    // Returning to an already sent size cancels an obsolete queued proposal.
    if (size.columns === this.sentSize?.columns && size.rows === this.sentSize.rows) return;
    this.queue.push({ type: 'resize', ...size }); this.flush();
  }
  private flush() {
    if (!this.valid() || !this.owned() || this.pendingFrame || !this.queue.length) return;
    let frame = this.queue[0];
    if (frame.type === 'input') {
      // Batch only the admitted input prefix. A resize remains an ordered barrier.
      let count = 0, length = 0;
      for (const next of this.queue) {
        if (next.type !== 'input') break;
        count++; length += next.bytes.length;
      }
      if (count > 1) {
        const bytes = new Uint8Array(length);
        let offset = 0;
        for (const next of this.queue) {
          if (next.type !== 'input') break;
          bytes.set(next.bytes, offset); offset += next.bytes.length;
        }
        frame = { type: 'input', bytes };
      }
      this.queue.splice(0, count);
    } else this.queue.shift();
    const sequence = Math.max(this.lastSequence, this.view.control!.last_sequence) + 1;
    if (!Number.isSafeInteger(sequence)) { this.fail('Terminal sequence limit reached.'); return; }
    this.lastSequence = sequence;
    const epoch = this.acknowledgedEpoch!;
    this.pendingFrame = { kind: frame.type, epoch, sequence, bytes: frame.type === 'input' ? frame.bytes.length : 0 };
    if (frame.type === 'input') {
      this.queuedBytes -= frame.bytes.length;
      let binary = ''; for (const byte of frame.bytes) binary += String.fromCharCode(byte);
      this.send({ type: 'input', epoch, sequence, data: btoa(binary) });
    } else {
      this.sentSize = { columns: frame.columns, rows: frame.rows };
      this.send({ type: 'resize', epoch, sequence, columns: frame.columns, rows: frame.rows });
    }
  }
  private receipt(frame: TerminalFrame) {
    if (frame.viewer_id !== this.viewer) { this.fail('Terminal receipt identity changed.'); return; }
    const pending = this.pendingFrame;
    // A late receipt cannot undo a newer control record or grant input.
    if (!pending || frame.epoch !== pending.epoch || frame.sequence !== pending.sequence || frame.kind !== pending.kind) {
      if (this.view.control && (frame.epoch < this.view.control.epoch || (frame.epoch === this.view.control.epoch && frame.sequence <= this.view.control.last_sequence))) return;
      this.fail('Unexpected terminal receipt.'); return;
    }
    this.pendingFrame = null;
    if (frame.outcome !== 'submitted' || frame.bytes_written !== pending.bytes) { this.fail('Terminal input confirmation is uncertain.'); return; }
    if (frame.epoch === this.view.control?.epoch) {
      this.applyControl({ ...this.view.control, last_sequence: frame.sequence }); this.publish();
    }
    this.flush();
  }
  dispose() {
    this.active = false; this.clearQueue(); this.clearDisplay(); this.pendingFrame = null; this.pendingControl = null; this.acknowledgedEpoch = null;
    this.socket.onmessage = null; this.socket.onerror = null; this.socket.onclose = null;
    this.socket.close(); this.observer.disconnect(); this.removeHumanInput(); this.terminal.dispose();
  }
}
