"""Readiness and setup of the agent browser, for the owner's Settings card and for Ordubashy's ``agent_browser_manage``.

There is one browser, the Shared browser, and every permanent agent is connected to it. The card and Ordubashy read the same
state and start the same setup: the card's Retry setup button and Ordubashy's ``ensure`` both start the download of Qorqbot's
own Chrome when it is missing and needed (``managed_chrome.needed``: the browser runs on that build) and set the browser up
without waiting out an earlier failure.

A new browser runs on the installed Google Chrome only. Where there is none the browser is not created and the card says
Google Chrome is needed (``GOOGLE_CHROME_NEEDED``); the background loop looks for it on every pass (a file lookup) and sets
the browser up as soon as it is there."""

import logging
import time
import uuid

from qorqut_operations.contracts import DomainError
from qorqut.agents.global_scope import active_global
from qorqut.agents.roles import ORDUBASHY_ROLE, GLOBAL_WORKER_ROLE
from qorqut.management.identity import installation_owner
from . import managed_chrome

logger = logging.getLogger(__name__)

SETUP_STATES = frozenset({"missing", "stopped", "target_missing"})
# What only ``repair`` sets right. The owner's Retry setup does it; automatic setup never does.
REPAIR_STATES = frozenset({"conflict", "invalid"})
BACKOFF_BASE, BACKOFF_LIMIT = 240, 3600
# The background loop's failure record has one entry for the browser and one for the download of Qorqbot's own Chrome.
BROWSER_BACKOFF, CHROME_BACKOFF = "browser", "managed-chrome"
# The latest setup failure (``{"code": ...}``), so the card can say why even when the background loop was the one that failed.
LAST_FAILURE = {}
GOOGLE_CHROME_NEEDED = "google_chrome_needed"
RETRY = "Press Retry setup."
RETRY_AFTER = "press Retry setup."
RESTART = "If it keeps happening, restart Qorqbot."
FAILURE_TEXT = {
    GOOGLE_CHROME_NEEDED: managed_chrome.GOOGLE_CHROME_NEEDED,
    "browser_capability_missing": "Chrome or the browser tool wasn't ready on this computer. Qorqbot tries again by itself, or " + RETRY_AFTER,
    "browser_chrome_missing": "The Chrome the agents' browser was set up with is no longer installed on this computer. Install it "
                              "again (Google Chrome: google.com/chrome), then " + RETRY_AFTER + " Website sign-ins are kept.",
    "browser_not_ready": "Chrome started but didn't respond in time. " + RETRY + " " + RESTART,
    "browser_management_busy": "Browser setup is already running. This card updates by itself.",
    "browser_in_use": "An agent is using the browser right now. Press Retry setup when it finishes.",
    "browser_state_unverified": "A browser process is running that Qorqbot can't identify, so it left it alone. Restart this computer, then " + RETRY_AFTER,
    "maintenance": "Qorqbot is paused for maintenance. Press Retry setup in a minute.",
}
STATE_TEXT = {
    "missing": "The browser hasn't been set up yet. " + RETRY,
    "stopped": "The browser isn't running. " + RETRY + " " + RESTART,
    "target_missing": "The browser isn't connected to every agent yet. " + RETRY,
    "unsupported": "Qorqbot can't create the browser until Chrome and the browser tool are ready. It tries again by itself, or " + RETRY_AFTER,
    "busy": "The browser is in use or being set up. This card updates by itself.",
    "conflict": "The browser's saved settings don't match. " + RETRY + " Website sign-ins are kept.",
    "invalid": "The browser's saved settings can't be read. " + RETRY + " Website sign-ins are kept.",
    "unavailable": "Qorqbot couldn't check the browser. " + RESTART,
}


def explanation(state, *, stop_requested=False, failure_code=None):
    """Why the browser isn't ready and what the owner can do, in the owner's language."""
    if state == "healthy":
        return ""
    if stop_requested and state == "stopped":
        return "The browser was stopped on purpose, so Qorqbot left it off. Press Retry setup to start it again."
    if failure_code and state != "busy":
        return FAILURE_TEXT.get(failure_code, "Browser setup didn't finish. " + RETRY + " " + RESTART)
    return STATE_TEXT.get(state, "The browser isn't ready. " + RETRY + " " + RESTART)


def for_agent(text):
    """An owner-facing explanation as Ordubashy reads it: he has no button to press, he has ``ensure``."""
    return (text.replace(managed_chrome.GOOGLE_CHROME_NEEDED, managed_chrome.GOOGLE_CHROME_NEEDED_FOR_AGENT)
            .replace("Press Retry setup when it finishes.", "Use action=ensure when it finishes.")
            .replace("Press Retry setup", "Use action=ensure").replace("press Retry setup", "use action=ensure")
            .replace("This card updates by itself.", "Check again with action=status."))


def targets(runtime):
    """Current permanent agents that use the browser, lead agent first."""
    with runtime.database.read() as uow:
        owner = installation_owner(uow)
        rows = uow.connection.execute(
            "SELECT a.agent_id,p.display_name FROM agent_profiles a JOIN principals p ON p.id=a.agent_id "
            "WHERE a.status='active' AND p.active=1 ORDER BY a.agent_id").fetchall()
        result = []
        for row in rows:
            data = active_global(uow.connection, row["agent_id"])
            if data and data["role"]["id"] in {ORDUBASHY_ROLE, GLOBAL_WORKER_ROLE}:
                result.append({"agent_id": row["agent_id"], "name": row["display_name"],
                               "role": data["role"]["id"]})
    return owner, sorted(result, key=lambda item: (item["role"] != ORDUBASHY_ROLE, item["name"]))


def host(runtime):
    """What this computer has for the browser: the browser tool, whether the browser has a Chrome it may run on
    (``managed_chrome.chrome_available``: false asks the owner to install Google Chrome), and the state of Qorqbot's own
    Chrome."""
    from qorqut.bootstrap import browser
    own_chrome = managed_chrome.view(runtime.paths.runtime)
    chrome = managed_chrome.chrome_available(runtime.paths.runtime)
    try:
        browser.runtime_executable(runtime.browser_access.runtime_directory())
        tool = True
    except (browser.BrowserError, OSError, ValueError):
        tool = False
    return {"tool_installed": tool, "chrome_installed": chrome, "managed_chrome": own_chrome}


def view(runtime):
    """What the Settings card shows: the one browser's state, why it is not ready, and the agents connected to it."""
    owner, agents = targets(runtime)
    result = {"kind": "built_in", **host(runtime), "diagnostic": "",
              "agents": [{"agent_id": agent["agent_id"], "name": agent["name"]} for agent in agents]}
    try:
        status = runtime.browser_manager.manage(owner, {"action": "status"}, owner_control=True)
        notes = [f"{agent['name']}: {agent['problem']}" for agent in status["agents"] if agent.get("problem")]
        result.update({"state": status["state"], "diagnostic": status["diagnostic"], "agents": status["agents"],
                       "explanation": " ".join(filter(None, [explanation(
                           status["state"], stop_requested=bool(status.get("stop_requested")),
                           failure_code=_failure_code(status["state"], result["chrome_installed"])), *notes]))})
    except DomainError as error:
        result.update({"state": "unavailable", "diagnostic": str(error), "explanation": explanation("unavailable")})
    except Exception:
        logger.exception("Could not read the status of the agent browser")
        result.update({"state": "unavailable", "diagnostic": "The browser's status could not be read.",
                       "explanation": explanation("unavailable")})
    result["ready"] = result["tool_installed"] and bool(agents) and result["state"] == "healthy"
    return result


def _failure_code(state, chrome):
    """The failure the card explains: Google Chrome needed while the browser cannot be created for want of it, never once a
    Chrome is there again; else the latest failure."""
    code = LAST_FAILURE.get("code")
    if not chrome and state == "unsupported":
        return GOOGLE_CHROME_NEEDED
    return None if code == GOOGLE_CHROME_NEEDED and chrome else code


LOOP_WAIT, CHROME_WAIT = 120, 10


def loop_wait(card):
    """Seconds until the background loop's next pass, from the card ``ensure_all`` returned: short while Google Chrome is
    awaited (such a pass only looks for the file), so the browser is set up moments after the owner installs it."""
    return CHROME_WAIT if isinstance(card, dict) and card.get("chrome_installed") is False else LOOP_WAIT


def _wait_longer(backoff, key, clock):
    """Record one more failure for ``key`` and when it may be tried again."""
    failures = backoff.get(key, {}).get("failures", 0) + 1
    backoff[key] = {"failures": failures, "next_attempt": clock() + min(BACKOFF_BASE * 2 ** (failures - 1), BACKOFF_LIMIT)}


def prepare_chrome(runtime, backoff=None, clock=time.monotonic):
    """Start installing Qorqbot's own Chrome in the background when it is not installed yet and is needed, and return at once.
    Where no browser runs on Qorqbot's own build, nothing is downloaded (``managed_chrome.start`` decides).

    Nothing waits for it: the browser that runs on it starts on the first pass after the download. A failure waits longer
    each time like the browser's; an explicit retry (no ``backoff``) does not wait.
    """
    failure = backoff.get(CHROME_BACKOFF) if backoff is not None else None
    if failure is not None and clock() < failure["next_attempt"]:
        return

    def finished(error):
        if backoff is None:
            return
        if error is None:
            backoff.pop(CHROME_BACKOFF, None)
        else:
            _wait_longer(backoff, CHROME_BACKOFF, clock)

    managed_chrome.start(runtime.paths.runtime, finished, automatic=backoff is not None)


def ensure_all(runtime, backoff=None, clock=time.monotonic):
    """Converge the browser and connect every agent to it. Returns the card's view.

    With ``backoff`` (the background loop's failure record) this is automatic setup: it respects a deliberate stop, waits
    longer after each failure and never repairs. Without it this is the owner's Retry setup, which does none of those.

    Where Google Chrome is needed the browser's state is ``unsupported``, so no pass starts anything or waits longer; each
    pass's status looks for Google Chrome again (a file lookup), and the first pass that finds it creates the browser.
    """
    prepare_chrome(runtime, backoff, clock)
    failure = backoff.get(BROWSER_BACKOFF) if backoff is not None else None
    if failure is None or clock() >= failure["next_attempt"]:
        owner, _ = targets(runtime)
        try:
            status = runtime.browser_manager.manage(owner, {"action": "status"}, owner_control=True)
            state = status["state"]
            action = ("ensure" if state in SETUP_STATES and not (backoff is not None and status.get("stop_requested")) else
                      "repair" if state in REPAIR_STATES and backoff is None else None)
            if action is not None:
                runtime.browser_manager.manage(owner, {"action": action}, owner_control=True,
                                               idempotency_key=("automatic" if backoff is not None else "owner")
                                               + "-browser-" + str(uuid.uuid4()))
            if action is not None or state == "healthy":
                LAST_FAILURE.clear()
            if backoff is not None:
                backoff.pop(BROWSER_BACKOFF, None)
        except Exception as error:
            # Readiness reports the precise state and offers a retry after prerequisites are fixed.
            LAST_FAILURE["code"] = error.code if isinstance(error, DomainError) else "unexpected"
            if not isinstance(error, (DomainError, OSError, ValueError)):
                logger.exception("Automatic setup of the agent browser failed")
            if backoff is not None and LAST_FAILURE["code"] != GOOGLE_CHROME_NEEDED:      # looking again costs nothing
                _wait_longer(backoff, BROWSER_BACKOFF, clock)
    return view(runtime)


def agent_manage(runtime, principal_id, arguments, idempotency_key=None):
    """Ordubashy's ``agent_browser_manage``: one action on the browser, answered with what the Settings card knows too.

    ``ensure`` and ``repair`` are the card's Retry setup and more: they start the download of Qorqbot's own Chrome when it is
    missing and needed, and do not wait out an earlier failure. Who may call this is decided by the manager, before anything
    starts."""
    manager, action = runtime.browser_manager, arguments.get("action")
    if action != "status":
        manager.manage(principal_id, {"action": "status"})        # refuses anyone but Ordubashy before any effect
        if action in {"ensure", "repair"}:
            prepare_chrome(runtime)
    try:
        result = manager.manage(principal_id, arguments, idempotency_key)
    except DomainError as error:
        if action != "status":
            LAST_FAILURE["code"] = error.code
        raise
    if action != "status" and result["state"] == "healthy":
        LAST_FAILURE.clear()
    facts = host(runtime)
    own_chrome = facts["managed_chrome"]
    result["setup"] = {"tool_installed": facts["tool_installed"], "chrome_installed": facts["chrome_installed"],
                       "own_chrome": {"state": own_chrome["state"], "explanation": for_agent(own_chrome["explanation"])},
                       **({"last_failure": for_agent(explanation(result["state"], failure_code=LAST_FAILURE["code"]))}
                          if LAST_FAILURE.get("code") and result["state"] != "healthy" else {})}
    return result
