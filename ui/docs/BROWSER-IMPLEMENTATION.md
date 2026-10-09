# D57 browser UI implementation

2026-10-09. Implements the UI lane of the Manager's frozen
[D57 contract at core c96952f](https://github.com/qorqutbot/qorqut-core/blob/c96952f/docs/BROWSER-APP-INTEGRATION.md).
This records frontend behavior; combined installed-core/TigerVNC acceptance and
deployment belong to Manager.

The UI validates BrowserView, BrowserSurface, protocol-1 Binding,
InputRequestView and the nullable ConversationItem.browser_request annotation.
A request card requires source `{type:"browser_request",request_id}`, the exact
prompt and matching agent/conversation/requester identity. Unknown shapes have
no viewer. Terminal cards remain historical and cannot reopen control. The
browser annotation is a mutable read view; the UI creates no transcript entries.

Selected-agent browser_get observations use the existing abort, selection and
recovery guards and bounded polling. New browser actions use the same operation
client and Pending map as other mutations. An ambiguous retry retains its
original operation, arguments, expected revision and idempotency key. Notes
remain in page memory. Revision ordering prevents a stale receipt or poll from
reopening a terminal request. Servers without the advertised browser operations
retain an unavailable Browser card and ordinary Chat/Terminal behavior.

One BrowserConnection owns the noVNC RFB object and its DOM container. Inline,
Computer and expanded presentations move that container without reconnecting.
Expansion does not change framebuffer dimensions; collapse returns it inline.
Agent/view changes dispose input, retaining the durable request in core. A new
connection waits for the previous socket to close. Socket loss requires explicit
Reconnect; interrupted control uses Resume. No input or mutation is replayed by
the viewer. Opening Computer's attached Browser creates `Manual browser control`
through core or opens the existing request. Machine/Desktop remains unavailable.

The lockfile pins @novnc/novnc **1.7.0**. Its installed package exports RFB from
`@novnc/novnc`; the local declaration covers only the public API used here.
The [official API](https://github.com/novnc/noVNC/blob/master/docs/API.md) and
installed pinned source were checked. RFB uses scaleViewport=true,
resizeSession=false, shared=true and viewOnly=false only for a validated ready
request with a matching fresh browser observation. The sole destination is
same-origin `/api/v1/browser/{request_id}/stream`. The UI sends raw binary RFB,
without a custom initial frame, browser-service destination or added headers.
Core handles its service handshake. Original licenses, attribution and source
locations ship at `/assets/browser-notices.txt`.

The existing synthetic API and Chrome suite own the UI checks. The small local
RFB fixture exchanges real WebSocket/RFB frames with the installed noVNC client;
it supplies synthetic pixels and records ordinary pointer/key events. It uses
no native inference, owner profile or live service. Playwright's WebSocket mock
is unsuitable for noVNC's raw-channel property checks, so Terminal's existing
mock and the browser's socket fixture are exercised separately.

See [focused UI evidence](evidence/browser-app-ui-r1.md). Actual browser pages,
TigerVNC relay admission, core receipts/notifications/restart and native MCP
loading remain the Manager's combined functional acceptance.
