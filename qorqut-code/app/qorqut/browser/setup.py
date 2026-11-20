"""Owner-facing readiness and automatic setup of the browser Qorqbot's agents use."""

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
BACKOFF_BASE, BACKOFF_LIMIT = 240, 3600
# The download of Qorqbot's own Chrome has one entry in the background loop's failure record, beside the agents'.
CHROME_BACKOFF = "managed-chrome"
# The latest setup failure per agent, so Settings can say why even when the background loop was the one that failed.
LAST_FAILURES = {}
RETRY = "Press Retry setup."
RETRY_AFTER = "press Retry setup."
RESTART = "If it keeps happening, restart Qorqbot."
FAILURE_TEXT = {
    "browser_capability_missing": "Chrome or the browser tool wasn't ready on this computer. Qorqbot tries again by itself, or " + RETRY_AFTER,
    "browser_workspace_unavailable": "This agent's folder isn't ready yet. Send the agent a message in Chat once, then " + RETRY_AFTER,
    "browser_not_ready": "Chrome started but didn't respond in time. " + RETRY + " " + RESTART,
    "browser_management_busy": "Browser setup is already running for this agent. This card updates by itself.",
    "browser_in_use": "An agent is using the browser right now. Press Retry setup when it finishes.",
    "browser_target_conflict": "This agent already has different browser settings, and Qorqbot won't overwrite them. Ask Ordubashy to check this agent's browser.",
    "browser_configuration_invalid": "This agent's saved browser settings can't be read, and Qorqbot won't overwrite them. Ask Ordubashy to check this agent's browser.",
    "browser_metadata_invalid": "This agent's saved browser record can't be read, and Qorqbot won't overwrite it. Ask Ordubashy to check this agent's browser.",
    "maintenance": "Qorqbot is paused for maintenance. Press Retry setup in a minute.",
}
STATE_TEXT = {
    "missing": "The agents' browser hasn't been set up yet. " + RETRY,
    "stopped": "The agents' browser isn't running. " + RETRY + " " + RESTART,
    "target_missing": "This agent isn't connected to the agents' browser yet. " + RETRY + " Qorqbot will connect it.",
    "unsupported": "Qorqbot can't create a browser here until Chrome and the browser tool are ready. It tries again by itself, or " + RETRY_AFTER,
    "busy": "Browser setup is in progress for this agent. This card updates by itself.",
    "conflict": FAILURE_TEXT["browser_target_conflict"],
    "invalid": FAILURE_TEXT["browser_configuration_invalid"],
    "unavailable": "Qorqbot couldn't check this agent's browser. " + RESTART,
}


def explanation(state, *, stop_requested=False, failure_code=None):
    """Why a browser isn't ready and what the owner can do, in the owner's language."""
    if state == "healthy":
        return ""
    if stop_requested and state == "stopped":
        return "This browser was stopped on purpose, so Qorqbot left it off. Press Retry setup to start it again."
    if failure_code and state != "busy":
        return FAILURE_TEXT.get(failure_code, "Browser setup didn't finish. " + RETRY + " " + RESTART)
    return STATE_TEXT.get(state, "This agent's browser isn't ready. " + RETRY + " " + RESTART)


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


INSTALLED_CHROME_USED = ("Qorqbot's own browser can't be used on this computer, so agents use the installed Google Chrome. "
                         "Nothing to do.")


def view(runtime):
    from qorqut.native_paths import installed_chrome
    from qorqut.bootstrap import browser
    owner, agents = targets(runtime)
    own_chrome = managed_chrome.view(runtime.paths.runtime)
    chrome = installed_chrome() is not None or own_chrome["state"] == "ready"
    if installed_chrome() is not None and own_chrome["state"] in {"unusable", "unsupported"}:
        # Its own text asks the owner to install Google Chrome, which this computer already has and agents already use.
        own_chrome = {**own_chrome, "explanation": INSTALLED_CHROME_USED}
    try:
        browser.runtime_executable(runtime.browser_access.runtime_directory())
        tool = True
    except (browser.BrowserError, OSError, ValueError):
        tool = False
    from . import sign_in
    for agent in agents:
        if sign_in.active(agent["agent_id"]):
            agent.update({"state": "signing_in", "diagnostic": "", "explanation": "Paused while you sign in. It comes back when you close the Chrome window."})
            continue
        try:
            result = runtime.browser_manager.manage(owner, {"agent_id": agent["agent_id"], "action": "status"},
                                                    owner_control=True)
            agent.update({"state": result["state"], "diagnostic": result["diagnostic"], "browser": result["browser"],
                          "explanation": explanation(result["state"], stop_requested=bool(result.get("stop_requested")),
                                                     failure_code=LAST_FAILURES.get(agent["agent_id"]))})
        except DomainError as error:
            agent.update({"state": "unavailable", "diagnostic": str(error), "explanation": explanation("unavailable")})
        except Exception:
            logger.exception("Could not read the status of agent browser %s", agent["agent_id"])
            agent.update({"state": "unavailable", "diagnostic": "This browser's status could not be read.",
                          "explanation": explanation("unavailable")})
    return {"kind": "built_in", "tool_installed": tool, "chrome_installed": chrome, "managed_chrome": own_chrome,
            "ready": tool and bool(agents) and all(agent["state"] in {"healthy", "signing_in"} for agent in agents), "agents": agents}


def _wait_longer(backoff, key, clock):
    """Record one more failure for ``key`` and when it may be tried again."""
    failures = backoff.get(key, {}).get("failures", 0) + 1
    backoff[key] = {"failures": failures, "next_attempt": clock() + min(BACKOFF_BASE * 2 ** (failures - 1), BACKOFF_LIMIT)}


def prepare_chrome(runtime, backoff=None, clock=time.monotonic):
    """Start installing Qorqbot's own Chrome in the background when it is not installed yet, and return at once.

    Nothing waits for it: agents are set up on the installed Chrome meanwhile, and where there is none, on the first pass
    after the download. A failure waits longer each time like an agent's; the owner's retry (no ``backoff``) does not wait.
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
    """Connect each current agent to the browser. A failure leaves other agents eligible for setup.

    With ``backoff`` (the background loop's per-agent failure record) this is automatic setup: it respects a deliberate
    stop and waits longer after each failure. Without it this is the owner's explicit retry, which does neither.
    """
    from . import sign_in
    prepare_chrome(runtime, backoff, clock)
    owner, agents = targets(runtime)
    for agent in agents:
        agent_id = agent["agent_id"]
        if sign_in.active(agent_id):
            continue
        failure = backoff.get(agent_id) if backoff is not None else None
        if failure is not None and clock() < failure["next_attempt"]:
            continue
        try:
            status = runtime.browser_manager.manage(owner, {"agent_id": agent_id, "action": "status"}, owner_control=True)
            if status["state"] not in SETUP_STATES or (backoff is not None and status.get("stop_requested")):
                if backoff is not None:
                    backoff.pop(agent_id, None)
                if status["state"] == "healthy":
                    LAST_FAILURES.pop(agent_id, None)
                continue
            runtime.browser_manager.manage(owner, {"agent_id": agent_id, "action": "ensure"},
                                           idempotency_key="automatic-browser-" + str(uuid.uuid4()),
                                           owner_control=True)
            LAST_FAILURES.pop(agent_id, None)
            if backoff is not None:
                backoff.pop(agent_id, None)
        except Exception as error:
            # Readiness reports the precise state and offers a retry after prerequisites are fixed.
            LAST_FAILURES[agent_id] = error.code if isinstance(error, DomainError) else "unexpected"
            if not isinstance(error, (DomainError, OSError, ValueError)):
                logger.exception("Automatic setup failed for agent browser %s", agent_id)
            if backoff is not None:
                _wait_longer(backoff, agent_id, clock)
    return view(runtime)
