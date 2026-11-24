"""What follows a change of account: restarting the sessions of the product that was signed in again, and moving one agent to the
other product.

Both are the owner's own actions (the sign-in on Settings > Agents; the button in an agent's chat). The owner has said agents may
be interrupted for them, so nothing here asks, waits for a quiet moment or protects a running turn: a session is closed, then opened
again.

``after_sign_in`` is the completion handler of ``agent_cli_sign_in``: every managed session that runs on the product is closed and
opened again (``SessionOpener.restart``), resuming its conversation, so it starts with the new sign-in. A session whose last turn
ended in a usage limit or a sign-in problem is also sent a short notice to carry on. The notice is a system message, not the
owner's: it starts with an opening tag, which is how Chat already shows Qorqbot's own notices (as the wake notice does), and it
reaches the host the same way the wake notice does. The counts are written to ``account_state`` for Settings.

``use_other_product`` changes one agent's client and restarts its session on it. A session of one client cannot resume the
conversation of the other, so the agent gets a fresh conversation (its role first) and the notice below says what happened; the
earlier conversation stays in Chat's history.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging

from qorqut_operations.contracts import DomainError

from qorqut import account_state
from qorqut.managed_runtime import clients as table, mode, options as stored
from . import store

LOG = logging.getLogger(__name__)

WORKERS = 3                  # sessions restarted at the same time (the managed runtime allows a few starts at once)
PROBLEM_WORDS = {"usage_limit": "its usage limit was reached", "sign_in": "it could not sign in"}
CARRY_ON = ('<qorqut-account-notice>\nThe {name} account Qorqbot uses was changed and this session was restarted. Your last turn '
            'had stopped because {why}. Please carry on with what you were doing. This notice needs no other reply.\n'
            '</qorqut-account-notice>')
MOVED = ('<qorqut-account-notice>\nThe owner moved this agent to {name}{why}. This is a new conversation: the earlier one stays '
         'in the chat history, but {name} cannot continue it. Carry on with your work from your tasks and records. This notice '
         'needs no other reply.\n</qorqut-account-notice>')


class AccountSwitch:
    def __init__(self, chat):
        self.chat = chat

    @property
    def managed(self):
        return self.chat.managed

    # ----- after a sign-in -------------------------------------------------------------------------------------------

    def after_sign_in(self, provider, entry):
        """The completion handler of a sign-in on ``provider``: restart its running sessions and say how it went."""
        counts = self.restart_product(provider, entry["at"])
        account_state.update_switch(provider, state="done", **counts)

    def sessions_of(self, provider) -> list:
        """``(profile, last turn problem kind or None)`` of every agent whose session is open on ``provider``."""
        managed = self.managed
        if managed is None or mode.is_terminal(self.chat.runtime.paths):
            return []
        found = []
        for item in managed.running_hosts():
            agent_id = item["agent_id"]
            try:
                host = managed.handle(agent_id)
                client = host.record.get("client") if host is not None else None
            except Exception:  # noqa: BLE001 - a session that cannot be verified is left alone
                continue
            if (client if table.known(client) else table.CLAUDE) != provider:
                continue
            with self.chat.database.read() as uow:
                profile = store.active_profile(uow.connection, agent_id, self.chat.runtime.paths)
            if profile is None:
                continue
            try:
                kind = managed.snapshot(agent_id).get("turn_error_kind")
            except Exception:  # noqa: BLE001
                kind = None
            found.append((profile, kind))
        return found

    def restart_product(self, provider, key) -> dict:
        """Close and open again every session on ``provider``. ``{"restarted", "continued", "failed"}``: sessions that came back,
        those of them that were also sent the notice to carry on, and sessions that could not be restarted (they open again on
        their next message, with the new sign-in)."""
        sessions = self.sessions_of(provider)
        counts = {"restarted": 0, "continued": 0, "failed": 0}
        if not sessions:
            return counts
        name = table.NAMES[provider]

        def one(item):
            profile, kind = item
            text = CARRY_ON.format(name=name, why=PROBLEM_WORDS[kind]) if kind in PROBLEM_WORDS else None
            try:
                self.chat.opener.restart(profile, text, f"account:{provider}:{key}")
            except DomainError:
                LOG.info("The session of %s was not restarted after the sign-in", profile["agent_id"], exc_info=True)
                return "failed"
            except Exception:  # noqa: BLE001 - one session never stops the others
                LOG.warning("The session of %s was not restarted after the sign-in", profile["agent_id"], exc_info=True)
                return "failed"
            return "continued" if text is not None else "restarted"

        with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="account-restart") as pool:
            for outcome in pool.map(one, sessions):
                if outcome == "failed":
                    counts["failed"] += 1
                else:
                    counts["restarted"] += 1
                    counts["continued"] += outcome == "continued"
                account_state.update_switch(provider, **counts)
        return counts

    # ----- one agent on the other product ----------------------------------------------------------------------------

    def use_other_product(self, principal_id, agent_id, client=None) -> dict:
        """Move one agent to the other product (or to ``client``) and restart its session on it, sending the notice. An agent
        with no session open only has its client changed: its next message opens it on the new product. Returns the agent's
        options view (``launch_options.view``) plus ``previous_client``, ``restarted`` and ``continued``. ``DomainError`` for an
        installation that runs agents in terminals, a product that is not ready, or a client the agent already uses."""
        from . import launch_options
        chat = self.chat
        with chat.database.read() as uow:
            profile = chat._target(uow, principal_id, agent_id)
        if mode.is_terminal(chat.runtime.paths):
            raise DomainError("validation_error", "Only Claude Code agents can run in terminals.")
        ready = stored.ready_function()
        current = stored.effective_client(chat.runtime.paths, agent_id, profile, ready)
        target = client if client is not None else account_state.other(current)
        if not table.known(target):
            raise DomainError("validation_error", "Choose claude or codex")
        if target == current:
            raise DomainError("validation_error", f"This agent already uses {table.NAMES[target]}.")
        if not ready(target):
            raise DomainError("cli_not_signed_in", f"{table.NAMES[target]} isn't installed and signed in yet. Do that under "
                                                   "Settings > Agents first.")
        change = lambda: launch_options.change(chat, chat.runtime, principal_id, agent_id, {"client": target}, ready=ready)  # noqa: E731
        was_open = self.managed is not None and self._open(agent_id)
        if not was_open:
            change()
            return {**launch_options.view(chat, chat.runtime, principal_id, agent_id, ready=ready), "previous_client": current,
                    "restarted": False, "continued": False}
        kind = (self.managed.snapshot(agent_id) or {}).get("turn_error_kind")
        why = f", because {PROBLEM_WORDS[kind]} on {table.NAMES[current]}" if kind in PROBLEM_WORDS else ""
        text = MOVED.format(name=table.NAMES[target], why=why)
        chat.opener.restart(profile, text, f"account-move:{target}:{agent_id}", between=change)
        return {**launch_options.view(chat, chat.runtime, principal_id, agent_id, ready=ready), "previous_client": current,
                "restarted": True, "continued": True}

    def _open(self, agent_id) -> bool:
        try:
            return self.managed.handle(agent_id) is not None
        except Exception:  # noqa: BLE001 - a session that cannot be verified counts as open: fail closed
            return True
