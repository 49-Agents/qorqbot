"""The owner's live view of an agent's browser: which tabs it lists, what reaches the page, and when a stream ends."""

import json
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.browser import live, setup, sign_in

AGENT = "11111111-1111-4111-8111-111111111111"
TAB = "C3016B049B49B2E2FBC864793BBCF652"


class Socket:
    """Chrome's side of one DevTools connection: scripted messages in, sent commands recorded."""

    def __init__(self, incoming=()):
        self.incoming, self.sent, self.closed = list(incoming), [], False

    def send(self, text):
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if not self.incoming:
            raise TimeoutError()
        message = self.incoming.pop(0)
        if isinstance(message, Exception):
            raise message
        return json.dumps(message)

    def close(self):
        self.closed = True

    def methods(self):
        return [item["method"] for item in self.sent]


def frame(number, width=1280, height=800):
    return {"method": "Page.screencastFrame", "params": {"data": f"jpeg-{number}", "sessionId": number,
                                                         "metadata": {"deviceWidth": width, "deviceHeight": height}}}


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.addCleanup(live._views.clear)

    def open(self, incoming=()):
        self.socket = Socket(incoming)
        view = live.View(AGENT, 20400, TAB, connect=lambda _port, _tab: self.socket, clock=lambda: self.now)
        self.addCleanup(view.close)
        return view

    def test_opening_brings_the_tab_forward_and_starts_the_pictures(self):
        self.open()
        self.assertEqual(self.socket.methods(), ["Page.bringToFront", "Page.startScreencast"])
        self.assertEqual(self.socket.sent[1]["params"]["format"], "jpeg")

    def test_a_tab_that_cannot_be_reached_is_reported_and_leaves_no_view(self):
        def refuse(_port, _tab):
            raise OSError("no such tab")
        with self.assertRaises(DomainError) as refused:
            live.View(AGENT, 20400, TAB, connect=refuse)
        self.assertEqual(refused.exception.code, "browser_not_ready")
        self.assertEqual(live._views, {})

    def test_each_picture_is_acknowledged_and_returned_with_the_page_size(self):
        view = self.open([{"id": 1, "result": {}}, frame(7, 1000, 600)])
        self.assertEqual(view.next(1), {"data": "jpeg-7", "width": 1000, "height": 600})
        self.assertEqual(self.socket.sent[-1], {"id": 3, "method": "Page.screencastFrameAck", "params": {"sessionId": 7}})

    def test_pictures_faster_than_the_dashboard_rate_are_acknowledged_and_dropped(self):
        view = self.open([frame(1), frame(2), frame(3)])
        self.assertEqual(view.next(1)["data"], "jpeg-1")
        self.assertIsNone(view.next(1))                                             # 2 and 3 came at the same instant
        self.assertEqual(self.socket.methods().count("Page.screencastFrameAck"), 3)
        self.now += live.FRAME_INTERVAL
        self.socket.incoming.append(frame(4))
        self.assertEqual(view.next(1)["data"], "jpeg-4")

    def test_no_picture_in_time_is_not_an_error(self):
        self.assertIsNone(self.open().next(0.5))

    def test_a_closed_or_crashed_tab_ends_the_stream(self):
        for ending in (ConnectionError("gone"), {"method": "Inspector.detached", "params": {}}, {"method": "Inspector.targetCrashed"}):
            with self.subTest(ending=ending), self.assertRaises(live.Closed):
                self.open([ending]).next(1)

    def test_input_is_sent_in_order_on_the_same_connection(self):
        view = self.open()
        view.send([{"type": "mouse", "action": "down", "x": 10, "y": 20, "button": "left", "buttons": 1, "clicks": 1},
                   {"type": "mouse", "action": "up", "x": 10, "y": 20, "button": "left", "clicks": 1},
                   {"type": "text", "text": "héllo"}])
        self.assertEqual(self.socket.methods()[2:], ["Input.dispatchMouseEvent", "Input.dispatchMouseEvent", "Input.insertText"])
        self.assertEqual(self.socket.sent[2]["params"], {"x": 10, "y": 20, "modifiers": 0, "type": "mousePressed", "button": "left",
                                                         "buttons": 1, "clickCount": 1})

    def test_one_invalid_event_stops_the_whole_batch_before_the_page_sees_any_of_it(self):
        view = self.open()
        for events in ([{"type": "text", "text": "ok"}, {"type": "mouse", "action": "teleport", "x": 1, "y": 1}], [], "x", [{}] * 65):
            with self.subTest(events=events), self.assertRaises(DomainError) as refused:
                view.send(events)
            self.assertEqual(refused.exception.code, "validation_error")
        self.assertEqual(len(self.socket.sent), 2)

    def test_input_to_a_closed_tab_is_reported(self):
        view = self.open()
        self.socket.closed = True
        with self.assertRaises(DomainError) as refused:
            view.send([{"type": "text", "text": "x"}])
        self.assertEqual(refused.exception.code, "browser_not_ready")

    def test_a_view_is_found_only_by_its_own_agent_and_only_while_open(self):
        view = self.open()
        self.assertIs(live.view(AGENT, view.id), view)
        for agent, view_id in (("another-agent", view.id), (AGENT, "unknown"), (AGENT, None), (AGENT, ["x"])):
            with self.subTest(agent=agent, view_id=view_id), self.assertRaises(DomainError) as refused:
                live.view(agent, view_id)
            self.assertEqual(refused.exception.code, "not_found")
        view.close()
        self.assertTrue(self.socket.closed)
        with self.assertRaises(DomainError):
            live.view(AGENT, view.id)


class CommandTest(unittest.TestCase):
    def test_pointer_wheel_keys_and_text(self):
        cases = [
            ({"type": "mouse", "action": "move", "x": 5.5, "y": 6, "buttons": 1, "modifiers": 8},
             ("Input.dispatchMouseEvent", {"x": 5.5, "y": 6, "modifiers": 8, "type": "mouseMoved", "button": "none", "buttons": 1, "clickCount": 0})),
            ({"type": "wheel", "x": 1, "y": 2, "dx": 0, "dy": -120},
             ("Input.dispatchMouseEvent", {"x": 1, "y": 2, "modifiers": 0, "type": "mouseWheel", "deltaX": 0, "deltaY": -120})),
            ({"type": "key", "action": "down", "key": "a", "code": "KeyA", "text": "a", "keyCode": 65},
             ("Input.dispatchKeyEvent", {"key": "a", "code": "KeyA", "modifiers": 0, "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
                                         "type": "keyDown", "text": "a"})),
            ({"type": "key", "action": "down", "key": "ArrowLeft", "code": "ArrowLeft", "keyCode": 37, "modifiers": 2},
             ("Input.dispatchKeyEvent", {"key": "ArrowLeft", "code": "ArrowLeft", "modifiers": 2, "windowsVirtualKeyCode": 37,
                                         "nativeVirtualKeyCode": 37, "type": "rawKeyDown"})),
            ({"type": "key", "action": "up", "key": "a", "code": "KeyA", "text": "a", "keyCode": 65},
             ("Input.dispatchKeyEvent", {"key": "a", "code": "KeyA", "modifiers": 0, "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
                                         "type": "keyUp"})),
            ({"type": "text", "text": "pasted ünï"}, ("Input.insertText", {"text": "pasted ünï"})),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(live.command(event), expected)

    def test_anything_else_is_refused(self):
        bad = [None, "click", {}, {"type": "script", "text": "x"}, {"type": "mouse", "action": "down", "x": -1, "y": 0},
               {"type": "mouse", "action": "down", "x": 1, "y": 1, "button": "fourth"}, {"type": "mouse", "action": "down", "x": "1", "y": 1},
               {"type": "mouse", "action": "down", "x": True, "y": 1}, {"type": "mouse", "action": "down", "x": float("nan"), "y": 1},
               {"type": "wheel", "x": 1, "y": 1, "dy": 1e9}, {"type": "key", "action": "press", "key": "a"},
               {"type": "key", "action": "down", "key": "a" * 65}, {"type": "key", "action": "down", "key": "a", "keyCode": 999},
               {"type": "key", "action": "down", "key": "a", "modifiers": 16}, {"type": "text", "text": ""},
               {"type": "text", "text": "x" * (live.MAX_TEXT + 1)}, {"type": "text", "text": 5}]
        for event in bad:
            with self.subTest(event=event), self.assertRaises(DomainError) as refused:
                live.command(event)
            self.assertEqual(refused.exception.code, "validation_error")


class BrowserTest(unittest.TestCase):
    def test_only_pages_are_listed(self):
        listed = [{"id": TAB, "type": "page", "title": "Inbox", "url": "https://mail.example/"},
                  {"id": "7E29CAF178D33DB378FD303FE03F6A31", "type": "background_page", "url": "chrome-extension://x/"},
                  {"id": "../../json/close", "type": "page", "url": "https://x.example/"}, "junk",
                  {"id": "B61A88157A879FBF6E4EFFB3355F8F17", "type": "page"}]
        with mock.patch.object(sign_in, "_get_json", return_value=listed):
            self.assertEqual(live.tabs(20400), [{"id": TAB, "title": "Inbox", "url": "https://mail.example/"},
                                                {"id": "B61A88157A879FBF6E4EFFB3355F8F17", "title": "", "url": ""}])

    def test_a_browser_that_does_not_answer_is_not_ready(self):
        with mock.patch.object(sign_in, "_get_json", side_effect=OSError("refused")), self.assertRaises(DomainError) as refused:
            live.tabs(20400)
        self.assertEqual(refused.exception.code, "browser_not_ready")

    def test_the_port_is_the_one_of_the_browser_the_agent_uses_and_only_for_an_agent_with_a_browser(self):
        runtime = SimpleNamespace()
        with mock.patch.object(setup, "targets", return_value=("owner", [{"agent_id": AGENT}])), \
                mock.patch.object(sign_in, "_meta", return_value={"managed": {"port": 20417}}) as meta:
            self.assertEqual(live.port(runtime, AGENT), 20417)
            meta.assert_called_once_with(runtime, AGENT)
            with self.assertRaises(DomainError) as refused:
                live.port(runtime, "22222222-2222-4222-8222-222222222222")
            self.assertEqual(refused.exception.code, "not_found")

    def test_a_view_opens_only_on_a_tab_the_browser_lists(self):
        with mock.patch.object(live, "port", return_value=20400), mock.patch.object(live, "tabs", return_value=[{"id": TAB}]), \
                mock.patch.object(live, "View") as view:
            self.assertEqual(live.open_view(None, AGENT, TAB), (view.return_value, 20400))
            for tab in ("B61A88157A879FBF6E4EFFB3355F8F17", None, "../x"):
                with self.subTest(tab=tab), self.assertRaises(DomainError) as refused:
                    live.open_view(None, AGENT, tab)
                self.assertEqual(refused.exception.code, "not_found")
        view.assert_called_once_with(AGENT, 20400, TAB)

    def test_the_picture_stream_is_never_compressed(self):
        from qorqut.service.web import SelectiveGZip
        base = f"/api/root/agents/{AGENT}/browser-live"
        for path in (base, base + "/stream", base + "/input"):
            self.assertFalse(SelectiveGZip._compressible(path), path)
        self.assertTrue(SelectiveGZip._compressible(f"/api/root/agents/{AGENT}/viewbox"))


class Request:
    def __init__(self, method="GET", query=None, body=None, headers=None, disconnect_after=None):
        self.method, self.query_params = method, query or {}
        self.headers = {"content-type": "application/json", **(headers or {})}
        self._body, self._checks, self._limit = json.dumps(body).encode(), 0, disconnect_after

    async def is_disconnected(self):
        self._checks += 1
        return self._limit is not None and self._checks > self._limit

    async def body(self):
        return self._body

    async def stream(self):
        yield self._body
        yield b""


class RouteTest(unittest.TestCase):
    """The dashboard route: the owner only, and a stream that always closes its view."""

    def setUp(self):
        from contextlib import nullcontext
        from qorqut.transport.http import HTTPAPI
        installation = SimpleNamespace(get=lambda _key: {"owner_principal_id": "owner"})
        read = lambda: nullcontext(SimpleNamespace(repository=lambda _name: installation))
        self.api = SimpleNamespace(runtime=SimpleNamespace(database=SimpleNamespace(read=read)))
        self.route = HTTPAPI.browser_live
        self.owner = SimpleNamespace(principal_kind="human", principal_id="owner")
        self.pictures, self.closed, self.tabs = [], [], [{"id": TAB, "title": "Inbox", "url": "https://mail.example/"}]
        test = self

        class View:
            id = "view-1"

            def next(self, _timeout):
                item = test.pictures.pop(0) if test.pictures else None
                if isinstance(item, Exception):
                    raise item
                return item

            def close(self):
                test.closed.append(True)

        self.view = View()
        for name, value in (("open_view", lambda _runtime, _agent, _tab: (self.view, 20400)), ("tabs", lambda _port: list(self.tabs)),
                            ("port", lambda _runtime, _agent: 20400)):
            patcher = mock.patch.object(live, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, request, tail, context=None):
        import anyio
        async def run():
            response = await self.route(self.api, request, context or self.owner, AGENT, tail, "request-1")
            if hasattr(response, "body_iterator"):
                return [chunk.decode() async for chunk in response.body_iterator]
            return json.loads(response.body)
        return anyio.run(run)

    def names(self, chunks):
        return [chunk.split("\n", 1)[0] for chunk in chunks]

    def test_only_the_owners_dashboard_may_look(self):
        cases = [(Request(), SimpleNamespace(principal_kind="agent", principal_id="owner")),
                 (Request(), SimpleNamespace(principal_kind="human", principal_id="someone-else")),
                 (Request(headers={"authorization": "Bearer x"}), self.owner)]
        for tail in ([], ["stream"], ["input"]):
            for request, context in cases:
                with self.subTest(tail=tail, context=context), self.assertRaises(DomainError) as refused:
                    self.call(request, tail, context)
                self.assertEqual(refused.exception.code, "forbidden")

    def test_the_tabs_are_listed(self):
        self.assertEqual(self.call(Request(), [])["data"], {"agent_id": AGENT, "tabs": self.tabs})

    def test_the_stream_names_its_view_lists_the_tabs_sends_pictures_and_closes_the_view_when_the_dashboard_leaves(self):
        self.pictures = [{"data": "jpeg-1", "width": 1280, "height": 800}, None, {"data": "jpeg-2", "width": 1280, "height": 800}]
        chunks = self.call(Request(query={"tab": TAB}, disconnect_after=3), ["stream"])
        self.assertEqual(self.names(chunks), ["event: open", "event: tabs", "event: frame", "event: frame", "event: closed"])
        self.assertEqual(json.loads(chunks[0].split("data: ", 1)[1]), {"view": "view-1", "tab": TAB})
        self.assertEqual(json.loads(chunks[2].split("data: ", 1)[1]), {"data": "jpeg-1", "width": 1280, "height": 800})
        self.assertEqual(self.closed, [True])

    def test_a_tab_that_closes_ends_the_stream_and_closes_the_view(self):
        for ending in ("the tab leaves the list", live.Closed()):
            self.closed.clear()
            self.tabs = [{"id": TAB, "title": "", "url": ""}]
            if isinstance(ending, Exception):
                self.pictures = [ending]
            else:
                self.tabs = []
            with self.subTest(ending=ending):
                chunks = self.call(Request(query={"tab": TAB}, disconnect_after=50), ["stream"])
                self.assertEqual(self.names(chunks)[-1], "event: closed")
                self.assertEqual(self.closed, [True])

    def test_input_goes_to_the_named_view_of_this_agent(self):
        sent = []
        with mock.patch.object(live, "view", side_effect=lambda agent, view_id: SimpleNamespace(send=lambda events: sent.append((agent, view_id, events)))):
            events = [{"type": "text", "text": "hello"}]
            self.assertEqual(self.call(Request("POST", body={"view": "view-1", "events": events}), ["input"])["status"], "ok")
        self.assertEqual(sent, [(AGENT, "view-1", events)])

    def test_unknown_fields_and_paths_are_refused(self):
        with self.assertRaises(DomainError) as refused:
            self.call(Request("POST", body={"view": "view-1", "events": [], "tab": TAB}), ["input"])
        self.assertEqual(refused.exception.code, "validation_error")
        for request, tail in ((Request("POST"), ["stream"]), (Request(), ["input"]), (Request(), ["other"])):
            with self.subTest(tail=tail), self.assertRaises(DomainError) as refused:
                self.call(request, tail)
            self.assertEqual(refused.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
