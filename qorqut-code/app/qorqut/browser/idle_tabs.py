"""A tab of a managed browser that nobody used for a while is unloaded; its next use loads the page again.

Why: Chrome keeps drawing the tab in front and keeps every tab's page in memory. Measured on headless Chrome 153 with an
animated page: the front tab held 33-44% of a processor core for as long as it stayed open, and about 95 MB.

Unloading is a navigation of the same tab to a small page that says so (``parked_page``). The tab and its DevTools id stay,
which is what keeps an agent's session bound to it: closing the tab gives the agent ``tab_gone``, and Chrome's own tab
discarding gives the tab a new id and is refused anyway while ``agent-browser`` is connected. The processor is free at
once; Chrome keeps the page in its back/forward cache for ten minutes and frees its memory then. Loading again is one
step back in the tab's history: within those ten minutes the page returns exactly as it was, later it is fetched again,
with what was typed into form fields restored by Chrome and everything else the page held in memory lost.

Two parts, with nothing stored of our own:

* ``watch`` runs in the browser's supervisor and unloads. A tab's idle time counts from the latest of its last use by an
  agent, the last moment the owner had it open in the live view, and the last change of its address (so a page somebody
  navigates by hand, or loads again from the unloaded page's link, starts afresh).
* ``before_command`` runs before every browser command. It marks the session's tab as used and loads it again if it was
  unloaded, so the agent never has to know.

"Used by an agent" is read from ``agent-browser`` itself: each session keeps ``<session>.target`` with the id of the tab
it is bound to, and ``before_command`` sets that file's modification time. Asking the session instead would cost a process
per look and opens a stray tab when the session has none.

"Watched by the owner" cannot be read from Chrome: it does not say which tab has a screencast, and ``agent-browser``
is attached to every tab anyway. The live view runs in the application and ``watch`` in the supervisor, so the view
leaves a mark both can see: an empty file named by the tab in ``<browser's state directory>/watched``, whose
modification time the view renews while it is open (``note_watch``) and ``watch`` reads (``recent_watches``).
"""

from __future__ import annotations

from html import escape
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote, urlsplit

IDLE_LIMIT = 30 * 60     # Seconds without use after which a tab is unloaded. Long enough to wait for an owner's answer.
SWEEP_EVERY = 60         # Seconds between looks at the browser's tabs.
WAKE_WAIT = 15           # Seconds a command waits at most for its tab's page to load again; then it runs anyway.
SESSIONS = "QORQUT_BROWSER_SESSION_DIRECTORIES"      # See session_directories.
WATCHED = "watched"      # In a browser's state directory: the tabs the owner has, or lately had, open in the live view.
MARK = "<!--qorqut-idle-tab-->"
PARKED = "data:text/html;charset=utf-8," + quote(MARK)


def parked_page(title, url):
    """The address of the small page an unloaded tab shows: what happened, the page it held, and a way back for a person."""
    return PARKED + quote(
        f"<!doctype html><meta charset=utf-8><title>Idle: {escape(str(title)[:200])}</title>"
        "<body style='font:16px sans-serif;margin:3em'><p>This tab was not used for a while, so its page was unloaded. "
        f"It loads again when the tab is next used.</p><p>{escape(str(url)[:500])}</p>"
        "<p><a href='javascript:history.back()'>Load it again now</a></p>")


def is_parked(url):
    return isinstance(url, str) and url.startswith(PARKED)


def session_directories(environ=os.environ):
    """Where ``agent-browser`` 0.38.1 keeps its session files: the user's runtime directory when the process has one, else
    the home directory. The application, an agent's shell and the supervisor do not all have one, so all are looked at.

    The supervisor's own home is its browser's state directory, so from there the application's home is none of these.
    The application therefore hands the supervisor it starts its own list in ``SESSIONS`` (``agent_browsers`` and
    ``native_launcher``); without it every tab looked unused to the sweeper, however recently a command ran in it."""
    places = [Path(f"/run/user/{os.getuid()}/agent-browser"), Path.home() / ".agent-browser"]
    if environ.get("XDG_RUNTIME_DIR"):
        places.insert(0, Path(environ["XDG_RUNTIME_DIR"]) / "agent-browser")
    places += [Path(place) for place in environ.get(SESSIONS, "").split(os.pathsep) if os.path.isabs(place)]
    return list(dict.fromkeys(places))


def _bound_tab(path):
    try:
        tab = json.loads(path.read_bytes()[:4096]).get("targetId")
    except (OSError, ValueError, AttributeError):
        return None
    return tab if isinstance(tab, str) and tab else None


def note_use(session, directories=None):
    """Record that ``session`` uses its tab now, and return that tab's id (None for a session that has no tab yet)."""
    bound = []
    for directory in session_directories() if directories is None else directories:
        path = directory / f"{session}.target"
        tab = _bound_tab(path)
        if tab is not None:
            bound.append((path.stat().st_mtime, path, tab))
    if not bound:
        return None
    _, path, tab = max(bound)          # a session name left behind in another directory is older
    os.utime(path)
    return tab


def recent_uses(now, directories=None):
    """``{tab id: time of its last use}`` for the tabs some session used within the idle limit."""
    used = {}
    for directory in session_directories() if directories is None else directories:
        try:
            entries = [entry for entry in os.scandir(directory) if entry.name.endswith(".target")]
        except OSError:
            continue
        for entry in entries:
            try:
                at = entry.stat().st_mtime
            except OSError:
                continue
            if now - at >= IDLE_LIMIT:
                continue                      # read only the few files that can still keep a tab loaded
            tab = _bound_tab(Path(entry.path))
            if tab is not None:
                used[tab] = max(at, used.get(tab, 0))
    return used


def note_watch(marks, tab):
    """Record in the directory ``marks`` that the owner watches ``tab`` now."""
    marks.mkdir(mode=0o700, exist_ok=True)
    (marks / tab).touch(mode=0o600)


def recent_watches(now, marks):
    """``{tab id: when it was last watched}`` for the tabs the owner watched within the idle limit. An older mark is
    removed here, because the view that made it cannot know when its tab closed."""
    watched = {}
    try:
        entries = list(os.scandir(marks))
    except OSError:
        return watched                    # nobody has watched this browser yet
    for entry in entries:
        try:
            at = entry.stat().st_mtime
            if now - at < IDLE_LIMIT:
                watched[entry.name] = at
            else:
                os.unlink(entry.path)
        except OSError:
            continue
    return watched


def pages(port):
    from . import control
    return [item for item in control.devtools_json(port, "/json/list") if isinstance(item, dict) and item.get("type") == "page"]


def _connect(port, tab):
    from websockets.sync.client import connect
    return connect(f"ws://127.0.0.1:{port}/devtools/page/{tab}", proxy=None, max_size=None, open_timeout=5, close_timeout=2)


def _call(socket, number, method, params=None):
    socket.send(json.dumps({"id": number, "method": method, "params": params or {}}))
    while True:
        reply = json.loads(socket.recv(timeout=10))
        if reply.get("id") == number:
            if "error" in reply:
                raise OSError(str(reply["error"]))
            return reply.get("result") or {}


def park(port, page, *, connect=_connect):
    """Unload one tab: the same tab now shows the small page, and its own page is one step back in its history. False
    when the page stays as it is.

    A page that wants to be asked before it is left (a ``beforeunload`` handler: unsaved work) stays. It is not asked,
    because the answer would not be ours: ``agent-browser`` answers every dialog of a browser it is connected to at
    once, with "leave". Where the question comes up all the same (a frame inside the page asks) and nobody answers, it
    would block the page and every command on it for good, so it is answered "stay"; if somebody else answered "leave"
    first, the tab is unloaded like any other. Chrome reports the dialog only to a connection that enabled page events."""
    socket = connect(port, page["id"])
    try:
        _call(socket, 1, "Page.enable")
        window = _call(socket, 2, "Runtime.evaluate", {"expression": "window"}).get("result", {}).get("objectId")
        listeners = _call(socket, 3, "DOMDebugger.getEventListeners", {"objectId": window}).get("listeners") or []
        if any(listener.get("type") == "beforeunload" for listener in listeners):
            return False
        socket.send(json.dumps({"id": 4, "method": "Page.navigate",
                                "params": {"url": parked_page(page.get("title") or "", page.get("url") or "")}}))
        while True:
            reply = json.loads(socket.recv(timeout=10))
            if reply.get("method") == "Page.javascriptDialogOpening":
                socket.send(json.dumps({"id": 5, "method": "Page.handleJavaScriptDialog", "params": {"accept": False}}))
            elif reply.get("id") == 4:
                return "error" not in reply and not (reply.get("result") or {}).get("errorText")
    finally:
        socket.close()


def wake(port, tab, wait=WAKE_WAIT, *, connect=_connect, clock=time.monotonic, sleep=time.sleep):
    """Load an unloaded tab's page again and wait at most ``wait`` seconds until it has loaded. A command should find
    its page; the owner's live view would rather show it loading and waits 0 seconds."""
    socket = connect(port, tab)
    try:
        history = _call(socket, 1, "Page.getNavigationHistory")
        index, entries = history.get("currentIndex", 0), history.get("entries") or []
        if index < 1 or index >= len(entries) or not is_parked(entries[index].get("url")):
            return
        _call(socket, 2, "Page.navigateToHistoryEntry", {"entryId": entries[index - 1]["id"]})
        deadline, number = clock() + wait, 2
        while clock() < deadline:
            number += 1
            try:
                value = _call(socket, number, "Runtime.evaluate", {"returnByValue": True,
                    "expression": "document.readyState + ' ' + location.href"}).get("result", {}).get("value")
            except OSError:
                value = None              # between two documents there is nothing to ask
            if isinstance(value, str) and value.startswith("complete ") and not is_parked(value[9:]):
                return
            sleep(0.1)
    finally:
        socket.close()


def _port(endpoint):
    """The loopback DevTools port of a browser target, or None for any other kind of target."""
    try:
        parts = urlsplit(endpoint)
        return parts.port if parts.scheme == "http" and parts.hostname == "127.0.0.1" else None
    except ValueError:
        return None


def before_command(endpoint, session, pinned, *, directories=None, clock=time.monotonic):
    """Called before each browser command of ``session``: its tab counts as used, and is loaded again if it was unloaded.

    A session that is not pinned and has no tab (a new session, or one whose tab was closed) takes over a tab that is open,
    and which one is its own choice: every unloaded tab is loaded again. A pinned session opens a new tab instead.
    The command waits ``WAKE_WAIT`` at most for all of them together: each is asked to load, and what is left of the
    wait goes to the next, so a browser full of unloaded tabs does not hold a command for a quarter minute each.
    Nothing here may fail the command: without it the agent works as before, at worst on the unloaded tab's small page."""
    try:
        port = _port(endpoint)
        if port is None:
            return
        tab = note_use(session, directories)
        open_pages = pages(port)
        unknown = not pinned and tab not in [page.get("id") for page in open_pages]
        deadline = clock() + WAKE_WAIT
        for page in open_pages:
            if is_parked(page.get("url")) and (unknown or page.get("id") == tab):
                wake(port, page["id"], max(0, deadline - clock()))
    except Exception:
        return


class Sweeper:
    """Unloads the idle tabs of one browser. It remembers since when each tab has had its present address. ``marks`` is
    where the owner's live view records the tabs it shows (None: this browser has no such place)."""

    def __init__(self, port, marks=None, *, clock=time.time, uses=recent_uses, list_pages=pages, unload=park):
        self._port, self._clock, self._uses, self._pages, self._unload = port, clock, uses, list_pages, unload
        self._marks, self._since = marks, {}

    def sweep(self):
        now, open_pages = self._clock(), self._pages(self._port)
        seen = {}
        for page in open_pages:
            url, known = page.get("url"), self._since.get(page.get("id"))
            seen[page.get("id")] = known if known is not None and known[0] == url else (url, now)
        self._since = seen
        idle = [page for page in open_pages if str(page.get("url")).startswith(("http://", "https://"))
                and now - seen[page.get("id")][1] >= IDLE_LIMIT]
        if not idle:
            return
        used = self._uses(now)                 # read last, so a command that started meanwhile still counts
        if self._marks is not None:
            used = {**used, **recent_watches(now, self._marks)}
        failure = None
        for page in idle:
            if page.get("id") in used:
                continue
            try:
                if self._unload(self._port, page) is not False:
                    continue
            except Exception as error:         # one tab that cannot be unloaded must not keep the others loaded
                failure = failure or error
            self._since[page.get("id")] = (page.get("url"), now)       # it stays; asked again after another limit
        if failure is not None:
            raise failure                      # for the supervisor's log (watch)


def watch(port, stop, marks=None):
    """Unload idle tabs of the Chrome on ``port`` until ``stop`` is set. Run by the supervisor in a thread of its own; it
    never raises, because a browser must keep running without this. Its first failure is written to the supervisor's
    log, and only that one: a fault that stays would otherwise write a line a minute for as long as the browser lives."""
    sweeper, reported = Sweeper(port, marks), False
    while not stop.wait(SWEEP_EVERY):
        try:
            sweeper.sweep()
        except Exception as error:
            if not reported:
                reported = True
                print(f"Browser supervisor: idle tabs could not be looked at ({type(error).__name__}: {error}). "
                      "It keeps trying; further failures are not logged.", file=sys.stderr)
