"""The ``captcha`` browser command: a captcha on the caller's tab is answered for the owner.

An agent that sees a captcha sends ``captcha`` as a ``browser_turn`` command and names nothing else. Being a command, it is
authorized, bound to a handle, sent to the caller's own browser and held under the browser's use lock exactly like
``click``; only its execution differs. ``agent-browser`` is not involved: the server talks to the tab over Chrome's
loopback DevTools port, as the owner's live view does.

What happens, in this order, each step only if the one before did not settle it:

1. The tab's frames are listed, those of other sites too. A captcha widget is always a frame of its vendor, so widgets
   are recognised by their frame's address: reCAPTCHA v2, hCaptcha, Cloudflare Turnstile. The invisible reCAPTCHA v3 has
   no widget and is recognised by the script the page loads; Cloudflare's own "checking your browser" page by its markup.
2. A widget with a checkbox gets a real mouse click on it. Where a person's browser passes on that click, so does this.
3. Otherwise the paid solver (``captcha_solver``) is asked for an answer token, which is put into the page the way that
   widget's pages take it (``captcha.js``).
4. What is left is the owner's: the result says so in words, and names the tab (``tab_reference``) so that Ordubashy can ask
   the owner with a browser-action Signoff that opens it, instead of describing where to look.

The solver key is read from Global Settings here, on the server, for the one call. It is never part of a result, a log
line or an exception text, and neither is an answer token.

Every attempt is appended to ``<runtime>/browser-captcha.jsonl``: when, which agent, the site's host, the captcha's
type, the method, the outcome and how long it took, and the solver's charge when it reported one.

Cloudflare's own page cannot be answered by a token (its pass is a cookie bound to this browser's address), so it gets
the click and the wait only.
"""

from __future__ import annotations

from collections import namedtuple
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_system import locks, private_files

from . import captcha_solver, tab_reference

logger = logging.getLogger(__name__)

COMMAND = "captcha"
DEADLINE = 150           # Seconds for the whole command. An agent's tool call is given up on after 240.
CLICK_WAIT = 8           # Seconds a clicked checkbox gets to turn green before the solver is asked.
PAGE_WAIT = 25           # Seconds Cloudflare's own page gets to let the browser through.
POLL = 0.5               # Seconds between looks at the page while waiting.
CALL_LIMIT = 10          # Seconds Chrome gets to answer one DevTools call.
SCAN_LIMIT = 40          # Documents of one tab that are read for step 1; a page with more frames than this is all adverts.
MIN_SIDE = 40            # Pixels. A widget drawn smaller than this on a side has no checkbox to click.
CHECKBOX_INSET = 30      # Pixels from a widget's left edge to the middle of its checkbox, when the widget hides where it is.
WORLD = "qorqutCaptcha"
LOG, LOG_LIMIT = "browser-captcha.jsonl", 1024 * 1024

ASK_OWNER = "Ask the owner."
NOT_FOUND = "No captcha was recognised on your tab. If the page still asks for one, ask the owner."
SOLVED_BY_TOKEN = "Continue now: the answer stops working after about two minutes."

Frame = namedtuple("Frame", "id parent url session")
Widget = namedtuple("Widget", "kind sitekey invisible enterprise")
# A captcha to act on. ``frame`` is the widget's frame, or for reCAPTCHA v3 the frame whose document loads it.
Found = namedtuple("Found", "kind frame sitekey invisible enterprise clickable")

_TURNSTILE_KEY = re.compile(r"/([0-3]x[0-9A-Za-z_-]{8,})(?:/|$)")


class CdpError(Exception):
    """Chrome refused a DevTools call, or the tab stopped answering."""


def widget(url):
    """The captcha widget a frame address belongs to, or None.

    By path and not by host: reCAPTCHA is served from several hosts and may be proxied through a site's own, and a
    stand-in page can be recognised the same way. A frame that merely imitates these paths costs one useless click."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    query, fragment = parse_qs(parts.query), parse_qs(parts.fragment)

    def first(values, name):
        return (values.get(name) or [None])[0]

    anchor = re.search(r"/recaptcha/(api2|enterprise)/anchor$", parts.path)
    if anchor:
        return Widget("recaptcha", first(query, "k"), first(query, "size") == "invisible", anchor[1] == "enterprise")
    # hCaptcha describes its frames after the "#": frame=checkbox is the widget, frame=challenge its picture puzzle.
    if parts.path.endswith("/hcaptcha.html") and first(fragment, "frame") == "checkbox":
        return Widget("hcaptcha", first(fragment, "sitekey"), first(fragment, "size") == "invisible", False)
    if "/cdn-cgi/challenge-platform/" in parts.path and "/turnstile/" in parts.path:
        sitekey = _TURNSTILE_KEY.search(parts.path)
        return Widget("turnstile", sitekey[1] if sitekey else None, False, False)
    return None


def _have(reply, *path):
    for name in path:
        reply = reply.get(name) if isinstance(reply, dict) else None
    if reply is None:
        raise CdpError("Chrome's answer lacks " + ".".join(path))
    return reply


class Tab:
    """One DevTools connection to a tab. A frame of another site is a session of its own on the same connection."""

    def __init__(self, socket, source, *, clock=time.monotonic):
        self._socket, self._calls, self._clock = socket, 0, clock
        self._run = "function(name, ...rest) { return (" + source + ")[name].apply(this, rest); }"
        self._roots, self._followed = {}, set()     # session -> the frame whose document it holds; sessions already followed

    def call(self, method, params=None, session=None):
        self._calls += 1
        number, message = self._calls, {"id": self._calls, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        deadline = self._clock() + CALL_LIMIT
        try:
            self._socket.send(json.dumps(message))
            while True:
                left = deadline - self._clock()
                if left <= 0:
                    raise TimeoutError()
                reply = json.loads(self._socket.recv(timeout=left))
                params = reply.get("params") or {}
                if reply.get("method") == "Target.attachedToTarget":
                    self._roots[params.get("sessionId")] = (params.get("targetInfo") or {}).get("targetId")
                elif reply.get("method") == "Target.detachedFromTarget":
                    self._roots.pop(params.get("sessionId"), None)
                elif reply.get("id") == number:
                    break
        except Exception as error:
            raise CdpError(f"{method}: {type(error).__name__}") from None
        if "error" in reply:
            raise CdpError(f"{method}: {(reply['error'] or {}).get('message')}")
        return reply.get("result") or {}

    def frames(self):
        """Every frame of the tab as ``{id: Frame}``, read afresh."""
        found, pending, visited = {}, [None], set()
        while pending:
            session = pending.pop()
            visited.add(session)
            try:
                if session not in self._followed:
                    # Chrome then reports this document's frames of other sites, those there now and later ones.
                    self.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True,
                                                       "filter": [{"type": "iframe"}]}, session)
                    self._followed.add(session)
                trees = [self.call("Page.getFrameTree", {}, session).get("frameTree")]
            except CdpError:
                if session is None:
                    raise
                self._roots.pop(session, None)       # that frame went away meanwhile
                continue
            while trees:
                tree = trees.pop() or {}
                frame = tree.get("frame") or {}
                if isinstance(frame.get("id"), str):
                    found[frame["id"]] = Frame(frame["id"], frame.get("parentId"),
                                               str(frame.get("url") or "") + str(frame.get("urlFragment") or ""), session)
                trees.extend(tree.get("childFrames") or [])
            pending.extend(known for known in self._roots if known not in visited and known not in pending)
        return found

    def _owner(self, frame, frames):
        """The element that holds ``frame``, as Chrome's node number in the session of the document it is in. Chrome
        finds it in a closed shadow tree as well, where the page's own script cannot look."""
        if frame.parent not in frames:
            raise CdpError("the frame has no parent")
        session = frames[frame.parent].session
        return _have(self.call("DOM.getFrameOwner", {"frameId": frame.id}, session), "backendNodeId"), session

    def _origin(self, session, frames):
        """Where a session's document starts in the tab. Chrome measures inside a frame of another site from that frame's
        own corner, and inside a frame of the same site from the tab's."""
        root = frames.get(self._roots.get(session)) if session is not None else None
        box = self.box(root, frames) if root is not None else None
        return (box[0], box[1]) if box else (0, 0)

    def box(self, frame, frames):
        """Where a frame is drawn in the tab as ``(x, y, width, height)``, or None when it is not drawn."""
        node, session = self._owner(frame, frames)
        try:
            quad = _have(self.call("DOM.getBoxModel", {"backendNodeId": node}, session), "model", "content")
        except CdpError:
            return None                  # Chrome has no box for an element that is not drawn
        x, y = self._origin(session, frames)
        return x + quad[0], y + quad[1], quad[4] - quad[0], quad[5] - quad[1]

    def show(self, frame, frames):
        """Scroll a frame into view: a click lands only on what the tab shows."""
        node, session = self._owner(frame, frames)
        self.call("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node}, session)

    def click(self, x, y):
        """A left click as a mouse makes it, at a point of the tab. Chrome passes it on to whichever frame is there."""
        for kind, held in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)):
            self.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "buttons": held, "clickCount": int(kind != "mouseMoved"),
                                                   "button": "none" if kind == "mouseMoved" else "left"})

    def _function(self, where, session, name, arguments):
        reply = self.call("Runtime.callFunctionOn", {**where, "functionDeclaration": self._run, "returnByValue": True,
                                                     "arguments": [{"value": value} for value in (name, *arguments)]}, session)
        if "exceptionDetails" in reply:
            raise CdpError(f"the page script {name} failed")      # never the details: they can quote a token
        return (reply.get("result") or {}).get("value")

    def _world(self, frame):
        return _have(self.call("Page.createIsolatedWorld", {"frameId": frame.id, "worldName": WORLD}, frame.session), "executionContextId")

    def inside(self, frame, name, *arguments):
        """Run a ``captcha.js`` function in a frame's document, in a world of its own that the page cannot see."""
        return self._function({"executionContextId": self._world(frame)}, frame.session, name, arguments)

    def host(self, frame, frames, name, *arguments):
        """Run a ``captcha.js`` function in the page that holds a widget's frame, in the page's own world, with ``this``
        the frame's element."""
        node, session = self._owner(frame, frames)
        element = _have(self.call("DOM.resolveNode", {"backendNodeId": node}, session), "object", "objectId")
        return self._function({"objectId": element}, session, name, arguments)

    def page(self, frame, name, *arguments):
        """Run a ``captcha.js`` function in a frame's document, in the page's own world. Chrome hands out that world only
        through an element of the document, which is first picked up from our own world."""
        root = _have(self.call("Runtime.evaluate", {"expression": "document.documentElement", "contextId": self._world(frame)},
                               frame.session), "result", "objectId")
        node = _have(self.call("DOM.describeNode", {"objectId": root}, frame.session), "node", "backendNodeId")
        element = _have(self.call("DOM.resolveNode", {"backendNodeId": node}, frame.session), "object", "objectId")
        return self._function({"objectId": element}, frame.session, name, arguments)


def _outcome(captcha, **fields):
    return {"captcha": captcha, **{name: value for name, value in fields.items() if value is not None}}


def _failed(code, reason, *, next=ASK_OWNER, **fields):
    return _outcome("failed", code=code, reason=reason, next=next, **fields)


def _top(frames):
    return next((frame for frame in frames.values() if frame.parent is None and frame.session is None), None)


def _host(url):
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _address(url):
    """A page address as the solver gets it: without the query and what follows, which can name a person or a session."""
    return url.split("#", 1)[0].split("?", 1)[0]


def _widgets(tab, frames):
    """The widgets among the frames as ``Found``, those with a checkbox to click first."""
    found = []
    for frame in frames.values():
        described = widget(frame.url)
        if described is None or frame.parent not in frames:
            continue
        try:
            box = tab.box(frame, frames)
        except CdpError:
            box = None
        found.append(Found(described.kind, frame, described.sitekey, described.invisible, described.enterprise,
                           clickable=not described.invisible and box is not None and min(box[2:]) >= MIN_SIDE))
    return sorted(found, key=lambda item: not item.clickable)


def _pick(tab, frames, v3):
    """The captcha to act on: a widget before reCAPTCHA v3, which many sites load on every page and use on few."""
    for found in _widgets(tab, frames):
        if not (found.kind == "recaptcha" and found.invisible and found.sitekey in v3):      # that frame is v3's own
            return found
    for sitekey, (frame, enterprise) in v3.items():
        return Found("recaptcha_v3", frame, sitekey, True, enterprise, False)
    return None


def _current(tab, found):
    """The widget as the page has it now, with the frames it was found in: pages draw a widget again after an error, in
    a new frame. None when the page shows no widget of its kind any more."""
    frames = tab.frames()
    same = [frame for frame in frames.values() if frame.parent in frames and getattr(widget(frame.url), "kind", None) == found.kind]
    kept = next((frame for frame in same if frame.id == found.frame.id), same[0] if same else None)
    return None if kept is None else (found._replace(frame=kept), frames)


def _state(tab, found, frames, *, aim=False):
    """What the page and the widget say: ``answered``, the page's ``sitekey``, ``action`` and ``cdata`` for the widget,
    and with ``aim`` the ``point`` to click, after scrolling there."""
    state = tab.host(found.frame, frames, "state", found.kind) or {}
    try:
        control = tab.inside(found.frame, "control")
    except CdpError:
        control = None
    if control and control.get("checked"):
        state["answered"] = True
    if aim:
        tab.show(found.frame, frames)
        box = tab.box(found.frame, frames)
        if box is not None:
            x, y, width, height = box
            state["point"] = ((x + control["x"], y + control["y"]) if control and control.get("drawn")
                              else (x + min(CHECKBOX_INSET, width / 2), y + height / 2))
    return state


def _cloudflare(tab, clock, sleep):
    """Cloudflare's own page: click its checkbox once it shows one, and wait for the page to give way to the site."""
    clicked, until = False, clock() + PAGE_WAIT
    while True:
        try:
            frames = tab.frames()
            top = _top(frames)
            if top is not None and not (tab.inside(top, "scan") or {}).get("challenge"):
                return _outcome("solved", type="cloudflare", method="click" if clicked else "waited")
            turnstile = next((found for found in _widgets(tab, frames) if found.kind == "turnstile" and found.clickable), None)
            if turnstile is not None and not clicked:
                point = _state(tab, turnstile, frames, aim=True).get("point")
                if point:
                    tab.click(*point)
                    clicked = True
        except CdpError:
            pass                         # between two documents there is nothing to ask
        if clock() >= until:
            return _failed("cloudflare_refused", "Cloudflare did not let this browser through.", type="cloudflare",
                           method="click" if clicked else None)
        sleep(POLL)


def _v3(tab, found, solve):
    """reCAPTCHA v3: learn the action the page asks a score for, then leave a solver's answer for its next request."""
    memo = tab.page(found.frame, "v3", None, None)
    if memo is None:
        return _failed("captcha_not_loaded", "reCAPTCHA has not finished loading on this page.", type=found.kind,
                       next="Wait a moment, then run captcha again.")
    if not memo.get("seen"):
        return _outcome("again", type=found.kind, next="This site checks invisibly when its form is sent. Send the form once "
                                                       "more, then run captcha again.")
    token, cost = solve(found, _address(found.frame.url), found.sitekey, action=memo.get("action") or None)
    tab.page(found.frame, "v3", token, memo.get("action") or "")
    return _outcome("solved", type=found.kind, method="solver", cost=cost,
                    next="Send the form again now: the answer stops working after about two minutes.")


def attempt(tab, *, key, provider=None, clock=time.monotonic, sleep=time.sleep, solver=captcha_solver.solve):
    """Handle the captcha on a tab. Returns the outcome as a mapping: ``captcha`` (solved, not_found, again, failed) with
    ``type``, ``method``, ``code``, ``reason``, ``next``, ``host`` and ``cost`` where they apply. Raises ``CdpError``
    when the tab stops answering."""
    deadline = clock() + DEADLINE
    tab.call("Page.bringToFront")        # some widgets wait until their page is the one shown
    frames = tab.frames()
    top = _top(frames)
    if top is None:
        raise CdpError("the tab has no page")
    scans = {}
    for frame in list(frames.values())[:SCAN_LIMIT]:
        try:
            scans[frame.id] = tab.inside(frame, "scan") or {}
        except CdpError:
            continue                     # a frame that left meanwhile
    site = {"host": _host(top.url)}
    if scans.get(top.id, {}).get("challenge"):
        return {**_cloudflare(tab, clock, sleep), **site}
    v3 = {sitekey: (frames[frame], bool(enterprise)) for frame, scan in scans.items() for sitekey, enterprise in scan.get("v3") or []}
    found = _pick(tab, frames, v3)
    if found is None:
        return _outcome("not_found", **site, next=NOT_FOUND)

    def solve(found, url, sitekey, **details):
        left = deadline - clock()
        if left <= 0:
            raise DomainError("captcha_solver_timeout", captcha_solver.REASONS["captcha_solver_timeout"])
        return solver(captcha_solver.task(found.kind, url, sitekey, invisible=found.invisible, enterprise=found.enterprise, **details),
                      key=key, provider=provider, wait=min(captcha_solver.SOLVE_LIMIT, left), clock=clock, sleep=sleep)

    clicked = False
    try:
        if found.kind == "recaptcha_v3":
            if not key:
                raise DomainError("captcha_solver_no_key", captcha_solver.REASONS["captcha_solver_no_key"])
            return {**_v3(tab, found, solve), **site}
        state = _state(tab, found, frames, aim=found.clickable)
        if state.get("answered"):
            return _outcome("solved", type=found.kind, method="already", **site)
        if state.get("point"):
            tab.click(*state["point"])
            clicked, until = True, min(deadline, clock() + CLICK_WAIT)
            while clock() < until:
                sleep(POLL)
                try:
                    current = _current(tab, found)
                    # No widget any more: the page took the click and moved on.
                    if current is None or _state(tab, *current).get("answered"):
                        return _outcome("solved", type=found.kind, method="click", **site)
                except CdpError:
                    continue             # between two documents there is nothing to ask
        sitekey = found.sitekey or state.get("sitekey")
        if not sitekey:
            raise DomainError("captcha_unreadable", "The captcha's site key could not be read from the page.")
        token, cost = solve(found, _address(frames[found.frame.parent].url), sitekey, action=state.get("action"), cdata=state.get("cdata"))
    except DomainError as error:
        reason = ("Clicking the checkbox did not pass it. " if clicked else "") + error.message
        return _failed(error.code, reason, type=found.kind, method="click" if clicked else None, **site)
    current = _current(tab, found)
    if current is None:
        return _failed("captcha_page_changed", "The page changed while the captcha was being solved.", type=found.kind, method="solver",
                       cost=cost, **site, next="Look at the page, and run captcha again if it still shows one.")
    placed = tab.host(current[0].frame, current[1], "deliver", found.kind, token, sitekey) or {}
    if not any(placed.get(name) for name in ("fields", "called", "patched")):
        return _failed("captcha_not_accepted", "The page offered no place to put the captcha's answer.", type=found.kind,
                       method="solver", cost=cost, **site)
    return _outcome("solved", type=found.kind, method="solver", cost=cost, next=SOLVED_BY_TOKEN, **site)


def script():
    return Path(__file__).with_name("captcha.js").read_text(encoding="utf-8")


def _connect(port, tab):
    from websockets.sync.client import connect
    return connect(f"ws://127.0.0.1:{port}/devtools/page/{tab}", proxy=None, max_size=None, open_timeout=5, close_timeout=2)


def _port(endpoint):
    try:
        parts = urlsplit(endpoint)
        return parts.port if parts.scheme == "http" and parts.hostname == "127.0.0.1" else None
    except ValueError:
        return None


def on_tab(endpoint, session, pinned, *, key, provider=None, connect=_connect, **options):
    """Handle the captcha on the tab an ``agent-browser`` session is bound to, and name that tab (``tab``) in the outcome.
    Which tab that is, is read where the idle-tab sweeper reads it: asking the session would open a stray tab when it has none."""
    from qorqut.bootstrap.browser import BrowserError
    from . import control, idle_tabs
    port = _port(endpoint)
    if port is None:
        return _failed("captcha_browser_unsupported", "The captcha tool works only in a browser on this computer.")
    idle_tabs.before_command(endpoint, session, pinned)        # the tab counts as used, and is loaded again if it was unloaded
    bound = idle_tabs.note_use(session)
    try:
        open_tabs = [page.get("id") for page in control.devtools_json(port, "/json/list") if isinstance(page, dict) and page.get("type") == "page"]
    except (OSError, ValueError):
        raise BrowserError("The browser did not answer. Nothing was done.") from None
    if bound not in open_tabs:
        return _outcome("not_found", next="You have no tab open. Open the page first, then run captcha again.")
    try:
        socket = connect(port, bound)
    except Exception:
        raise BrowserError("The tab could not be reached. Nothing was done.") from None
    try:
        return {**attempt(Tab(socket, script()), key=key, provider=provider, **options), "tab": bound}
    except CdpError as error:
        logger.warning("Captcha: the tab stopped answering (%s)", error)
        return _failed("captcha_tab_lost", "The tab stopped answering while its captcha was handled.",
                       next="Look at the page, and run captcha again if it still shows one.")
    finally:
        try:
            socket.close()
        except Exception:
            pass


def settings(runtime):
    """The solver key and service name from Global Settings, as ``(key, provider)``; each None when not set.

    Read through the Settings files' own reader, the private values over the shared ones, so a key the owner keeps
    private (given to no role) is found and is the one used."""
    from qorqut.service.local import LocalFiles
    local, values = LocalFiles(runtime), {}
    for read in (lambda: {pair["key"]: pair["value"] for pair in local.environment_pairs()},
                 lambda: local.private_environment(owner=True)):
        try:
            values.update(read())
        except (DomainError, OSError):
            continue                     # unreadable Settings are no key: the click still works
    return values.get(captcha_solver.KEY) or None, values.get(captcha_solver.PROVIDER) or None


def record(directory, entry):
    """Append one attempt to the installation's captcha log. The file is replaced by a fresh one past ``LOG_LIMIT``, the
    full one kept beside it once. Nothing here may fail the command."""
    path = Path(directory) / LOG
    try:
        if path.stat().st_size >= LOG_LIMIT:
            os.replace(path, path.with_suffix(".1.jsonl"))
    except OSError:
        pass
    try:
        private_files.append_entry(directory, LOG, (json.dumps(entry, sort_keys=True) + "\n").encode(), mode=0o600)
    except OSError:
        logger.warning("Captcha: the attempt could not be written to %s", path)


def is_command(command):
    """Whether a ``browser_turn`` command is this one. It takes nothing: the tab is the caller's, the rest the server's."""
    if not command or command[0] != COMMAND:
        return False
    if len(command) > 1:
        raise DomainError("validation_error", "captcha takes no arguments; it works on your current tab")
    return True


def owner_reply(outcome, instance, reply, endpoint=None):
    """The reply with the tab's ``reference`` added, and ``next`` saying what to do with it, where the outcome leaves the
    captcha to the owner and the tab is known. Any other reply is returned as it is. The agent named is the one whose browser
    this is (``tab_reference.browser_agent``), which is not always the caller. The tab's current ``title`` and ``url`` come
    beside the reference when its browser (``endpoint``) gives them, so the agent can check which page it is naming."""
    named = None
    if outcome.get("next") in (ASK_OWNER, NOT_FOUND) and outcome.get("tab"):
        named = tab_reference.reference(tab_reference.browser_agent(instance), outcome["tab"])
    if named is None:
        return reply
    port = _port(endpoint) if endpoint else None
    label = tab_reference.describe(port, outcome["tab"]) if port else {}
    return {**reply, "next": f"{reply['next']} {tab_reference.SHOW_FIELD}", "reference": named, **label}


def runner(runtime, agent_id):
    """What runs the command in place of ``bootstrap.browser.run``: the same arguments in, the same
    ``(exit code, stdout, stderr)`` out, so everything around a browser command (locks, leases, replies) stays as it is."""
    def run(workspace, _command, *, target, instance="", capture=True, pinned=False):
        from qorqut.bootstrap import browser
        from qorqut.bootstrap.client import path_has_symlink
        endpoint, _executable = target
        session = browser.session_name(workspace, endpoint, instance)
        directory = workspace / "work/browser" / session
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path_has_symlink(directory):
            raise browser.BrowserError("Browser work directory crosses a symbolic link.")
        with (directory / "command.lock").open("a") as lock:
            try:
                # The session's own command lock: no click or navigation of the same agent runs into the middle of this.
                locks.acquire(lock, wait=False)
            except BlockingIOError:
                raise browser.BrowserError("Another browser command is running in this workspace; wait for it to finish.") from None
            try:
                started = time.monotonic()
                key, provider = settings(runtime)
                outcome = on_tab(endpoint, session, bool(pinned), key=key, provider=provider)
                seconds = round(time.monotonic() - started, 1)
            finally:
                locks.release(lock)
        entry = {"at": utc_now(), "agent": agent_id, "seconds": seconds,
                 **{name: outcome.get(name) for name in ("captcha", "host", "type", "method", "code", "cost") if outcome.get(name) is not None}}
        record(runtime.paths.runtime, entry)
        logger.info("Captcha %s", json.dumps(entry, sort_keys=True))
        reply = {name: outcome[name] for name in ("captcha", "type", "method", "reason", "next") if name in outcome}
        return (0 if outcome["captcha"] == "solved" else 1), json.dumps(owner_reply(outcome, instance, reply, endpoint)) + "\n", ""
    return run
