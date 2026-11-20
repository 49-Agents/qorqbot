"""The owner signs in to a website in a visible Chrome window on the profile of the Shared browser, the one browser the agents use.

The headless browser pauses for every agent, because Chrome allows one process per profile. The window uses a private debugging
port no agent is ever told about, so agents cannot watch or drive it while the owner types. Closing the window brings the browser
back with the session in place. Qorqbot records which sites were signed in to, never a password.

A computer without a screen cannot show the window. There ``open`` puts the site's sign-in page into a tab of the running browser
and leaves that browser alone, and the owner signs in through the dashboard's live view of it (``live.py``).
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
from qorqut.agent_trust import _atomic_write
from .agent_browsers import SHARED_ID, STOP_REQUEST_FILE, _read_json, agent_state
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
_window = {}     # ``{"site": ...}`` while a sign-in window is open, else empty


def active():
    """Whether the owner's sign-in window is open, which pauses the browser for every agent."""
    return bool(_window)


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


def _state(runtime):
    """The Shared browser's state directory."""
    return agent_state(runtime.browser_manager.paths, SHARED_ID)


def _read_sites(runtime):
    try:
        data = json.loads((_state(runtime) / SITES_FILE).read_text())
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("site"), str)] if isinstance(data, list) else []


def _write_sites(runtime, sites):
    _atomic_write(_state(runtime) / SITES_FILE, json.dumps(sites).encode(), mode=0o600)


def _record_site(runtime, site):
    _write_sites(runtime, [*(item for item in _read_sites(runtime) if item["site"] != site), {"site": site, "added_at": utc_now()}])


def _meta(runtime):
    meta = _read_json(_state(runtime) / "meta.json")
    if not meta or meta.get("mode") != "managed":
        raise DomainError("browser_not_ready", "The browser isn't set up yet. Wait until it shows Ready, then try again.")
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
    """The sites the owner signed in to and whether the browser still holds cookies for each."""
    sites, cookies = _read_sites(runtime), None
    if sites and not active():
        try:
            cookies = _cookies(_meta(runtime)["managed"]["port"])
        except Exception:
            cookies = None
    return {"signing_in": _window.get("site"),
            "sites": [{"site": item["site"], "added_at": item.get("added_at"),
                       "session_saved": None if cookies is None else any(covers(c.get("domain", ""), item["site"]) for c in cookies)}
                      for item in sites]}


def _owner(runtime):
    from . import setup
    return setup.targets(runtime)[0]


def has_screen():
    """Whether a visible Chrome window can open here. Only a Linux computer can lack a screen."""
    return sys.platform != "linux" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _manage(runtime, owner, action):
    return runtime.browser_manager.manage(owner, {"action": action}, idempotency_key=f"sign-in-{action}-{uuid.uuid4()}",
                                          owner_control=True)


def _open_window(runtime, owner, url):
    meta = _meta(runtime)
    directory = _state(runtime)
    _manage(runtime, owner, "stop")
    # This pause is not the owner's stop: if Qorqbot quits mid sign-in, automatic setup must still bring the browser back.
    (directory / STOP_REQUEST_FILE).unlink(missing_ok=True)
    port = _free_port()
    process = subprocess.Popen(visible_chrome_command(meta, url, port), cwd=directory, env=chrome_environment(directory),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    time.sleep(1)
    if process.poll() is not None:
        raise DomainError("browser_start_failed", "Chrome couldn't open a sign-in window. Try again, or sign in through the Browser tab.")
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


def _finish(runtime, owner, site, process, port):
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
        _record_site(runtime, site)
        _manage(runtime, owner, "ensure")
        lasting = lasting_copies(latest, time.time())
        if lasting:
            _cdp(_meta(runtime)["managed"]["port"], [("Network.setCookies", {"cookies": lasting})])
    except Exception:
        # Automatic setup retries the browser; the Settings card reports anything that stays broken.
        logger.exception("Could not finish the browser sign-in to %s", site)
    finally:
        with _lock:
            _window.clear()


def start(runtime, value):
    site, url = parse(value)
    owner = _owner(runtime)
    if not has_screen():
        # The tool does not get here: without a screen it opens the page in the live view's browser instead (open_in_live_view).
        # Stopping the browser for a window that cannot open would only leave it stopped.
        raise DomainError("browser_no_screen", "This computer has no screen for a sign-in window. The owner signs in through the "
                                               "Browser tab in the dashboard instead.")
    with _lock:
        if _window:
            raise DomainError("conflict", f"Finish signing in to {_window['site']} first, then close that Chrome window.")
        _window["site"] = site
    try:
        process, port = _open_window(runtime, owner, url)
    except Exception:
        with _lock:
            _window.clear()
        try:
            _manage(runtime, owner, "ensure")
        except Exception:
            logger.exception("Could not restore the browser after a failed sign-in")
        raise
    threading.Thread(target=_finish, args=(runtime, owner, site, process, port), daemon=True, name="browser-sign-in").start()
    return accounts(runtime)


def open_in_live_view(runtime, principal_id, site, url):
    """Where no window can open: put the sign-in page into a new tab of the browser, for the owner to sign in at through the
    dashboard's live view.

    Nothing is paused, stopped or restarted, and ``_window`` is not set, because setup must go on caring for this browser. The
    tab is made over DevTools on the browser itself, as the live view reaches it, and not with an ``agent-browser`` command:
    that would navigate the tab of some session, which is the page an agent is working in. A tab made here is bound to no
    session (a session is bound to its tab's id, ``idle_tabs``), so every agent's own tab stays as it is. The profile is the
    browser's own, so no cookies have to be copied."""
    from . import live, setup
    port = live.shared_port(runtime)      # refuses unless the supervisor says the exact Chrome it started owns this port
    try:
        _open_tab(port, url)
    except Exception:
        logger.exception("Could not open %s in the browser", site)
        raise DomainError("browser_not_ready", "Qorqbot couldn't open the sign-in page in the browser. Check it with "
                                               "agent_browser_manage (action status), then try again.") from None
    try:
        _record_site(runtime, site)
    except OSError:
        logger.warning("Could not record the sign-in to %s", site, exc_info=True)
    name = next((agent["name"] for agent in setup.targets(runtime)[1] if agent["agent_id"] == principal_id), "Ordubashy")
    return {"opened": site, "where": "live_view", "agent": name,
            "next": f"No window can open on this computer, so {site}'s sign-in page is open in the Shared browser. Tell the owner "
                    f"to open {name}'s Browser tab in the dashboard (the {site} page, if another is shown) and sign in there "
                    "themselves. Every agent uses this browser, so all of them have the sign-in. Qorqbot cannot see when they "
                    "are done: wait until they say so, then open the site in your browser and check that the account is signed "
                    "in. If they cannot use the live view, ask for the password or code instead."}


def agent_call(runtime, principal_id, arguments):
    """Ordubashy's ``website_sign_in`` tool. The window opens without a Signoff, so it is limited to the one configured Ordubashy."""
    from qorqut.management.identity import configured_ordubashy
    with runtime.database.read() as uow:
        if configured_ordubashy(uow) != principal_id:
            raise DomainError("forbidden", "Only Ordubashy can open website sign-in.")
    action = arguments.get("action")
    if action == "status":
        return accounts(runtime)
    if action == "sign_out":
        return sign_out(runtime, arguments.get("site"))
    site, url = parse(arguments.get("site"))
    if urlsplit(url).scheme != "https" and site not in LOOPBACK:
        raise DomainError("validation_error", "Use the site's https address, so the owner's password is never sent unencrypted.")
    if not has_screen():
        return open_in_live_view(runtime, principal_id, site, url)
    start(runtime, arguments.get("site"))
    return {"opened": site, "next": f"Chrome is open at {site}. Tell the owner to check the address bar, sign in themselves and close the "
                                    "window when finished. Then call status to confirm."}


def sign_out(runtime, value):
    site, _ = parse(value)
    if active():
        raise DomainError("conflict", "Close the sign-in window first, then sign out.")
    try:
        port = _meta(runtime)["managed"]["port"]
        matching = [cookie for cookie in _cookies(port) if covers(cookie.get("domain", ""), site)]
        origins = sorted({f"https://{host}" for host in [site, f"www.{site}", *(c["domain"].lstrip(".") for c in matching)]})
        _cdp(port, [*(("Network.deleteCookies", {"name": c["name"], "domain": c["domain"], "path": c["path"]}) for c in matching),
                    *(("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"}) for origin in origins)])
    except DomainError:
        raise
    except Exception:
        raise DomainError("browser_not_ready", "The browser isn't running, so Qorqbot couldn't sign out. "
                          "Wait until it shows Ready, then try again.") from None
    _write_sites(runtime, [item for item in _read_sites(runtime) if item["site"] != site])
    return accounts(runtime)
