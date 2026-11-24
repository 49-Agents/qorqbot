"""The owner signs in to a website in the dashboard's live view of the Shared browser, the one browser the agents use.

``open`` puts the site's sign-in page into a new tab of the running browser and leaves that browser alone: nothing is paused,
stopped or restarted. The owner signs in through the live view (``live.py``), so it is the same on every computer.
Qorqbot cannot see when the owner is done, so it records the site when the page opens and never claims a sign-in.
Qorqbot records which sites were signed in to, never a password.
"""
import json
import logging
import re
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from qorqut_operations.contracts import DomainError, utc_now
from qorqut.atomic_write import atomic_write as _atomic_write
from .agent_browsers import SHARED_ID, _read_json, agent_state

logger = logging.getLogger(__name__)
SITES_FILE = "sign-ins.json"
HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
LOOPBACK = {"localhost", "127.0.0.1"}
ENTER_SITE = "Enter a website address, such as slack.com."


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


def _open_tab(port, url):
    """Open ``url`` in a new tab of the browser on ``port`` and return the tab's id. Chrome puts a new tab in front."""
    return _browser_call(port, "Target.createTarget", {"url": url})["targetId"]


def accounts(runtime):
    """The sites the owner signed in to and whether the browser still holds cookies for each."""
    sites, cookies = _read_sites(runtime), None
    if sites:
        try:
            cookies = _cookies(_meta(runtime)["managed"]["port"])
        except Exception:
            cookies = None
    return {"sites": [{"site": item["site"], "added_at": item.get("added_at"),
                       "session_saved": None if cookies is None else any(covers(c.get("domain", ""), item["site"]) for c in cookies)}
                      for item in sites]}


def open_in_live_view(runtime, principal_id, site, url):
    """Put the sign-in page into a new tab of the browser, for the owner to sign in at through the dashboard's live view.

    Nothing is paused, stopped or restarted, because setup must go on caring for this browser. The tab is made over
    DevTools on the browser itself, as the live view reaches it, and not with an ``agent-browser`` command: that would
    navigate the tab of some session, which is the page an agent is working in. A tab made here is bound to no session
    (a session is bound to its tab's id, ``idle_tabs``), so every agent's own tab stays as it is. The profile is the
    browser's own, so no cookies have to be copied.

    When the new tab can be named as one of the caller's tabs (the browser the caller uses is the one it opened in), the reply
    carries its ``reference`` with the page's ``title`` and ``url``, and Ordubashy asks the owner with a browser-action
    Signoff that opens that tab, instead of describing where to go."""
    from . import live, setup, tab_reference
    port = live.shared_port(runtime)      # refuses unless the supervisor says the exact Chrome it started owns this port
    try:
        tab = _open_tab(port, url)
    except Exception:
        logger.exception("Could not open %s in the browser", site)
        raise DomainError("browser_not_ready", "Qorqbot couldn't open the sign-in page in the browser. Check it with "
                                               "agent_browser_manage (action status), then try again.") from None
    try:
        _record_site(runtime, site)
    except OSError:
        logger.warning("Could not record the sign-in to %s", site, exc_info=True)
    name = next((agent["name"] for agent in setup.targets(runtime)[1] if agent["agent_id"] == principal_id), "Ordubashy")
    named = tab_reference.reference(principal_id, tab) if isinstance(tab, str) else None
    if named is not None:
        try:
            same = live.port(runtime, principal_id) == port
        except Exception:
            same = False
        if same:
            page = tab_reference.describe(port, tab)
            return {"opened": site, "where": "live_view", "agent": name, "reference": named,
                    "title": page.get("title", ""), "url": page.get("url", url),
                    "next": f"{site}'s sign-in page is open in a new tab of the Shared browser. Ask the owner to sign in there with a browser-action Signoff: signoffs_input_create with "
                            "form kind browser_action, selection_mode none, tab set to this reference's id, an explanation of "
                            "what to do in the page, and source naming the Task that waits. Every agent uses this browser, so "
                            "all of them get the sign-in. When the owner answers Done, open the site in your browser and check "
                            "that the account is signed in; if they could not, read their note, and ask for the password or code "
                            "only if they cannot use the tab."}
    return {"opened": site, "where": "live_view", "agent": name,
            "next": f"{site}'s sign-in page is open in the Shared browser. Tell the owner to open "
                    f"{name}'s Browser tab in the dashboard (the {site} page, if another is shown) and sign in there "
                    "themselves. Every agent uses this browser, so all of them have the sign-in. Qorqbot cannot see when they "
                    "are done: wait until they say so, then open the site in your browser and check that the account is signed "
                    "in. If they cannot use the live view, ask for the password or code instead."}


def agent_call(runtime, principal_id, arguments):
    """Ordubashy's ``website_sign_in`` tool. The page opens without a Signoff, so it is limited to the one configured Ordubashy."""
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
    return open_in_live_view(runtime, principal_id, site, url)


def sign_out(runtime, value):
    site, _ = parse(value)
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
