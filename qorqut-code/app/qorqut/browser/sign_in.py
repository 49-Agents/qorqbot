"""The owner signs in to a website in a visible Chrome window on the profile of the browser the agents use.

The agent's headless browser pauses, because Chrome allows one process per profile. The window uses a private debugging port the
agent is never told about, so agents cannot watch or drive it while the owner types. Closing the window brings the agent's browser
back with the session in place. Qorqbot records which sites were signed in to, never a password.

For an agent set to the Shared browser the profile is the Shared one: the sign-in pauses every agent that uses it, and the
sites it records are theirs together.

A computer without a screen cannot show the window. There ``open`` puts the site's sign-in page into a tab of the browser the
agent already runs and leaves that browser alone, and the owner signs in through the dashboard's live view of it (``live.py``).
Qorqbot cannot see when the owner is done, so it records the site when the page opens and never claims a sign-in.
"""
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from qorqut_operations.contracts import DomainError, utc_now
from qorqut.atomic_write import atomic_write as _atomic_write
from .agent_browsers import SHARED_ID, STOP_REQUEST_FILE, _read_json, agent_state, uses_shared
from .agent_supervisor import chrome_environment, request_close, stop_child, visible_chrome_command

logger = logging.getLogger(__name__)
SITES_FILE = "sign-ins.json"
HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
LOOPBACK = {"localhost", "127.0.0.1"}
SESSION_COOKIE_LIFETIME = 30 * 24 * 60 * 60
COOKIE_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")
WINDOW_LIMIT = 30 * 60
ENTER_SITE = "Enter a website address, such as slack.com."
_lock = threading.Lock()
_active = {}


def active(agent_id):
    return agent_id in _active


def parse(value):
    """``(site, url)`` for what the owner typed: a bare domain or a full web address."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 300:
        raise DomainError("validation_error", ENTER_SITE)
    url = text if "://" in text else "https://" + text
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise DomainError("validation_error", ENTER_SITE) from None
    if (parts.scheme not in {"http", "https"} or parts.username or parts.password or not HOST.fullmatch(host)
            or ("." not in host and host != "localhost")):
        raise DomainError("validation_error", ENTER_SITE)
    return host.removeprefix("www."), url


def covers(cookie_domain, site):
    """A cookie belongs to a site when it is set on that host, a subdomain of it, or a parent domain of it."""
    domain = cookie_domain.lstrip(".").lower()
    return domain == site or domain.endswith("." + site) or site.endswith("." + domain)


def _state(runtime, agent_id):
    """The state directory of the browser the agent uses: its own, or the Shared browser's."""
    paths = runtime.browser_manager.paths
    return agent_state(paths, SHARED_ID if uses_shared(paths, agent_id) else agent_id)


def _paused(runtime, agent_id):
    """The agents a sign-in for ``agent_id`` pauses: itself, and with the Shared browser every agent that uses it."""
    paths = runtime.browser_manager.paths
    if not uses_shared(paths, agent_id):
        return [agent_id]
    from . import setup
    _, agents = setup.targets(runtime)
    others = []
    for agent in agents:
        try:
            if agent["agent_id"] != agent_id and uses_shared(paths, agent["agent_id"]):
                others.append(agent["agent_id"])
        except DomainError:
            continue
    return [agent_id, *others]


def _sites_path(runtime, agent_id, directory=None):
    return (directory or _state(runtime, agent_id)) / SITES_FILE


def _read_sites(runtime, agent_id, directory=None):
    try:
        data = json.loads(_sites_path(runtime, agent_id, directory).read_text())
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("site"), str)] if isinstance(data, list) else []


def _write_sites(runtime, agent_id, sites, directory=None):
    _atomic_write(_sites_path(runtime, agent_id, directory), json.dumps(sites).encode(), mode=0o600)


def _meta(runtime, agent_id):
    meta = _read_json(_state(runtime, agent_id) / "meta.json")
    if not meta or meta.get("mode") != "managed":
        raise DomainError("browser_not_ready", "This agent's browser isn't set up yet. Wait until it shows Ready, then try again.")
    return meta


def _get_json(port, path):
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.load(response)


def _cdp(port, calls):
    """Run DevTools ``calls`` on one of the browser's pages and return their results in order."""
    from websockets.sync.client import connect
    pages = [item for item in _get_json(port, "/json/list") if item.get("type") == "page"]
    if not pages:
        raise OSError("no page")
    results = []
    with connect(pages[0]["webSocketDebuggerUrl"], proxy=None, max_size=None, open_timeout=5, close_timeout=2) as socket_:
        for number, (method, params) in enumerate(calls, 1):
            socket_.send(json.dumps({"id": number, "method": method, "params": params}))
            while True:
                reply = json.loads(socket_.recv(timeout=10))
                if reply.get("id") == number:
                    break
            if "error" in reply:
                raise OSError(str(reply["error"]))
            results.append(reply.get("result", {}))
    return results


def _cookies(port):
    return _cdp(port, [("Network.getAllCookies", {})])[0].get("cookies", [])


def _browser_call(port, method, params=None):
    """One DevTools call on the browser itself rather than on one of its pages, so it needs no page to be open."""
    from websockets.sync.client import connect
    endpoint = _get_json(port, "/json/version")["webSocketDebuggerUrl"]
    with connect(endpoint, proxy=None, max_size=None, open_timeout=5, close_timeout=2) as socket_:
        socket_.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(socket_.recv(timeout=10))
            if reply.get("id") == 1:
                break
    if "error" in reply:
        raise OSError(str(reply["error"]))
    return reply.get("result", {})


def _window_cookies(port):
    """Every cookie in the sign-in window, read at browser level so it still works once its last tab has closed."""
    return _browser_call(port, "Storage.getCookies")["cookies"]


def _open_tab(port, url):
    """Open ``url`` in a new tab of the browser on ``port`` and return the tab's id. Chrome puts a new tab in front."""
    return _browser_call(port, "Target.createTarget", {"url": url})["targetId"]


def lasting_copies(cookies, now):
    """Login cookies that end with the browser session, made to outlive it, since the agent's browser is a new Chrome process."""
    return [{**{key: cookie[key] for key in COOKIE_FIELDS if key in cookie}, "expires": now + SESSION_COOKIE_LIFETIME}
            for cookie in cookies if cookie.get("session")]


def accounts(runtime):
    """Per agent: the sites the owner signed in to and whether the browser still holds cookies for each."""
    from . import setup
    _, agents = setup.targets(runtime)
    result = []
    for agent in agents:
        agent_id = agent["agent_id"]
        sites, cookies = _read_sites(runtime, agent_id), None
        if sites and not active(agent_id):
            try:
                cookies = _cookies(_meta(runtime, agent_id)["managed"]["port"])
            except Exception:
                cookies = None
        result.append({"agent_id": agent_id, "name": agent["name"], "signing_in": _active.get(agent_id),
                       "sites": [{"site": item["site"], "added_at": item.get("added_at"),
                                  "session_saved": None if cookies is None else any(covers(c.get("domain", ""), item["site"]) for c in cookies)}
                                 for item in sites]})
    return {"agents": result}


def _target(runtime, agent_id):
    """``(owner, agent)`` for an agent that has a browser."""
    from . import setup
    owner, agents = setup.targets(runtime)
    agent = next((item for item in agents if item["agent_id"] == agent_id), None)
    if agent is None:
        raise DomainError("not_found", "That agent doesn't have a browser.")
    return owner, agent


def _require_agent(runtime, agent_id):
    return _target(runtime, agent_id)[0]


def has_screen():
    """Whether a visible Chrome window can open here. Only a Linux computer can lack a screen."""
    return sys.platform != "linux" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _manage(runtime, owner, agent_id, action):
    return runtime.browser_manager.manage(owner, {"agent_id": agent_id, "action": action},
                                          idempotency_key=f"sign-in-{action}-{uuid.uuid4()}", owner_control=True)


def _open_window(runtime, owner, agent_id, url):
    meta = _meta(runtime, agent_id)
    directory = _state(runtime, agent_id)
    _manage(runtime, owner, agent_id, "stop")
    # This pause is not the owner's stop: if Qorqbot quits mid sign-in, automatic setup must still bring the browser back.
    (directory / STOP_REQUEST_FILE).unlink(missing_ok=True)
    port = _free_port()
    process = subprocess.Popen(visible_chrome_command(meta, url, port), cwd=directory, env=chrome_environment(directory),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    time.sleep(1)
    if process.poll() is not None:
        raise DomainError("browser_start_failed", "Chrome couldn't open a sign-in window. Try again, or sign in through the agent's Browser tab.")
    return process, port


def watch_window(process, port, *, limit=WINDOW_LIMIT, sleep=time.sleep, clock=time.monotonic, pages=None, on_poll=None):
    """Return once the owner has closed every window. On Mac, Chrome keeps running after its last window closes."""
    pages = pages or (lambda: [item for item in _get_json(port, "/json/list") if item.get("type") == "page"])
    deadline, seen, empty = clock() + limit, False, 0
    while process.poll() is None and clock() < deadline:
        sleep(1)
        try:
            count = len(pages())
        except (OSError, URLError, ValueError):
            continue
        if on_poll is not None:
            on_poll()
        seen = seen or count > 0
        empty = empty + 1 if seen and count == 0 else 0
        if empty >= 2:
            break


def _quit_cleanly(process, port):
    """Chrome writes cookies to disk in batches; only its own shutdown is sure to save a sign-in that just happened."""
    if not request_close(port, timeout=5, pid=process.pid):
        logger.debug("Chrome closed before it could be asked to quit")
    try:
        process.wait(timeout=10)
    except Exception:
        logger.debug("Chrome did not quit by itself", exc_info=True)


def _finish(runtime, owner, agent_id, site, process, port, paused=None, home=None):
    """``home`` is the state directory of the browser the window was opened on, which is where the owner signed in: the site goes into
    that browser's list, where its cookies are."""
    try:
        latest = []
        def remember_cookies():
            try:
                latest[:] = _window_cookies(port)
            except Exception:
                logger.debug("Could not read the sign-in window's cookies", exc_info=True)
        watch_window(process, port, on_poll=remember_cookies)
        remember_cookies()
        _quit_cleanly(process, port)
        stop_child(process)
        home = home or _state(runtime, agent_id)
        moved = _state(runtime, agent_id) != home
        sites = [item for item in _read_sites(runtime, agent_id, home) if item["site"] != site]
        _write_sites(runtime, agent_id, [*sites, {"site": site, "added_at": utc_now()}], home)
        _manage(runtime, owner, agent_id, "ensure")
        lasting = lasting_copies(latest, time.time())
        if moved:
            logger.warning("Agent %s was switched to another browser during its sign-in to %s; the sign-in stays with the browser "
                           "it was made on and its session cookies were not copied", agent_id, site)
        elif lasting:
            _cdp(_meta(runtime, agent_id)["managed"]["port"], [("Network.setCookies", {"cookies": lasting})])
    except Exception:
        # Automatic setup retries the browser; the Settings card reports anything that stays broken.
        logger.exception("Could not finish browser sign-in for agent %s", agent_id)
    finally:
        with _lock:
            for paused_agent in paused or (agent_id,):
                _active.pop(paused_agent, None)


def start(runtime, agent_id, value):
    site, url = parse(value)
    owner = _require_agent(runtime, agent_id)
    if not has_screen():
        # The tool does not get here: without a screen it opens the page in the live view's browser instead (open_in_live_view).
        # Stopping the agent's browser for a window that cannot open would only leave it stopped.
        raise DomainError("browser_no_screen", "This computer has no screen for a sign-in window. The owner signs in through the agent's "
                                               "Browser tab in the dashboard instead.")
    paused, home = _paused(runtime, agent_id), _state(runtime, agent_id)
    with _lock:
        busy = next((agent for agent in paused if agent in _active), None)
        if busy is not None:
            raise DomainError("conflict", f"Finish signing in to {_active[busy]} first, then close that Chrome window.")
        for paused_agent in paused:
            _active[paused_agent] = site
    try:
        process, port = _open_window(runtime, owner, agent_id, url)
    except Exception:
        with _lock:
            for paused_agent in paused:
                _active.pop(paused_agent, None)
        try:
            _manage(runtime, owner, agent_id, "ensure")
        except Exception:
            logger.exception("Could not restore agent browser %s after a failed sign-in", agent_id)
        raise
    threading.Thread(target=_finish, args=(runtime, owner, agent_id, site, process, port, paused, home), daemon=True,
                     name="browser-sign-in").start()
    return accounts(runtime)


def open_in_live_view(runtime, agent_id, site, url):
    """Where no window can open: put the sign-in page into a new tab of the agent's browser, for the owner to sign in at through
    the dashboard's live view.

    Nothing is paused, stopped or restarted, and ``_active`` is not set, because setup must go on caring for this browser. The tab
    is made over DevTools on the browser itself, as the live view reaches it, and not with an ``agent-browser`` command: that
    would navigate the tab of some session, which for the caller's own browser is the page the agent is working in. A tab made
    here is bound to no session (a session is bound to its tab's id, ``idle_tabs``), so the tab a pinned agent on the Shared
    browser works in stays as it is. The profile is the browser's own, so no cookies have to be copied."""
    from . import live
    _, agent = _target(runtime, agent_id)
    port = live.port(runtime, agent_id)      # refuses unless the supervisor says the exact Chrome it started owns this port
    try:
        _open_tab(port, url)
    except Exception:
        logger.exception("Could not open %s in the browser of agent %s", site, agent_id)
        raise DomainError("browser_not_ready", "Qorqbot couldn't open the sign-in page in this agent's browser. Wait until it shows "
                                               "Ready in Settings, then try again.") from None
    try:
        home = _state(runtime, agent_id)
        sites = [item for item in _read_sites(runtime, agent_id, home) if item["site"] != site]
        _write_sites(runtime, agent_id, [*sites, {"site": site, "added_at": utc_now()}], home)
    except OSError:
        logger.warning("Could not record the sign-in to %s for agent %s", site, agent_id, exc_info=True)
    name = agent["name"]
    if uses_shared(runtime.browser_manager.paths, agent_id):
        where, check = "the Shared browser", "Every agent on the Shared browser shows the same pages, so any of them has the sign-in. "
    else:
        where, check = f"{name}'s browser", ""
    return {"opened": site, "where": "live_view", "agent": name,
            "next": f"No window can open on this computer, so {site}'s sign-in page is open in {where}. Tell the owner to open "
                    f"{name}'s Browser tab in the dashboard (the {site} page, if another is shown) and sign in there themselves. "
                    f"{check}Qorqbot cannot see when they are done: wait until they say so, then open the site in your browser "
                    "and check that the account is signed in. If they cannot use the live view, ask for the password or code "
                    "instead."}


def agent_call(runtime, principal_id, arguments):
    """Ordubashy's ``website_sign_in`` tool. The window opens without a Signoff, so it is limited to the one configured Ordubashy."""
    from qorqut.management.identity import configured_ordubashy
    with runtime.database.read() as uow:
        if configured_ordubashy(uow) != principal_id:
            raise DomainError("forbidden", "Only Ordubashy can open website sign-in.")
    action, agent_id = arguments.get("action"), arguments.get("agent_id") or principal_id
    if action == "status":
        return accounts(runtime)
    if action == "sign_out":
        return sign_out(runtime, agent_id, arguments.get("site"))
    site, url = parse(arguments.get("site"))
    if urlsplit(url).scheme != "https" and site not in LOOPBACK:
        raise DomainError("validation_error", "Use the site's https address, so the owner's password is never sent unencrypted.")
    if not has_screen():
        return open_in_live_view(runtime, agent_id, site, url)
    start(runtime, agent_id, arguments.get("site"))
    return {"opened": site, "next": f"Chrome is open at {site}. Tell the owner to check the address bar, sign in themselves and close the "
                                    "window when finished. Then call status to confirm."}


def sign_out(runtime, agent_id, value):
    site, _ = parse(value)
    _require_agent(runtime, agent_id)
    if active(agent_id):
        raise DomainError("conflict", "Close the sign-in window first, then sign out.")
    try:
        port = _meta(runtime, agent_id)["managed"]["port"]
        matching = [cookie for cookie in _cookies(port) if covers(cookie.get("domain", ""), site)]
        origins = sorted({f"https://{host}" for host in [site, f"www.{site}", *(c["domain"].lstrip(".") for c in matching)]})
        _cdp(port, [*(("Network.deleteCookies", {"name": c["name"], "domain": c["domain"], "path": c["path"]}) for c in matching),
                    *(("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"}) for origin in origins)])
    except DomainError:
        raise
    except Exception:
        raise DomainError("browser_not_ready", "The agent's browser isn't running, so Qorqbot couldn't sign out. "
                          "Wait until it shows Ready, then try again.") from None
    _write_sites(runtime, agent_id, [item for item in _read_sites(runtime, agent_id) if item["site"] != site])
    return accounts(runtime)
