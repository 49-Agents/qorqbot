"""What follows a change of what an agent has in effect from the installed extensions: its open session is restarted.

An extension's skills reach an agent when its session is prepared (``AgentConfigResolver.managed_skills``), and a session
whose skill set changed cannot repair itself: its next hook is refused with ``reprepare_required`` until it is opened again.
So after each change of the set of extensions in effect for some agents (``ExtensionRegistry.subscribe``: a switch, a
change of status into or out of ``ready``, a plugin part installed, updated or removed at start, or an extension taken out
of effect for a fault in its layer copy), the session of each of them that is open is closed and opened again
(``SessionOpener.restart``), resuming its conversation, and the new session is sent one notice saying what changed. As
with a change of account (``account_switch.py``), nothing asks, waits for a quiet moment or protects a running turn.

Restarts run off the committing thread. Changes made while the service starts (``sync``) are held until Chat has started
(``release``). An agent with no session open, or one that is off or paused, is left alone: its next session is prepared
with what is in effect then. A restart that fails is logged and not tried again. In Terminals mode nothing is restarted:
the owner closes the agent and opens it again.
"""

from __future__ import annotations

import logging
import threading
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.managed_runtime import mode
from . import store
from .account_switch import restart_session

LOG = logging.getLogger(__name__)

# The notice is a system message, not the owner's: it starts with an opening tag, as the account notices do.
NOTICE = ('<qorqut-extension-notice>\n{changes} Please carry on with what you were doing. This notice needs no other '
          'reply.\n</qorqut-extension-notice>')
# One sentence for each extension in the notice, by what happened: the owner's switch, an update of its plugin part, or
# otherwise the state it is in now (a change of status, or what a start found).
SWITCHED_ON = "The extension {name} was switched on for you."
SWITCHED_OFF = "The extension {name} was switched off for you."
UPDATED = "The extension {name} was updated."
IN_EFFECT = "The extension {name} is now in effect for you."
REMOVED = "The extension {name} was removed."
NEEDS_ATTENTION = "The extension {name} was taken out of use because it needs attention."
UNSUPPORTED = "The extension {name} was taken out of use because it cannot run on this computer."
NOT_IN_EFFECT = "The extension {name} is no longer in effect for you."

# What happens to the agent's session, for the owner's switch and the log line of every change.
SESSION = {
    "restarted": "Its session is being restarted now, so the change takes effect at once.",
    "not_open": "It has no session open, so it gets the change when its session next opens.",
    "terminal": "It runs in a terminal, so the owner has to close it and open it again for the change to take effect.",
    "unchanged": "What it has in effect did not change, so its session is left as it is.",
}


def _in_background(work):
    threading.Thread(target=work, name="extension-restart", daemon=True).start()


class ExtensionRestarts:
    """Subscribed to the registry of installed extensions; restarts the open sessions whose extensions in effect changed.
    ``start`` runs the restarts off the committing thread (a thread of their own unless a test gives another). With
    ``held``, work waits until ``release``."""

    def __init__(self, chat, extensions, *, start=_in_background, held=False):
        self.chat, self.extensions, self._start = chat, extensions, start
        self._lock = threading.Lock()
        self._held = [] if held else None
        extensions.subscribe(self.changed)

    def changed(self, changes):
        """The registry's subscriber, on the committing thread. Only schedules the restarts."""
        affected = {}
        for change in changes:
            for agent_id in change.affected:
                found = affected.setdefault(agent_id, [])
                if (change.extension, change.what) not in found:
                    found.append((change.extension, change.what))
        if not affected:
            return
        work = lambda: self.restart(affected)  # noqa: E731
        with self._lock:
            if self._held is not None:
                self._held.append(work)
                return
        self._start(work)

    def release(self):
        """Run what was held, once the opener can open sessions (Chat has started); later work runs at once."""
        with self._lock:
            held, self._held = self._held or [], None
        for work in held:
            self._start(work)

    def _open(self, agent_id) -> bool:
        managed = self.chat.managed
        try:
            return managed is not None and managed.handle(agent_id) is not None
        except Exception:  # noqa: BLE001 - a session that cannot be verified is left alone
            return False

    def expected(self, agent_id, changed) -> str:
        """What happens to the agent's session after a committed change: a key of ``SESSION``."""
        if not changed:
            return "unchanged"
        if mode.is_terminal(self.chat.runtime.paths):
            return "terminal"
        return "restarted" if self._open(agent_id) else "not_open"

    def notice(self, agent_id, items) -> str:
        return NOTICE.format(changes=" ".join(self._sentence(agent_id, ext_id, what) for ext_id, what in items))

    def _sentence(self, agent_id, ext_id, what) -> str:
        try:
            entry = self.extensions.get(ext_id)
        except DomainError:
            return REMOVED.format(name=ext_id)
        name = entry.manifest.name if entry.manifest else ext_id
        on = self.extensions.is_on(ext_id, agent_id)
        if what == "updated":
            sentence = UPDATED
        elif what == "switch":
            sentence = SWITCHED_ON if on else SWITCHED_OFF
        elif on:
            sentence = IN_EFFECT
        elif entry.status == "needs_attention":
            sentence = NEEDS_ATTENTION
        elif entry.status == "unsupported":
            sentence = UNSUPPORTED
        else:
            sentence = NOT_IN_EFFECT
        return sentence.format(name=name)

    def restart(self, affected) -> dict:
        """Restart the open session of each agent in ``affected`` (agent id to its ``(extension, what)`` changes). Returns
        each agent's outcome: a key of ``SESSION``, or ``failed``."""
        outcomes = {}
        for agent_id, items in affected.items():
            outcome = self.expected(agent_id, True)
            if outcome == "restarted":
                outcome = self._restart(agent_id, items)
            outcomes[agent_id] = outcome
            LOG.info("extension_change agent=%s extensions=%s %s", agent_id,
                     ",".join(dict.fromkeys(ext_id for ext_id, _ in items)),
                     SESSION.get(outcome, "Its session could not be restarted; it gets the change when its session next opens."))
        return outcomes

    def _restart(self, agent_id, items) -> str:
        with self.chat.database.read() as uow:
            profile = store.active_profile(uow.connection, agent_id, self.chat.runtime.paths)
        if profile is None:
            return "not_open"
        restarted = restart_session(self.chat, profile, self.notice(agent_id, items), f"extensions:{uuid.uuid4().hex}",
                                    "an extension change")
        return "restarted" if restarted else "failed"
