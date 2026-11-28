"""Signing the owner in to Claude Code and Codex from the dashboard, and reading whether each is signed in.

Nothing here opens a terminal. Claude signs in through ``claude auth login --claudeai`` (the CLI opens the owner's
browser, receives the result on its own local listener, stores the credential and exits). Codex signs in through its
app-server: ``account/login/start`` returns a page that is opened in the browser, and the server later reports the login
as completed. One sign-in is pending per provider; a second start replaces the first, a sign-in that has not finished
after ten minutes fails, and the processes are stopped on cancel and when the service exits (``stop_all``).

Switching and servers: ``claude auth login`` and Codex's login run the same whether or not the product is already signed in, and
the stored sign-in stays in use until the new one is written, so a switch is the same sign-in started on a signed-in product
(nothing is changed by cancelling, failing or timing out). On a machine without a browser the owner opens the page elsewhere:
Claude's CLI reads the code that page shows from its standard input (``submit_code``), and Codex offers a device-code login
(``account/login/start`` with ``chatgptDeviceCode``: a page and a short code the owner types there). The page and the code are shown
to the owner in the view and are never logged. When a sign-in completes, the handler set with ``set_completion_handler`` is told
(Chat restarts the agents of that product).

Status:

* Claude is read from the credential the CLI stored (macOS Keychain item ``Claude Code-credentials``, else
  ``<config dir>/.credentials.json``), never by running the CLI while a usable sign-in is stored. ``claude auth status``
  starts a token refresh and exits after two seconds without saving the rotated tokens when the refresh is slower; the next
  Claude process then gets ``invalid_grant`` and the owner is signed out (anthropics/claude-code#95822, #105645, as
  reported by OpenBot; not verified here). So variables that sign the CLI in from the environment (API key, token, another
  backend) are answered from the environment and never reach the CLI, and the CLI is asked only when the store holds no
  usable sign-in or cannot be read. That call is single-flight (one at a time, whatever the number of readers), never made
  while a sign-in is pending, and its answer, a failure or a timeout included, is kept so a polling dashboard cannot turn it
  into a poll.
* Codex runs ``codex login status``: one short read-only process that needs no handshake, where ``account/read`` would
  start an app-server and negotiate ``initialize`` on every poll. ``account/read`` is used after a sign-in completes,
  to confirm what the login produced. Only the login method is taken from its output; the text is never passed on.

Where the CLI binary is comes from ``qorqut.agent_cli_install`` (imported lazily; until it exists the CLI on PATH is used).
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlsplit
import webbrowser

from qorqut import account_state
from qorqut_system import platform as _platform
from qorqut_system import processes as _processes

PROVIDERS = ("claude", "codex")
SIGN_IN_TIMEOUT = 600            # seconds a pending sign-in may wait for the owner
CLI_TIMEOUT = 15
KEYCHAIN_TIMEOUT = 3
KEYCHAIN_ITEM_NOT_FOUND = 44     # `security` exit status when the Keychain has no such item
CLI_STATUS_TTL = 60              # seconds a `claude auth status` answer is reused
CLI_STATUS_FAILURE_TTL = 300     # ... and a failed or timed-out run (the one that can sign the owner out), which is not retried sooner
CODEX_REQUEST_TIMEOUT = 30
CODEX_START_TIMEOUT = 120        # the first launch of a large binary can take long to answer `initialize` (macOS checks it)
STOP_GRACE = 3                   # seconds one process gets to exit after SIGTERM
STOP_ALL_BUDGET = 4              # seconds ``stop_all`` waits in all for the polite stop, then the rest are killed
OUTPUT_LINE_LIMIT = 4096         # bytes of one line of the sign-in CLI's output that are looked at
OUTPUT_LIMIT = 65536             # bytes of the sign-in CLI's output that are looked at in all
OUTPUT_POLL = 0.2
REASON_LIMIT = 200
CODE_LIMIT = 512                 # characters of the code the owner pastes (Claude's is about a hundred)
CODEX_METHODS = ("browser", "device_code")
DEVICE_CODE_LIMIT = 64

# Variables that sign the CLI in from the environment. ``auth status`` would report them; the stored sign-in alone would
# give a wrong answer, and asking the CLI could spend a stored refresh token for nothing, so they are answered here.
API_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
TOKEN_VARIABLES = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
                   "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE")
# Variables that send the CLI to another API backend: it runs on that provider's credentials, not on a Claude sign-in.
BACKEND_VARIABLES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_GATEWAY",
                     "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_MANTLE")
# Variables that move or rename the credential store or point the sign-in at another server: the default store says nothing
# about them. Their state is not known here (shown as not signed in; Connect runs the CLI with the same variables).
STORE_VARIABLES = ("CLAUDE_CODE_HOST_CREDS_FILE", "CLAUDE_CODE_HOST_AUTH_ENV_VAR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
                   "CLAUDE_CODE_FORCE_WINDOWS_CREDMAN", "USE_STAGING_OAUTH", "USE_LOCAL_OAUTH", "CLAUDE_CODE_CUSTOM_OAUTH_URL")
AUTH_OVERRIDE_VARIABLES = API_KEY_VARIABLES + TOKEN_VARIABLES + BACKEND_VARIABLES + STORE_VARIABLES

# Where a sign-in page may be sent to the browser from the CLI's output.
SIGN_IN_HOSTS = ("claude.ai", "claude.com", "anthropic.com")
_URL = re.compile(r"https://[^\s\"'<>]+")
# "could not open the browser" and the like; "if the browser didn't open" (the CLI's usual hint) does not match.
_BROWSER_FAILED = re.compile(r"(?:could\s*n[o']t|can\s*n[o']t|can't|unable to|failed to|did\s*n[o']t|didn't)\s+"
                             r"(?:automatically\s+)?(?:open|launch|start)\s+(?:a |an |the |your |any |default )?(?:web\s*)?browser"
                             r"|no\s+(?:web\s*)?browser", re.IGNORECASE)

_lock = threading.RLock()
_pending: dict = {}              # provider -> _Pending
_failed: dict = {}               # provider -> plain reason of the last failed sign-in
_cli_status_cache: dict = {}     # (path, HOME, CLAUDE_CONFIG_DIR) -> (monotonic time it expires, result)
_cli_status_flight = threading.Lock()   # held while a ``claude auth status`` runs: never two at once
_completion_handler = None       # called as handler(provider, switch) in its own thread when a sign-in completed


class SignInError(Exception):
    """A sign-in request that cannot be done now; ``message`` is a plain sentence for the owner."""

    def __init__(self, message, code="validation_error"):
        super().__init__(message)
        self.message, self.code = message, code


def set_completion_handler(handler):
    """``handler(provider, switch)`` runs in a new thread after a sign-in completed (``switch`` is ``account_state.switch``'s entry);
    None removes it. Chat uses it to restart the sessions of that product."""
    global _completion_handler
    _completion_handler = handler


# ---------------------------------------------------------------- the CLI binary (agent_cli_install's contract)

def _install():
    try:
        from qorqut import agent_cli_install
    except ModuleNotFoundError as error:
        if error.name != "qorqut.agent_cli_install":
            raise
        return None
    return agent_cli_install


def resolve_cli(provider, environ=None):
    """``{"path", "source", "version"}`` of the CLI the agents run, or None. Falls back to the CLI on PATH while the
    install module is not part of the build."""
    install = _install()
    if install is not None:
        return install.resolve(provider, environ)
    path = shutil.which(provider, path=(os.environ if environ is None else environ).get("PATH"))
    return {"path": path, "source": "system", "version": None} if path else None


def install_status(provider, found=None, environ=None):
    """``{"phase", "progress", "message", "version"}``; without the install module, ready exactly when a CLI was found. ``environ`` is
    the owner's environment the CLI was looked up in (``resolve_cli``): the same one decides why a copy was passed over (too old)."""
    install = _install()
    if install is not None:
        return install.status(provider, environ=environ)
    return {"phase": "ready" if found else "not_downloaded", "progress": None, "message": None,
            "version": (found or {}).get("version")}


def launch_environment(provider):
    """Variables the CLI must get on top of the owner's environment (for example its updater turned off)."""
    install = _install()
    return dict(install.launch_environment(provider) or {}) if install is not None else {}


def launch_arguments(provider):
    """Extra command-line arguments of the managed copy (Codex's update check turned off), when the install module has any."""
    install = _install()
    return list(getattr(install, "launch_arguments", lambda provider: [])(provider) or []) if install is not None else []


def cli_environment(provider, environ=None):
    """The environment the owner's agents get, plus what the managed copy needs."""
    from qorqut.connectors import _probe_environment
    return {**_probe_environment(environ), **launch_environment(provider)}


def _run(run, command, env):
    try:
        return run(command, capture_output=True, text=True, timeout=CLI_TIMEOUT, stdin=subprocess.DEVNULL, env=env,
                   cwd=env.get("HOME") or "/")
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- Claude status

def _read_text(path):
    """The file's text, ``None`` when it does not exist, ``False`` when it exists but cannot be read."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError):
        return False


def _keychain_account(environ):
    name = environ.get("USER") or ""
    if not name:
        try:
            import getpass
            name = getpass.getuser()
        except Exception:
            name = ""
    return name if name and all(c.isalnum() or c in "._-" for c in name) else "claude-code-user"


def _stored_credential_text(environ, config_dir, custom, system, run):
    """The credential text the CLI stored, ``None`` when no store holds one, ``False`` when a store cannot be read.
    On macOS the Keychain wins when it has the item; the file is the CLI's fallback there and its only store elsewhere."""
    if system == "mac":
        suffix = "-" + hashlib.sha256(config_dir.encode()).hexdigest()[:8] if custom else ""
        # The CLI writes the item with /usr/bin/security, so reading it with the same tool asks the owner nothing.
        done = _run(run, ["/usr/bin/security", "find-generic-password", "-a", _keychain_account(environ), "-w",
                          "-s", "Claude Code-credentials" + suffix], environ)
        if done is None:
            return False                      # timeout or no tool: a locked Keychain does not say the item is absent
        if done.returncode == 0:
            text = (done.stdout or "").strip()
            try:                              # `security -w` prints a value that is not plain text as hex
                if text and len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text):
                    text = bytes.fromhex(text).decode("utf-8")
            except ValueError:
                return False
            return text
        if done.returncode != KEYCHAIN_ITEM_NOT_FOUND:
            return False
    return _read_text(Path(config_dir) / ".credentials.json")


def _stored_account(environ, config_dir, home):
    legacy = _read_text(Path(config_dir) / ".config.json")
    text = legacy if legacy else _read_text(Path(environ.get("CLAUDE_CONFIG_DIR") or home) / ".claude.json")
    try:
        account = json.loads(text).get("oauthAccount") if isinstance(text, str) else None
    except (ValueError, AttributeError):
        return {}
    return account if isinstance(account, dict) else {}


def _text(value):
    return value if isinstance(value, str) and value else None


def _stored_claude_sign_in(environ, system, run, now):
    """``{"signed_in": True, "account": ...}`` when the store holds a refresh token that has not expired, else None
    (no store, unreadable store, or no usable token: the caller decides whether to ask the CLI)."""
    home = environ.get("HOME") or str(Path.home())
    custom = bool(environ.get("CLAUDE_CONFIG_DIR"))
    config_dir = unicodedata.normalize("NFC", environ.get("CLAUDE_CONFIG_DIR") or str(Path(home) / ".claude"))
    text = _stored_credential_text(environ, config_dir, custom, system, run)
    if not isinstance(text, str):
        return None
    try:
        oauth = json.loads(text).get("claudeAiOauth")
    except (ValueError, AttributeError):
        return None
    if not isinstance(oauth, dict) or not _text(oauth.get("refreshToken")):
        return None
    expires = oauth.get("refreshTokenExpiresAt")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires <= now * 1000:
        return None
    stored = _stored_account(environ, config_dir, home)
    return {"signed_in": True, "account": {"signed_in": True, "method": "claude.ai", "email": _text(stored.get("emailAddress")),
                                           "plan": _text(oauth.get("subscriptionType"))}}


SIGNED_OUT = {"signed_in": False, "account": None}


def _cli_claude_status(path, env, run):
    """What ``claude auth status --json`` says. This is the call that can sign the owner out, so it runs at most once at a
    time, and what it answered (a failure or a timeout too) is kept: ``CLI_STATUS_TTL`` seconds, ``CLI_STATUS_FAILURE_TTL`` when
    it did not answer. A reader that arrives while one runs gets the last answer, or "not signed in" when there is none."""
    key = (path, env.get("HOME"), env.get("CLAUDE_CONFIG_DIR"))

    def remembered(allow_stale):
        with _lock:
            cached = _cli_status_cache.get(key)
        if cached and (allow_stale or time.monotonic() < cached[0]):
            return cached[1]
        return None

    answer = remembered(False)
    if answer is not None:
        return answer
    if not _cli_status_flight.acquire(blocking=False):
        stale = remembered(True)
        return stale if stale is not None else dict(SIGNED_OUT)
    try:
        answer = remembered(False)            # another reader may have finished just before this one got the turn
        if answer is not None:
            return answer
        done = _run(run, [path, "auth", "status", "--json"], env)
        try:
            data = json.loads(done.stdout) if done is not None else None
        except ValueError:
            data = None
        account = None
        if isinstance(data, dict):
            account = {"signed_in": data.get("loggedIn") is True, "method": _text(data.get("authMethod")),
                       "email": _text(data.get("email")), "plan": _text(data.get("subscriptionType"))}
        result = {"signed_in": bool(account and account["signed_in"]), "account": account}
        ttl = CLI_STATUS_TTL if isinstance(data, dict) else CLI_STATUS_FAILURE_TTL
        with _lock:
            _cli_status_cache[key] = (time.monotonic() + ttl, result)
        return result
    finally:
        _cli_status_flight.release()


def _environment_claude_status(env):
    """The sign-in an environment variable gives the CLI, or None when none is set. No CLI is run for it."""
    for names, method in ((API_KEY_VARIABLES, "api_key"), (TOKEN_VARIABLES, "token"), (BACKEND_VARIABLES, "third_party")):
        if any(env.get(name) for name in names):
            return {"signed_in": True, "account": {"signed_in": True, "method": method, "email": None, "plan": None}}
    if any(env.get(name) for name in STORE_VARIABLES):
        return dict(SIGNED_OUT)
    return None


def claude_status(environ=None, cli_path=None, run=subprocess.run, system=None, now=None):
    """``{"signed_in", "account": {"signed_in", "method", "email", "plan"} | None}`` for the Claude the agents use.

    A variable that signs the CLI in from the environment is answered from the environment. Otherwise the stored
    credential is read. ``claude auth status --json`` runs only when the store gives no usable sign-in, never while a
    Claude sign-in is pending, and at most once at a time (see ``_cli_claude_status``)."""
    env = cli_environment("claude", environ)
    from_environment = _environment_claude_status(env)
    if from_environment is not None:
        return from_environment
    stored = _stored_claude_sign_in(env, system or _platform.family(), run, time.time() if now is None else now)
    if stored is not None:
        return stored
    if sign_in_state("claude")["state"] == "connecting":
        return dict(SIGNED_OUT)
    if cli_path is None:
        found = resolve_cli("claude", env)
        cli_path = found["path"] if found else None
    if not cli_path:
        return dict(SIGNED_OUT)
    return _cli_claude_status(cli_path, env, run)


# ---------------------------------------------------------------- Codex status

def codex_status(environ=None, cli_path=None, run=subprocess.run):
    """``{"signed_in", "account": {"signed_in", "method": "chatgpt" | "api_key", "email", "plan"} | None}`` from
    ``codex login status`` (read-only, no handshake). Only the method is taken from the CLI's text: the text itself can
    name an account or a part of a key, so it is not passed on."""
    env = cli_environment("codex", environ)
    if cli_path is None:
        found = resolve_cli("codex", env)
        cli_path = found["path"] if found else None
    if not cli_path:
        return dict(SIGNED_OUT)
    done = _run(run, [cli_path, "login", "status"], env)
    text = ((done.stdout or "") + (done.stderr or "")).strip() if done is not None else ""
    lowered = text.lower()
    signed_in = done is not None and done.returncode == 0 and "logged in" in lowered and "not logged in" not in lowered
    account = None
    if signed_in:
        account = {"signed_in": True, "method": "api_key" if "api key" in lowered else "chatgpt", "email": None, "plan": None}
    return {"signed_in": signed_in, "account": account}


# ---------------------------------------------------------------- pending sign-ins

class _Pending:
    def __init__(self, provider):
        self.provider, self.process, self.server, self.login_id = provider, None, None, None
        self.cancelled = threading.Event()
        # Stop the whole process group (Codex's app-server and whatever it started) or only the process. Claude's CLI opens
        # the owner's browser itself; on Linux a browser that xdg-open starts cold is a member of the CLI's group, and a
        # group kill would close the owner's browser window with it. The CLI has no other helper, so only it is signalled.
        self.group = provider != "claude"
        self.output = None                   # the Claude CLI's output file (an unlinked temporary file), read by the watcher
        self.stdin = None                    # the Claude CLI's input: the code the owner pastes goes here
        self.page_url, self.user_code, self.method = None, None, None
        self.accepts_code, self.code_submitted = False, False
        self.previous, self.switching, self.env = None, False, {}
        self.started_at = time.time()


def _popen(command, **options):
    """The one place a sign-in process is created; tests replace it."""
    return _processes.start_in_background(command, **options)


def _signal_tree(process, gentle=True, group=True):
    """Signal the process and, with ``group``, everything it started. Only a group this module made (the process leads
    it) is signalled as a group."""
    (_processes.terminate_process if gentle else _processes.kill_process)(process, group=group)


def _wait(process, timeout):
    try:
        process.wait(timeout=max(0.0, timeout))
        return True
    except subprocess.TimeoutExpired:
        return False


def _stop_process(process, group=True):
    if process is None or process.poll() is not None:
        return
    _signal_tree(process, group=group)
    if not _wait(process, STOP_GRACE):
        _signal_tree(process, gentle=False, group=group)
        _wait(process, STOP_GRACE)


def sign_in_details(provider):
    """What the owner needs to finish the sign-in that is waiting, else all empty: ``method`` (``browser`` or ``device_code``),
    ``page_url`` (the sign-in page, https on the product's own domain), ``user_code`` (Codex's device code, typed on that page),
    ``accepts_code`` (Claude: the page shows a code to paste back, ``submit_code``), ``code_submitted``, ``started_at`` and
    ``expires_at`` (ISO, UTC), ``switching`` (a sign-in was already stored) and ``previous_account`` (``{email, plan}`` or None).
    The page and the code are for the owner's eyes only: nothing here logs them."""
    with _lock:
        pending = _pending.get(provider)
        if pending is None:
            return {"method": None, "page_url": None, "user_code": None, "accepts_code": False, "code_submitted": False,
                    "started_at": None, "expires_at": None, "switching": False, "previous_account": None}
        return {"method": pending.method, "page_url": pending.page_url, "user_code": pending.user_code,
                "accepts_code": pending.accepts_code, "code_submitted": pending.code_submitted,
                "started_at": _stamp(pending.started_at), "expires_at": _stamp(pending.started_at + SIGN_IN_TIMEOUT),
                "switching": pending.switching, "previous_account": pending.previous}


def _stamp(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_method(provider, system=None):
    """How a sign-in is done unless the owner chooses: Claude's CLI has one way. Codex opens its page in a browser on the
    computer that runs Qorqbot where that is a desktop (macOS, Windows) and uses the device code on Linux, which is a server."""
    if provider != "codex":
        return "browser"
    return "device_code" if (system or _platform.family()) == "linux" else "browser"


def sign_in_state(provider):
    """``{"state": "idle" | "connecting" | "failed", "reason": plain sentence or None}``."""
    with _lock:
        if provider in _pending:
            return {"state": "connecting", "reason": None}
        if provider in _failed:
            return {"state": "failed", "reason": _failed[provider]}
    return {"state": "idle", "reason": None}


def _finish(pending, reason=None, account=None):
    """Record how a sign-in ended, unless it was already replaced or cancelled. A sign-in that completed (``reason`` None) is
    reported to the completion handler; ``account`` is the account Codex confirmed (Claude's is read from its store)."""
    with _lock:
        if _pending.get(pending.provider) is not pending:
            return
        del _pending[pending.provider]
        if reason:
            _failed[pending.provider] = reason
        else:
            _failed.pop(pending.provider, None)
            _cli_status_cache.clear()         # the next read must see the new sign-in, not a remembered signed-out answer
    if not reason:
        _completed(pending, account)


def _account_of(status):
    """``{"email", "plan"}`` of a status that is signed in, else None."""
    account = status.get("account") if isinstance(status, dict) and status.get("signed_in") else None
    return {"email": account.get("email"), "plan": account.get("plan")} if isinstance(account, dict) else None


def _current_account(env):
    """The account Claude's store holds now, read from the files (never by asking the CLI), else None."""
    try:
        return _account_of(_stored_claude_sign_in(env, _platform.family(), subprocess.run, time.time()))
    except Exception:  # noqa: BLE001 - the account is a courtesy in the view; the sign-in itself did complete
        return None


def _completed(pending, account):
    """A sign-in finished: remember the switch (the account before and after), make the next status read fresh, and tell the
    handler, which restarts the agents of this product in a thread of its own."""
    provider = pending.provider
    if account is None and provider == "claude":
        account = _current_account(pending.env)
    previous = pending.previous
    changed = not (previous and previous.get("email") and previous.get("email") == (account or {}).get("email"))
    handler = _completion_handler
    entry = account_state.record_switch(provider, previous_account=previous, account=account, changed=changed,
                                        state="restarting" if handler is not None else "done")
    account_state.forget_clients()
    if handler is not None:
        threading.Thread(target=_run_handler, args=(handler, provider, entry), daemon=True, name=f"{provider}-sign-in-done").start()


def _run_handler(handler, provider, entry):
    try:
        handler(provider, entry)
    except Exception:  # noqa: BLE001 - nothing may escape a thread of the service; the view says the switch ended
        account_state.update_switch(provider, state="done")


def _close_input(pending):
    stream, pending.stdin = pending.stdin, None
    if stream is not None:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _stop_pending(pending, cancel_login=True):
    pending.cancelled.set()
    _close_input(pending)
    server = pending.server
    if cancel_login and server is not None and pending.login_id:
        server.try_send({"id": 0, "method": "account/login/cancel", "params": {"loginId": pending.login_id}})
    _stop_process(pending.process, pending.group)


def cancel_sign_in(provider):
    """Stops the pending sign-in, if any, and forgets a failed one. Returns the state (``idle``)."""
    with _lock:
        pending = _pending.pop(provider, None)
        _failed.pop(provider, None)
    if pending is not None:
        _stop_pending(pending)
    return sign_in_state(provider)


def stop_all(budget=None):
    """Stops every pending sign-in; called when the service shuts down. The processes are asked to stop together and
    given ``budget`` seconds (``STOP_ALL_BUDGET``) in all; whatever is still running then is killed."""
    budget = STOP_ALL_BUDGET if budget is None else budget
    with _lock:
        pendings = [_pending.pop(provider) for provider in PROVIDERS if provider in _pending]
        _failed.clear()
    for pending in pendings:
        pending.cancelled.set()
        if pending.server is not None and pending.login_id:
            pending.server.try_send({"id": 0, "method": "account/login/cancel", "params": {"loginId": pending.login_id}})
        if pending.process is not None and pending.process.poll() is None:
            _signal_tree(pending.process, group=pending.group)
    end = time.monotonic() + budget
    for pending in pendings:
        if pending.process is not None and not _wait(pending.process, end - time.monotonic()):
            _signal_tree(pending.process, gentle=False, group=pending.group)
            _wait(pending.process, 1)


atexit.register(stop_all)


def _open_browser(url):
    return webbrowser.open(url)


def _fail_now(provider, reason):
    with _lock:
        _failed[provider] = reason
    return sign_in_state(provider)


def _reason(value, fallback):
    """A short single-line sentence from what a CLI reported, or the fallback."""
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text[:REASON_LIMIT] if text else fallback


def start_sign_in(provider, *, opener=None, environ=None, method=None, run=subprocess.run):
    """Starts the sign-in of ``provider`` and returns at once; poll ``sign_in_state`` and ``sign_in_details``. A pending sign-in of
    the same provider is stopped and replaced. ``opener(url)`` shows Codex's sign-in page (default: the system browser).

    The same call signs in and switches accounts: a stored sign-in stays in use until the new one is written, and a sign-in that is
    cancelled, fails or times out changes nothing. Whether one was stored is recorded (``previous_account``) so the view can say what
    changed. ``method`` is Codex's way: ``browser`` (a page opened on this computer) or ``device_code`` (a page and a code, for a
    computer without a browser); the default is ``default_method``."""
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider")
    name = "Claude Code" if provider == "claude" else "Codex"
    if method not in (None, "auto", *CODEX_METHODS) or (provider == "claude" and method == "device_code"):
        raise SignInError(f"{name} cannot sign in that way.")
    env = cli_environment(provider, environ)
    found = resolve_cli(provider, env)
    cancel_sign_in(provider)
    if not found:
        return _fail_now(provider, f"{name} is not downloaded yet. Download it first.")
    pending = _Pending(provider)
    pending.env = env
    pending.method = "browser" if provider == "claude" else (method if method in CODEX_METHODS else default_method(provider))
    if provider == "claude":
        pending.previous = _current_account(env)
        pending.switching = pending.previous is not None or _stored_claude_sign_in(env, _platform.family(), run, time.time()) is not None
    else:
        pending.switching = bool(codex_status(env, cli_path=found["path"], run=run)["signed_in"])
    cwd = env.get("HOME") or "/"
    open_page = opener or _open_browser
    try:
        if provider == "claude":
            # The CLI opens the browser itself. Its output goes to an unlinked temporary file (not a pipe: a browser it starts
            # inherits the descriptor and would keep a pipe open after the CLI is gone) that ``_watch_claude`` reads in
            # bounded pieces, so a sign-in page the CLI could not open is still shown. The layer starts the CLI in a session of
            # its own, out of the service's terminal group; stopping it signals only the CLI (see ``_Pending.group``). Its input
            # is a pipe that carries one thing: the code the sign-in page shows, which the owner pastes into Settings
            # (``submit_code``), for a computer that has no browser to receive it. A browser the CLI starts may inherit that
            # pipe's read end; it never writes to it.
            output = tempfile.TemporaryFile() if hasattr(os, "pread") else None
            sink = output if output is not None else subprocess.DEVNULL
            try:
                pending.process = _popen([found["path"], "auth", "login", "--claudeai"], stdin=subprocess.PIPE,
                                         stdout=sink, stderr=subprocess.STDOUT if output is not None else subprocess.DEVNULL,
                                         env=env, cwd=cwd)
            except BaseException:
                if output is not None:
                    output.close()
                raise
            pending.output = output
            pending.stdin = getattr(pending.process, "stdin", None)
            pending.accepts_code = pending.stdin is not None
            target, args = _watch_claude, (pending, open_page)
        else:
            pending.server = _AppServer([found["path"], "app-server", "--listen", "stdio://", *launch_arguments(provider)], env, cwd)
            pending.process = pending.server.process
            target, args = _codex_flow, (pending, open_page)
    except OSError:
        return _fail_now(provider, f"{name} could not be started. Download it again.")
    with _lock:
        earlier = _pending.get(provider)       # a concurrent start won the race: it is replaced too
        _pending[provider] = pending
        _failed.pop(provider, None)
    if earlier is not None:
        _stop_pending(earlier)
    threading.Thread(target=target, args=args, daemon=True, name=f"{provider}-sign-in").start()
    return sign_in_state(provider)


def _sign_in_page(text):
    """The https sign-in page of an Anthropic/Claude domain named in a line of the CLI's output, else None."""
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}")
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            port = parts.port
        except ValueError:
            continue
        if parts.scheme == "https" and port in (None, 443) and "@" not in parts.netloc and any(
                host == domain or host.endswith("." + domain) for domain in SIGN_IN_HOSTS):
            return url
    return None


def _read_claude_output(pending, opener):
    """Follow what the Claude CLI prints while it waits for the owner. It prints the sign-in page; when it says it could not
    open the browser, the page is opened here. Reads at most ``OUTPUT_LIMIT`` bytes, a line at most ``OUTPUT_LINE_LIMIT``;
    nothing read is kept beyond the current line, logged or reported."""
    file = pending.output
    seen = {"offset": 0, "line": b"", "url": None, "failed": False, "opened": False}

    def look(raw):
        text = raw[:OUTPUT_LINE_LIMIT].decode("utf-8", "replace")
        seen["url"] = seen["url"] or _sign_in_page(text)
        if seen["url"] and pending.page_url is None:
            with _lock:
                pending.page_url = seen["url"]
        seen["failed"] = seen["failed"] or _BROWSER_FAILED.search(text) is not None

    try:
        while True:
            finished = pending.cancelled.is_set() or pending.process.poll() is not None
            try:
                room = OUTPUT_LIMIT - seen["offset"]
                data = os.pread(file.fileno(), room, seen["offset"]) if room > 0 else b""
            except (OSError, ValueError):
                return
            seen["offset"] += len(data)
            for chunk in re.split(rb"(?<=\n)", data):
                seen["line"] += chunk
                if seen["line"].endswith(b"\n") or len(seen["line"]) >= OUTPUT_LINE_LIMIT:
                    look(seen["line"])
                    seen["line"] = b""
            if finished and not data and seen["line"]:
                look(seen["line"])
                seen["line"] = b""
            if seen["url"] and seen["failed"] and not seen["opened"] and not pending.cancelled.is_set():
                seen["opened"] = True
                try:
                    opener(seen["url"])
                except Exception:
                    pass
            if finished and not data:
                return
            if not data:
                time.sleep(OUTPUT_POLL)
    finally:
        try:
            file.close()
        except OSError:
            pass


def _watch_claude(pending, opener=None):
    if pending.output is not None:
        threading.Thread(target=_read_claude_output, args=(pending, opener or _open_browser), daemon=True,
                         name="claude-sign-in-output").start()
    try:
        code = pending.process.wait(timeout=SIGN_IN_TIMEOUT)
    except subprocess.TimeoutExpired:
        _stop_process(pending.process, pending.group)
        _close_input(pending)
        _finish(pending, "Claude Code sign-in was not finished in 10 minutes. Try again.")
        return
    _close_input(pending)
    if pending.cancelled.is_set():
        return
    _finish(pending, None if code == 0 else "Claude Code sign-in did not finish. Try again.")


def _checked_code(code):
    """The code the owner pasted, trimmed, or ``SignInError``. One line of printable characters without spaces, at most
    ``CODE_LIMIT``: it is written to the CLI's input followed by one newline and nothing else."""
    if not isinstance(code, str):
        raise SignInError("Paste the code the sign-in page shows.")
    code = code.strip()
    if not code:
        raise SignInError("Paste the code the sign-in page shows.")
    if len(code) > CODE_LIMIT or not code.isprintable() or any(c.isspace() for c in code):
        raise SignInError("That does not look like the code from the sign-in page. Copy it again, all of it.")
    return code


def submit_code(provider, code):
    """Gives Claude's waiting sign-in the code its page shows (the CLI reads it from its input). The code is never logged, kept
    or reported back; it is written once and forgotten. ``SignInError`` when the code is not one, when no sign-in is waiting, or
    when this sign-in takes none (Codex's device code is typed on its page). Returns ``sign_in_state``."""
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider")
    code = _checked_code(code)
    with _lock:
        pending = _pending.get(provider)
        if pending is None:
            raise SignInError("No sign-in is waiting. Start one first.")
        if not pending.accepts_code or pending.stdin is None:
            raise SignInError("This sign-in does not take a code here. Finish it on the sign-in page.")
        stream = pending.stdin
        try:
            stream.write(code.encode("utf-8") + b"\n")
            stream.flush()
        except (OSError, ValueError):
            raise SignInError("Claude Code is not waiting for a code any more. Start the sign-in again.") from None
        pending.code_submitted = True
    return sign_in_state(provider)


# ---------------------------------------------------------------- Codex app-server (same framing as qorqut_context.runner)

class _AppServer:
    """One ``codex app-server --listen stdio://`` process: a JSON object per line each way, no ``jsonrpc`` field."""

    def __init__(self, command, env, cwd):
        self.process = _popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=env, cwd=cwd, text=True, encoding="utf-8")
        self.messages, self.next_id, self.write_lock = queue.Queue(), 0, threading.Lock()
        threading.Thread(target=self._read, daemon=True, name="codex-app-server-reader").start()

    def _read(self):
        try:
            for line in iter(self.process.stdout.readline, ""):
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    self.messages.put(value)
        except (OSError, ValueError):
            pass
        self.messages.put(None)                                    # end of output

    def try_send(self, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
        try:
            with self.write_lock:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def receive(self, deadline):
        """The next message, ``None`` at end of output; ``TimeoutError`` at the deadline."""
        try:
            return self.messages.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError from None

    def answer_request(self, value):
        """The server asked us something (an approval, say): a sign-in client supports none of it."""
        self.try_send({"id": value["id"], "error": {"code": -32601, "message": "Unsupported request"}})

    def request(self, method, params, deadline, notifications=None, limit=None):
        self.next_id += 1
        request_id = self.next_id
        if not self.try_send({"id": request_id, "method": method, "params": params}):
            raise OSError("Codex closed its input")
        while True:
            value = self.receive(min(deadline, time.monotonic() + (limit or CODEX_REQUEST_TIMEOUT)))
            if value is None:
                raise OSError("Codex stopped")
            if value.get("id") == request_id and "method" not in value:
                if "error" in value:
                    raise OSError(_reason((value["error"] or {}).get("message") if isinstance(value["error"], dict) else None,
                                          "Codex refused the request"))
                return value.get("result") if isinstance(value.get("result"), dict) else {}
            if "method" in value and "id" in value:
                self.answer_request(value)
            elif notifications is not None and "method" in value:
                notifications.append(value)                        # e.g. a completion that arrives before the reply


def _codex_flow(pending, opener):
    server = pending.server
    deadline = time.monotonic() + SIGN_IN_TIMEOUT
    early = []
    reason = "Codex sign-in did not finish. Try again."
    stage = "start"
    confirmed = None
    try:
        # The first launch of the large binary can be slow to answer (macOS checks it first): a longer limit than the rest.
        server.request("initialize", {"clientInfo": {"name": "qorqbot", "title": "Qorqbot", "version": "1"}}, deadline,
                       limit=CODEX_START_TIMEOUT)
        stage = "login"
        server.try_send({"method": "initialized"})
        device = pending.method == "device_code"
        if pending.switching:
            try:           # the account being replaced is a courtesy for the view: not knowing it never stops the sign-in
                pending.previous = _codex_account(server.request("account/read", {"refreshToken": False}, deadline).get("account"))
            except (OSError, TimeoutError):
                if pending.cancelled.is_set() or server.process.poll() is not None:
                    raise
        login = server.request("account/login/start", {"type": "chatgptDeviceCode" if device else "chatgpt"}, deadline, early)
        url, pending.login_id = login.get("verificationUrl" if device else "authUrl"), login.get("loginId")
        if pending.cancelled.is_set():
            return
        if not isinstance(url, str) or not url.startswith("https://"):
            reason = "Codex did not give a sign-in page. Try again."
            return
        if device:
            user_code = login.get("userCode")
            if not isinstance(user_code, str) or not user_code.strip() or len(user_code) > DEVICE_CODE_LIMIT or not user_code.isprintable():
                reason = "Codex did not give a sign-in code. Try again."
                _stop_pending(pending)
                return
            with _lock:
                pending.page_url, pending.user_code = url, user_code.strip()
        else:
            with _lock:
                pending.page_url = url
            try:
                opened = opener(url)
            except Exception:
                opened = False
            if opened is False:
                reason = "Qorqbot could not open the Codex sign-in page in your browser."
                _stop_pending(pending)
                return
        completed = _await_completion(server, pending, early, deadline)
        if pending.cancelled.is_set():
            return
        if completed is not True:
            reason = completed
            return
        account = server.request("account/read", {"refreshToken": False}, deadline).get("account")
        if isinstance(account, dict) and account.get("type") == "chatgpt":
            reason, confirmed = None, _codex_account(account)
        else:
            reason = "Codex could not confirm the ChatGPT sign-in. Try again."
    except TimeoutError:
        if stage == "start":
            reason = "Codex did not start. Try again."
        elif time.monotonic() >= deadline - 0.05:
            reason = "Codex sign-in was not finished in 10 minutes. Try again."
        else:
            reason = "Codex did not answer in time. Try again."
    except OSError as error:
        reason = _reason(str(error), reason) if not pending.cancelled.is_set() else None
    finally:
        _finish(pending, reason, confirmed)
        _stop_pending(pending, cancel_login=reason is not None)


def _codex_account(account):
    """``{"email", "plan"}`` from Codex's ``account/read`` answer for a ChatGPT sign-in, else None."""
    if isinstance(account, dict) and account.get("type") == "chatgpt":
        return {"email": _text(account.get("email")), "plan": _text(account.get("planType"))}
    return None


def _await_completion(server, pending, early, deadline):
    """True when the server reports this login completed; otherwise a plain reason (or the cancel flag is set)."""
    queued = list(early)
    while not pending.cancelled.is_set():
        value = queued.pop(0) if queued else server.receive(deadline)
        if value is None:
            return "Codex stopped before the sign-in finished. Try again."
        if "method" in value and "id" in value:
            server.answer_request(value)
            continue
        if value.get("method") != "account/login/completed":
            continue
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        if params.get("loginId") not in (None, pending.login_id):
            continue
        if params.get("success") is True:
            return True
        return _reason(params.get("error"), "The ChatGPT sign-in was not completed. Try again.")
    return "cancelled"
