// Only the pinned noVNC 1.7.0 public API used by this UI.
declare module '@novnc/novnc' {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, channel: WebSocket, options: { shared: boolean });
    scaleViewport: boolean;
    resizeSession: boolean;
    viewOnly: boolean;
    focus(options?: FocusOptions): void;
    disconnect(): void;
  }
}
