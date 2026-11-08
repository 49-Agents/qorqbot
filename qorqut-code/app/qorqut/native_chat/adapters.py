"""Safe provider input adapters: the only code that types into a native session.

An adapter receives a verified binding, the exact text, and a ``checkpoint`` it calls once, after its last pre-input check and
before the first input. The arbiter's checkpoint re-verifies the exact session and makes the durable claim; if it raises, nothing
is typed. After the checkpoint every outcome other than an explicit ``deferred`` may follow a write, so it is reported as
``submitted`` or ``uncertain`` and is never replayed. The text, the screen and the terminal identity never appear in a result:
``receipt`` names only what was observed (client, a digest of the terminal identity, the composer fingerprint).

* ``TerminalAdapter`` types into the agent's one registered tmux pane for Claude Code or Codex through the existing verified
  composer path (one bracketed paste, Enter with one verified pending-paste retry, no input into a modal).
  Owner Chat can replace an existing draft.
* ``CodexQueueAdapter`` uses explicit turn/steer (busy) or turn/start (idle) for steering inputs on the verified
  shared server. Non-steering producers retain codex queue. It never starts or resumes a client.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from qorqut.bootstrap import codex_session
from qorqut.transport.agent_terminal_control import OwnerInputOutcome

from .contracts import PROVIDER_RUNTIMES
from . import codex_control

CODEX_QUEUE_TIMEOUT = 30
SUBMITTED = "submitted"
DEFERRED = "deferred"
UNCERTAIN = "uncertain"
FAILED = "failed"


class TerminalAdapter:
    """Claude Code or Codex in the registered tmux terminal. ``backend`` is a callable returning the host terminal backend."""

    name = "terminal"

    def __init__(self, backend):
        self._backend = backend

    def submit(self, profile: dict, session: dict, text: str, *, steer: bool, echo: bool, checkpoint, replace_composer=False) -> OwnerInputOutcome:
        backend = self._backend()
        if backend is None:
            return OwnerInputOutcome(DEFERRED, "terminal_unavailable")
        workdir = Path(profile["workspace"]) / ".qorqut/chat-input"
        try:
            workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            return OwnerInputOutcome(DEFERRED, "terminal_unavailable")
        # The exact native client of the bound session decides the composer rules, including an owner who chose another CLI
        # in the terminal than the profile names.
        delivery = {**profile, "profile_json": json.dumps({**json.loads(profile["profile_json"]),
                                                           "runtime": PROVIDER_RUNTIMES[session["provider"]]})}
        # ``steer`` lets the text coexist with a running turn (the client queues or steers it as if the owner typed it); the
        # composer must be verified editable. Only owner Chat replaces drafts with editing keys; ``echo`` demands the whole pasted
        # text visible in the composer before it is submitted; Chat and Telegram free-form text may be collapsed by the client
        # and may do without the whole-text echo unless replacing a draft. Replacement always verifies
        # the paste. A producer without ``steer`` also keeps the idle-only default.
        # A bare slash command is typed with one trailing space, which closes the CLI's completion menu (commands.py).
        from .commands import delivery_text
        return backend.owner_input(delivery, delivery_text(text), workdir=workdir, checkpoint=checkpoint,
                                   require_paste_echo=echo or replace_composer, allow_busy=steer,
                                   replace_composer=replace_composer)


class CodexQueueAdapter:
    """Queue into the exact existing Codex desktop conversation (its thread-writer lock must still be held)."""

    name = "codex_queue"

    def __init__(self, *, run=subprocess.run, alive=codex_session.owner_alive, executable="codex", steering=codex_control.submit):
        self._run, self._alive, self._executable = run, alive, executable
        self._steering = steering

    def submit(self, profile: dict, session: dict, text: str, *, steer: bool, echo: bool, checkpoint, replace_composer=False) -> OwnerInputOutcome:
        owner = session.get("owner")
        if not owner or not self._alive(owner):
            return OwnerInputOutcome(DEFERRED, "codex_session_closed")
        if steer:
            return self._steering(session, text, checkpoint, alive=self._alive)
        try:
            checkpoint()
        except Exception:
            return OwnerInputOutcome(DEFERRED, "checkpoint_failed")
        if not self._alive(owner):
            return OwnerInputOutcome(DEFERRED, "codex_session_closed")
        receipt = {"client": "codex", "transport": "codex_queue"}
        try:
            result = self._run([self._executable, "queue", "--thread", session["conversation_id"], "--message", text],
                               cwd=profile["workspace"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=CODEX_QUEUE_TIMEOUT, env=os.environ.copy())
        except subprocess.TimeoutExpired:
            return OwnerInputOutcome(UNCERTAIN, "codex_queue_timeout", receipt)
        except OSError:
            # The executable could not start: nothing reached the queue, but the claim was made, so this is a failure, not a
            # deferral to retry blindly.
            return OwnerInputOutcome(FAILED, "codex_queue_unavailable", receipt)
        # A nonzero exit may follow a queue write; the saved attempt is never replayed.
        if result.returncode == 0:
            return OwnerInputOutcome(SUBMITTED, "processing_not_confirmed", {**receipt, "exit": 0})
        return OwnerInputOutcome(UNCERTAIN, "codex_queue_refused", {**receipt, "exit": result.returncode})
