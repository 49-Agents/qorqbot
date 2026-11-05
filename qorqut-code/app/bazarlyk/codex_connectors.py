"""Codex's own connectors: remote MCP servers in the owner's Codex configuration (``~/.codex/config.toml``).

Claude's connectors belong to the owner's claude.ai account and Codex cannot use them. Codex reaches a service only
through its public MCP address, added with ``codex mcp add NAME --url URL`` and signed in with ``codex mcp login NAME``.
Both write the owner's normal Codex configuration, so every Codex agent (and the owner's own Codex) gets the connector.

Qorqbot reuses an entry the owner already has for the same address, and removes only the entries it added itself
(recorded in ``connectors.json`` as ``codex_added``). Signing in runs ``codex mcp login --no-browser`` in the background:
Codex prints the service's own authorization link and waits on a local callback; the owner opens the link and approves
on the service's page. Qorqbot never sees the token, which Codex stores.
"""
import json
import re
import shutil
import subprocess
import threading
import time

from qorqut_operations.contracts import DomainError

LIST_TIMEOUT = 20
CHANGE_TIMEOUT = 30
LOGIN_TIMEOUT = 300
LINK_WAIT = 20
LIST_FRESH_FOR = 5
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
LINK = re.compile(r"(https://\S+)")
# auth_status from `codex mcp list --json`: "o_auth" and "bearer_token" have a credential, "unsupported" needs none.
SIGNED_IN = frozenset({"o_auth", "bearer_token", "unsupported"})


def normalize(url) -> str:
    return url.strip().rstrip("/").lower() if isinstance(url, str) else ""


def parse_list(text: str) -> list:
    """The remote servers in ``codex mcp list --json``: ``[{"name", "url", "auth", "enabled"}]``."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise DomainError("dependency_unavailable", "Codex returned an unreadable server list") from error
    servers = []
    for entry in data if isinstance(data, list) else ():
        transport = entry.get("transport") if isinstance(entry, dict) else None
        if not isinstance(transport, dict) or transport.get("type") != "streamable_http":
            continue
        if not isinstance(entry.get("name"), str) or not isinstance(transport.get("url"), str):
            continue
        servers.append({"name": entry["name"], "url": transport["url"], "auth": entry.get("auth_status"),
                        "enabled": entry.get("enabled") is not False})
    return servers


def state_for(url, servers) -> dict:
    """Codex's state for one service address: connected, needs_sign_in, off (disabled) or not_added."""
    for server in servers:
        if normalize(server["url"]) == normalize(url):
            status = ("off" if not server["enabled"] else "connected" if server["auth"] in SIGNED_IN
                      else "needs_sign_in")
            return {"status": status, "name": server["name"]}
    return {"status": "not_added"}


class Codex:
    """Runs the ``codex mcp`` commands for the owner's dashboard; one sign-in at a time per service."""

    def __init__(self, run=subprocess.run, popen=subprocess.Popen, clock=time.monotonic):
        self._run, self._popen, self._clock = run, popen, clock
        self._lock = threading.Lock()
        self._listed, self._listed_at = None, None
        self._logins = {}

    def _executable(self, environ):
        return shutil.which("codex", path=environ.get("PATH"))

    def _call(self, environ, *args, timeout):
        codex = self._executable(environ)
        if not codex:
            raise DomainError("prerequisite_missing", "Codex is not installed. Install it, then try again.")
        try:
            completed = self._run([codex, "mcp", *args], capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, env=environ, cwd=environ.get("HOME") or "/")
        except (OSError, subprocess.SubprocessError) as error:
            raise DomainError("dependency_unavailable", "Codex did not answer") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise DomainError("dependency_unavailable", "Codex: " + (detail[-1] if detail else "the command failed"))
        return completed.stdout

    def servers(self, environ, fresh=False):
        """``{"available": bool, "servers": [...], "reason"?}``; reading the list does not contact any service."""
        with self._lock:
            if not fresh and self._listed is not None and self._clock() - self._listed_at < LIST_FRESH_FOR:
                return self._listed
        if not self._executable(environ):
            result = {"available": False, "reason": "codex_missing", "servers": []}
        else:
            try:
                result = {"available": True, "servers": parse_list(self._call(environ, "list", "--json",
                                                                               timeout=LIST_TIMEOUT))}
            except DomainError:
                result = {"available": False, "reason": "codex_failed", "servers": []}
        with self._lock:
            self._listed, self._listed_at = result, self._clock()
        return result

    def sign_in(self, connector_id):
        """The running or last sign-in for a connector: ``{"running", "link", "failed"}`` or None."""
        with self._lock:
            job = self._logins.get(connector_id)
            return None if job is None else {"running": job["running"], "link": job["link"], "failed": job["failed"]}

    def add(self, environ, name, url):
        if not NAME.match(name) or not url.startswith("https://"):
            raise DomainError("validation_error", "Not a valid Codex connector")
        self._call(environ, "add", name, "--url", url, timeout=CHANGE_TIMEOUT)
        self._listed = None

    def remove(self, environ, name):
        if not NAME.match(name):
            raise DomainError("validation_error", "Not a valid Codex connector")
        try:
            self._call(environ, "logout", name, timeout=CHANGE_TIMEOUT)
        except DomainError:
            pass  # Not signed in: nothing to log out of; removing the entry still applies.
        self._call(environ, "remove", name, timeout=CHANGE_TIMEOUT)
        self._listed = None

    def start_sign_in(self, environ, connector_id, name):
        """Start ``codex mcp login NAME --no-browser`` and wait for the authorization link it prints."""
        if not NAME.match(name):
            raise DomainError("validation_error", "Not a valid Codex connector")
        with self._lock:
            current = self._logins.get(connector_id)
            if current and current["running"]:
                link = current["link"]
                ready = current["ready"]
            else:
                link, ready = None, None
        if ready is None:
            codex = self._executable(environ)
            if not codex:
                raise DomainError("prerequisite_missing", "Codex is not installed. Install it, then try again.")
            try:
                process = self._popen([codex, "mcp", "login", name, "--no-browser"], stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, env=environ,
                                      cwd=environ.get("HOME") or "/")
            except OSError as error:
                raise DomainError("dependency_unavailable", "Codex sign-in could not start") from error
            job = {"running": True, "link": None, "failed": None, "ready": threading.Event(), "process": process}
            with self._lock:
                self._logins[connector_id] = job
            threading.Thread(target=self._follow, args=(connector_id, job), daemon=True,
                             name="qorqbot-codex-login").start()
            ready = job["ready"]
        ready.wait(LINK_WAIT)
        state = self.sign_in(connector_id)
        if not state or not state["link"]:
            raise DomainError("dependency_unavailable",
                              (state or {}).get("failed") or "Codex did not give a sign-in link. Try again.")
        return state

    def _follow(self, connector_id, job):
        process = job["process"]
        timer = threading.Timer(LOGIN_TIMEOUT, process.kill)
        timer.daemon = True
        timer.start()
        last = ""
        try:
            for line in process.stdout:
                last = line.strip() or last
                found = LINK.search(line)
                if found and job["link"] is None:
                    with self._lock:
                        job["link"] = found.group(1)
                    job["ready"].set()
            code = process.wait()
        finally:
            timer.cancel()
        with self._lock:
            job["running"] = False
            if code != 0:
                job["failed"] = ("Sign-in timed out. Press Connect to try again." if code < 0
                                 else last or "Codex sign-in failed")
            self._listed = None
        job["ready"].set()


CODEX = Codex()
