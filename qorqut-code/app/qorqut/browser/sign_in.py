"""The owner signs in to a website in a visible Chrome window on an agent's own browser profile.

The agent's headless browser pauses, because Chrome allows one process per profile. The window uses a private debugging port the
agent is never told about, so agents cannot watch or drive it while the owner types. Closing the window brings the agent's browser
back with the session in place. Qorqbot records which sites were signed in to, never a password.
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
from qorqut.agent_trust import _atomic_write
from .agent_browsers import STOP_REQUEST_FILE, _read_json, agent_state
from .agent_supervisor import chrome_environment, stop_child, visible_chrome_command

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


def _sites_path(runtime, agent_id):
    return agent_state(runtime.browser_manager.paths, agent_id) / SITES_FILE


def _read_sites(runtime, agent_id):
    try:
        data = json.loads(_sites_path(runtime, agent_id).read_text())
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("site"), str)] if isinstance(data, list) else []


def _write_sites(runtime, agent_id, sites):
    _atomic_write(_sites_path(runtime, agent_id), json.dumps(sites).encode(), mode=0o600)


def _meta(runtime, agent_id):
    meta = _read_json(agent_state(runtime.browser_manager.paths, agent_id) / "meta.json")
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


def _window_cookies(port):
    """Every cookie in the sign-in window, read at browser level so it still works once its last tab has closed."""
    from websockets.sync.client import connect
    endpoint = _get_json(port, "/json/version")["webSocketDebuggerUrl"]
    with connect(endpoint, proxy=None, max_size=None, open_timeout=5, close_timeout=2) as socket_:
        socket_.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            reply = json.loads(socket_.recv(timeout=10))
            if reply.get("id") == 1:
                return reply["result"]["cookies"]


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


def _require_agent(runtime, agent_id):
    from . import setup
    owner, agents = setup.targets(runtime)
    if agent_id not in {agent["agent_id"] for agent in agents}:
        raise DomainError("not_found", "That agent doesn't have a browser.")
    return owner


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _manage(runtime, owner, agent_id, action):
    return runtime.browser_manager.manage(owner, {"agent_id": agent_id, "action": action},
                                          idempotency_key=f"sign-in-{action}-{uuid.uuid4()}", owner_control=True)


def _open_window(runtime, owner, agent_id, url):
    meta = _meta(runtime, agent_id)
    directory = agent_state(runtime.browser_manager.paths, agent_id)
    _manage(runtime, owner, agent_id, "stop")
    # This pause is not the owner's stop: if Qorqbot quits mid sign-in, automatic setup must still bring the browser back.
    (directory / STOP_REQUEST_FILE).unlink(missing_ok=True)
    port = _free_port()
    process = subprocess.Popen(visible_chrome_command(meta, url, port), cwd=directory, env=chrome_environment(directory),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    time.sleep(1)
    if process.poll() is not None:
        raise DomainError("browser_start_failed", "Chrome couldn't open a sign-in window. Make sure Chrome is installed, then try again.")
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
    from websockets.sync.client import connect
    try:
        endpoint = _get_json(port, "/json/version")["webSocketDebuggerUrl"]
        with connect(endpoint, proxy=None, open_timeout=5, close_timeout=2) as socket_:
            socket_.send(json.dumps({"id": 1, "method": "Browser.close"}))
            socket_.recv(timeout=5)
    except Exception:
        logger.debug("Chrome closed before it could be asked to quit", exc_info=True)
    try:
        process.wait(timeout=10)
    except Exception:
        logger.debug("Chrome did not quit by itself", exc_info=True)


def _finish(runtime, owner, agent_id, site, process, port):
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
        sites = [item for item in _read_sites(runtime, agent_id) if item["site"] != site]
        _write_sites(runtime, agent_id, [*sites, {"site": site, "added_at": utc_now()}])
        _manage(runtime, owner, agent_id, "ensure")
        lasting = lasting_copies(latest, time.time())
        if lasting:
            _cdp(_meta(runtime, agent_id)["managed"]["port"], [("Network.setCookies", {"cookies": lasting})])
    except Exception:
        # Automatic setup retries the browser; the Settings card reports anything that stays broken.
        logger.exception("Could not finish browser sign-in for agent %s", agent_id)
    finally:
        with _lock:
            _active.pop(agent_id, None)


def start(runtime, agent_id, value):
    site, url = parse(value)
    owner = _require_agent(runtime, agent_id)
    if sys.platform == "linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise DomainError("browser_no_screen", "This computer has no screen for a sign-in window. Open the site's own sign-in page in your "
                                               "own browser instead, ask the owner for the password or code and type it yourself. For a worker's "
                                               "own browser, acquire it with browser_turn (browser_id from action list) and "
                                               "sign in there the same way.")
    with _lock:
        if agent_id in _active:
            raise DomainError("conflict", f"Finish signing in to {_active[agent_id]} first, then close that Chrome window.")
        _active[agent_id] = site
    try:
        process, port = _open_window(runtime, owner, agent_id, url)
    except Exception:
        with _lock:
            _active.pop(agent_id, None)
        try:
            _manage(runtime, owner, agent_id, "ensure")
        except Exception:
            logger.exception("Could not restore agent browser %s after a failed sign-in", agent_id)
        raise
    threading.Thread(target=_finish, args=(runtime, owner, agent_id, site, process, port), daemon=True,
                     name="browser-sign-in").start()
    return accounts(runtime)


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
