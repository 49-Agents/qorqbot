"""A private, owner-authorized manual viewer for one confined Chrome instance.

Composition supplies the owner/lease boundary.  This module never creates a
browser, accepts a browser URL, or stores a lease: it only uses the current
``BrowserBinding`` and the fixed namespace socket connector.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import re
from urllib.parse import parse_qsl, urlsplit

import anyio
from starlette.responses import HTMLResponse, JSONResponse, Response
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from qorqut_operations.contracts import DomainError
from bazarlyk.transport.auth import normalize_origin
from .cdp import BrowserCDPProxy
from .socket import BrowserBinding


_TARGET = re.compile(r"[A-Za-z0-9._~-]{1,256}\Z")
_MAX_TEXT = 4096
_MAX_IMAGE = 8 * 1024 * 1024


class _NoRedirectConnect(connect):
    def process_redirect(self, error):
        return error


@dataclass(frozen=True)
class BrowserViewerRoute:
    """One pre-authorized private origin bound to an exact browser instance."""

    browser: BrowserBinding
    origin: str

    def __post_init__(self):
        if not isinstance(self.browser, BrowserBinding):
            raise TypeError("Browser viewer requires a browser binding")
        object.__setattr__(self, "origin", normalize_origin(self.origin))


def _header(scope, name):
    needle = name.encode("ascii").lower()
    return next((value.decode("latin-1") for key, value in scope.get("headers", ())
                 if key.lower() == needle), None)


def _png(value):
    try:
        image = base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise DomainError("environment_unavailable", "Browser image data is invalid", retryable=True) from None
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > _MAX_IMAGE:
        raise DomainError("environment_unavailable", "Browser image data is unavailable", retryable=True)
    return image


class BrowserCDP:
    """Fixed CDP operations over one connector-provided private socket."""

    def __init__(self, connector, binding):
        self.connector, self.binding = connector, binding

    async def _socket(self):
        return await anyio.to_thread.run_sync(self.connector.connect, self.binding)

    async def _http(self, path):
        sock = await self._socket()
        writer = None
        try:
            reader, writer = await asyncio.open_connection(sock=sock)
            writer.write(("GET " + path + " HTTP/1.1\r\nHost: 127.0.0.1:" + str(self.binding.cdp_port)
                          + "\r\nConnection: close\r\n\r\n").encode("ascii"))
            await writer.drain()
            # Chrome's response is ordinary HTTP over a local socket, but TCP
            # is still a stream: headers and body may arrive in fragments.
            # Keep one deadline for the complete request so a peer cannot keep
            # this private viewer busy forever by dribbling bytes.
            async with asyncio.timeout(10):
                status, headers, remainder = await BrowserCDPProxy._headers(reader)
                lengths = [value for key, value in headers if key == "content-length"]
                if status != 200 or len(lengths) != 1 or any(
                    key == "transfer-encoding" for key, _value in headers
                ):
                    raise ValueError
                body = await BrowserCDPProxy._body(reader, headers, remainder)
            return json.loads(body)
        except (OSError, ValueError, TypeError, UnicodeDecodeError, asyncio.TimeoutError):
            raise DomainError("environment_unavailable", "Browser CDP inventory is unavailable", retryable=True) from None
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            else:
                sock.close()

    async def tabs(self):
        value = await self._http("/json/list")
        if not isinstance(value, list):
            raise DomainError("environment_unavailable", "Browser CDP inventory is invalid", retryable=True)
        result = []
        for row in value:
            identifier = row.get("id") if isinstance(row, dict) else None
            if not isinstance(row, dict) or row.get("type") != "page" or not isinstance(identifier, str) or not _TARGET.fullmatch(identifier):
                continue
            title, url = row.get("title", ""), row.get("url", "")
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            result.append({"id": row["id"], "title": title[:512], "url": url[:2048]})
        return result

    async def _page(self, target, method, params=None, *, frame=False):
        tabs = await self.tabs()
        if target not in {tab["id"] for tab in tabs}:
            raise DomainError("forbidden", "Browser tab is not in the current inventory")
        sock = await self._socket()
        uri = f"ws://127.0.0.1:{self.binding.cdp_port}/devtools/page/{target}"
        try:
            async with asyncio.timeout(10):
                async with _NoRedirectConnect(uri, sock=sock, proxy=None, compression=None, max_size=_MAX_IMAGE * 2,
                                              max_queue=8, open_timeout=10, ping_interval=None) as websocket:
                    await websocket.send(json.dumps({"id": 1, "method": method, "params": params or {}}, separators=(",", ":")))
                    while True:
                        raw = await websocket.recv()
                        if not isinstance(raw, str):
                            raise ValueError
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            raise ValueError
                        if not frame:
                            if value.get("id") == 1:
                                if "error" in value or not isinstance(value.get("result", {}), dict):
                                    raise ValueError
                                return value["result"]
                            continue
                        if value.get("id") == 1 and "error" in value:
                            raise ValueError
                        if value.get("method") == "Page.screencastFrame":
                            frame_params = value.get("params")
                            if not isinstance(frame_params, dict) or not isinstance(frame_params.get("data"), str):
                                raise ValueError
                            session = frame_params.get("sessionId")
                            if not isinstance(session, int):
                                raise ValueError
                            await websocket.send(json.dumps({"id": 2, "method": "Page.screencastFrameAck",
                                                             "params": {"sessionId": session}}, separators=(",", ":")))
                            await websocket.send(json.dumps({"id": 3, "method": "Page.stopScreencast", "params": {}}, separators=(",", ":")))
                            return frame_params
        except (OSError, ValueError, TypeError, asyncio.TimeoutError, ConnectionClosed, WebSocketException):
            raise DomainError("environment_unavailable", "Browser CDP operation is unavailable", retryable=True) from None
        finally:
            # ``connect`` consumes the socket on success.  On a failed
            # handshake it remains ours, so close it explicitly either way.
            if sock.fileno() >= 0:
                sock.close()

    async def screenshot(self, target):
        return _png((await self._page(target, "Page.captureScreenshot", {"format": "png"})).get("data"))

    async def screencast(self, target):
        return _png((await self._page(target, "Page.startScreencast", {"format": "png"}, frame=True)).get("data"))

    async def action(self, target, method, params):
        await self._page(target, method, params)


_PAGE = """<!doctype html><meta charset=utf-8><title>Private Browser</title>
<main><p><button id=acquire>Acquire browser control</button><button id=release>Release control</button></p><p id=status role=status></p><label>Tab <select id=tabs></select></label><input id=url aria-label=Address><button id=go>Go</button><button id=shot>Refresh</button><button id=live>Live frame</button><textarea id=text aria-label="Type into page"></textarea><button id=type>Type</button><p id=title></p><img id=screen tabindex=0 alt="Browser screenshot"></main>
<script>
const q=s=>document.querySelector(s), tabs=q('#tabs'), url=q('#url'), title=q('#title'), screen=q('#screen'), text=q('#text'), status=q('#status');
let selected='', actions=Promise.resolve(), lastMove=0, frameTimer; const note=v=>status.textContent=v; const call=async(path,opt={})=>{let r=await fetch(path,opt);if(!r.ok)throw Error('Browser action unavailable');return r.status===204?{}:r.json()}; const queue=work=>actions=actions.catch(()=>{}).then(work); const action=body=>queue(async()=>{const result=await call('api/action',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});if(body.type!=='mouseMoved'&&body.type!=='keyUp'){clearTimeout(frameTimer);frameTimer=setTimeout(()=>{if(selected)screen.src='api/screenshot?target='+encodeURIComponent(selected)},80)}return result});
async function refresh(){try{let prior=selected||tabs.value,v=await call('api/tabs');tabs.replaceChildren();for(const t of v.tabs){let o=document.createElement('option');o.value=t.id;o.textContent=t.title||t.url||'Untitled';tabs.append(o)}selected=v.tabs.some(t=>t.id===prior)?prior:(v.tabs[0]?.id||'');tabs.value=selected;let t=v.tabs.find(x=>x.id===selected)||{};title.textContent=t.title||'';url.value=t.url||'';if(selected)screen.src='api/screenshot?target='+encodeURIComponent(selected);note(selected?'Control acquired':'No page tab is available')}catch(_){note('Acquire control before viewing the browser')}}
tabs.onchange=()=>{selected=tabs.value;refresh()};q('#acquire').onclick=async()=>{try{await queue(()=>call('api/acquire',{method:'POST'}));await refresh()}catch(_){note('Browser control could not be acquired')}};q('#release').onclick=async()=>{try{await queue(()=>call('api/release',{method:'POST'}));selected='';screen.removeAttribute('src');note('Control released')}catch(_){note('Browser control could not be released')}};q('#shot').onclick=refresh;q('#live').onclick=()=>{if(selected)screen.src='api/screencast?target='+encodeURIComponent(selected)};q('#go').onclick=()=>action({target:selected,kind:'address',url:url.value}).then(refresh).catch(()=>note('Navigation failed'));q('#type').onclick=()=>action({target:selected,kind:'text',text:text.value}).then(()=>text.value='').catch(()=>note('Text input failed'));
for(const [event,type] of [['mousedown','mousePressed'],['mouseup','mouseReleased'],['mousemove','mouseMoved']])screen.addEventListener(event,e=>{if(!selected||!screen.naturalWidth||!screen.naturalHeight)return;if(type==='mouseMoved'&&Date.now()-lastMove<50)return;lastMove=Date.now();if(type==='mousePressed')screen.focus();let r=screen.getBoundingClientRect(),p={target:selected,kind:'mouse',type,x:(e.clientX-r.left)*screen.naturalWidth/r.width,y:(e.clientY-r.top)*screen.naturalHeight/r.height};if(type!=='mouseMoved'){p.button='left';p.clickCount=1}action(p).catch(()=>note('Pointer action failed'))});screen.addEventListener('wheel',e=>{if(!selected)return;e.preventDefault();let r=screen.getBoundingClientRect();action({target:selected,kind:'mouse',type:'mouseWheel',x:(e.clientX-r.left)*screen.naturalWidth/r.width,y:(e.clientY-r.top)*screen.naturalHeight/r.height,deltaX:e.deltaX,deltaY:e.deltaY}).catch(()=>note('Scroll failed'))},{passive:false});
for(const [event,type] of [['keydown','keyDown'],['keyup','keyUp']])screen.addEventListener(event,e=>{if(!selected)return;e.preventDefault();action({target:selected,kind:'key',type,key:e.key,code:e.code,windowsVirtualKeyCode:e.keyCode,modifiers:(e.altKey?1:0)|(e.ctrlKey?2:0)|(e.metaKey?4:0)|(e.shiftKey?8:0),text:type==='keyDown'&&!e.ctrlKey&&!e.metaKey?(e.key==='Enter'?'\\r':Array.from(e.key).length===1?e.key:''):''}).catch(()=>note('Key input failed'))});
</script>"""


class BrowserViewer:
    """Small ASGI owner viewer; owner and lease policy are injected by composition."""

    def __init__(self, route, *, connector, current_binding, authorize, acquire, release, client_factory=BrowserCDP):
        if not isinstance(route, BrowserViewerRoute) or not callable(current_binding) or not callable(authorize) or not callable(acquire) or not callable(release):
            raise TypeError("Browser viewer needs trusted route, binding, owner and lease boundaries")
        self.route, self.connector, self.current_binding = route, connector, current_binding
        self.authorize, self.acquire, self.release, self.client_factory = authorize, acquire, release, client_factory

    async def _binding(self, scope, action, *, acquiring=False, releasing=False, require_origin=False):
        host, origin = _header(scope, "host"), _header(scope, "origin")
        expected = urlsplit(self.route.origin)
        accepted_hosts = {expected.netloc.lower()}
        if expected.port in {80, 443} and expected.hostname:
            accepted_hosts.add(expected.hostname.lower())
        if host is None or host.lower() not in accepted_hosts or (require_origin and origin is None) or (origin is not None and normalize_origin(origin) != self.route.origin):
            raise DomainError("forbidden", "Private browser viewer origin is invalid")
        if await anyio.to_thread.run_sync(self.current_binding) != self.route.browser:
            raise DomainError("environment_conflict", "Private browser viewer binding is stale")
        callback = self.acquire if acquiring else self.release if releasing else self.authorize
        try:
            allowed = await anyio.to_thread.run_sync(callback, scope, self.route.browser) if (acquiring or releasing) else await anyio.to_thread.run_sync(callback, scope, self.route.browser, action)
        except DomainError:
            raise
        except Exception:
            allowed = False
        if allowed is not True:
            raise DomainError("forbidden", "Private browser viewer owner lease is invalid")
        return self.route.browser

    @staticmethod
    async def _json(receive):
        data = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        raise DomainError("operation_unavailable", "Viewer client disconnected")
                    if message["type"] == "http.request":
                        data.extend(message.get("body", b""))
                        if len(data) > 8192:
                            raise DomainError("invalid_request", "Viewer request is too large")
                        if not message.get("more_body"):
                            break
        except asyncio.TimeoutError:
            raise DomainError("operation_unavailable", "Viewer request timed out", retryable=True) from None
        try:
            value = json.loads(data)
        except (ValueError, TypeError):
            raise DomainError("invalid_request", "Viewer request is invalid") from None
        if not isinstance(value, dict):
            raise DomainError("invalid_request", "Viewer request is invalid")
        return value

    @staticmethod
    def _target(scope):
        try:
            pairs = parse_qsl(scope.get("query_string", b"").decode("ascii"), keep_blank_values=True, strict_parsing=True)
        except (AttributeError, UnicodeDecodeError, ValueError):
            raise DomainError("invalid_request", "Viewer tab selection is invalid") from None
        if len(pairs) != 1 or pairs[0][0] != "target" or not isinstance(pairs[0][1], str) or not _TARGET.fullmatch(pairs[0][1]):
            raise DomainError("invalid_request", "Viewer tab selection is invalid")
        return pairs[0][1]

    @staticmethod
    async def _current_tab(client, target):
        if target not in {tab["id"] for tab in await client.tabs()}:
            raise DomainError("forbidden", "Viewer tab is not in the current browser inventory")
        return target

    @staticmethod
    def _private(response):
        response.headers.update({
            "cache-control": "no-store",
            "pragma": "no-cache",
            "x-content-type-options": "nosniff",
            "content-security-policy": "default-src 'self'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        })
        return response

    async def __call__(self, scope, receive, send):
        try:
            if scope.get("type") != "http":
                raise DomainError("invalid_request", "Viewer only accepts HTTP")
            path, method = scope.get("path"), scope.get("method")
            if path == "/" and method == "GET":
                await self._binding(scope, "view")
                return await self._private(HTMLResponse(_PAGE))(scope, receive, send)
            if path == "/api/acquire" and method == "POST":
                await self._binding(scope, "acquire", acquiring=True, require_origin=True)
                return await self._private(JSONResponse({"acquired": True}))(scope, receive, send)
            if path == "/api/release" and method == "POST":
                await self._binding(scope, "release", releasing=True, require_origin=True)
                return await self._private(JSONResponse({"released": True}))(scope, receive, send)
            binding = await self._binding(scope, "interact", require_origin=method == "POST")
            client = self.client_factory(self.connector, binding)
            if path == "/api/tabs" and method == "GET":
                return await self._private(JSONResponse({"tabs": await client.tabs()}))(scope, receive, send)
            if path in {"/api/screenshot", "/api/screencast"} and method == "GET":
                target = await self._current_tab(client, self._target(scope))
                binding = await self._binding(scope, "interact")
                client = self.client_factory(self.connector, binding)
                image = await (client.screenshot(target) if path.endswith("screenshot") else client.screencast(target))
                return await self._private(Response(image, media_type="image/png"))(scope, receive, send)
            if path == "/api/action" and method == "POST":
                value, target = await self._json(receive), None
                if (set(value) - {"target", "kind", "url", "text", "key", "type", "x", "y", "button", "clickCount", "deltaX", "deltaY", "code", "windowsVirtualKeyCode", "modifiers"}
                        or not isinstance(value.get("target"), str) or not _TARGET.fullmatch(value["target"])):
                    raise DomainError("invalid_request", "Viewer action is invalid")
                target, kind = await self._current_tab(client, value["target"]), value.get("kind")
                # Recheck an owner lease and the exact instance after parsing
                # the body and inventory, immediately before changing Chrome.
                binding = await self._binding(scope, "interact", require_origin=True)
                client = self.client_factory(self.connector, binding)
                if kind == "address" and set(value) == {"target", "kind", "url"}:
                    url = value["url"]
                    try:
                        parsed = urlsplit(url) if isinstance(url, str) and len(url) <= 2048 else None
                    except ValueError:
                        parsed = None
                    if not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                        raise DomainError("invalid_request", "Viewer address is invalid")
                    await client.action(target, "Page.navigate", {"url": url})
                elif kind == "text" and set(value) == {"target", "kind", "text"} and isinstance(value["text"], str) and len(value["text"]) <= _MAX_TEXT:
                    await client.action(target, "Input.insertText", {"text": value["text"]})
                elif kind == "key" and value.get("type") in {"keyDown", "keyUp"} and isinstance(value.get("key"), str) and 1 <= len(value["key"]) <= 64:
                    keys = {"target", "kind", "type", "key", "code", "windowsVirtualKeyCode", "modifiers", "text"}
                    if (set(value) - keys
                            or not isinstance(value.get("code", ""), str) or len(value.get("code", "")) > 64
                            or not isinstance(value.get("text", ""), str) or len(value.get("text", "")) > 64
                            or value["type"] == "keyUp" and value.get("text")
                            or type(value.get("windowsVirtualKeyCode", 0)) is not int or not 0 <= value.get("windowsVirtualKeyCode", 0) <= 255
                            or type(value.get("modifiers", 0)) is not int or not 0 <= value.get("modifiers", 0) <= 15):
                        raise DomainError("invalid_request", "Viewer key input is invalid")
                    await client.action(target, "Input.dispatchKeyEvent", {key: item for key, item in value.items()
                                                                           if key not in {"target", "kind"}})
                elif kind == "mouse" and value.get("type") in {"mouseMoved", "mousePressed", "mouseReleased", "mouseWheel"} and all(type(value.get(k)) in {int, float} and 0 <= value[k] <= 16384 for k in ("x", "y")):
                    mouse_type = value["type"]
                    keys = {"target", "kind", "type", "x", "y"}
                    params = {key: value[key] for key in ("type", "x", "y")}
                    if mouse_type in {"mousePressed", "mouseReleased"}:
                        if value.get("button") not in {"left", "middle", "right"} or type(value.get("clickCount")) is not int or not 1 <= value["clickCount"] <= 3:
                            raise DomainError("invalid_request", "Viewer action is invalid")
                        keys |= {"button", "clickCount"}
                        params.update({"button": value["button"], "clickCount": value["clickCount"]})
                    elif mouse_type == "mouseWheel":
                        if not all(type(value.get(key)) in {int, float} and -16384 <= value[key] <= 16384 for key in ("deltaX", "deltaY")):
                            raise DomainError("invalid_request", "Viewer action is invalid")
                        keys |= {"deltaX", "deltaY"}
                        params.update({"deltaX": value["deltaX"], "deltaY": value["deltaY"]})
                    if set(value) != keys:
                        raise DomainError("invalid_request", "Viewer action is invalid")
                    await client.action(target, "Input.dispatchMouseEvent", params)
                else:
                    raise DomainError("invalid_request", "Viewer action is invalid")
                return await self._private(JSONResponse({"ok": True}))(scope, receive, send)
            raise DomainError("invalid_request", "Viewer route is invalid")
        except DomainError as exc:
            status = 403 if exc.code == "forbidden" else 409 if exc.code == "environment_conflict" else 400
            return await self._private(JSONResponse({"error": exc.code}, status_code=status))(scope, receive, send)
