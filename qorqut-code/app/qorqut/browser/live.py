"""The owner's live view of an agent's browser: the page as pictures out, pointer and keys in.

It talks to the agent's managed Chrome over that Chrome's own loopback DevTools port and never through ``agent-browser``, so
it opens no tab, holds no browser lock and does not pause the agent: the agent keeps working while the owner watches or
types. Chrome only draws the tab that is in front, so opening a view brings its tab to the front.

Nothing here decides who may watch; the HTTP route admits the installation owner only.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid

from qorqut_operations.contracts import DomainError

TAB_ID = re.compile(r"[0-9A-F]{32}")
FRAME_INTERVAL = 1 / 20          # At most this many pictures a second reach the dashboard; Chrome offers up to 60.
JPEG_QUALITY = 70
MAX_EVENTS, MAX_TEXT, MAX_KEY = 64, 10000, 64
MOUSE = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}
BUTTONS = {"none", "left", "middle", "right"}

_views, _lock = {}, threading.Lock()


class Closed(Exception):
    """The tab or its browser is gone."""


def port(runtime, agent_id):
    """The DevTools port of the managed browser the agent uses: its own, or the Shared browser."""
    from . import setup, sign_in
    _, agents = setup.targets(runtime)
    if agent_id not in [agent["agent_id"] for agent in agents]:
        raise DomainError("not_found", "This agent has no browser")
    return sign_in._meta(runtime, agent_id)["managed"]["port"]


def tabs(port_):
    """The browser's open pages, as Chrome lists them."""
    from .sign_in import _get_json
    try:
        targets = _get_json(port_, "/json/list")
    except (OSError, ValueError) as error:
        raise DomainError("browser_not_ready", "This agent's browser isn't running") from error
    return [{"id": item["id"], "title": str(item.get("title") or "")[:300], "url": str(item.get("url") or "")[:2000]}
            for item in targets if isinstance(item, dict) and item.get("type") == "page" and TAB_ID.fullmatch(str(item.get("id")))]


def _number(event, name, low, high):
    value = event.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise DomainError("validation_error", "Browser input is invalid")
    return value


def _text(event, name, limit):
    value = event.get(name, "")
    if not isinstance(value, str) or len(value) > limit:
        raise DomainError("validation_error", "Browser input is invalid")
    return value


def command(event):
    """One input event from the dashboard as a DevTools ``(method, params)``. Coordinates are CSS pixels of the page."""
    if not isinstance(event, dict):
        raise DomainError("validation_error", "Browser input is invalid")
    kind, modifiers = event.get("type"), int(_number(event, "modifiers", 0, 15))
    if kind in {"mouse", "wheel"}:
        params = {"x": _number(event, "x", 0, 32768), "y": _number(event, "y", 0, 32768), "modifiers": modifiers}
        if kind == "wheel":
            return "Input.dispatchMouseEvent", {**params, "type": "mouseWheel", "deltaX": _number(event, "dx", -10000, 10000),
                                                "deltaY": _number(event, "dy", -10000, 10000)}
        if event.get("action") not in MOUSE or event.get("button", "none") not in BUTTONS:
            raise DomainError("validation_error", "Browser input is invalid")
        return "Input.dispatchMouseEvent", {**params, "type": MOUSE[event["action"]], "button": event.get("button", "none"),
                                            "buttons": int(_number(event, "buttons", 0, 31)),
                                            "clickCount": int(_number(event, "clicks", 0, 3))}
    if kind == "key":
        if event.get("action") not in {"down", "up"}:
            raise DomainError("validation_error", "Browser input is invalid")
        text, code = _text(event, "text", 8), int(_number(event, "keyCode", 0, 255))
        params = {"key": _text(event, "key", MAX_KEY), "code": _text(event, "code", MAX_KEY), "modifiers": modifiers,
                  "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        if event["action"] == "up":
            return "Input.dispatchKeyEvent", {**params, "type": "keyUp"}
        # A key that types a character carries it; any other key (arrows, Enter with a modifier, shortcuts) is a raw key.
        return "Input.dispatchKeyEvent", ({**params, "type": "keyDown", "text": text} if text else {**params, "type": "rawKeyDown"})
    if kind == "text":
        text = _text(event, "text", MAX_TEXT)
        if not text:
            raise DomainError("validation_error", "Browser input is invalid")
        return "Input.insertText", {"text": text}
    raise DomainError("validation_error", "Browser input is invalid")


def _connect(port_, tab_id):
    from websockets.sync.client import connect
    return connect(f"ws://127.0.0.1:{port_}/devtools/page/{tab_id}", proxy=None, max_size=None, open_timeout=5, close_timeout=2)


class View:
    """One open stream of one tab. ``next`` is called by the stream's own thread; ``send`` by any request thread."""

    def __init__(self, agent_id, port_, tab_id, *, connect=_connect, clock=time.monotonic):
        self.agent_id, self.tab_id, self.id = agent_id, tab_id, str(uuid.uuid4())
        self._clock, self._calls, self._sent_at, self._write = clock, 0, None, threading.Lock()
        try:
            self._socket = connect(port_, tab_id)
        except Exception as error:
            raise DomainError("browser_not_ready", "This tab could not be opened. It may have been closed.") from error
        try:
            self._call("Page.bringToFront")
            self._call("Page.startScreencast", {"format": "jpeg", "quality": JPEG_QUALITY})
        except Closed as error:
            self.close()
            raise DomainError("browser_not_ready", "This tab could not be opened. It may have been closed.") from error
        with _lock:
            _views[self.id] = self

    def _call(self, method, params=None):
        try:
            with self._write:
                self._calls += 1
                self._socket.send(json.dumps({"id": self._calls, "method": method, "params": params or {}}))
        except Exception as error:
            raise Closed() from error

    def next(self, timeout):
        """The next picture to show as ``{"data", "width", "height"}``, or None when none arrived within ``timeout`` seconds.

        Every picture is acknowledged, since Chrome sends the next one only after that; pictures arriving faster than the
        dashboard's rate are acknowledged and dropped."""
        deadline = self._clock() + timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            try:
                message = json.loads(self._socket.recv(timeout=remaining))
            except TimeoutError:
                return None
            except Exception as error:
                raise Closed() from error
            if message.get("method") in {"Inspector.detached", "Inspector.targetCrashed"}:
                raise Closed()
            if message.get("method") != "Page.screencastFrame":
                continue
            params = message.get("params") or {}
            self._call("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})
            now, metadata = self._clock(), params.get("metadata") or {}
            if self._sent_at is not None and now - self._sent_at < FRAME_INTERVAL:
                continue
            self._sent_at = now
            return {"data": params.get("data"), "width": metadata.get("deviceWidth"), "height": metadata.get("deviceHeight")}

    def send(self, events):
        if not isinstance(events, list) or not 0 < len(events) <= MAX_EVENTS:
            raise DomainError("validation_error", "Browser input is invalid")
        commands = [command(event) for event in events]      # all checked before any reaches the page
        try:
            for method, params in commands:
                self._call(method, params)
        except Closed as error:
            raise DomainError("browser_not_ready", "This tab is closed") from error

    def close(self):
        with _lock:
            _views.pop(self.id, None)
        try:
            self._socket.close()      # Chrome ends the screencast with the connection
        except Exception:
            pass


def view(agent_id, view_id):
    """The open stream ``view_id`` of this agent."""
    with _lock:
        found = _views.get(view_id) if isinstance(view_id, str) else None
    if found is None or found.agent_id != agent_id:
        raise DomainError("not_found", "This browser view is closed. Reopen the Browser tab.")
    return found


def open_view(runtime, agent_id, tab_id):
    port_ = port(runtime, agent_id)
    if tab_id not in [tab["id"] for tab in tabs(port_)]:
        raise DomainError("not_found", "This tab is closed")
    return View(agent_id, port_, tab_id), port_
