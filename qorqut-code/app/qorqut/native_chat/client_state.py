"""What stands between an agent and an open session on its client, and the one sentence the owner reads about it.

An open can fail on the client in these ways, in the order they are checked (before anything starts, so nothing starts and fails late):

``not_installed``   this agent's client is not installed (Qorqbot's own copy is not downloaded and no accepted copy is on the owner's PATH)
``downloading``     Qorqbot's own copy is being downloaded right now
``too_old``         the owner's own copy is older than ``agent_cli_install.MINIMUM_VERSIONS``
``no_version``      the owner's own copy did not report its version (so it is not used)
``signing_in``      a browser sign-in of this client is open and not finished
``not_signed_in``   installed and not signed in
``ready``           installed and signed in

An account that is refused, or out of quota, at the first turn is not knowable here: it shows up as the turn's own error text (the
host keeps it, ``managed.turn_error``).

The sentence is composed here, once, with the agent's name and its client, and the dashboard shows it as it is for these codes
(``cli_unavailable``: the client is missing, too old or still downloading; ``cli_not_signed_in``: it is installed but not signed in).
"""

from __future__ import annotations

from qorqut.managed_runtime import clients as table

CLI_UNAVAILABLE, CLI_NOT_SIGNED_IN = "cli_unavailable", "cli_not_signed_in"
CODES = (CLI_UNAVAILABLE, CLI_NOT_SIGNED_IN)
READY = "ready"
NAME_LIMIT = 40
# Where the owner downloads, updates and signs in a client, and where an agent's client is chosen.
PLACE = "Settings > Model/Provider"


def _state(row) -> str:
    """The state of one client's row of ``owner_settings.agent_clients_view``."""
    if not isinstance(row, dict):
        return "not_installed"
    install = row.get("install") if isinstance(row.get("install"), dict) else {}
    sign_in = row.get("sign_in") if isinstance(row.get("sign_in"), dict) else {}
    if row.get("installed"):
        if row.get("signed_in"):
            return READY
        return "signing_in" if sign_in.get("state") == "connecting" else "not_signed_in"
    if install.get("phase") == "downloading":
        return "downloading"
    message = str(install.get("message") or "").lower()
    if "too old" in message:
        return "too_old"
    if "did not report its version" in message:
        return "no_version"
    return "not_installed"


def states(environ=None, run=None) -> dict:
    """``{"claude": {"state": ...}, "codex": {"state": ...}}`` from the status Settings shows. A status that cannot be read is
    ``unknown`` for both: nothing is then refused on its account (the open itself still finds the client or not)."""
    try:
        from qorqut import owner_settings
        view = owner_settings.agent_clients_view(environ) if run is None else owner_settings.agent_clients_view(environ, run=run)
    except Exception:  # noqa: BLE001
        return {name: {"state": "unknown"} for name in table.CLIENTS}
    return {name: {"state": _state(view.get(name))} for name in table.CLIENTS}


def ready_function(found: dict):
    """A ``ready(client)`` over ``states`` (an unknown state counts as not ready)."""
    return lambda client: found.get(client, {}).get("state") == READY


_HEAD = {
    "not_installed": "{client} isn't installed yet",
    "downloading": "{client} is still downloading",
    "too_old": "the {client} on this computer is too old",
    "no_version": "the {client} on this computer didn't report its version",
    "signing_in": "{client} isn't signed in yet",
    "not_signed_in": "{client} isn't signed in yet",
}
_ACTION = {
    "not_installed": "Download it under " + PLACE,
    "downloading": "Wait for the download to finish under " + PLACE,
    "too_old": "Download Qorqbot's own copy under " + PLACE,
    "no_version": "Download Qorqbot's own copy under " + PLACE,
    "signing_in": "Finish signing in in your browser, or start again under " + PLACE,
    "not_signed_in": "Sign in under " + PLACE,
}


def refusal(label, client, found: dict):
    """``(code, sentence)`` for an agent called ``label`` whose client is ``client``, or None when that client is ready or its state
    is not known. The sentence names the agent and its client, says what is wrong and what to do, and offers the other client
    only when it is ready."""
    state = (found.get(client) or {}).get("state")
    if state in (None, READY, "unknown") or state not in _HEAD:
        return None
    name, other = table.NAMES[client], table.NAMES[table.CODEX if client == table.CLAUDE else table.CLAUDE]
    who = (str(label).strip() or "This agent")[:NAME_LIMIT]
    other_ready = (found.get(table.CODEX if client == table.CLAUDE else table.CLAUDE) or {}).get("state") == READY
    ending = f", or switch {who} to {other} there." if other_ready else ", then send again."
    text = f"{who} is set to use {name}, and {_HEAD[state].format(client=name)}. {_ACTION[state]}{ending}"
    return (CLI_NOT_SIGNED_IN if state in ("signing_in", "not_signed_in") else CLI_UNAVAILABLE), text


def missing_text(label, client) -> str:
    """The sentence for a client that the launch could not find although the status said otherwise (a copy that vanished)."""
    name = table.NAMES[client]
    return f"{(str(label).strip() or 'This agent')[:NAME_LIMIT]} is set to use {name}, and {name} couldn't be found. Download it under {PLACE}, then send again."
