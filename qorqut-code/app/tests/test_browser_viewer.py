import asyncio
import socket

import httpx

from bazarlyk.browser.socket import BrowserBinding
from bazarlyk.browser.viewer import BrowserViewer, BrowserViewerRoute, _PAGE


BUSINESS = "00000000-0000-4000-8000-000000000001"
BROWSER = "a" * 64
PNG = b"\x89PNG\r\n\x1a\nsynthetic-canary"


class Client:
    def __init__(self):
        self.calls = []
    async def tabs(self):
        return [{"id": "page-1", "title": "<untrusted title>", "url": "https://login.example/"}]
    async def screenshot(self, target):
        self.calls.append(("screenshot", target))
        return PNG
    async def screencast(self, target):
        self.calls.append(("screencast", target))
        return PNG
    async def action(self, target, method, params):
        self.calls.append((target, method, params))


def app(*, current=None, allowed=True, acquired=True, released=True):
    binding = BrowserBinding(BUSINESS, BROWSER, 4, BROWSER + ":42:99", 19400)
    client, events = Client(), []
    viewer = BrowserViewer(BrowserViewerRoute(binding, "https://viewer.example"), connector=object(),
        current_binding=lambda: binding if current is None else current,
        authorize=lambda scope, observed, action: events.append(("authorize", action, observed)) or allowed,
        acquire=lambda scope, observed: events.append(("acquire", observed)) or acquired,
        release=lambda scope, observed: events.append(("release", observed)) or released,
        client_factory=lambda *_: client)
    return viewer, client, events, binding


def request(viewer, method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=viewer)
        async with httpx.AsyncClient(transport=transport, base_url="https://viewer.example") as client:
            return await client.request(method, path, headers={"origin": "https://viewer.example"}, **kwargs)
    return asyncio.run(run())


def test_viewer_uses_only_current_binding_and_explicit_owner_acquire():
    viewer, client, events, binding = app()
    page = request(viewer, "GET", "/")
    acquire = request(viewer, "POST", "/api/acquire")
    release = request(viewer, "POST", "/api/release")
    tabs = request(viewer, "GET", "/api/tabs")
    image = request(viewer, "GET", "/api/screenshot?target=page-1")
    frame = request(viewer, "GET", "/api/screencast?target=page-1")
    assert page.status_code == 200 and acquire.json() == {"acquired": True} and release.json() == {"released": True}
    assert tabs.json()["tabs"][0]["title"] == "<untrusted title>"
    assert image.content == frame.content == PNG and client.calls == [("screenshot", "page-1"), ("screencast", "page-1")]
    assert events[0][0:2] == ("authorize", "view")
    assert events[1][0] == "acquire" and events[2][0] == "release" and all(event[-1] == binding for event in events)
    assert page.headers["cache-control"] == "no-store" and "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_viewer_rejects_forged_origin_stale_instance_and_failed_acquire():
    viewer, client, events, binding = app(current=BrowserBinding(BUSINESS, BROWSER, 4, BROWSER + ":43:99", 19400))
    stale = request(viewer, "GET", "/api/tabs")
    assert stale.status_code == 409 and client.calls == []
    viewer, client, events, _ = app(acquired=False)
    denied = request(viewer, "POST", "/api/acquire")
    assert denied.status_code == 403 and events[-1][0] == "acquire"
    async def wrong_origin():
        transport = httpx.ASGITransport(app=viewer)
        async with httpx.AsyncClient(transport=transport, base_url="https://viewer.example") as http:
            return await http.get("/api/tabs", headers={"origin": "https://forged.example"})
    assert asyncio.run(wrong_origin()).status_code == 403
    async def absent_origin():
        transport = httpx.ASGITransport(app=viewer)
        async with httpx.AsyncClient(transport=transport, base_url="https://viewer.example") as http:
            return await http.post("/api/acquire")
    assert asyncio.run(absent_origin()).status_code == 403


def test_viewer_selects_only_inventory_tabs_and_uses_fixed_cdp_actions():
    viewer, client, _events, _binding = app()
    bad = request(viewer, "GET", "/api/screenshot?target=foreign")
    action = request(viewer, "POST", "/api/action", json={"target": "page-1", "kind": "address", "url": "https://signin.example/"})
    text = request(viewer, "POST", "/api/action", json={"target": "page-1", "kind": "text", "text": "synthetic"})
    mouse = request(viewer, "POST", "/api/action", json={"target": "page-1", "kind": "mouse", "type": "mousePressed", "x": 3, "y": 4, "button": "left", "clickCount": 1})
    invalid = request(viewer, "POST", "/api/action", json={"target": "page-1", "kind": "address", "url": "file:///private"})
    assert bad.status_code == 403 and action.status_code == text.status_code == mouse.status_code == 200
    assert invalid.status_code == 400
    assert client.calls == [("page-1", "Page.navigate", {"url": "https://signin.example/"}),
                            ("page-1", "Input.insertText", {"text": "synthetic"}),
                            ("page-1", "Input.dispatchMouseEvent", {"type": "mousePressed", "x": 3, "y": 4, "button": "left", "clickCount": 1})]


def test_static_viewer_uses_safe_text_rendering_without_raw_cdp_origin():
    assert "textContent" in _PAGE and "innerHTML" not in _PAGE
    assert "127.0.0.1" not in _PAGE and "19400" not in _PAGE
    assert "/api/acquire',{method:'POST'}).then(refresh)" not in _PAGE
    assert "id=release" in _PAGE and "keyUp" in _PAGE and "mouseWheel" in _PAGE
    assert "actions=Promise.resolve" in _PAGE and "Date.now()-lastMove<50" in _PAGE and "tabindex=0" in _PAGE


def test_keyboard_forwards_text_virtual_key_and_modifiers_with_bounded_validation():
    viewer, client, _events, _binding = app()
    value = {"target": "page-1", "kind": "key", "type": "keyDown", "key": "A", "code": "KeyA",
             "text": "A", "windowsVirtualKeyCode": 65, "modifiers": 8}
    assert request(viewer, "POST", "/api/action", json=value).status_code == 200
    assert client.calls[-1] == ("page-1", "Input.dispatchKeyEvent", {key: item for key, item in value.items()
                                                                  if key not in {"target", "kind"}})
    for change in ({"type": "keyUp"}, {"windowsVirtualKeyCode": 256}, {"modifiers": 16}, {"code": []}):
        assert request(viewer, "POST", "/api/action", json={**value, **change}).status_code == 400
    assert len(client.calls) == 1


def test_cdp_inventory_handles_fragmented_http_and_refuses_ambiguous_framing():
    from bazarlyk.browser.viewer import BrowserCDP

    binding = BrowserBinding(BUSINESS, BROWSER, 4, BROWSER + ":42:99", 19400)

    async def run(response):
        async def upstream(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            for part in response:
                writer.write(part)
                await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        address = server.sockets[0].getsockname()
        class Connector:
            def connect(self, observed):
                assert observed == binding
                return socket.create_connection(address)
        try:
            return await BrowserCDP(Connector(), binding)._http("/json/list")
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(run([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n", b"\r\n[]"])) == []
    with __import__("pytest").raises(Exception) as exc:
        asyncio.run(run([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n[]"]))
    assert getattr(exc.value, "code", None) == "environment_unavailable"
