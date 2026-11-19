"""Cookie banners are refused in a managed browser, without the agent doing or knowing anything.

The browser's supervisor keeps one DevTools connection to its Chrome for as long as that Chrome lives. Chrome reports on
it every document a tab or frame loads, and each gets ``cookie_banners.js``. A browser command costs nothing extra and no
agent instruction mentions banners. What the script clicks, and what it never clicks, is described at its top.

Why not an extension: branded Chrome 153 ignores ``--load-extension`` (tested, also with the feature switch that used to
restore it); only Chrome for Testing still loads one. This way works on both.

Why not ``Page.addScriptToEvaluateOnNewDocument``, which has Chrome run a script in each new document by itself: on real
sites it missed the first document of a new tab often enough to matter (a tab is reported while its first page is still
on the way, and that page then never got the script). Running the script when Chrome reports the document does not.

The script runs in a JavaScript world of its own (``WORLD``): the page cannot see it, and it cannot collide with the page.
A page whose refusal the script does not recognise keeps its banner, exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

WORLD = "qorqutCookieBanner"
RETRY_AFTER = 5          # Seconds before reconnecting to a Chrome whose connection dropped or that is not answering yet.
POLL = 1                 # Seconds between looks at the stop request while Chrome is silent.
FRAMES = {"page", "iframe"}


def script() -> str:
    return (Path(__file__).with_name("cookie_banners.js")).read_text(encoding="utf-8")


def _connect(port):
    from websockets.sync.client import connect
    from . import control
    return connect(control.devtools_json(port, "/json/version")["webSocketDebuggerUrl"], proxy=None, max_size=None, open_timeout=5,
                   close_timeout=2)


class Installer:
    """One DevTools connection to a browser. Chrome reports every document a tab or frame loads; each gets the script."""

    def __init__(self, socket, source):
        self._socket, self._source, self._calls = socket, source, 0
        self._worlds, self._trees = {}, set()      # calls whose answer is awaited: world call -> its session; frame lists

    def _send(self, method, params, session=None):
        self._calls += 1
        message = {"id": self._calls, "method": method, "params": params}
        if session is not None:
            message["sessionId"] = session
        self._socket.send(json.dumps(message))
        return self._calls

    def _follow(self, kind, session=None):
        """Have Chrome report the targets of this ``kind`` below the browser or below ``session``, those open now and
        those opened later. Pages are followed through their tabs, as Chrome's own tools do."""
        self._send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True,
                                            "filter": [{"type": kind}]}, session)

    def _run_in(self, frame, session):
        if isinstance(frame, str):
            self._worlds[self._send("Page.createIsolatedWorld", {"frameId": frame, "worldName": WORLD}, session)] = session

    def start(self):
        self._follow("tab")

    def handle(self, message):
        method, params, session = message.get("method"), message.get("params") or {}, message.get("sessionId")
        if method == "Target.attachedToTarget":
            below, kind = params.get("sessionId"), (params.get("targetInfo") or {}).get("type")
            if kind == "tab":
                self._follow("page", below)
            elif kind in FRAMES:
                self._send("Page.enable", {}, below)                                  # from now on: Page.frameNavigated
                self._trees.add(self._send("Page.getFrameTree", {}, below))           # the documents already there
                self._follow("iframe", below)     # a frame of another site is a target of its own, with its own documents
        elif method == "Page.frameNavigated":
            self._run_in((params.get("frame") or {}).get("id"), session)
        elif message.get("id") in self._trees:
            self._trees.discard(message["id"])
            pending = [(message.get("result") or {}).get("frameTree")]
            while pending:
                tree = pending.pop() or {}
                self._run_in((tree.get("frame") or {}).get("id"), session)
                pending.extend(tree.get("childFrames") or [])
        elif message.get("id") in self._worlds:
            session, context = self._worlds.pop(message["id"]), (message.get("result") or {}).get("executionContextId")
            if context is not None:               # no context: the frame was gone before Chrome got to it
                self._send("Runtime.evaluate", {"expression": self._source, "contextId": context}, session)


def watch(port, stop, *, connect=_connect):
    """Keep the script installed in the Chrome on ``port`` until ``stop`` is set. Run by the supervisor in a thread of its
    own; it never raises, because a browser without this must keep running. The first connection that fails is written
    to the supervisor's log, and only that one: a fault that stays would otherwise write a line every few seconds."""
    try:
        source = script()
    except OSError:
        print("Browser supervisor: the cookie banner script is missing; banners are left alone.", file=sys.stderr)
        return
    reported = False
    while not stop.is_set():
        try:
            socket = connect(port)
        except Exception as error:
            if not reported:
                reported = True
                print(f"Browser supervisor: cookie banners are left alone while Chrome cannot be reached "
                      f"({type(error).__name__}: {error}). It keeps trying; further failures are not logged.", file=sys.stderr)
            stop.wait(RETRY_AFTER)
            continue
        try:
            installer = Installer(socket, source)
            installer.start()
            while not stop.is_set():
                try:
                    installer.handle(json.loads(socket.recv(timeout=POLL)))
                except TimeoutError:
                    continue
        except Exception:
            stop.wait(RETRY_AFTER)
        finally:
            try:
                socket.close()
            except Exception:
                pass
