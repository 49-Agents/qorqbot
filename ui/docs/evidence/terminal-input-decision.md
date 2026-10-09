# D38 input admission amendment 198

Manager decision, 2026-10-08. This is a dependency/event-ordering probe, not
acceptance of the unfinished Terminal UI or a real backend connection.

The UI builder reported that Chrome runs the queued clearing microtask between
the DOM capture listener and xterm's keyboard listener. Its installed-Chrome
trace showed trusted `a` and Ctrl-C events reaching `onData` after the proposed
guard was cleared. The Manager read that diagnostic and rejected the guard.

The Manager independently downloaded the official `@xterm/xterm` 6.0.0 package,
verified its published SHA-512 integrity against the previously pinned value,
and inspected `CoreBrowserTerminal`, `Clipboard`, `CompositionHelper` and
`CoreService`. Public `onKey` includes the encoded keyboard string and its DOM
event; public `paste` synchronously emits the library's prepared/framed text.
Ordinary `onData` also carries output-parser replies and delayed composition.
The public API is documented by [xterm](https://xtermjs.org/docs/api/terminal/classes/terminal/).

An independent, disposable page used installed Chrome 153.0.8010.36 and
Playwright 1.64.0 with that verified package. The replacement input paths produced
exactly nine expected entries: lowercase, uppercase, Ctrl-C, left arrow, Enter,
Unicode insertion, one bracketed multiline paste, one Japanese composition and
a following key. A later device-status/attribute/color-query write produced no
pane input. Keyboard and Unicode insertion used Playwright input; composition
used Chrome's input protocol. The paste event was DOM-dispatched and synthetic.
The same probe rejected keyboard/text forwarding after control was disabled.

Private reproducible evidence is under the build run's `manager-input198/`:
`metadata.json`, the integrity-checked package, `explicit-input-probe.mjs` and
`explicit-input-probe.json`. It uses no owner browser, credentials, native CLI,
real tmux pane or application implementation. It does not establish physical IME
coverage across platforms. The builder must still complete the owning Chrome
checks and a fresh independent review; actual core/UI integration follows those.
