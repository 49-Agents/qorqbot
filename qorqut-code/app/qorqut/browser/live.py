"""The owner's live view of an agent's browser: the page as pictures out, pointer and keys in.

It talks to the agent's managed Chrome over that Chrome's own loopback DevTools port and never through ``agent-browser``, so
it opens no tab, holds no browser lock and does not pause the agent: the agent keeps working while the owner watches or
types. Chrome only draws, and gives keyboard focus to, the tab that is in front, and that changes all the time: every agent
on the Shared browser brings its own tab to the front with each command (``agent-browser`` activates the tab it works in).
A watched tab sent to the back stopped sending pictures, so the owner's typing reached the page but never showed, and the
page counted as not focused. So a view brings its tab to the front once and holds Chrome's focus emulation on it for as long
as the view is open: the tab is drawn and focused as if it were in front, whichever tab the agents bring forward. The
emulation belongs to the view's own DevTools connection and ends with it; the agents' tabs are not affected.

Watching is use: a tab with an open view is not unloaded for idleness (``idle_tabs``), and a view opened on a tab that
was unloaded loads its page again, as an agent's next command would.

Nothing here decides who may watch; the HTTP route admits the installation owner only.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import uuid

from qorqut_operations.contracts import DomainError

TAB_ID = re.compile(r"[0-9A-F]{32}")
FRAME_INTERVAL = 1 / 20          # At most this many pictures a second reach the dashboard; Chrome offers up to 60.
JPEG_QUALITY = 70
WATCH_MARK_EVERY = 60            # Seconds between renewals of an open view's "watched" mark; far below the idle limit.
MAX_EVENTS, MAX_TEXT, MAX_KEY = 64, 10000, 64
MAX_VIEWS_PER_AGENT, MAX_VIEWS = 4, 16    # Each view holds a DevTools connection, a server thread and a screencast.
MOUSE = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}
BUTTONS = {"none", "left", "middle", "right"}

# Chrome on macOS runs a Command-key shortcut in a text field only when the key event names the editing command; the key
# and its modifier alone do nothing there (plain keys and Option shortcuts work as they are). By (Shift held, key).
MAC_COMMANDS = {
    (False, "a"): "selectAll", (False, "z"): "undo", (True, "z"): "redo", (False, "c"): "copy", (False, "x"): "cut",
    (False, "Backspace"): "deleteToBeginningOfLine",
    (False, "ArrowLeft"): "moveToBeginningOfLine", (True, "ArrowLeft"): "moveToBeginningOfLineAndModifySelection",
    (False, "ArrowRight"): "moveToEndOfLine", (True, "ArrowRight"): "moveToEndOfLineAndModifySelection",
    (False, "ArrowUp"): "moveToBeginningOfDocument", (True, "ArrowUp"): "moveToBeginningOfDocumentAndModifySelection",
    (False, "ArrowDown"): "moveToEndOfDocument", (True, "ArrowDown"): "moveToEndOfDocumentAndModifySelection",
}
ALT, CTRL, META, SHIFT = 1, 2, 4, 8

_views, _slots, _lock = {}, {}, threading.Lock()    # _slots: every view being opened or open, by id, to its agent


class Closed(Exception):
    """The tab or its browser is gone."""


def port(runtime, agent_id):
    """The DevTools port of the browser for an agent that uses it. The agent ID only says whose tabs the owner is watching:
    there is one browser."""
    from . import setup
    _, agents = setup.targets(runtime)
    if agent_id not in [agent["agent_id"] for agent in agents]:
        raise DomainError("not_found", "This agent has no browser")
    return shared_port(runtime)


def shared_port(runtime):
    """The DevTools port of the Shared browser, only while it is the exact Chrome its supervisor started."""
    from . import sign_in
    from .agent_browsers import meta_valid, supervisor_state
    meta, directory = sign_in._meta(runtime), sign_in._state(runtime)
    # The port in the record is only a claim: another program may hold it now, and the owner's clicks and keys would go there.
    if not meta_valid(meta, directory.name, directory) or supervisor_state(directory, meta)["state"] != "ready":
        raise DomainError("browser_not_ready", "The browser isn't running. Wait until it shows Ready in Settings, then reconnect.")
    return meta["managed"]["port"]


def tabs(port_):
    """The browser's open pages, as Chrome lists them."""
    from .sign_in import _get_json
    try:
        targets = _get_json(port_, "/json/list")
    except (OSError, ValueError) as error:
        raise DomainError("browser_not_ready", "This agent's browser isn't running") from error
    return [{"id": item["id"], "title": str(item.get("title") or "")[:300], "url": str(item.get("url") or "")[:2000]}
            for item in targets if isinstance(item, dict) and item.get("type") == "page" and TAB_ID.fullmatch(str(item.get("id")))]


def _choice(event, name, allowed, default=None):
    """The event's ``name`` when it is one of ``allowed``. A JSON list or object is refused here, not hashed (TypeError)."""
    value = event.get(name, default)
    if not isinstance(value, str) or value not in allowed:
        raise DomainError("validation_error", "Browser input is invalid")
    return value


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
    if isinstance(kind, str) and kind in {"mouse", "wheel"}:
        params = {"x": _number(event, "x", 0, 32768), "y": _number(event, "y", 0, 32768), "modifiers": modifiers}
        if kind == "wheel":
            return "Input.dispatchMouseEvent", {**params, "type": "mouseWheel", "deltaX": _number(event, "dx", -10000, 10000),
                                                "deltaY": _number(event, "dy", -10000, 10000)}
        action, button = _choice(event, "action", MOUSE), _choice(event, "button", BUTTONS, "none")
        return "Input.dispatchMouseEvent", {**params, "type": MOUSE[action], "button": button,
                                            "buttons": int(_number(event, "buttons", 0, 31)),
                                            "clickCount": int(_number(event, "clicks", 0, 3))}
    if kind == "key":
        action = _choice(event, "action", {"down", "up"})
        text, code = _text(event, "text", 8), int(_number(event, "keyCode", 0, 255))
        params = {"key": _text(event, "key", MAX_KEY), "code": _text(event, "code", MAX_KEY), "modifiers": modifiers,
                  "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        if action == "up":
            return "Input.dispatchKeyEvent", {**params, "type": "keyUp"}
        # A key that types a character carries it; any other key (arrows, Enter with a modifier, shortcuts) is a raw key.
        if text:
            return "Input.dispatchKeyEvent", {**params, "type": "keyDown", "text": text}
        return "Input.dispatchKeyEvent", {**params, "type": "rawKeyDown", **_mac_command(params["key"], modifiers)}
    if kind == "text":
        text = _text(event, "text", MAX_TEXT)
        if not text:
            raise DomainError("validation_error", "Browser input is invalid")
        return "Input.insertText", {"text": text}
    raise DomainError("validation_error", "Browser input is invalid")


def _mac_command(key, modifiers, platform=None):
    """The editing command a Command-key shortcut needs spelled out, as extra key event parameters. The browser runs on
    this computer, so this computer's system decides."""
    if (platform or sys.platform) != "darwin" or modifiers & (ALT | CTRL | META) != META:
        return {}
    name = MAC_COMMANDS.get((bool(modifiers & SHIFT), key.lower() if len(key) == 1 else key))
    return {"commands": [name]} if name else {}


def _connect(port_, tab_id):
    from websockets.sync.client import connect
    return connect(f"ws://127.0.0.1:{port_}/devtools/page/{tab_id}", proxy=None, max_size=None, open_timeout=5, close_timeout=2)


class View:
    """One open stream of one tab. ``next`` is called by the stream's own thread; ``send`` by any request thread."""

    def __init__(self, agent_id, port_, tab_id, *, marks=None, connect=_connect, clock=time.monotonic):
        self.agent_id, self.tab_id, self.id = agent_id, tab_id, str(uuid.uuid4())
        with _lock:     # The slot is taken before the connection is made, so concurrent requests cannot all pass the check.
            if len(_slots) >= MAX_VIEWS or sum(1 for owner in _slots.values() if owner == agent_id) >= MAX_VIEWS_PER_AGENT:
                raise DomainError("busy", "Too many browser views are open. Close one (another Browser tab or window of the "
                                          "dashboard) and reconnect.")
            _slots[self.id] = agent_id
        self._sent_at, self._held = None, None
        self._clock, self._calls, self._write = clock, 0, threading.Lock()
        self._marks, self._marked_at = marks, None
        try:
            self._socket = connect(port_, tab_id)
        except Exception as error:
            self.close()
            raise DomainError("browser_not_ready", "This tab could not be opened. It may have been closed.") from error
        self._watching()
        try:
            self._call("Page.bringToFront")
            self._call("Emulation.setFocusEmulationEnabled", {"enabled": True})     # stays drawn and focused when sent back
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

    def _watching(self):
        """Renew the mark that keeps this tab from being unloaded as idle (``idle_tabs.note_watch``) when it is due. The
        view goes on without it: at worst the tab is unloaded and the owner sees that page's link to load it again."""
        now = self._clock()
        if self._marks is None or (self._marked_at is not None and now - self._marked_at < WATCH_MARK_EVERY):
            return
        self._marked_at = now
        from . import idle_tabs
        try:
            idle_tabs.note_watch(self._marks, self.tab_id)
        except OSError:
            pass

    def _picture(self, params):
        metadata = params.get("metadata") or {}
        return {"data": params.get("data"), "width": metadata.get("deviceWidth"), "height": metadata.get("deviceHeight")}

    def _release(self):
        """Show the newest picture that arrived too soon after the last one shown."""
        picture, self._held, self._sent_at = self._held, None, self._clock()
        return picture

    def next(self, timeout):
        """The next picture to show as ``{"data", "width", "height"}``, or None when none arrived within ``timeout`` seconds.

        Every picture is acknowledged, since Chrome sends the next one only after that. A picture arriving faster than the
        dashboard's rate is acknowledged and held, the newest one only: when the page then stands still no more come, and
        dropping it for good would leave the owner a stale picture. It is shown when the interval has passed or this call's
        wait ends."""
        self._watching()               # here, not on a picture: a page that stands still sends none while it is watched
        deadline = self._clock() + timeout
        while True:
            now = self._clock()
            wait = deadline - now
            if self._held is not None:
                if now - self._sent_at >= FRAME_INTERVAL:
                    return self._release()
                wait = min(wait, self._sent_at + FRAME_INTERVAL - now)
            if wait <= 0:
                return None if self._held is None else self._release()
            try:
                message = json.loads(self._socket.recv(timeout=wait))
            except TimeoutError:
                return None if self._held is None else self._release()
            except Exception as error:
                raise Closed() from error
            if message.get("method") in {"Inspector.detached", "Inspector.targetCrashed"}:
                raise Closed()
            if message.get("method") != "Page.screencastFrame":
                continue
            params = message.get("params") or {}
            self._call("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})
            self._held = self._picture(params)
            if self._sent_at is None or self._clock() - self._sent_at >= FRAME_INTERVAL:
                return self._release()

    def send(self, events):
        if not isinstance(events, list) or not 0 < len(events) <= MAX_EVENTS:
            raise DomainError("validation_error", "Browser input is invalid")
        commands = [command(event) for event in events]      # all checked before any reaches the page
        self._watching()
        try:
            for method, params in commands:
                self._call(method, params)
        except Closed as error:
            raise DomainError("browser_not_ready", "This tab is closed") from error

    def close(self):
        with _lock:
            _views.pop(self.id, None)
            _slots.pop(self.id, None)
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
    """Open a view of one listed tab. A tab that was unloaded for idleness gets its page back first, without waiting for
    it, so the owner sees it load. When that fails the view shows the unloaded tab's own page, whose link does the same."""
    from . import idle_tabs, sign_in
    port_ = port(runtime, agent_id)
    listed = [tab for tab in tabs(port_) if tab["id"] == tab_id]
    if not listed:
        raise DomainError("not_found", "This tab is closed")
    if idle_tabs.is_parked(listed[0].get("url")):
        try:
            idle_tabs.wake(port_, tab_id, 0)
        except Exception:
            pass
    return View(agent_id, port_, tab_id, marks=sign_in._state(runtime) / idle_tabs.WATCHED), port_
