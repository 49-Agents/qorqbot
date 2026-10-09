import RFB from '@novnc/novnc';

// One owner per selected agent/view. Presentation moves this node; it never
// creates another connection. An attempted connection is not retried by polling.
export class BrowserConnection {
  readonly container = document.createElement('div');
  status: 'idle' | 'connecting' | 'live' | 'lost' = 'idle';
  private rfb?: RFB;
  private socket?: WebSocket;
  private attempted = '';
  private generation = 0;
  private static closing: Promise<void> = Promise.resolve();
  private timer?: number;
  private disposed = false;
  constructor(private changed: () => void) { this.container.className = 'browser-rfb'; }
  private update(status: BrowserConnection['status']) { this.status = status; if (!this.disposed) this.changed(); }
  show(host: HTMLElement, requestId: string, attempt: number) {
    if (this.disposed) return;
    if (this.container.parentElement !== host) {
      host.append(this.container);
      // Run after a newly expanded dialog enters the top layer. Polls keep the
      // same host and never steal focus from the note, composer or Terminal.
      if (this.rfb) queueMicrotask(() => {
        if (host.isConnected && this.container.parentElement === host) this.rfb?.focus({ preventScroll: true });
      });
    }
    const key = `${requestId}:${attempt}`;
    if (key === this.attempted) return;
    this.attempted = key;
    this.stop();
    const generation = this.generation;
    this.update('connecting');
    void BrowserConnection.closing.then(() => {
      if (this.disposed || generation !== this.generation || !this.container.isConnected) return;
      const url = new URL(`/api/v1/browser/${requestId}/stream`, location.href);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      try {
        // Core consumes the service handshake. noVNC speaks raw RFB from byte 1.
        const socket = this.socket = new WebSocket(url);
        const rfb = this.rfb = new RFB(this.container, socket, { shared: true });
        rfb.scaleViewport = true;
        rfb.resizeSession = false;
        rfb.viewOnly = false;
        const current = () => !this.disposed && generation === this.generation;
        rfb.addEventListener('connect', () => { if (current()) { clearTimeout(this.timer); this.update('live'); } });
        const lost = () => { if (current()) this.stop(); };
        rfb.addEventListener('disconnect', lost);
        rfb.addEventListener('credentialsrequired', lost);
        rfb.addEventListener('securityfailure', lost);
        rfb.addEventListener('serververification', lost);
        socket.addEventListener('close', lost);
        this.timer = window.setTimeout(lost, 15000);
      } catch { this.stop(); }
    });
  }
  stop() {
    ++this.generation;
    clearTimeout(this.timer);
    const socket = this.socket, rfb = this.rfb;
    this.rfb = undefined; this.socket = undefined;
    if (socket) {
      // A replacement must wait for actual socket closure, not just disconnect().
      BrowserConnection.closing = socket.readyState === WebSocket.CLOSED ? Promise.resolve()
        : new Promise(resolve => socket.addEventListener('close', () => resolve(), { once: true }));
      if (rfb) { rfb.viewOnly = true; rfb.disconnect(); }
      socket.close();
    }
    this.container.replaceChildren();
    if (this.status !== 'idle' && this.status !== 'lost') this.update('lost');
  }
  reset() { this.stop(); this.attempted = ''; this.update('idle'); }
  dispose() { this.disposed = true; this.stop(); this.container.remove(); }
}
