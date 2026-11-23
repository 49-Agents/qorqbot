"""The ``tab-reference`` browser command: the caller's current tab, named so that the owner can be shown it in Chat.

When a page needs the owner (a captcha that could not be solved, a sign-in, a confirmation), describing where to click is the
worst way to ask: the owner first has to find the right agent, browser and tab. An agent sends ``tab-reference`` as a
``browser_turn`` command, alone and without arguments, and gets the identity-only ``tab`` reference of the tab its session is
bound to. Ordubashy (or the Overseer) passes it to ``request_user_input``; any other agent gives it to Ordubashy. Chat then shows
the tab as a card, and the owner opens its live view (``live``), which the HTTP route allows the owner only.

Like ``captcha`` it is a command the server runs itself, so authorization, the handle, the browser's use lock and the reply are
those of ``click``; only ``agent-browser`` is left out. It is read-only: it touches no page and changes no browser. The one
mark it leaves is that the session used its tab (``idle_tabs.note_use``), which is right, since the owner may take a while to
answer. It takes no command lock of its own: it runs for milliseconds and writes nothing a command running beside it reads.

The reference names a tab and nothing more. It grants no control of the browser and no authorization to look at it.
"""

from __future__ import annotations

import json

from qorqut_operations.contracts import DomainError

from qorqut.native_chat import references

COMMAND = "tab-reference"
# A target's ``instance`` (``access._worker_target``) is ``configured:<agent id>`` for that agent's tabs in the Shared browser (not
# always the caller: Ordubashy may work in a worker's) and ``desktop:<surface>`` for a managed desktop, which is no agent's.
OWN = "configured:"
SHOW = ("Pass this reference to request_user_input, or give it to Ordubashy, to show the owner this tab in Chat. "
        "title and url say which page it is: check that they match what you tell the owner.")
SHOW_FIELD = SHOW.replace("this reference", "reference")      # the same advice in a reply that names the reference as a field
NO_TAB = "You have no tab open. Open the page first, then run tab-reference again."
NOT_AN_AGENT_BROWSER = "This browser is not the agents' browser, so its tabs cannot be named. Show the owner a browser reference instead."
NOT_LOCAL = "tab-reference works only in a browser on this computer."


def reference(agent_id, tab_id):
    """The ``tab`` reference of one tab of an agent's browser, or None when the two do not make an id that
    ``request_user_input`` takes."""
    identity = f"{agent_id}:{tab_id}"
    return {"type": references.TYPE_TAB, "id": identity} if agent_id and references.TAB.fullmatch(identity) else None


def describe(port, tab_id):
    """``{"title", "url"}`` of the open tab ``tab_id`` in the browser on ``port``, as Chrome lists it now, or ``{}`` when the
    browser does not answer or the tab is not listed. A reference is only an id, so an agent that holds several cannot tell
    them apart (it once gave the owner a PayPal tab as the Mercury sign-in); the page's own words are what it can compare. They
    are siblings of the reference, never part of it, and never a reason for the command to fail."""
    from . import live
    try:
        tab = next((item for item in live.tabs(port) if item["id"] == tab_id), None)
    except Exception:
        return {}
    return {"title": tab["title"], "url": tab["url"]} if tab else {}


def browser_agent(instance):
    """The agent whose browser a target is, from its ``instance`` (see ``OWN``), or None for any other target."""
    return instance[len(OWN):] if isinstance(instance, str) and instance.startswith(OWN) else None


def current(port, session):
    """The id of the tab ``session`` is bound to, if that tab is open in the browser on ``port``, else None. Which tab that
    is, is read where the idle-tab sweeper reads it: asking the session would open a stray tab when it has none. Raises
    ``BrowserError`` when the browser does not answer."""
    from qorqut.bootstrap.browser import BrowserError
    from . import idle_tabs, live
    bound = idle_tabs.note_use(session)
    try:
        open_tabs = {tab["id"] for tab in live.tabs(port)}
    except DomainError:
        raise BrowserError("The browser did not answer. Nothing was done.") from None
    return bound if bound in open_tabs else None


def is_command(command):
    """Whether a ``browser_turn`` command is this one. It takes nothing: the tab is the caller's own."""
    if not command or command[0] != COMMAND:
        return False
    if len(command) > 1:
        raise DomainError("validation_error", "tab-reference takes no arguments; it names your current tab")
    return True


def _refused(reason):
    return 1, json.dumps({"reason": reason}) + "\n", ""


def runner():
    """What runs the command in place of ``bootstrap.browser.run``: the same arguments in, the same
    ``(exit code, stdout, stderr)`` out. The agent named is the one whose browser the command ran in (from ``instance``), which
    is the caller's own except where Ordubashy works in a worker's; that is the agent whose tabs the owner's view lists."""
    def run(workspace, _command, *, target, instance="", capture=True, pinned=False):
        from qorqut.bootstrap import browser
        from . import idle_tabs
        endpoint, _executable = target
        owner, port = browser_agent(instance), idle_tabs._port(endpoint)
        if owner is None:
            return _refused(NOT_AN_AGENT_BROWSER)
        if port is None:
            return _refused(NOT_LOCAL)
        found = current(port, browser.session_name(workspace, endpoint, instance))
        named = reference(owner, found) if found else None
        if named is None:
            return _refused(NO_TAB)
        return 0, json.dumps({"reference": named, **describe(port, found), "next": SHOW}) + "\n", ""
    return run
